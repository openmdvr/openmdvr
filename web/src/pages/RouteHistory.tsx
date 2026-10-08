import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { MapContainer, Marker, Popup, useMap } from "react-leaflet";
import {
  api,
  ApiError,
  type AlarmSeverity,
  type AlarmVideoClip,
  type Device,
  type RouteHistoryEvent,
  type RouteHistoryPoint,
  type RouteHistoryReport,
} from "../lib/api";
import { Alert, Button, Skeleton } from "../components/ui";
import { HotlinePolyline, HOTLINE_MAX_KMH, HOTLINE_MIN_KMH, HOTLINE_PALETTE } from "../components/HotlinePolyline";
import { AlarmClipPlayer, CLIP_STATUS_LABEL } from "../components/AlarmClipPlayer";
import { alarmTypeLabel } from "../lib/alarmLabels";
import { MapBaseLayer, MapStyleSwitcher } from "../components/MapBaseLayer";
import { useGeofenceOverlay } from "../lib/useGeofenceOverlay";
import { GeofenceLayer } from "../components/GeofenceLayer";
import { useIsMobile } from "../lib/useIsMobile";
import { formatDuration } from "../lib/geofences";
import { daysAgoLocalIso, startOfLocalDayIso, startOfNextLocalDayIso, todayLocalIso } from "../lib/localDate";

// Route history:
// - The map is the protagonist, with a glass panel holding the controls.
// - Quick ranges (Today / Yesterday / 7 days) in addition to two dates.
// - "Level of detail" instead of a technical "max points" field.
// - Trip statistics (distance, moving time, max/average speed, stops) computed
//   from the points already received.
// - Timeline playback: a marker travels the route with its time and speed, at
//   1x-240x. The real optimization lives in the backend (GET
//   /devices/{id}/route-history: time_bucket() inside Postgres, 31-day cap,
//   statement_timeout and rate limit), using the (device_id, time) index from
//   0052 so the query never scans the whole tenant fleet's positions.

const MEXICO_CITY: [number, number] = [19.4326, -99.1332];
const DETAIL_LEVELS = [
  { label: "Normal", points: 1500 },
  { label: "Alto", points: 3000 },
];
const PLAYBACK_SPEEDS = [1, 16, 60, 240];
const STOP_MIN_SECONDS = 180;

const SEVERITY_COLOR: Record<AlarmSeverity, string> = { critical: "#f43f5e", warning: "#f5b83d", info: "#2f93ff" };

type Preset = "today" | "yesterday" | "7d" | "custom";

function eventIcon(severity: AlarmSeverity): L.DivIcon {
  const color = SEVERITY_COLOR[severity];
  return L.divIcon({
    className: "",
    html: `<div style="background:${color};width:16px;height:16px;border-radius:6px;transform:rotate(45deg);border:2.5px solid white;box-shadow:0 2px 8px rgba(0,0,0,.5)"></div>`,
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  });
}

function playheadIcon(heading: number | null): L.DivIcon {
  const h = heading == null ? 0 : Math.round(heading);
  return L.divIcon({
    className: "",
    html: `<div class="omd-marker"><div class="omd-marker-dot" style="background:#037dfe;width:26px;height:26px"><div class="omd-marker-arrow" style="transform:rotate(${h}deg)"></div></div></div>`,
    iconSize: [26, 26],
    iconAnchor: [13, 13],
  });
}

function haversineKm(a: RouteHistoryPoint, b: RouteHistoryPoint): number {
  const r = 6371;
  const dLat = ((b.lat - a.lat) * Math.PI) / 180;
  const dLon = ((b.lon - a.lon) * Math.PI) / 180;
  const la1 = (a.lat * Math.PI) / 180;
  const la2 = (b.lat * Math.PI) / 180;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin(dLon / 2) ** 2;
  return 2 * r * Math.asin(Math.min(1, Math.sqrt(h)));
}

interface RouteStats {
  distanceKm: number;
  movingSeconds: number;
  maxSpeed: number;
  avgMovingSpeed: number | null;
  stops: number;
  durationSeconds: number;
}

