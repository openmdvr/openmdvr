// Device status: TWO distinct concepts, documented here so both tooltip texts
// live next to the logic they describe instead of being repeated by hand in each
// component:
//
// 1. `devices.status` ("active"/"inactive"/"maintenance"): an explicit database
//   field set by a human from Administration -- unrelated to whether the device
//   is talking to the server right now.
// 2. "Seen now"/liveness: a FRONTEND computation over `last_seen_at` (when the
//   last message from the device reached the JT808/GT06 server, heartbeats
//   included) against a platform-wide configurable threshold
//   (`platform_monitoring_settings.device_offline_threshold_seconds`, default
//   300s -- see migration 0028 and api/app/routers/platform.py). An "active"
//   device can perfectly well show grey (no recent signal) if it ran out of
//   battery/coverage.
import { useEffect, useState } from "react";
import { api } from "./api";

// Forces a periodic re-render -- WITHOUT this, "X ago"/"seen X ago" are computed
// once in the render that showed them and stay FROZEN (Date.now() is only
// evaluated when something else triggers a render) until the page is reloaded.
// One interval per calling component (DeviceListPanel/DeviceDetailPanel) --
// never one per row/field.
export function useNowTick(intervalMs = 30_000): void {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
}

// Tooltip copy is written for end users: one short sentence each, without
// implementation jargon.
export const STATUS_TOOLTIP = "Lo define un administrador. No indica si está transmitiendo ahora.";

export function livenessTooltip(thresholdSeconds: number): string {
  const minutes = Math.round(thresholdSeconds / 60);
  const window = minutes >= 1 ? `${minutes} min` : `${thresholdSeconds}s`;
  return `Verde si reportó hace menos de ${window}, gris si no.`;
}

// Default threshold while useDeviceOfflineThreshold() has not resolved its first
// fetch -- matches the migration DEFAULT (300s) so the UI does not "flicker"
// from an arbitrary value to the real one.
const FALLBACK_THRESHOLD_SECONDS = 300;

export function useDeviceOfflineThreshold(): number {
  const [thresholdSeconds, setThresholdSeconds] = useState(FALLBACK_THRESHOLD_SECONDS);

  useEffect(() => {
    let cancelled = false;
    api
      .getMonitoringSettings()
      .then((s) => {
        if (!cancelled) setThresholdSeconds(s.device_offline_threshold_seconds);
      })
      .catch(() => {
        // Silent on purpose, same as useUnacknowledgedAlarms: a transient error
        // must not break the rest of the UI -- stays on the fallback until the
        // next mount/reload.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return thresholdSeconds;
}

export function isDeviceRecent(lastSeenAt: string | null, thresholdSeconds: number): boolean {
  if (!lastSeenAt) return false;
  return Date.now() - new Date(lastSeenAt).getTime() < thresholdSeconds * 1000;
}

export function lastSeenLabel(lastSeenAt: string | null, thresholdSeconds: number, compact = false): string {
  const prefix = compact ? "" : "visto ";
  if (!lastSeenAt) return compact ? "nunca" : "nunca visto";
  const diffMs = Date.now() - new Date(lastSeenAt).getTime();
  if (diffMs < thresholdSeconds * 1000) return compact ? "ahora" : "visto ahora";
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 60) return `${prefix}hace ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${prefix}hace ${hours} h`;
  return `${prefix}hace ${Math.floor(hours / 24)} d`;
}

// Generic "X ago" (without lastSeenLabel's liveness/threshold semantics) -- used
// by ignition_changed_at/power_changed_at (see DeviceDetailPanel.tsx); any
// future "last changed" timestamp should reuse this instead of another
// hand-rolled computation.
export function timeAgoLabel(iso: string | null): string {
  if (!iso) return "";
  const diffMs = Date.now() - new Date(iso).getTime();
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return "hace instantes";
  if (minutes < 60) return `hace ${minutes}min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `hace ${hours}h`;
  return `hace ${Math.floor(hours / 24)}d`;
}

// Operational status of a unit -- one rule for the list, the map markers and the
// fleet summary. Priority: unacknowledged alarm > no signal > moving > idling
// (ignition on and stopped) > stopped.
export type UnitStatus = "alarm" | "offline" | "moving" | "idle" | "stopped";

export const MOVING_SPEED_KMH = 5;

export const UNIT_STATUS_META: Record<UnitStatus, { label: string; color: string; tone: "danger" | "muted" | "success" | "warning" | "neutral" }> = {
  alarm: { label: "Con alarma", color: "#f43f5e", tone: "danger" },
  moving: { label: "En movimiento", color: "#22c55e", tone: "success" },
  idle: { label: "En ralentí", color: "#f5b83d", tone: "warning" },
  stopped: { label: "Detenida", color: "#5aa9ff", tone: "neutral" },
  offline: { label: "Sin señal", color: "#64748b", tone: "muted" },
};

export function unitStatus(
  lastSeenAt: string | null,
  thresholdSeconds: number,
  speedKmh: number | null | undefined,
  ignitionOn: boolean | null,
  hasAlarm: boolean,
): UnitStatus {
  if (hasAlarm) return "alarm";
  if (!isDeviceRecent(lastSeenAt, thresholdSeconds)) return "offline";
  if ((speedKmh ?? 0) >= MOVING_SPEED_KMH) return "moving";
  if (ignitionOn) return "idle";
  return "stopped";
}
