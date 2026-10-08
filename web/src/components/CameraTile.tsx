import { formatCountdown, formatQuota } from "../lib/duration";
import { useEffect, useId, useRef, useState } from "react";
import { registerLivePlayer, unregisterLivePlayer, useLiveBalance } from "../lib/liveUsage";
import Plyr from "plyr";
import "plyr/dist/plyr.css";
import { api, ApiError, negotiateWebrtc, type DeviceProtocol } from "../lib/api";

// WebRTC/WHEP playback logic shared by the single-camera view and the
// multi-camera grid (LiveView.tsx): one place for the RTCPeerConnection
// lifecycle instead of duplicating it.
//
// Why WebRTC instead of HTTP-FLV: HTTP-FLV via mpegts.js depends on
// ManagedMediaSource on iOS (17.1+ only, with rough edges), while WebRTC is
// native in Safari since iOS 11. The WHEP client is hand-written
// (RTCPeerConnection is a native API and WHEP is just a few HTTP calls) to keep
// full control over the retry logic below. AlarmClipPlayer.tsx (VOD playback of
// recorded clips) still uses mpegts.js -- a recorded file is not a live stream.
//
// Plyr (MIT, https://plyr.io) skins the <video>; the media comes from
// `video.srcObject` set to the remote MediaStream delivered in pc.ontrack.
// Styling lives in the --plyr-* CSS variables in index.css, not in JS. The
// time-limit status/countdown is our own overlay on top of the frame (Plyr knows
// nothing about it).
//
// Per-tenant time limit: the REAL cut happens server side (jt808-server bridge,
// enforceLiveViewLimit). This component only reflects it, never enforces it. The
// countdown keeps the user from being surprised when video stops; "watch again"
// simply requests video again, which opens a new session because the bridge
// already marked the old one inactive.
//
// One-time ticket: the `webrtc_url` returned by api.requestVideo carries
// ?token=... issued by the API after checking permissions, and validated by
// ZLMediaKit's on_play hook (fired for every playback protocol, WebRTC
// included). The URL is used as-is exactly once -- every retry goes through
// api.requestVideo again (which mints a NEW ticket), never reusing a URL whose
// ticket is already consumed.
type Status = "idle" | "pidiendo" | "reconectando" | "reproduciendo" | "agotado" | "error";

// Automatic reconnection after a mid-session drop. A real cellular link (dashcam
// in a moving vehicle) has gaps of several seconds and even full reconnections
// (minutes). Without retries the tile would go straight to "ended" and require a
// manual click -- by the time the operator clicks, the server may already have
// torn down the stream for lack of viewers (~20s, handleStreamNoneReader),
// forcing a full restart of the device push. Instead we retry with growing
// backoff over a time window, showing "connecting" (never a red error), and only
// ask for a click once the window is exhausted.
const RETRY_WINDOW_MS = 120_000;
const RETRY_BACKOFF_MS = [1500, 3000, 5000, 8000, 10_000];

// SESSION_DEADLINE_GRACE_MS: see sessionDeadlineRef below -- margin before the
// computed deadline (Date.now() + expires_in_seconds) within which an ICE drop
// is treated as "the session limit was reached", never as a network drop to
// retry. 3s is generous relative to the observed latency between the server
// closing the socket and the browser reporting "failed"/"disconnected".
const SESSION_DEADLINE_GRACE_MS = 3000;

// Automatic retry only makes sense for TRANSIENT failures. A 402 (monthly video
// quota exhausted) or 404/400 (unknown device or no camera) will not resolve on
// its own -- retrying only delays the real message. Only 502 (bridge did not
// answer) and 503 (the camera has no active session RIGHT NOW, the typical case
// of an intermittent dashcam in the field) are retried; any error that is not an
// `ApiError` (network failure, our own startup timeout) is also treated as
// transient, since it is not an explicit rejection from the server.
function isRetryableRequestError(err: unknown): boolean {
  if (err instanceof ApiError) return err.status === 502 || err.status === 503 || err.status === 504 || err.status === 409;
  return true;
}

// PLAYBACK_STARTUP_TIMEOUT_MS: RTCPeerConnection.connectionState moves to
// "failed" on its own when ICE cannot connect (server-side [rtc] timeoutSec=15
// plus the browser's internal timeout), but this timeout is kept as defense in
// depth: if `connectionState` never reaches "connected" NOR a terminal state,
// the tile still leaves "connecting" instead of hanging forever. Generous
// relative to the documented server-side worst case (gt06VideoStartupTimeout=8s
// + network margin + ICE timeoutSec=15).
const PLAYBACK_STARTUP_TIMEOUT_MS = 20_000;