function computeStats(points: RouteHistoryPoint[]): RouteStats {
  let distanceKm = 0;
  let movingSeconds = 0;
  let maxSpeed = 0;
  let movingSpeedSum = 0;
  let movingSpeedCount = 0;
  let stops = 0;
  let stoppedSince: number | null = null;
  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    const speed = p.speed_kmh ?? 0;
    maxSpeed = Math.max(maxSpeed, speed);
    if (speed >= 5) {
      movingSpeedSum += speed;
      movingSpeedCount += 1;
    }
    if (i === 0) continue;
    const prev = points[i - 1];
    distanceKm += haversineKm(prev, p);
    const dt = (new Date(p.time).getTime() - new Date(prev.time).getTime()) / 1000;
    if ((prev.speed_kmh ?? 0) >= 5) movingSeconds += dt;
    const t = new Date(p.time).getTime();
    if (speed < 5) {
      if (stoppedSince == null) stoppedSince = new Date(prev.time).getTime();
    } else if (stoppedSince != null) {
      if ((t - stoppedSince) / 1000 >= STOP_MIN_SECONDS) stops += 1;
      stoppedSince = null;
    }
  }
  const durationSeconds =
    points.length > 1 ? (new Date(points[points.length - 1].time).getTime() - new Date(points[0].time).getTime()) / 1000 : 0;
  return {
    distanceKm,
    movingSeconds,
    maxSpeed,
    avgMovingSpeed: movingSpeedCount ? movingSpeedSum / movingSpeedCount : null,
    stops,
    durationSeconds,
  };
}

interface LoadedRoute {
  deviceId: string;
  label: string;
  report: RouteHistoryReport;
}

function FitToRoutes({ routes }: { routes: LoadedRoute[] }) {
  const map = useMap();
  useEffect(() => {
    const allPoints = routes.flatMap((r) => r.report.points.map((p): [number, number] => [p.lat, p.lon]));
    if (allPoints.length === 0) return;
    if (allPoints.length === 1) map.setView(allPoints[0], 15);
    else map.fitBounds(allPoints, { padding: [50, 50], maxZoom: 16 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routes.map((r) => r.deviceId).join(",")]);
  return null;
}

function FlyToPoint({ point }: { point: [number, number] | null }) {
  const map = useMap();
  useEffect(() => {
    if (point) map.flyTo(point, Math.max(map.getZoom(), 15), { duration: 0.6 });
  }, [point, map]);
  return null;
}

// Keeps the playback marker in view while it advances.
function FollowPlayhead({ point, active }: { point: [number, number] | null; active: boolean }) {
  const map = useMap();
  useEffect(() => {
    if (!active || !point) return;
    if (!map.getBounds().pad(-0.15).contains(point)) map.panTo(point, { animate: true, duration: 0.4 });
  }, [point, active, map]);
  return null;
}

function SpeedLegend() {
  const stops = Object.entries(HOTLINE_PALETTE)
    .map(([k, v]) => [Number(k), v] as const)
    .sort((a, b) => a[0] - b[0]);
  const gradient = stops.map(([k, v]) => `${v} ${k * 100}%`).join(", ");
  return (
    <div className="flex items-center gap-2 text-[11px] text-ink-dim">
      <span>{HOTLINE_MIN_KMH}</span>
      <div className="h-1.5 w-24 rounded-full" style={{ background: `linear-gradient(to right, ${gradient})` }} />
      <span>{HOTLINE_MAX_KMH}+ km/h</span>
    </div>
  );
}

function EventClip({ event }: { event: RouteHistoryEvent }) {
  const [clip, setClip] = useState<AlarmVideoClip | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setClip(await api.getAlarmClip(event.id));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando el clip");
    } finally {
      setLoading(false);
    }
  }

  if (!clip && !loading) {
    return (
      <Button variant="secondary" className="px-3 py-1.5 text-xs" onClick={load}>
        Ver clip
      </Button>
    );
  }
  if (loading) return <Skeleton className="h-24 w-full" />;
  if (error) return <Alert>{error}</Alert>;
  if (!clip) return null;
  if (clip.status !== "ready") return <p className="text-xs text-ink-dim">{CLIP_STATUS_LABEL[clip.status]}</p>;
  return <AlarmClipPlayer url={clip.url!} secondaryUrl={clip.url_secondary} />;
}

