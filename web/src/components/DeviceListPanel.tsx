import { useEffect, useMemo, useState } from "react";
import { hasCamera, usesGT06Imei, type AlarmSeverity, type Device, type DevicePosition, type Vehicle } from "../lib/api";
import {
  isDeviceRecent,
  lastSeenLabel,
  unitStatus,
  UNIT_STATUS_META,
  useDeviceOfflineThreshold,
  useNowTick,
  type UnitStatus,
} from "../lib/deviceStatus";
import { useFloatingCameras } from "../lib/floatingCameras";
import { CameraIcon, IgnitionKeyIcon, SearchIcon, TruckIcon } from "./icons";
import { Input } from "./ui";

// Unit list (Map side panel on desktop, Units tab on mobile): status filters
// with counts, rows with a status avatar, live speed or "seen X ago", and a
// camera button that opens the camera directly.
//
// The real cost to bound on mobile is RENDERING (hundreds of DOM rows), not
// network: rows are shown STEP at a time with "show more" instead of
// previous/next pagination (more natural when scrolling on a phone).
const STEP = 40;

type Filter = "all" | UnitStatus;

export interface UnitRow {
  device: Device;
  vehicle: Vehicle | undefined;
  position: DevicePosition | undefined;
  status: UnitStatus;
  liveSpeed: number | null;
}

// Also used by MapView (markers and fleet summary) -- a single status
// computation for the whole screen.
export function useUnitRows(
  devices: Device[],
  vehicleById: Map<string, Vehicle>,
  positionByDevice?: Map<string, DevicePosition>,
  severityByDevice?: Map<string, AlarmSeverity>,
): UnitRow[] {
  const threshold = useDeviceOfflineThreshold();
  useNowTick(); // keeps "seen X ago" and the switch to "no signal" from freezing
  return useMemo(
    () =>
      devices.map((d) => {
        const position = positionByDevice?.get(d.id);
        // Speed only counts if the POSITION is recent: a parked GT06 keeps
        // sending heartbeats without new positions, and its old last speed must
        // not show it as "moving".
        const liveSpeed = position && isDeviceRecent(position.time, threshold) ? position.speed_kmh : null;
        return {
          device: d,
          vehicle: d.vehicle_id ? vehicleById.get(d.vehicle_id) : undefined,
          position,
          liveSpeed,
          status: unitStatus(d.last_seen_at, threshold, liveSpeed, d.ignition_on, severityByDevice?.has(d.id) ?? false),
        };
      }),
    [devices, vehicleById, positionByDevice, severityByDevice, threshold],
  );
}

const FILTERS: Filter[] = ["all", "moving", "idle", "stopped", "offline", "alarm"];

export function StatusAvatar({ status, size = 36 }: { status: UnitStatus; size?: number }) {
  const color = UNIT_STATUS_META[status].color;
  return (
    <span
      className="relative flex shrink-0 items-center justify-center rounded-xl"
      style={{ width: size, height: size, background: `${color}22`, color }}
      title={UNIT_STATUS_META[status].label}
    >
      <TruckIcon />
      <span className="absolute -right-0.5 -bottom-0.5 h-3 w-3 rounded-full ring-2 ring-surface" style={{ background: color }} aria-hidden />
    </span>
  );
}