// Standard public STUN. The server (ZLMediaKit) already advertises its own
// candidate via externIP (no NAT on the server side), but the BROWSER (almost
// always behind NAT -- cellular, home wifi) needs to gather its own
// server-reflexive candidates. No TURN on purpose (enableTurn=0).
const ICE_SERVERS: RTCIceServer[] = [{ urls: "stun:stun.l.google.com:19302" }];

// activeSessions: IN-MEMORY registry (this tab only, lost on reload) of which
// device+channel some CameraTile instance believes is streaming right now. The
// detail panel and the camera dock are SEPARATE mounts of this component (each
// with its own <video>), and without this registry each would require its own
// "watch live" click even when the other is already watching the same camera. If
// the original stream has already been cut by the time the second one is clicked
// (no viewers after ~20s, server-side handleStreamNoneReader), requesting it
// again restarts the device's RTMP push from scratch -- exactly the data cost
// this project avoids. With the registry, a NEW instance for the same
// device+channel starts on its own while the entry is fresh. It still requests
// its own ticket (idempotent server side if the stream is alive, no cost to the
// device); it only removes a redundant manual click.
const activeSessions = new Map<string, number>(); // key -> most recent "seen active" timestamp
// Deliberately longer than the server's streamNoneReaderDelayMS (20s) so it
// never competes with the server's no-viewer cut. This is only a UX hint
// ("probably still active"); the server always decides the real cut.
const ACTIVE_SESSION_TTL_MS = 25_000;

function sessionKeyFor(deviceId: string, channel: number): string {
  return `${deviceId}:${channel}`;
}

// Default preview photo: show a cheap still image instead of forcing a full live
// view. Only gt06_video (JC261/JC400) for now; jt808 keeps the large "watch
// live" button until it is validated against real hardware. This is the only
// place to change to extend it to another protocol -- the rest of this component
// is already protocol-agnostic.
const SNAPSHOT_ENABLED_PROTOCOLS: ReadonlySet<DeviceProtocol> = new Set(["gt06_video"]);

// Cadence/cap: every 2 min, up to 3 photos per viewing session. After that, a
// subtle notice + "Refresh" button instead of polling forever (a finite budget,
// never an endless loop). Resets on reload/re-entry -- intentionally no
// persisted state (localStorage); the monthly video quota is the real cost
// barrier, this only keeps a tab left open indefinitely from requesting photos.
const SNAPSHOT_INTERVAL_MS = 2 * 60_000;
const SNAPSHOT_MAX_REFRESHES = 3;
const SNAPSHOT_QUICK_RETRY_MS = 10_000;

type SnapshotStatus = "idle" | "loading" | "ready" | "paused" | "error";

const statusDot: Record<Status, string> = {
  idle: "bg-slate-500",
  pidiendo: "bg-brand-500",
  reconectando: "bg-accent-warn",
  reproduciendo: "bg-emerald-500",
  agotado: "bg-accent-warn",
  error: "bg-slate-400",
};

const statusLabel: Record<Status, string> = {
  idle: "en pausa",
  pidiendo: "conectando",
  reconectando: "conectando con la cámara",
  reproduciendo: "en vivo",
  agotado: "tiempo agotado",
  error: "error",
};

