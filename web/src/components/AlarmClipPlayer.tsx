import { useEffect, useRef, useState } from "react";
import mpegts from "mpegts.js";
import type { AlarmVideoClip, DeviceProtocol } from "../lib/api";

// Player for ONE camera of a RECORDED (not live) clip. Reuses mpegts.js in VOD
// mode: the JC261 uploads clips as real .ts (MPEG-TS), not .mp4, for resilience
// against interrupted uploads, and mpegts.js demuxes .ts natively (`type:
// "mpegts"`). Unlike CameraTile (live: one-time tickets, auto-reconnect, tenant
// time limits), this is a static file in R2 behind a short-lived signed URL --
// none of that applies here.
//
// Layout: the container has its own max width (see AlarmClipPlayer below) so a
// low-resolution dashcam clip is not stretched across a wide card, and loading
// state is explicit text (same convention as CameraTile) instead of the
// browser's native spinner. The <video> is always in the DOM (mpegts.js needs to
// attach to it) but stays at opacity-0 until the real `loadeddata` event.
function ClipVideo({ url, label }: { url: string; label?: string }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setReady(false);
    setError(null);
    const videoEl = videoRef.current;
    if (!videoEl) return;
    if (!mpegts.isSupported()) {
      setError("Este navegador no soporta reproducir este clip (Media Source Extensions)");
      return;
    }
    const player = mpegts.createPlayer({ type: "mpegts", url, isLive: false });
    player.on(mpegts.Events.ERROR, () => setError("no se pudo reproducir el clip -- puede haber expirado"));
    const onReady = () => setReady(true);
    videoEl.addEventListener("loadeddata", onReady);
    player.attachMediaElement(videoEl);
    player.load();
    return () => {
      videoEl.removeEventListener("loadeddata", onReady);
      player.destroy();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url]);

  return (
    <div className="overflow-hidden rounded-sm border border-line bg-black">
      {label && (
        <div className="border-b border-line bg-surface-2 px-2 py-1 text-xs font-medium text-ink-dim">{label}</div>
      )}
      <div className="relative aspect-video w-full">
        {error ? (
          <p className="flex h-full items-center justify-center p-3 text-center text-xs text-red-400">{error}</p>
        ) : (
          <>
            {!ready && (
              <p className="absolute inset-0 flex items-center justify-center text-xs text-ink-faint">
                Cargando clip...
              </p>
            )}
            <video
              ref={videoRef}
              controls
              playsInline
              className={`h-full w-full transition-opacity duration-150 ${ready ? "opacity-100" : "opacity-0"}`}
            />
          </>
        )}
      </div>
    </div>
  );
}

// AlarmClipPlayer: one or two cameras (Front + Cabin) side by side on desktop,
// stacked on mobile. Has its own max width (max-w-lg for one camera, max-w-2xl
// for two) and never inherits the full width of its parent container.
export function AlarmClipPlayer({ url, secondaryUrl }: { url: string; secondaryUrl?: string | null }) {
  if (secondaryUrl) {
    return (
      <div className="grid max-w-2xl grid-cols-1 gap-3 sm:grid-cols-2">
        <ClipVideo url={url} label="Frontal" />
        <ClipVideo url={secondaryUrl} label="Cabina" />
      </div>
    );
  }
  return (
    <div className="max-w-lg">
      <ClipVideo url={url} />
    </div>
  );
}

// The only protocol with clip retrieval implemented today. JT808 is designed but
// not implemented, and "gt06" (GPS-only) has no camera.
export const CLIP_SUPPORTED_PROTOCOL: DeviceProtocol = "gt06_video";

// Alarm types for which the device actually stores video on its SD card:
// JC261/JC400 camera events (collision, panic/SOS...). Ignition, engine cut,
// geofence or speed alarms never have a file, so offering "request clip" there
// would always end in "the device did not upload the file in time". Same list as
// CLIP_CAPABLE_ALARM_TYPES in api/app/routers/alarms.py.
export const CLIP_CAPABLE_ALARM_TYPES: ReadonlySet<string> = new Set(["gt06_camera_event"]);

export function alarmCanHaveClip(protocol: DeviceProtocol | undefined, alarmType: string | null | undefined): boolean {
  return protocol === CLIP_SUPPORTED_PROTOCOL && !!alarmType && CLIP_CAPABLE_ALARM_TYPES.has(alarmType);
}

export const CLIP_STATUS_LABEL: Record<AlarmVideoClip["status"], string> = {
  requested: "pidiendo clip...",
  uploading: "subiendo clip...",
  ready: "clip listo",
  failed: "no se pudo obtener el clip",
  unsupported: "este dispositivo no soporta clips",
};
