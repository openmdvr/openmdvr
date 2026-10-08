import { formatQuota } from "../lib/duration";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  ApiError,
  hasCamera,
  usesGT06Imei,
  type AlarmSeverity,
  type AlarmVideoClip,
  type Device,
  type DeviceCommand,
  type DeviceCommandType,
  type DeviceProtocol,
  type DevicePosition,
  type Alarm,
  type Tenant,
  type Vehicle,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import {
  STATUS_TOOLTIP,
  UNIT_STATUS_META,
  isDeviceRecent,
  lastSeenLabel,
  timeAgoLabel,
  unitStatus,
  useDeviceOfflineThreshold,
  useNowTick,
} from "../lib/deviceStatus";
import { useFloatingCameras } from "../lib/floatingCameras";
import { StatusAvatar } from "./DeviceListPanel";
import { Alert, Badge, Button, Card, CardTitle, CollapsibleSection, Input, Tooltip, type BadgeTone } from "./ui";
import { CameraIcon, CloseIcon, HistoryIcon, IgnitionKeyIcon, RouteHistoryIcon } from "./icons";
import { SpeedGauge } from "./SpeedGauge";
import { CameraTile } from "./CameraTile";
import { AlarmClipPlayer, CLIP_STATUS_LABEL, alarmCanHaveClip } from "./AlarmClipPlayer";
import { alarmTypeLabel } from "../lib/alarmLabels";
import { useLiveBalance } from "../lib/liveUsage";

const SEVERITY_LABEL: Record<AlarmSeverity, string> = {
  critical: "Crítica",
  warning: "Advertencia",
  info: "Info",
};

const severityTone: Record<AlarmSeverity, BadgeTone> = {
  critical: "danger",
  warning: "warning",
  info: "brand",
};

// Ignition: telemetry reported by the unit itself, never edited by a human.
// Gated by `recent` (see isDeviceRecent) instead of always being shown as the
// current state: an old value shown without saying it is old is worse than
// showing nothing. The current/unknown/never-reported distinction is already in
// the visible LABEL ("Ignition: —" / "unknown (last signal: X)" / "on"), so the
// tooltip does not repeat it.
//
// The equivalent "engine cut" block is not shown here; it is already visible
// through the same icon in DeviceListPanel.tsx.
//
// Tooltip copy is written for end users: one short sentence, no implementation
// details, no repetition of the label.
const IGNITION_TOOLTIP = "Si el motor está encendido o apagado.";