export function CameraTile({
  deviceId,
  label,
  channel = 1,
  protocol,
  autoStart = false,
  restartSignal,
  bare = false,
  onPopOut,
}: {
  deviceId: string;
  label?: string;
  channel?: number;
  protocol?: DeviceProtocol;
  // Floating window opened explicitly with "watch live": starts without
  // requiring a second click (the user already expressed intent).
  autoStart?: boolean;
  // Changes when the user requests this same camera again (see
  // FloatingCamera.startNonce): if the video already ended, it restarts; if it
  // is requesting/playing, it is left alone.
  restartSignal?: number;
  // No frame or header of its own -- for use inside a floating window
  // (FloatingCameras.tsx), which already has its own title bar.
  bare?: boolean;
  // If provided, the header shows a button to open this camera in a floating
  // window (lib/floatingCameras.tsx).
  onPopOut?: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const peerRef = useRef<RTCPeerConnection | null>(null);
  const plyrRef = useRef<Plyr | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [secondsLeft, setSecondsLeft] = useState<number | null>(null);
  // Remaining MONTHLY tenant quota (different from the per-session counter
  // above) -- informational. If we got here, the real block has already happened
  // server side; this only explains to the operator why video will eventually
  // stop being available.
  const [quotaSecondsRemaining, setQuotaSecondsRemaining] = useState<number | null>(null);
  // Incrementing this re-runs the effect below -- "request video again" without
  // depending on deviceId/channel changing.
  const [sessionKey, setSessionKey] = useState(0);
  const statusRef = useRef<Status>("idle");
  statusRef.current = status;
  const lastRestartSignalRef = useRef(restartSignal);
  useEffect(() => {
    if (restartSignal === lastRestartSignalRef.current) return;
    lastRestartSignalRef.current = restartSignal;
    const s = statusRef.current;
    if (s === "pidiendo" || s === "reproduciendo" || s === "reconectando") return;
    resetRetries();
    setStarted(true);
    setSessionKey((k) => k + 1);
  }, [restartSignal]);
  // On-demand video, never automatic: request the stream (a real JT808 0x9101,
  // not free) only when the user clicks "watch live". Until then the tile issues
  // no request. Saves real bandwidth in the common case (the operator only wants
  // to know where the vehicle is).
  //
  // Exception: if ANOTHER instance of this component (detail panel vs. dock, see
  // activeSessions above) believes this device+channel is active RIGHT NOW,
  // start right away without a second click. Requesting video again is cheap in
  // that case (idempotent server side if the stream is alive); the expensive
  // outcome would be UI friction delaying the second click until the stream has
  // been cut for lack of viewers, forcing a real restart of the device push.
  const deviceSessionKey = sessionKeyFor(deviceId, channel);
  const [started, setStarted] = useState(() => {
    if (autoStart) return true;
    const lastSeen = activeSessions.get(deviceSessionKey);
    return lastSeen !== undefined && Date.now() - lastSeen < ACTIVE_SESSION_TTL_MS;
  });
  // Automatic reconnection counter -- a ref, not state: it must not trigger a
  // re-render on its own; only the effect below reads/updates it. Reset on every
  // new manual start (started goes from false to true) and every time playback
  // actually begins, so an ISOLATED drop does not consume the retry budget of a
  // future one.
  const autoReconnectsRef = useRef(0);
  const retryStartRef = useRef<number | null>(null);
  function resetRetries() {
    autoReconnectsRef.current = 0;
    retryStartRef.current = null;
  }
  // Delay before the next retry, or null if the window is exhausted.
  function nextRetryDelay(): number | null {
    const now = Date.now();
    if (retryStartRef.current === null) retryStartRef.current = now;
    if (now - retryStartRef.current > RETRY_WINDOW_MS) return null;
    const delay = RETRY_BACKOFF_MS[Math.min(autoReconnectsRef.current, RETRY_BACKOFF_MS.length - 1)];
    autoReconnectsRef.current += 1;
    return delay;
  }

  // sessionDeadlineRef: wall-clock time (epoch ms) at which the server's
  // PER-SESSION limit (max_live_view_seconds, enforced server side by
  // enforceLiveViewLimit) should be reached. When the limit hits, the ICE drop
  // must not be treated like a transient network drop -- reconnecting would
  // request a NEW session with a fresh clock, exactly what the limit exists to
  // prevent (bandwidth cost control).
  //
  // The on-screen countdown (`secondsLeft`) cannot be used for this: it starts
  // only once playback begins (after the full WHEP negotiation), while the
  // SERVER clock starts earlier, at api.requestVideo(). A real time-limit cut
  // would arrive while the on-screen countdown still showed several seconds
  // left. sessionDeadlineRef uses Date.now() from the moment expires_in_seconds
  // arrived, so it is immune to that offset.
  const sessionDeadlineRef = useRef<number | null>(null);

  // --- Preview photo (see constants above) --- hasPlayedLiveRef: `stop()`
  // (below) sets `started` back to false, and the photo polling effect only
  // looks at `started` -- so stopping a live stream would RESTART the photo
  // cycle with a fresh budget, triggering real signaling to the device again (a
  // photo is not free: request_snapshot in video.py sends the same real command
  // as requesting video). A preview makes sense BEFORE the operator decides to
  // watch live; once they have watched and stopped it themselves, polling photos
  // only wastes device data. This ref (not state -- must not trigger a
  // re-render) is set the FIRST time playback is reached and is never reset
  // while the component stays mounted.
  const hasPlayedLiveRef = useRef(false);
  const snapshotEligible = protocol != null && SNAPSHOT_ENABLED_PROTOCOLS.has(protocol);
  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotStatus, setSnapshotStatus] = useState<SnapshotStatus>("idle");
  // refreshCount is a ref (not state): only the polling effect below
  // reads/updates it and it must not trigger a re-render on its own
  // (snapshotStatus already does that when appropriate).
  const snapshotRefreshCountRef = useRef(0);
  const snapshotInFlightRef = useRef(false);
  // Incrementing this restarts polling (same pattern as sessionKey for "request
  // video again") WITHOUT making snapshotStatus a dependency of the polling
  // effect: if it were, every loading->ready transition would tear down and
  // recreate the setInterval on every photo.
  const [snapshotResetTick, setSnapshotResetTick] = useState(0);
  // IntersectionObserver: never request photos for a camera that is not visible
  // (horizontally scrolled dock, tile outside the viewport) -- only while
  // someone is actually looking at the screen.
  const [isVisible, setIsVisible] = useState(false);

  useEffect(() => {
    if (!snapshotEligible || !containerRef.current) return;
    const el = containerRef.current;
    const observer = new IntersectionObserver(([entry]) => setIsVisible(entry.isIntersecting), { threshold: 0.15 });
    observer.observe(el);
    return () => observer.disconnect();
  }, [snapshotEligible]);

  useEffect(() => {
    if (!snapshotEligible || started || !isVisible || hasPlayedLiveRef.current) return;
    // Budget already exhausted (ref, not state) -- wait for a click on "Refresh"
    // (restartSnapshots), which resets the ref AND bumps snapshotResetTick to
    // re-enter here.
    if (snapshotRefreshCountRef.current >= SNAPSHOT_MAX_REFRESHES) return;
    let cancelled = false;
    let quickRetry: ReturnType<typeof setTimeout> | null = null;

    async function fetchSnapshot() {
      if (snapshotInFlightRef.current) return;
      snapshotInFlightRef.current = true;
      setSnapshotStatus((s) => (s === "ready" ? s : "loading"));
      try {
        const blob = await api.requestSnapshot(deviceId, channel);
        if (cancelled) return;
        const url = URL.createObjectURL(blob);
        setSnapshotUrl((prev) => {
          if (prev) URL.revokeObjectURL(prev);
          return url;
        });
        setSnapshotStatus("ready");
        snapshotRefreshCountRef.current += 1;
        if (snapshotRefreshCountRef.current >= SNAPSHOT_MAX_REFRESHES) {
          setSnapshotStatus("paused");
        }
      } catch (err) {
        if (cancelled) return;
        // Informational -- a failed photo must not cover the rest of the tile or
        // prevent "watch live" from working.
        setSnapshotStatus((s) => (s === "ready" ? s : "error"));
        // A failure ALSO counts against the budget -- otherwise a persistently
        // unreachable camera (device without an active GT06 connection) would
        // retry every SNAPSHOT_INTERVAL_MS forever while the tile stays visible.
        // A PERMANENT failure (e.g. 402, monthly quota exhausted) exhausts the
        // budget at once instead of "spending" attempts every 2 min with no
        // chance of success -- same rule isRetryableRequestError applies to live
        // video.
        snapshotRefreshCountRef.current = isRetryableRequestError(err)
          ? snapshotRefreshCountRef.current + 1
          : SNAPSHOT_MAX_REFRESHES;
        if (snapshotRefreshCountRef.current >= SNAPSHOT_MAX_REFRESHES) {
          setSnapshotStatus("paused");
        } else if (!quickRetry) {
          setSnapshotStatus((s) => (s === "ready" ? s : "loading")); // stays "loading" while retrying
          // The camera sometimes uploads the photo late (the JC261 front camera
          // can take 30 s+): the server caches it on arrival, so a short second
          // attempt usually gets it instantly without waiting for the 2-minute
          // refresh.
          quickRetry = setTimeout(() => {
            quickRetry = null;
            if (!cancelled) void fetchSnapshot();
          }, snapshotRefreshCountRef.current <= 1 ? SNAPSHOT_QUICK_RETRY_MS : SNAPSHOT_QUICK_RETRY_MS * 2.5);
        }
      } finally {
        snapshotInFlightRef.current = false;
      }
    }

    fetchSnapshot();
    const interval = setInterval(() => {
      if (snapshotRefreshCountRef.current < SNAPSHOT_MAX_REFRESHES) fetchSnapshot();
    }, SNAPSHOT_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
      if (quickRetry) clearTimeout(quickRetry);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshotEligible, started, isVisible, deviceId, channel, snapshotResetTick]);

  // Revoke the last object URL on unmount. Through a ref, not snapshotUrl
  // directly: an effect with [] deps captures snapshotUrl from the FIRST render
  // (always null at mount), so its cleanup would never see the real URL (stale
  // closure). The ref always holds the latest URL without re-subscribing the
  // effect on every photo.
  const snapshotUrlRef = useRef<string | null>(null);
  useEffect(() => {
    snapshotUrlRef.current = snapshotUrl;
  }, [snapshotUrl]);
  useEffect(() => {
    return () => {
      if (snapshotUrlRef.current) URL.revokeObjectURL(snapshotUrlRef.current);
    };
  }, []);

  function restartSnapshots() {
    snapshotRefreshCountRef.current = 0;
    setSnapshotStatus("idle");
    setSnapshotResetTick((t) => t + 1);
  }

  // Plyr is mounted ONCE on the <video>. WebRTC still drives actual playback
  // (video.srcObject is set in pc.ontrack in the effect below); Plyr only skins
  // the controls of the same DOM element.
  useEffect(() => {
    if (!videoRef.current) return;
    plyrRef.current = new Plyr(videoRef.current, {
      // No "volume" slider: Plyr adapts its controls by VIEWPORT media query,
      // not by the actual width of THIS tile, so in a narrow column (resizable
      // panel or the multi-camera dock) the volume slider never hid itself and
      // crowded the other icons. "mute" is enough for surveillance video --
      // reduced controls are never clipped.
      controls: ["play", "mute", "fullscreen"],
      clickToPlay: false,
      tooltips: { controls: false, seek: false },
    });
    return () => {
      plyrRef.current?.destroy();
      plyrRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!started) return;
    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let sessionDeleteUrl: string | null = null;
    setStatus((s) => (s === "reconectando" ? s : "pidiendo"));
    setError(null);
    setSecondsLeft(null);
    setQuotaSecondsRemaining(null);

    // reachedPlayback tells where a WebRTC state change happens (see
    // onconnectionstatechange below) without reading React state inside the
    // callback: the FIRST "connected" is when video actually starts showing, so
    // a failure before that point is a startup failure and one after it is a
    // mid-session drop.
    let reachedPlayback = false;

    (async () => {
      try {
        const { webrtc_url, expires_in_seconds, live_view_seconds_remaining } = await api.requestVideo(deviceId, channel);
        if (cancelled || !videoRef.current) return;
        // The per-session limit clock starts SERVER side at THIS instant
        // (api.requestVideo already opened/confirmed the stream) -- computed
        // here with Date.now(), never from the on-screen countdown (see
        // sessionDeadlineRef above).
        sessionDeadlineRef.current = expires_in_seconds > 0 ? Date.now() + expires_in_seconds * 1000 : null;

        if (typeof RTCPeerConnection === "undefined") {
          setError("Este navegador no soporta WebRTC");
          setStatus("error");
          return;
        }

        const pc = new RTCPeerConnection({ iceServers: ICE_SERVERS });
        peerRef.current = pc;
        // recvonly: this tile only PLAYS, it never sends its own audio/video.
        pc.addTransceiver("video", { direction: "recvonly" });
        pc.addTransceiver("audio", { direction: "recvonly" });
        pc.ontrack = (event) => {
          if (videoRef.current && videoRef.current.srcObject !== event.streams[0]) {
            videoRef.current.srcObject = event.streams[0];
            // Assigning srcObject does NOT start playback by itself: without
            // this, the <video> received real data (readyState=4, correct
            // dimensions, "live" track) but stayed PAUSED forever, showing
            // black. The element is `muted`, so the browser autoplay policy
            // allows it without a user gesture. A rejected promise (rare, given
            // muted) must not break the rest of the flow -- it is only logged.
            videoRef.current.play().catch((err) => {
              // eslint-disable-next-line no-console
              console.error("CameraTile: video.play() rejected", { deviceId, channel, err });
            });
          }
        };

        const connected = new Promise<void>((resolve, reject) => {
          pc.onconnectionstatechange = () => {
            if (cancelled) return;
            const state = pc.connectionState;
            // Always log, regardless of which branch follows: it is the only
            // real clue for diagnosing a remote device without physical access.
            // eslint-disable-next-line no-console
            console.log("CameraTile: webrtc connectionState", { deviceId, channel, state });

            if (!reachedPlayback) {
              // STARTUP path, before the first connection.
              if (state === "connected") {
                resolve();
              } else if (state === "failed" || state === "closed") {
                reject(new Error(`no se pudo conectar el video (${state})`));
              }
              // "connecting"/"new"/"disconnected" before the first connection:
              // keep waiting -- PLAYBACK_STARTUP_TIMEOUT_MS covers the case
              // where this never resolves.
              return;
            }

            // MID-SESSION path -- playback had already started. Same
            // budget/backoff as any other drop, see
            // RETRY_WINDOW_MS/RETRY_BACKOFF_MS.
            if (state === "failed" || state === "disconnected") {
              // Having reached (or being about to reach) sessionDeadlineRef
              // means THIS drop is the PER-SESSION limit being enforced server
              // side (enforceLiveViewLimit), not a transient network drop.
              // Reconnecting automatically here would request a NEW session with
              // a fresh clock (api.requestVideo does not find the old stream
              // active and treats it as a new request) -- exactly what the limit
              // exists to prevent. SESSION_DEADLINE_GRACE_MS: margin before the
              // exact deadline, since the server-side cut can take a moment to
              // surface as "failed" in the browser.
              const sessionLimitReached =
                sessionDeadlineRef.current !== null && Date.now() >= sessionDeadlineRef.current - SESSION_DEADLINE_GRACE_MS;
              const retryDelay = sessionLimitReached ? null : nextRetryDelay();
              if (retryDelay !== null) {
                // MID-SESSION drop (real cellular link, still within the allowed
                // time): retry on our own, like a video call client, instead of
                // asking the operator for a manual click while the server's
                // no-viewer clock keeps running (handleStreamNoneReader, ~20s).
                // api.requestVideo is idempotent server side if the stream is
                // still alive -- a quick reconnect does not restart the device
                // push, only the viewer connection.
                setStatus("reconectando");
                reconnectTimer = setTimeout(() => {
                  if (!cancelled) setSessionKey((k) => k + 1);
                }, retryDelay);
              } else {
                // Automatic reconnection budget exhausted, OR the per-session
                // limit was reached -- either way a manual click ("watch
                // again"/"watch live") is required, which also resets the budget
                // and deliberately requests a new session (never automatically).
                setStatus("agotado");
              }
            }
          };
        });

        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        if (cancelled) return;
        // WHEP negotiation with the same one-time ticket (see the docstring
        // above): an invalid/reused/expired ticket is rejected here with a
        // non-2xx ApiError and never attempts to connect.
        const { answerSdp, deleteUrl } = await negotiateWebrtc(webrtc_url, offer.sdp ?? "");
        sessionDeleteUrl = deleteUrl;
        if (cancelled) return;
        await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });

        let startupTimer: ReturnType<typeof setTimeout> | null = null;
        try {
          await Promise.race([
            connected,
            new Promise<never>((_, reject) => {
              startupTimer = setTimeout(
                () => reject(new Error("tiempo de espera agotado iniciando el video (posible corte de red)")),
                PLAYBACK_STARTUP_TIMEOUT_MS,
              );
            }),
          ]);
        } finally {
          if (startupTimer) clearTimeout(startupTimer);
        }
        reachedPlayback = true;
        resetRetries(); // playback resumed -- fresh budget for the next drop
        hasPlayedLiveRef.current = true; // see hasPlayedLiveRef -- this mount never polls photos again
        if (cancelled) return;
        setStatus("reproduciendo");
        activeSessions.set(deviceSessionKey, Date.now());
        if (expires_in_seconds > 0) setSecondsLeft(expires_in_seconds);
        if (live_view_seconds_remaining > 0) setQuotaSecondsRemaining(Math.floor(live_view_seconds_remaining));
      } catch (err) {
        if (cancelled) return;
        const message = err instanceof ApiError ? err.message : "no se pudo iniciar el video";
        // A failure BEFORE the stream is requested (e.g. 503 "the camera has no
        // active connection right now", typical of an intermittent dashcam in
        // the field) gets the same retry budget/backoff as a mid-session drop.
        // Cheap here: the device already reported it has no active connection,
        // so retrying never sends a real command over the air, it only
        // re-queries the internal registry. Once the budget is exhausted it
        // falls back to "error" with the usual manual button.
        //
        // But ONLY if the failure is transient (see isRetryableRequestError): a
        // 402 (quota exhausted) or 404/400 (invalid device) will never resolve
        // by retrying, so they go straight to "error" with the real message.
        // Note: a 406 from WHEP negotiation (invalid ticket OR stream not yet
        // published -- ZLMediaKit uses the same code for both) lands here as
        // non-retryable; in practice this is rare because api.requestVideo
        // already waited for the real publish confirmation before returning the
        // URL.
        const retryDelay = isRetryableRequestError(err) ? nextRetryDelay() : null;
        if (retryDelay !== null) {
          // No visible error: the user only sees "connecting to the camera".
          console.info("CameraTile: retrying", { deviceId, channel, message, retryDelay });
          setStatus("reconectando");
          reconnectTimer = setTimeout(() => {
            if (!cancelled) setSessionKey((k) => k + 1);
          }, retryDelay);
          return;
        }
        setError(message);
        setStatus("error");
      }
    })();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (videoRef.current) videoRef.current.srcObject = null;
      peerRef.current?.close();
      peerRef.current = null;
      // Clean close on the ZLMediaKit side (DELETE of the WHEP session resource,
      // see the Location header in negotiateWebrtc). Best-effort, never blocks
      // cleanup: ZLMediaKit detects the ICE disconnect on its own after
      // timeoutSec anyway, this only makes it faster.
      if (sessionDeleteUrl) {
        fetch(sessionDeleteUrl, { method: "DELETE" }).catch(() => {});
      }
      // Leaving this effect means the session ended for THIS instance (stopped,
      // unmounted, or channel/device changed). Clear the "active" mark so a new
      // instance does not think a stream this one just closed is still alive. An
      // automatic retry (sessionKey++) repopulates it right away if the new
      // attempt reaches playback.
      activeSessions.delete(deviceSessionKey);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [started, deviceId, channel, sessionKey]);

  // activeSessions heartbeat while playing -- independent of the countdown above
  // (which may not exist if the server did not report expires_in_seconds), so
  // the "active" entry does not expire by TTL while video is genuinely still
  // running.
  useEffect(() => {
    if (status !== "reproduciendo") return;
    activeSessions.set(deviceSessionKey, Date.now());
    const t = setInterval(() => activeSessions.set(deviceSessionKey, Date.now()), 10_000);
    return () => clearInterval(t);
  }, [status, deviceSessionKey]);

  useEffect(() => {
    if (status !== "reproduciendo" || secondsLeft === null) return;
    if (secondsLeft <= 0) return;
    const t = setTimeout(() => setSecondsLeft((s) => (s !== null ? s - 1 : s)), 1000);
    return () => clearTimeout(t);
  }, [status, secondsLeft]);

  // The monthly balance also ticks down every second while playing (local
  // estimate; the server recomputes the real value from on_flow_report bytes and
  // duration on the next request). The month's balance has one source shared by
  // all cameras of the tenant (lib/liveUsage.ts, backed by the server's central
  // meter) -- with two cameras open it drops twice as fast, same as on the
  // server. The value returned by the video request is only used until the first
  // shared query arrives.
  const playerId = useId();
  useEffect(() => {
    if (status !== "reproduciendo") return;
    registerLivePlayer(playerId, deviceId);
    return () => unregisterLivePlayer(playerId);
  }, [status, playerId, deviceId]);
  const sharedBalance = useLiveBalance({ deviceId });
  const quotaShown = sharedBalance ? sharedBalance.remaining : quotaSecondsRemaining;
  const camerasOpen = sharedBalance?.active ?? 1;

  // Manual stop: lets the operator cut the stream instead of waiting for the
  // server's time-limit or no-viewer cut. setStarted(false) triggers the cleanup
  // of the effect above (destroys the player, clears activeSessions) without
  // unmounting the component.
  function stop() {
    resetRetries();
    setStarted(false);
    setStatus("idle");
    setError(null);
    setSecondsLeft(null);
    setQuotaSecondsRemaining(null);
  }

  function startLive() {
    resetRetries(); // new manual start -- fresh reconnection budget
    setStarted(true);
  }

  const liveActions =
    status === "agotado" || status === "error" ? (
      // "Watch again" -- api.requestVideo mints a NEW ticket (the previous one
      // was already consumed).
      <button
        onClick={() => {
          resetRetries(); // manual retry -- fresh reconnection budget
          setSessionKey((k) => k + 1);
        }}
        className="rounded-lg bg-white/10 px-2.5 py-1 text-xs font-medium text-white backdrop-blur hover:bg-white/20"
      >
        Ver de nuevo
      </button>
    ) : status === "pidiendo" || status === "reconectando" || status === "reproduciendo" ? (
      // Manual stop -- saves real device data as soon as the operator no longer
      // needs the camera.
      <button
        onClick={stop}
        title="Detener transmisión"
        className="rounded-lg bg-black/55 px-2.5 py-1 text-xs font-medium text-white backdrop-blur hover:bg-rose-600/80"
      >
        Detener
      </button>
    ) : null;

  return (
    <div
      ref={containerRef}
      className={bare ? "flex h-full flex-col bg-black" : "overflow-hidden rounded-2xl border border-line bg-surface"}
    >
      {!bare && (
        <div className="flex items-center justify-between gap-2 px-3 py-2">
          <span className="truncate text-sm font-medium text-ink">{label ?? deviceId}</span>
          <span className="flex items-center gap-1.5">
          {onPopOut && (
            <button
              onClick={onPopOut}
              title="Abrir en ventana flotante"
              aria-label="Abrir en ventana flotante"
              className="flex h-6 w-6 items-center justify-center rounded-md text-ink-faint hover:bg-white/10 hover:text-ink"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M14 4h6v6M20 4l-8 8M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          )}
          {status === "reproduciendo" ? (
            <span className="flex items-center gap-1 rounded-full bg-rose-500/15 px-2 py-0.5 text-[10px] font-bold tracking-wider text-rose-300">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-rose-500" aria-hidden />
              EN VIVO
            </span>
          ) : (
            <span className="font-data text-[11px] text-ink-faint">{deviceId.slice(0, 8)}</span>
          )}
          </span>
        </div>
      )}

      {error && (status === "error" || status === "agotado") && <p className="bg-rose-500/10 px-3 py-2 text-xs text-rose-200">{error}</p>}

      <div className={`relative w-full bg-black ${bare ? "min-h-0 flex-1" : "aspect-video"}`}>
        {/* muted: browsers block autoplay with audio without user interaction. */}
        <video ref={videoRef} muted playsInline className="h-full w-full" />

        {!started && snapshotEligible && !hasPlayedLiveRef.current ? (
          // Default preview photo. absolute inset-0 container with z-index above
          // Plyr: without it Plyr's native controls showed ON TOP of the photo.
          <div className="absolute inset-0 z-20">
            {snapshotUrl && (
              <img
                src={snapshotUrl}
                alt=""
                className={`h-full w-full object-cover transition-opacity ${snapshotStatus === "loading" ? "opacity-60" : "opacity-100"}`}
              />
            )}
            {!snapshotUrl && (
              <div className="absolute inset-0 flex items-center justify-center bg-gradient-to-br from-zinc-900 to-black">
                <span className="text-[11px] font-medium tracking-wide text-white/50">
                  {snapshotStatus === "error" ? "Sin vista previa" : "Cargando vista previa…"}
                </span>
              </div>
            )}
            <button
              onClick={startLive}
              title="Ver en vivo"
              className="absolute right-2.5 bottom-2.5 flex items-center gap-1.5 rounded-full bg-black/60 py-1.5 pr-3 pl-2.5 text-xs font-semibold text-white shadow-lg backdrop-blur transition-colors hover:bg-brand-600"
            >
              <span aria-hidden>▶</span> En vivo
            </button>
            {snapshotStatus === "paused" && (
              <div className="absolute inset-x-0 top-0 flex items-center justify-between gap-2 bg-gradient-to-b from-black/75 to-transparent px-2.5 py-2">
                <span className="text-[11px] font-medium text-white/75">Vista previa pausada</span>
                <button onClick={restartSnapshots} className="rounded-lg bg-white/15 px-2 py-0.5 text-[11px] font-semibold text-white hover:bg-white/25">
                  Actualizar
                </button>
              </div>
            )}
          </div>
        ) : !started ? (
          <button
            onClick={startLive}
            className="group absolute inset-0 flex flex-col items-center justify-center gap-2 bg-gradient-to-br from-zinc-900 to-black text-white/75 transition-colors hover:text-white"
          >
            <span className="flex h-12 w-12 items-center justify-center rounded-full bg-white/10 text-lg backdrop-blur transition-all group-hover:scale-105 group-hover:bg-brand-600">
              ▶
            </span>
            <span className="text-xs font-semibold">Ver en vivo</span>
          </button>
        ) : (
          // Status in the top-left corner, CCTV-timestamp style.
          <div className="pointer-events-none absolute top-2 left-2 z-10 flex items-center gap-1.5 rounded-lg bg-black/55 px-2 py-1 backdrop-blur">
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${statusDot[status]}`} aria-hidden />
            <span className="text-[11px] font-semibold tracking-wide text-white">{statusLabel[status]}</span>
            {status === "reproduciendo" && secondsLeft !== null && secondsLeft > 0 && (
              <span className="font-data text-[11px] text-white/70">· {formatCountdown(secondsLeft)}</span>
            )}
          </div>
        )}
        {started && (status === "pidiendo" || status === "reconectando") && (
          <div className="pointer-events-none absolute inset-0 z-[5] flex flex-col items-center justify-center gap-2">
            <span className="h-6 w-6 animate-spin rounded-full border-2 border-white/25 border-t-white/80" aria-hidden />
            {status === "reconectando" && (
              <span className="max-w-[85%] text-center text-[11px] font-medium text-white/70">
                La cámara tarda en responder (señal celular). Seguimos intentando…
              </span>
            )}
          </div>
        )}

        {status === "reproduciendo" && quotaShown !== null && (
          <div className="pointer-events-none absolute top-2 right-2 z-10 max-w-[60%] rounded-lg bg-black/55 px-2 py-1 backdrop-blur">
            <span
              className="block truncate font-data text-[11px] font-medium text-white/80"
              title={`Tiempo de video en vivo que le queda a la empresa este mes${camerasOpen > 1 ? ` · ${camerasOpen} cámaras abiertas descuentan a la vez` : ""}`}
            >
              {formatQuota(quotaShown)}
              {camerasOpen > 1 && <span className="text-white/55"> · ×{camerasOpen}</span>}
            </span>
          </div>
        )}

        {liveActions && <div className="absolute right-2 bottom-12 z-30">{liveActions}</div>}
      </div>
    </div>
  );
}