function Stat({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div className="rounded-2xl border border-line bg-fg/[0.03] px-3 py-2.5">
      <p className="text-[11px] text-ink-faint">{label}</p>
      <p className="mt-0.5 text-base font-semibold text-ink">
        {value}
        {unit && <span className="ml-0.5 text-xs font-medium text-ink-faint">{unit}</span>}
      </p>
    </div>
  );
}

function findIndexAtTime(points: RouteHistoryPoint[], t: number): number {
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (new Date(points[mid].time).getTime() <= t) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

export default function RouteHistory() {
  const isMobile = useIsMobile();
  const geofenceOverlay = useGeofenceOverlay();
  const [searchParams] = useSearchParams();
  const preselectedDeviceId = searchParams.get("device");

  const [devices, setDevices] = useState<Device[]>([]);
  const [deviceId, setDeviceId] = useState("");
  const [preset, setPreset] = useState<Preset>("today");
  const [dateFrom, setDateFrom] = useState(todayLocalIso());
  const [dateTo, setDateTo] = useState(todayLocalIso());
  const [maxPoints, setMaxPoints] = useState(DETAIL_LEVELS[0].points);

  const [routes, setRoutes] = useState<LoadedRoute[]>([]);
  const [activeRouteId, setActiveRouteId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedEvent, setSelectedEvent] = useState<RouteHistoryEvent | null>(null);
  const [flyTarget, setFlyTarget] = useState<[number, number] | null>(null);

  // Playback
  const [playTime, setPlayTime] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [playSpeed, setPlaySpeed] = useState(60);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    api
      .listDevices({ limit: 1000, exclude_inactive: true })
      .then(({ items }) => {
        setDevices(items);
        if (preselectedDeviceId && items.some((d) => d.id === preselectedDeviceId)) setDeviceId(preselectedDeviceId);
        else if (items.length > 0) setDeviceId(items[0].id);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "error cargando unidades"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function applyPreset(p: Preset) {
    setPreset(p);
    if (p === "today") {
      setDateFrom(todayLocalIso());
      setDateTo(todayLocalIso());
    } else if (p === "yesterday") {
      setDateFrom(daysAgoLocalIso(1));
      setDateTo(daysAgoLocalIso(1));
    } else if (p === "7d") {
      setDateFrom(daysAgoLocalIso(6));
      setDateTo(todayLocalIso());
    }
  }

  async function loadRoute() {
    if (!deviceId) return;
    setBusy(true);
    setError(null);
    try {
      const device = devices.find((d) => d.id === deviceId);
      const report = await api.deviceRouteHistory(deviceId, {
        from: startOfLocalDayIso(dateFrom),
        to: startOfNextLocalDayIso(dateTo),
        maxPoints,
      });
      setRoutes((prev) => [...prev.filter((r) => r.deviceId !== deviceId), { deviceId, label: device?.label ?? deviceId, report }]);
      setActiveRouteId(deviceId);
      setPlaying(false);
      setPlayTime(null);
      if (report.points.length === 0) setError("Esta unidad no tiene posiciones en ese rango.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando el recorrido");
    } finally {
      setBusy(false);
    }
  }

  function removeRoute(id: string) {
    setRoutes((prev) => prev.filter((r) => r.deviceId !== id));
    if (activeRouteId === id) {
      setActiveRouteId(null);
      setPlaying(false);
      setPlayTime(null);
    }
    setSelectedEvent(null);
  }

  // Memoized per route: HotlinePolyline recreates the layer when the REFERENCE
  // of its points changes -- without this, every playback frame (~60 renders/s)
  // redrew the whole route.
  const hotlinePoints = useMemo(
    () => new Map(routes.map((r) => [r.deviceId, r.report.points.map((p) => ({ lat: p.lat, lon: p.lon, speedKmh: p.speed_kmh ?? 0 }))])),
    [routes],
  );

  const activeRoute = routes.find((r) => r.deviceId === activeRouteId) ?? routes[routes.length - 1] ?? null;
  const points = activeRoute?.report.points ?? [];
  const stats = useMemo(() => (points.length > 1 ? computeStats(points) : null), [points]);
  const startMs = points.length ? new Date(points[0].time).getTime() : 0;
  const endMs = points.length ? new Date(points[points.length - 1].time).getTime() : 0;

  // Animation: advances simulated time at playSpeed x real time.
  useEffect(() => {
    if (!playing || points.length < 2) return;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = now - last;
      last = now;
      setPlayTime((t) => {
        const next = (t ?? startMs) + dt * playSpeed;
        if (next >= endMs) {
          setPlaying(false);
          return endMs;
        }
        return next;
      });
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [playing, playSpeed, points, startMs, endMs]);

  const playIndex = playTime != null && points.length ? findIndexAtTime(points, playTime) : null;
  const playPoint = playIndex != null ? points[playIndex] : null;
  const playLatLng: [number, number] | null = playPoint ? [playPoint.lat, playPoint.lon] : null;

  function selectEvent(event: RouteHistoryEvent) {
    setSelectedEvent(event);
    if (event.lat != null && event.lon != null) setFlyTarget([event.lat, event.lon]);
  }

  const map = (
    <MapContainer center={MEXICO_CITY} zoom={5} minZoom={3} maxZoom={19} className="h-full w-full">
      <MapBaseLayer />
      <MapStyleSwitcher className="top-3 right-3" />
      {geofenceOverlay.visible && <GeofenceLayer geofences={geofenceOverlay.geofences} />}
      <FitToRoutes routes={routes} />
      <FlyToPoint point={flyTarget} />
      <FollowPlayhead point={playLatLng} active={playing} />
      {routes.map((r) => (
        <HotlinePolyline
          key={r.deviceId}
          weight={r.deviceId === activeRoute?.deviceId ? 5 : 3}
          points={hotlinePoints.get(r.deviceId) ?? []}
        />
      ))}
      {routes.flatMap((r) =>
        r.report.events
          .filter((e) => e.lat != null && e.lon != null)
          .map((e) => (
            <Marker key={e.id} position={[e.lat as number, e.lon as number]} icon={eventIcon(e.severity)} eventHandlers={{ click: () => selectEvent(e) }}>
              <Popup>
                <strong>{alarmTypeLabel(e.alarm_type)}</strong>
                <br />
                {new Date(e.time).toLocaleString()}
              </Popup>
            </Marker>
          )),
      )}
      {playPoint && <Marker position={[playPoint.lat, playPoint.lon]} icon={playheadIcon(playPoint.heading)} zIndexOffset={2000} />}
    </MapContainer>
  );

  const playbackBar = activeRoute && points.length > 1 && (
    <div className="glass flex items-center gap-2.5 rounded-2xl px-3 py-2.5">
      <button
        onClick={() => {
          if (!playing && (playTime == null || playTime >= endMs)) setPlayTime(startMs);
          setPlaying((p) => !p);
        }}
        aria-label={playing ? "Pausar" : "Reproducir"}
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-brand-600 text-white shadow-lg hover:bg-brand-500"
      >
        {playing ? (
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
            <rect x="6" y="5" width="4" height="14" rx="1" />
            <rect x="14" y="5" width="4" height="14" rx="1" />
          </svg>
        ) : (
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
            <path d="M7 5v14l12-7z" />
          </svg>
        )}
      </button>
      <div className="min-w-0 flex-1">
        <input
          type="range"
          min={startMs}
          max={endMs}
          step={1000}
          value={playTime ?? startMs}
          onChange={(e) => {
            setPlayTime(Number(e.target.value));
          }}
          className="w-full accent-brand-600"
          aria-label="Línea de tiempo del recorrido"
        />
        <div className="flex justify-between text-[11px] text-ink-dim">
          <span className="font-data">
            {playPoint ? new Date(playPoint.time).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : new Date(startMs).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}
          </span>
          {playPoint && <span className="font-data font-semibold text-ink">{Math.round(playPoint.speed_kmh ?? 0)} km/h</span>}
        </div>
      </div>
      <div className="flex shrink-0 rounded-xl bg-fg/[0.05] p-0.5">
        {PLAYBACK_SPEEDS.map((s) => (
          <button
            key={s}
            onClick={() => setPlaySpeed(s)}
            className={`rounded-lg px-1.5 py-1 text-[11px] font-semibold ${playSpeed === s ? "bg-fg/15 text-ink" : "text-ink-faint hover:text-ink"}`}
          >
            {s}×
          </button>
        ))}
      </div>
    </div>
  );

  const panel = (
    <div className="space-y-4 p-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-ink">Historial de recorridos</h1>
        <p className="mt-0.5 text-xs text-ink-dim">Ruta coloreada por velocidad, eventos y reproducción.</p>
      </div>

      <div className="space-y-2.5">
        <select
          value={deviceId}
          onChange={(e) => setDeviceId(e.target.value)}
          className="block w-full rounded-xl border border-line-strong bg-surface-2 px-3 py-2 text-sm text-ink outline-none focus:border-brand-500"
          aria-label="Unidad"
        >
          <option value="" disabled>
            Selecciona una unidad…
          </option>
          {devices.map((d) => (
            <option key={d.id} value={d.id}>
              {d.label}
            </option>
          ))}
        </select>
        <div className="grid grid-cols-4 gap-1 rounded-xl bg-fg/[0.04] p-1">
          {(
            [
              ["today", "Hoy"],
              ["yesterday", "Ayer"],
              ["7d", "7 días"],
              ["custom", "Rango"],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              onClick={() => applyPreset(key)}
              className={`rounded-lg py-1.5 text-xs font-semibold transition-colors ${preset === key ? "bg-fg/[0.12] text-ink" : "text-ink-dim hover:text-ink"}`}
            >
              {label}
            </button>
          ))}
        </div>
        {preset === "custom" && (
          <div className="grid grid-cols-2 gap-2">
            <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} aria-label="Desde" className="rounded-xl border border-line-strong bg-surface-2 px-2.5 py-2 text-sm text-ink" />
            <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} aria-label="Hasta" className="rounded-xl border border-line-strong bg-surface-2 px-2.5 py-2 text-sm text-ink" />
          </div>
        )}
        <div className="flex items-center gap-2">
          <div className="flex rounded-xl bg-fg/[0.04] p-1" title="Nivel de detalle: más puntos = ruta más precisa, carga un poco más lenta">
            {DETAIL_LEVELS.map((lvl) => (
              <button
                key={lvl.points}
                onClick={() => setMaxPoints(lvl.points)}
                className={`rounded-lg px-2.5 py-1.5 text-xs font-semibold ${maxPoints === lvl.points ? "bg-fg/[0.12] text-ink" : "text-ink-dim hover:text-ink"}`}
              >
                {lvl.label}
              </button>
            ))}
          </div>
          <Button disabled={busy || !deviceId} onClick={loadRoute} className="flex-1">
            {busy ? "Cargando…" : routes.some((r) => r.deviceId === deviceId) ? "Actualizar" : "Ver recorrido"}
          </Button>
        </div>
      </div>

      {error && <Alert>{error}</Alert>}

      {routes.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {routes.map((r) => (
            <span
              key={r.deviceId}
              className={`inline-flex items-center gap-1 rounded-full border py-1 pr-1 pl-2.5 text-xs ${
                r.deviceId === activeRoute?.deviceId ? "border-brand-500/60 bg-brand-600/15 text-ink" : "border-line-strong text-ink-dim"
              }`}
            >
              <button onClick={() => { setActiveRouteId(r.deviceId); setPlaying(false); setPlayTime(null); }}>{r.label}</button>
              <button onClick={() => removeRoute(r.deviceId)} className="flex h-5 w-5 items-center justify-center rounded-full hover:bg-fg/10" aria-label={`Quitar ${r.label}`}>
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      {busy && !stats && (
        <div className="grid grid-cols-2 gap-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-16 w-full rounded-2xl" />
          ))}
        </div>
      )}

      {stats && activeRoute && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <p className="text-sm font-semibold text-ink">{activeRoute.label}</p>
            <SpeedLegend />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Stat label="Distancia" value={stats.distanceKm.toFixed(1)} unit="km" />
            <Stat label="En movimiento" value={formatDuration(stats.movingSeconds)} />
            <Stat label="Vel. máxima" value={String(Math.round(stats.maxSpeed))} unit="km/h" />
            <Stat label="Vel. promedio" value={stats.avgMovingSpeed != null ? String(Math.round(stats.avgMovingSpeed)) : "—"} unit={stats.avgMovingSpeed != null ? "km/h" : undefined} />
            <Stat label="Paradas (≥3 min)" value={String(stats.stops)} />
            <Stat label="Eventos" value={String(activeRoute.report.events.length)} />
          </div>
          <p className="text-[11px] text-ink-faint">Calculado sobre {points.length} puntos resumidos del periodo; valores aproximados.</p>
          {geofenceOverlay.geofences.length > 0 && (
            <label className="flex items-center gap-2 text-xs text-ink-dim">
              <input type="checkbox" className="h-3.5 w-3.5 accent-brand-600" checked={geofenceOverlay.visible} onChange={geofenceOverlay.toggle} />
              Mostrar geocercas
            </label>
          )}
        </div>
      )}

      {activeRoute && activeRoute.report.events.length > 0 && (
        <div className="space-y-1.5">
          <p className="text-sm font-semibold text-ink">Eventos</p>
          {activeRoute.report.events_truncated && (
            <Alert variant="info">Hay más eventos de los que se muestran; acota el rango para verlos todos.</Alert>
          )}
          <ul className="space-y-1">
            {activeRoute.report.events.map((e) => (
              <li key={e.id}>
                <button
                  onClick={() => selectEvent(e)}
                  className={`flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2 text-left transition-colors ${
                    selectedEvent?.id === e.id ? "bg-fg/[0.08]" : "hover:bg-fg/[0.04]"
                  }`}
                >
                  <span className="h-2.5 w-2.5 shrink-0 rotate-45 rounded-[3px]" style={{ background: SEVERITY_COLOR[e.severity] }} aria-hidden />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm text-ink">{alarmTypeLabel(e.alarm_type)}</span>
                    <span className="block text-[11px] text-ink-faint">{new Date(e.time).toLocaleString()}</span>
                  </span>
                  {e.has_video_clip && <span className="text-[10px] font-semibold text-brand-300">CLIP</span>}
                </button>
                {selectedEvent?.id === e.id && e.has_video_clip && (
                  <div className="px-2.5 pb-2">
                    <EventClip event={e} />
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {routes.length === 0 && !busy && (
        <p className="rounded-2xl border border-dashed border-line-strong p-4 text-center text-sm text-ink-dim">
          Elige una unidad y un periodo para ver su recorrido. Puedes cargar varias unidades a la vez.
        </p>
      )}
    </div>
  );

  if (isMobile) {
    return (
      <div className="flex min-h-full flex-col">
        <div className="relative h-[52vh] min-h-[300px] shrink-0 overflow-hidden">
          {map}
          {playbackBar && <div className="absolute inset-x-2 bottom-2 z-[1000]">{playbackBar}</div>}
        </div>
        {panel}
      </div>
    );
  }

  return (
    <div className="relative h-full overflow-hidden">
      <div className="absolute inset-0">{map}</div>
      <aside className="glass absolute top-3 bottom-3 left-3 z-[1100] w-[380px] overflow-y-auto rounded-3xl">{panel}</aside>
      {playbackBar && <div className="absolute right-16 bottom-4 left-[404px] z-[1100]">{playbackBar}</div>}
    </div>
  );
}
