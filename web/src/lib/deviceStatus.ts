import i18next from 'i18next';
import { useEffect, useState } from "react";
import { api } from "./api";

export function useNowTick(intervalMs = 30_000): void {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
}

export function statusTooltip(): string {
  return i18next.t('device_status.admin_tooltip');
}

// Keep for backward compatibility with existing callers
export const STATUS_TOOLTIP = "Lo define un administrador. No indica si está transmitiendo ahora.";

export function livenessTooltip(thresholdSeconds: number): string {
  const minutes = Math.round(thresholdSeconds / 60);
  const window = minutes >= 1 ? `${minutes} min` : `${thresholdSeconds}s`;
  return i18next.t('device_status.liveness_tooltip', { window });
}

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
      .catch(() => {});
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
  if (!lastSeenAt) return compact ? i18next.t('device_status.never_compact') : i18next.t('device_status.never');
  const diffMs = Date.now() - new Date(lastSeenAt).getTime();
  if (diffMs < thresholdSeconds * 1000) {
    return compact ? i18next.t('device_status.seen_now_compact') : i18next.t('device_status.seen_now');
  }
  const prefix = compact ? '' : i18next.t('device_status.seen_prefix');
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 60) return `${prefix}${i18next.t('device_status.ago_minutes', { count: minutes })}`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${prefix}${i18next.t('device_status.ago_hours', { count: hours })}`;
  return `${prefix}${i18next.t('device_status.ago_days', { count: Math.floor(hours / 24) })}`;
}

export function timeAgoLabel(iso: string | null): string {
  if (!iso) return '';
  const diffMs = Date.now() - new Date(iso).getTime();
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return i18next.t('device_status.ago_just_now');
  if (minutes < 60) return i18next.t('device_status.ago_short_minutes', { count: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return i18next.t('device_status.ago_short_hours', { count: hours });
  return i18next.t('device_status.ago_short_days', { count: Math.floor(hours / 24) });
}

export type UnitStatus = "alarm" | "offline" | "moving" | "idle" | "stopped";

export const MOVING_SPEED_KMH = 5;

export const UNIT_STATUS_META: Record<UnitStatus, { label: string; color: string; tone: "danger" | "muted" | "success" | "warning" | "neutral" }> = {
  alarm: { label: "Con alarma", color: "#f43f5e", tone: "danger" },
  moving: { label: "En movimiento", color: "#22c55e", tone: "success" },
  idle: { label: "En ralentí", color: "#f5b83d", tone: "warning" },
  stopped: { label: "Detenida", color: "#5aa9ff", tone: "neutral" },
  offline: { label: "Sin señal", color: "#64748b", tone: "muted" },
};

export function unitStatusLabel(status: UnitStatus): string {
  return i18next.t(`unit_status.${status}`, { defaultValue: UNIT_STATUS_META[status].label });
}

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