export function DeviceListPanel({
  devices,
  vehicleById,
  positionByDevice,
  severityByDevice,
  selectedId,
  onSelect,
}: {
  devices: Device[];
  vehicleById: Map<string, Vehicle>;
  positionByDevice?: Map<string, DevicePosition>;
  severityByDevice?: Map<string, AlarmSeverity>;
  selectedId: string | null;
  onSelect: (deviceId: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [visible, setVisible] = useState(STEP);
  const threshold = useDeviceOfflineThreshold();
  const rows = useUnitRows(devices, vehicleById, positionByDevice, severityByDevice);
  const { openCamera, isOpen } = useFloatingCameras();

  const counts = useMemo(() => {
    const c: Record<Filter, number> = { all: rows.length, moving: 0, idle: 0, stopped: 0, offline: 0, alarm: 0 };
    rows.forEach((r) => (c[r.status] += 1));
    return c;
  }, [rows]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows.filter((r) => {
      if (filter !== "all" && r.status !== filter) return false;
      if (!q) return true;
      const identifier = usesGT06Imei(r.device.protocol) ? r.device.gt06_imei : r.device.jt808_terminal_id;
      return (
        r.device.label.toLowerCase().includes(q) ||
        (identifier ?? "").includes(q) ||
        (r.vehicle?.plate ?? "").toLowerCase().includes(q) ||
        (r.vehicle?.current_driver_name ?? "").toLowerCase().includes(q)
      );
    });
  }, [rows, query, filter]);

  useEffect(() => setVisible(STEP), [query, filter]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="space-y-2.5 p-3">
        <div className="relative">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-ink-faint" />
          <Input className="pl-9" placeholder="Buscar unidad, placa, chofer…" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <div className="-mx-3 flex gap-1.5 overflow-x-auto px-3 pb-0.5 no-scrollbar">
          {FILTERS.map((f) => {
            if (f !== "all" && counts[f] === 0) return null;
            const active = filter === f;
            const color = f === "all" ? undefined : UNIT_STATUS_META[f].color;
            return (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors ${
                  active ? "border-brand-500/60 bg-brand-600/15 text-ink" : "border-line-strong text-ink-dim hover:text-ink"
                }`}
              >
                {color && <span className="h-2 w-2 rounded-full" style={{ background: color }} aria-hidden />}
                {f === "all" ? "Todas" : UNIT_STATUS_META[f].label}
                <span className="font-data text-ink-faint">{counts[f]}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {filtered.length === 0 ? (
          <p className="px-3 py-8 text-center text-sm text-ink-dim">{devices.length === 0 ? "Todavía no hay unidades." : "Sin resultados."}</p>
        ) : (
          <ul className="space-y-0.5">
            {filtered.slice(0, visible).map(({ device: d, vehicle, status, liveSpeed }) => {
              const selected = selectedId === d.id;
              const camChannels = !hasCamera(d.protocol) ? [] : d.protocol === "gt06_video" ? [0, 1] : [undefined];
              return (
                <li key={d.id} className={`group flex items-center gap-1 rounded-xl transition-colors ${selected ? "bg-brand-600/12 ring-1 ring-brand-500/40" : "hover:bg-fg/[0.04]"}`}>
                  <button onClick={() => onSelect(d.id)} className="flex min-w-0 flex-1 items-center gap-3 px-2 py-2 text-left">
                    <StatusAvatar status={status} />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-1.5">
                        <span className="truncate text-sm font-semibold text-ink">{d.label}</span>
                        {isDeviceRecent(d.last_seen_at, threshold) && d.ignition_on && (
                          <span className="shrink-0 text-accent-warn" title="Ignición encendida">
                            <IgnitionKeyIcon size={12} />
                          </span>
                        )}
                      </span>
                      <span className="block truncate text-xs text-ink-dim">
                        {[vehicle?.plate, vehicle?.current_driver_name ?? [vehicle?.make, vehicle?.model].filter(Boolean).join(" ")].filter(Boolean).join(" · ") ||
                          UNIT_STATUS_META[status].label}
                      </span>
                    </span>
                    {/*
                     * FIXED-width, right-aligned column: speed or "seen X ago"
                     * sits at the same position on every row regardless of
                     * camera buttons.
                     */}
                    <span className="w-[72px] shrink-0 text-right">
                      {status === "moving" && liveSpeed != null ? (
                        <span className="font-data text-sm font-semibold text-emerald-300">
                          {Math.round(liveSpeed)}
                          <span className="ml-0.5 text-[10px] font-medium text-ink-faint">km/h</span>
                        </span>
                      ) : (
                        <span className="text-[11px] whitespace-nowrap text-ink-faint">{lastSeenLabel(d.last_seen_at, threshold, true)}</span>
                      )}
                    </span>
                  </button>
                  {/*
                   * A single camera button per row (a dual-camera unit opens
                   * Front and Cabin together), with the same slot reserved on
                   * rows without a camera so all rows have the same width.
                   */}
                  <div className="flex w-10 shrink-0 justify-center pr-1.5">
                    {camChannels.length > 0 && (
                      <button
                        onClick={() =>
                          camChannels.forEach((ch) =>
                            openCamera({
                              deviceId: d.id,
                              channel: ch,
                              label: ch === 0 ? `${d.label} · Frontal` : ch === 1 ? `${d.label} · Cabina` : d.label,
                              protocol: d.protocol,
                            }),
                          )
                        }
                        title={camChannels.length > 1 ? "Ver las 2 cámaras en la cinta" : "Ver la cámara en la cinta"}
                        aria-label={camChannels.length > 1 ? `Abrir las 2 cámaras de ${d.label}` : `Abrir cámara de ${d.label}`}
                        className={`relative flex h-8 w-8 items-center justify-center rounded-lg transition-colors ${
                          camChannels.some((ch) => isOpen(d.id, ch)) ? "bg-rose-500/15 text-rose-300" : "text-ink-faint hover:bg-fg/[0.08] hover:text-ink"
                        }`}
                      >
                        <CameraIcon size={16} />
                        {camChannels.length > 1 && (
                          <span className="absolute -top-0.5 -right-0.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-fg/15 px-0.5 text-[8px] font-bold text-ink">
                            2
                          </span>
                        )}
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
        {filtered.length > visible && (
          <button onClick={() => setVisible((v) => v + STEP)} className="mt-1 w-full rounded-xl py-2.5 text-xs font-medium text-ink-dim hover:bg-fg/[0.04] hover:text-ink">
            Ver más ({filtered.length - visible})
          </button>
        )}
      </div>
    </div>
  );
}