// Preview of this unit's recent alarms. Reads /alarms for THIS unit -- the same
// source that decides the map/list "has alarm" state, so the two never
// contradict each other. "Acknowledge" calls POST /alarms/{id}/acknowledge.
// Deliberately its own mini-component with its own fetch, independent of the
// rest of the panel.
//
// Clip retrieval is on demand, never automatic: the "request clip" button only
// appears if THIS device is gt06_video AND the alarm type can have video on the
// device's SD card (alarmCanHaveClip: camera events; never ignition, geofences,
// speed...).
function DeviceAlarmsPreview({
  deviceId,
  deviceLabel,
  protocol,
  onActiveAlarmChange,
}: {
  deviceId: string;
  deviceLabel: string;
  protocol: DeviceProtocol;
  // Most recent unacknowledged alarm (warning or critical), so the panel header
  // says WHICH alarm it is, not just "has alarm".
  onActiveAlarmChange?: (alarm: Alarm | null) => void;
}) {
  // Source: the unit's ALARMS table -- the SAME one that drives the "has alarm"
  // state (useUnacknowledgedAlarms). The user's notification mailbox is not used
  // because it can be empty (platform accounts receive no notifications; a user
  // may have muted or read them), which would make the header and this section
  // contradict each other.
  const [items, setItems] = useState<Alarm[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [clips, setClips] = useState<Record<string, AlarmVideoClip>>({});
  const [clipBusyId, setClipBusyId] = useState<string | null>(null);
  const [openClipId, setOpenClipId] = useState<string | null>(null);
  const onActiveRef = useRef(onActiveAlarmChange);
  onActiveRef.current = onActiveAlarmChange;

  async function reload() {
    try {
      // Unacknowledged first (the ones that put the unit in "has alarm"), then
      // the most recent for context.
      const [pending, recentAlarms] = await Promise.all([
        api.listAlarms({ deviceId, unacknowledgedOnly: true, minSeverity: "warning", limit: 5 }),
        api.listAlarms({ deviceId, limit: 5 }),
      ]);
      const seen = new Set<string>();
      const merged: Alarm[] = [];
      for (const a of [...pending, ...recentAlarms]) {
        if (seen.has(a.id)) continue;
        seen.add(a.id);
        merged.push(a);
      }
      const shown = merged.slice(0, 5);
      setItems(shown);
      onActiveRef.current?.(pending[0] ?? null);
      setError(null);
      const withClip = shown.filter((a) => alarmCanHaveClip(protocol, a.alarm_type));
      if (withClip.length > 0) {
        const fetched = await Promise.all(withClip.map(async (a) => [a.id, await api.getAlarmClip(a.id)] as const));
        setClips((prev) => {
          const next = { ...prev };
          for (const [id, clip] of fetched) if (clip) next[id] = clip;
          return next;
        });
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando alarmas");
    }
  }

  useEffect(() => {
    reload();
    // Same cadence as the map state (useUnacknowledgedAlarms) so the header and
    // this list never disagree for long.
    const t = setInterval(reload, 15_000);
    return () => {
      clearInterval(t);
      onActiveRef.current?.(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId]);

  // Short polling ONLY while some clip requested by the operator is still in
  // progress -- stops on a terminal state (ready/failed/unsupported) and never
  // runs if nothing was requested.
  useEffect(() => {
    const pendingClips = Object.values(clips).filter((c) => c.status === "requested" || c.status === "uploading");
    if (pendingClips.length === 0) return;
    const t = setInterval(async () => {
      const updated = await Promise.all(pendingClips.map(async (c) => [c.alarm_id, await api.getAlarmClip(c.alarm_id)] as const));
      setClips((prev) => {
        const next = { ...prev };
        for (const [alarmId, clip] of updated) if (clip) next[alarmId] = clip;
        return next;
      });
    }, 4000);
    return () => clearInterval(t);
  }, [clips]);

  async function acknowledge(id: string) {
    setBusyId(id);
    try {
      await api.acknowledgeAlarm(id);
      await reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error reconociendo alarma");
    } finally {
      setBusyId(null);
    }
  }

  async function requestClip(alarmId: string) {
    setClipBusyId(alarmId);
    try {
      const clip = await api.requestAlarmClip(alarmId);
      setClips((prev) => ({ ...prev, [alarmId]: clip }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error pidiendo el clip");
    } finally {
      setClipBusyId(null);
    }
  }

  // Visible even when the section is collapsed: how many of the shown alarms are
  // still unacknowledged (warning or critical).
  const pendingCount = items.filter((a) => !a.acknowledged_at && a.severity !== "info").length;

  return (
    <CollapsibleSection
      title="Alarmas recientes"
      defaultOpen
      badge={pendingCount > 0 ? <Badge tone="danger">{pendingCount} sin reconocer</Badge> : undefined}
    >
      <div className="flex items-center justify-end text-xs">
        <Link
          to={`/notifications?device_id=${deviceId}&device_label=${encodeURIComponent(deviceLabel)}`}
          className="text-brand-500 hover:underline"
        >
          Ver todas
        </Link>
      </div>
      {error && <p className="text-xs text-red-400">{error}</p>}
      {items.length === 0 ? (
        <p className="text-ink-dim">Sin alarmas recientes.</p>
      ) : (
        <ul className="divide-y divide-line">
          {items.map((a) => {
            const clip = clips[a.id];
            return (
              <li key={a.id} className="space-y-1.5 py-1.5">
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5">
                      <Badge tone={severityTone[a.severity]}>{SEVERITY_LABEL[a.severity]}</Badge>
                      <span className="truncate text-ink">{alarmTypeLabel(a.alarm_type)}</span>
                    </div>
                    <p className="text-[11px] text-ink-dim">{new Date(a.time).toLocaleString()}</p>
                  </div>
                  {a.acknowledged_at ? (
                    <Badge tone="muted">reconocida</Badge>
                  ) : (
                    <Button variant="secondary" disabled={busyId === a.id} onClick={() => acknowledge(a.id)}>
                      Reconocer
                    </Button>
                  )}
                </div>

                {alarmCanHaveClip(protocol, a.alarm_type) && (
                  <div className="flex items-center gap-2 text-[11px]">
                    {/*
                     * After 'failed' a retry is offered again; 'unsupported'
                     * is not (protocol limitation).
                     */}
                    {(!clip || clip.status === "failed") && (
                      <Button variant="secondary" disabled={clipBusyId === a.id} onClick={() => requestClip(a.id)}>
                        {clip ? "Reintentar pedido de clip" : "Pedir clip de video (~1 min)"}
                      </Button>
                    )}
                    {clip && (clip.status === "requested" || clip.status === "uploading") && (
                      <span className="text-ink-dim">{CLIP_STATUS_LABEL[clip.status]}</span>
                    )}
                    {clip && clip.status === "ready" && clip.url && (
                      <Button variant="secondary" onClick={() => setOpenClipId((cur) => (cur === a.id ? null : a.id))}>
                        {openClipId === a.id ? "Ocultar clip" : "Ver clip"}
                      </Button>
                    )}
                    {clip && clip.status === "ready" && !clip.url && (
                      <span className="text-ink-dim">este clip ya no está disponible</span>
                    )}
                    {clip && (clip.status === "failed" || clip.status === "unsupported") && (
                      <span className="text-red-400">{clip.error_detail ?? CLIP_STATUS_LABEL[clip.status]}</span>
                    )}
                  </div>
                )}
                {clip && clip.status === "ready" && clip.url && openClipId === a.id && (
                  <AlarmClipPlayer url={clip.url} secondaryUrl={clip.url_secondary} />
                )}
              </li>
            );
          })}
        </ul>
      )}
    </CollapsibleSection>
  );
}

const ENGINE_COMMAND_LABEL: Record<DeviceCommandType, string> = {
  engine_stop: "Cortar motor",
  engine_resume: "Reconectar motor",
};

const COMMAND_STATUS_LABEL: Record<DeviceCommand["status"], string> = {
  pending: "en curso",
  success: "éxito",
  failed: "falló",
  timeout: "sin respuesta",
  device_offline: "sin conexión",
};

const COMMAND_STATUS_TONE: Record<DeviceCommand["status"], BadgeTone> = {
  pending: "brand",
  success: "success",
  failed: "danger",
  timeout: "warning",
  device_offline: "muted",
};

// How long the "resume" confirmation stays armed before auto-cancelling -- same
// value as DriverHome.tsx. "Cut engine" does NOT use this timer: it requires
// typing the unit label instead of a single tap (friction proportional to risk
// -- cutting fuel to a real vehicle is the most dangerous action in the
// product), and auto-cancelling mid-typing would be more frustrating than
// useful.
const RESUME_CONFIRM_TIMEOUT_MS = 8000;

// Engine controls -- ONLY for GT06 devices (see the gate in the main panel) and
// only visible to tenant_admin/platform bypass (same permission the backend
// requires, require_tenant_admin in device_commands.py; never misrepresent
// permissions on screen). Its own mini-component (same pattern as
// DeviceAlarmsPreview): its own fetch/state, independent of the rest of the
// panel.
function DeviceEngineControls({ deviceId, deviceLabel }: { deviceId: string; deviceLabel: string }) {
  const [history, setHistory] = useState<DeviceCommand[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stopArmed, setStopArmed] = useState(false);
  const [stopConfirmText, setStopConfirmText] = useState("");
  const [resumeArmed, setResumeArmed] = useState(false);
  const resumeTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function reload() {
    try {
      // Short preview (last 2, same as DeviceAlarmsPreview); the full history
      // with pagination/date filter lives on its own page (see "view full
      // history" below).
      setHistory((await api.listDeviceCommands(deviceId, { limit: 2 })).items);
    } catch {
      // Silent on purpose, same as DeviceAlarmsPreview: the history is
      // informational, a transient error must not cover the rest of the panel
      // with an intrusive message.
    }
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId]);

  useEffect(() => {
    return () => {
      if (resumeTimeoutRef.current) clearTimeout(resumeTimeoutRef.current);
    };
  }, []);

  function cancelResumeArm() {
    if (resumeTimeoutRef.current) clearTimeout(resumeTimeoutRef.current);
    setResumeArmed(false);
  }

  async function send(commandType: DeviceCommandType) {
    setBusy(true);
    setError(null);
    try {
      // Waits for the device's REAL reply (the backend does not answer until the
      // Go server receives 0x15 or times out) -- the result shown below (via
      // reload) is what actually happened, never a blind "command sent".
      await api.sendDeviceCommand(deviceId, commandType);
      await reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error enviando el comando");
    } finally {
      setBusy(false);
      setStopArmed(false);
      setStopConfirmText("");
      cancelResumeArm();
    }
  }

  return (
    // defaultOpen=false on purpose: the most sensitive action in the panel
    // (cutting fuel to a real vehicle) -- opened when needed, it does not take
    // up space by default like Alarms.
    <CollapsibleSection title="Motor">
      {error && <Alert>{error}</Alert>}

      {stopArmed ? (
        <div className="space-y-2 rounded-sm border border-red-500/40 bg-red-500/5 p-2">
          <p className="text-ink">
            Escribe <span className="font-data font-semibold text-ink">{deviceLabel}</span> para confirmar el corte
            de motor.
          </p>
          <Input
            autoFocus
            value={stopConfirmText}
            onChange={(e) => setStopConfirmText(e.target.value)}
            placeholder={deviceLabel}
          />
          <div className="flex gap-2">
            <Button
              disabled={busy || stopConfirmText !== deviceLabel}
              onClick={() => send("engine_stop")}
              className="flex-1 border-transparent bg-red-600 text-white hover:bg-red-700"
            >
              Sí, cortar motor
            </Button>
            <Button
              variant="secondary"
              className="flex-1"
              disabled={busy}
              onClick={() => {
                setStopArmed(false);
                setStopConfirmText("");
              }}
            >
              Cancelar
            </Button>
          </div>
        </div>
      ) : resumeArmed ? (
        <div className="flex gap-2">
          <Button disabled={busy} className="flex-1" onClick={() => send("engine_resume")}>
            Sí, reconectar
          </Button>
          <Button variant="secondary" disabled={busy} className="flex-1" onClick={cancelResumeArm}>
            Cancelar
          </Button>
        </div>
      ) : (
        <div className="flex gap-2">
          <Button
            disabled={busy}
            onClick={() => setStopArmed(true)}
            className="flex-1 border-transparent bg-red-600 text-white hover:bg-red-700"
          >
            Cortar motor
          </Button>
          <Button
            variant="secondary"
            disabled={busy}
            className="flex-1"
            onClick={() => {
              setResumeArmed(true);
              if (resumeTimeoutRef.current) clearTimeout(resumeTimeoutRef.current);
              resumeTimeoutRef.current = setTimeout(() => setResumeArmed(false), RESUME_CONFIRM_TIMEOUT_MS);
            }}
          >
            Reconectar motor
          </Button>
        </div>
      )}

      <div className="space-y-2 border-t border-line pt-2">
        <div className="flex items-center justify-between">
          <p className="font-semibold tracking-wide text-ink-dim uppercase">Historial</p>
          <Link
            to={`/devices/${deviceId}/commands?device_label=${encodeURIComponent(deviceLabel)}`}
            className="text-brand-500 hover:underline"
          >
            Ver historial completo
          </Link>
        </div>
        {history.length === 0 ? (
          <p className="text-ink-dim">Sin comandos enviados todavía.</p>
        ) : (
          <ul className="divide-y divide-line">
            {history.map((c) => (
              <li key={c.id} className="space-y-0.5 py-1.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="min-w-0 truncate text-ink">{ENGINE_COMMAND_LABEL[c.command_type]}</span>
                  <Badge tone={COMMAND_STATUS_TONE[c.status]}>{COMMAND_STATUS_LABEL[c.status]}</Badge>
                </div>
                <p className="text-[11px] text-ink-dim">
                  Pedido por {c.requested_by_email} · {new Date(c.requested_at).toLocaleString()}
                </p>
                <p className="text-[11px] text-ink-dim">
                  {c.completed_at ? (
                    <>Resuelto: {new Date(c.completed_at).toLocaleString()}</>
                  ) : (
                    <>Aún en curso, sin resolver</>
                  )}
                </p>
                <p className="text-[11px] text-ink-dim">
                  Respuesta del dispositivo:{" "}
                  {c.device_reply ? <span className="font-data text-ink">{c.device_reply}</span> : "(sin texto)"}
                </p>
              </li>
            ))}
          </ul>
        )}
      </div>
    </CollapsibleSection>
  );
}

// Detail panel for one unit: device info, telemetry/trail if it has reported a
// position, tenant monthly quota balance, and on-demand video (CameraTile, with
// its own "watch live" placeholder -- requests nothing until the user clicks).
// Opened the same way from the unit list or a map marker (MapView.tsx unifies
// both into the same "selectedId" state).
function QuickAction({
  label,
  onClick,
  to,
  active = false,
  children,
}: {
  label: string;
  onClick?: () => void;
  to?: string;
  active?: boolean;
  children: React.ReactNode;
}) {
  const cls = `flex flex-col items-center justify-center gap-1 rounded-2xl border py-2.5 text-[11px] font-semibold transition-colors ${
    active ? "border-brand-500/50 bg-brand-600/15 text-brand-300" : "border-line-strong bg-fg/[0.03] text-ink-dim hover:bg-fg/[0.07] hover:text-ink"
  }`;
  return to ? (
    <Link to={to} className={cls}>
      {children}
      {label}
    </Link>
  ) : (
    <button type="button" onClick={onClick} className={cls}>
      {children}
      {label}
    </button>
  );
}

function Metric({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0 rounded-2xl border border-line bg-fg/[0.03] px-3 py-2.5">
      <p className="text-[11px] text-ink-faint">{label}</p>
      <div className="mt-0.5 truncate text-base font-semibold text-ink">{children}</div>
    </div>
  );
}

export function DeviceDetailPanel({
  device,
  vehicle,
  position,
  tenant,
  trailVisible,
  onToggleTrail,
  onClose,
  hasAlarm = false,
}: {
  device: Device;
  // Installed vehicle, resolved by the parent (plate/make/driver live in
  // Vehicle, see migration 0014).
  vehicle: Vehicle | null;
  position: DevicePosition | null;
  tenant: Tenant | null;
  trailVisible: boolean;
  onToggleTrail: () => void;
  onClose: () => void;
  hasAlarm?: boolean;
}) {
  const offlineThresholdSeconds = useDeviceOfflineThreshold();
  useNowTick(); // without this, "X ago"/"seen X ago" would freeze
  const recent = isDeviceRecent(device.last_seen_at, offlineThresholdSeconds);
  const { role, isPlatform } = useAuth();
  const { openCamera, isOpen } = useFloatingCameras();
  // Shared balance (drops at the rate of cameras open in the tenant).
  const liveBalance = useLiveBalance({ deviceId: device.id });
  // Never show a control the backend would reject anyway (require_tenant_admin
  // in device_commands.py is the real barrier).
  const canManageEngine = isPlatform || role === "tenant_admin";
  const videoSectionRef = useRef<HTMLElement>(null);
  const liveSpeed = position && isDeviceRecent(position.time, offlineThresholdSeconds) ? position.speed_kmh : null;
  const status = unitStatus(device.last_seen_at, offlineThresholdSeconds, liveSpeed, device.ignition_on, hasAlarm);
  const meta = UNIT_STATUS_META[status];
  const [activeAlarm, setActiveAlarm] = useState<Alarm | null>(null);
  const channels: Array<{ ch: number | undefined; name: string }> =
    device.protocol === "gt06_video" ? [{ ch: 0, name: "Frontal" }, { ch: 1, name: "Cabina" }] : [{ ch: undefined, name: "Cámara" }];

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header: identity + status at a glance. */}
      <div className="flex items-start gap-3 p-4 pb-3">
        <StatusAvatar status={status} size={44} />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-base font-semibold tracking-tight text-ink">{device.label}</h3>
          <p className="truncate text-xs text-ink-dim">
            {[vehicle?.plate, [vehicle?.make, vehicle?.model].filter(Boolean).join(" ")].filter(Boolean).join(" · ") ||
              (usesGT06Imei(device.protocol) ? `IMEI ${device.gt06_imei}` : `Terminal ${device.jt808_terminal_id}`)}
          </p>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            <Badge tone={meta.tone}>
              {status === "alarm" && activeAlarm ? `Alarma: ${alarmTypeLabel(activeAlarm.alarm_type)}` : meta.label}
            </Badge>
            <span className="text-[11px] text-ink-faint">{lastSeenLabel(device.last_seen_at, offlineThresholdSeconds)}</span>
          </div>
        </div>
        <button
          onClick={onClose}
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-dim hover:bg-fg/[0.08] hover:text-ink"
          aria-label="Cerrar"
        >
          <CloseIcon size={15} />
        </button>
      </div>

      {/* Quick actions -- the most common things to do with a unit, no scrolling. */}
      <div className="grid grid-cols-3 gap-2 px-4 pb-3">
        {hasCamera(device.protocol) && (
          <QuickAction
            label={device.protocol === "gt06_video" ? "Cámaras" : "Cámara"}
            active={channels.some((c) => isOpen(device.id, c.ch))}
            onClick={() =>
              channels.forEach((c) =>
                openCamera({ deviceId: device.id, channel: c.ch, protocol: device.protocol, label: channels.length > 1 ? `${device.label} · ${c.name}` : device.label }),
              )
            }
          >
            <CameraIcon size={18} />
          </QuickAction>
        )}
        {position && (
          <QuickAction label={trailVisible ? "Ocultar ruta" : "Última hora"} active={trailVisible} onClick={onToggleTrail}>
            <RouteHistoryIcon size={18} />
          </QuickAction>
        )}
        <QuickAction label="Historial" to={`/route-history?device=${device.id}`}>
          <HistoryIcon size={18} />
        </QuickAction>
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 pb-4">
        {/* Metrics -- small label, large value. */}
        <div className="grid grid-cols-2 gap-2">
          <Metric label="Velocidad">
            {liveSpeed != null ? (
              <>
                {Math.round(liveSpeed)} <span className="text-xs font-medium text-ink-faint">km/h</span>
              </>
            ) : position?.speed_kmh != null ? (
              <span className="text-ink-dim">
                {Math.round(position.speed_kmh)} <span className="text-xs font-medium">km/h</span>
              </span>
            ) : (
              "—"
            )}
          </Metric>
          <Metric label="Ignición">
            <span className="flex items-center gap-1.5">
              <span className={recent && device.ignition_on ? "text-accent-warn" : "text-ink-faint"}>
                <IgnitionKeyIcon size={16} />
              </span>
              <span className={recent ? "" : "text-ink-dim"}>
                {device.ignition_on === null ? "—" : !recent ? "Desconocida" : device.ignition_on ? "Encendida" : "Apagada"}
              </span>
            </span>
          </Metric>
          <Metric label="Rumbo">{position?.heading != null ? `${position.heading.toFixed(0)}°` : "—"}</Metric>
          <Metric label="Conductor">
            <span className="truncate">{vehicle?.current_driver_name ?? "—"}</span>
          </Metric>
        </div>
        {device.ignition_changed_at && (
          <p className="text-[11px] text-ink-faint">
            {IGNITION_TOOLTIP} Último cambio {timeAgoLabel(device.ignition_changed_at)}.
          </p>
        )}

        {position && vehicle?.max_speed_kmh != null && position.speed_kmh != null && (
          <div className="flex justify-center rounded-2xl border border-line bg-surface-2/40 py-2">
            <SpeedGauge speedKmh={position.speed_kmh} maxSpeedKmh={vehicle.max_speed_kmh} stale={!recent} />
          </div>
        )}
        {position && vehicle?.max_speed_kmh == null && canManageEngine && (
          <p className="text-xs text-ink-dim">
            {device.vehicle_id ? (
              <>
                Sin límite de velocidad —{" "}
                <Link to={`/admin/tenants/${device.tenant_id}?tab=fleet`} className="text-brand-400 hover:underline">
                  configurar
                </Link>
              </>
            ) : (
              <>
                Sin vehículo vinculado —{" "}
                <Link to={`/admin/tenants/${device.tenant_id}`} className="text-brand-400 hover:underline">
                  vincular uno
                </Link>{" "}
                para configurar un límite.
              </>
            )}
          </p>
        )}
        {position ? (
          <p className="font-data text-[11px] text-ink-faint">
            {position.lat.toFixed(5)}, {position.lon.toFixed(5)} · {new Date(position.time).toLocaleString()}
          </p>
        ) : (
          <p className="text-xs text-ink-dim">Esta unidad todavía no ha reportado posición.</p>
        )}

        {/*
         * Video -- inline preview; "open in window" pops it out to a floating
         * window that survives navigation. A gt06_video (JC261/JC400) has TWO
         * independent physical cameras.
         */}
        {hasCamera(device.protocol) && (
          <Card ref={videoSectionRef} className="space-y-3 p-3">
            <CardTitle
              action={
                tenant && (
                  <span className="text-[11px] text-ink-dim">
                    <span className="font-data font-semibold text-ink">{formatQuota(liveBalance?.remaining ?? tenant.live_view_seconds_remaining)}</span> este mes
                  </span>
                )
              }
            >
              Video
            </CardTitle>
            {channels.map((c) => (
              <div key={String(c.ch)} className="space-y-1.5">
                <CameraTile deviceId={device.id} label={c.name} channel={c.ch} protocol={device.protocol} />
                <button
                  onClick={() =>
                    openCamera(
                      { deviceId: device.id, channel: c.ch, protocol: device.protocol, label: channels.length > 1 ? `${device.label} · ${c.name}` : device.label },
                      { mode: "floating" },
                    )
                  }
                  className="flex w-full items-center justify-center gap-1.5 rounded-xl py-1.5 text-xs font-medium text-ink-dim hover:bg-fg/[0.05] hover:text-ink"
                >
                  {isOpen(device.id, c.ch) ? "Ya está abierta" : "Abrir en ventana flotante ↗"}
                </button>
              </div>
            ))}
          </Card>
        )}

        {tenant && (
          <p className="text-[11px] text-ink-faint">
            <Tooltip label={STATUS_TOOLTIP}>
              <span>Estado administrativo: {device.status}</span>
            </Tooltip>
            {" · "}
            {tenant.name}
          </p>
        )}

        <DeviceAlarmsPreview
          deviceId={device.id}
          deviceLabel={device.label}
          protocol={device.protocol}
          onActiveAlarmChange={setActiveAlarm}
        />

        {device.protocol === "gt06" && canManageEngine && (
          <DeviceEngineControls deviceId={device.id} deviceLabel={device.label} />
        )}

        {device.notes && (
          <CollapsibleSection title="Notas">
            <p className="whitespace-pre-wrap text-ink">{device.notes}</p>
          </CollapsibleSection>
        )}
      </div>
    </div>
  );
}
