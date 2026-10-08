import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useNotifications } from "../lib/useNotifications";
import { useAuth } from "../lib/auth";
import { DeviceHealthPanel } from "../components/DeviceHealthPanel";
import { api, ApiError, type AlarmSeverity, type AlarmVideoClip, type DeviceProtocol, type Notification } from "../lib/api";
import { Alert, Button, Card, EmptyState, PageContainer, PageHeader } from "../components/ui";
import { AlarmClipPlayer, CLIP_STATUS_LABEL, alarmCanHaveClip } from "../components/AlarmClipPlayer";
import { alarmTypeFromNotificationTitle, notificationTitleLabel } from "../lib/alarmLabels";

// In-app mailbox -- a regular page with a nav badge, NOT a dropdown/floating
// menu (the project avoids that class of overlay on purpose). This is the ONLY
// surface for viewing alarms -- "mark read" also acknowledges the underlying
// alarm on the backend (see notifications.py), one button does both. Severity
// labels are localized instead of showing the raw value ("warning").
const SEVERITY_LABEL: Record<AlarmSeverity, string> = { critical: "Crítica", warning: "Advertencia", info: "Info" };
const SEVERITY_COLOR: Record<AlarmSeverity, string> = { critical: "#f43f5e", warning: "#f5b83d", info: "#2f93ff" };

type ListFilter = "all" | "unread" | "critical";

function relativeTime(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return "recién";
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `hace ${hours} h`;
  const days = Math.floor(hours / 24);
  return `hace ${days} d`;
}

export default function Notifications() {
  const { notifications, unreadCount, connectionState, markRead } = useNotifications();
  const { isPlatform } = useAuth();
  const navigate = useNavigate();

  // ?device_id= -- target of the "view all" link in DeviceDetailPanel.tsx's
  // alarm preview. When filtered, this screen stops depending on the global
  // Context (a sliding window of the latest 30, without backend filtering) and
  // runs its own paginated query.
  const [searchParams, setSearchParams] = useSearchParams();
  const deviceFilterId = searchParams.get("device_id");
  const deviceFilterLabel = searchParams.get("device_label");
  const [filteredItems, setFilteredItems] = useState<Notification[] | null>(null);
  const [filteredError, setFilteredError] = useState<string | null>(null);

  useEffect(() => {
    if (!deviceFilterId) {
      setFilteredItems(null);
      return;
    }
    let cancelled = false;
    setFilteredError(null);
    api
      .listNotifications({ deviceId: deviceFilterId, limit: 100 })
      .then((page) => {
        if (!cancelled) setFilteredItems(page.items);
      })
      .catch((err) => {
        if (!cancelled) setFilteredError(err instanceof ApiError ? err.message : "error cargando notificaciones");
      });
    return () => {
      cancelled = true;
    };
  }, [deviceFilterId]);

  // The filtered view must also receive new alerts without a reload. Unlike the
  // unfiltered view (where `notifications` comes from the SSE-connected
  // Context), `filteredItems` is a one-off REST fetch. Instead of opening a
  // second redundant EventSource, reuse the existing SSE connection: the Context
  // receives ALL of the user's new notifications, and any one matching
  // deviceFilterId is merged here too.
  useEffect(() => {
    if (!deviceFilterId) return;
    const matching = notifications.filter((n) => n.device_id === deviceFilterId);
    if (matching.length === 0) return;
    setFilteredItems((prev) => {
      if (!prev) return prev;
      const byId = new Map(prev.map((n) => [n.id, n]));
      for (const n of matching) byId.set(n.id, n);
      return Array.from(byId.values()).sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
    });
  }, [notifications, deviceFilterId]);

  const [listFilter, setListFilter] = useState<ListFilter>("all");
  const allItems = deviceFilterId ? (filteredItems ?? []) : notifications;
  const items = allItems.filter((n) => (listFilter === "unread" ? !n.read_at : listFilter === "critical" ? n.severity === "critical" : true));
  const unreadDisplay = deviceFilterId ? allItems.filter((n) => !n.read_at).length : unreadCount;

  async function handleMarkRead(id: string) {
    await markRead(id);
    if (deviceFilterId) {
      setFilteredItems((prev) =>
        prev ? prev.map((n) => (n.id === id ? { ...n, read_at: n.read_at ?? new Date().toISOString() } : n)) : prev
      );
    }
  }

  // Video clip retrieval: this page (not only DeviceDetailPanel.tsx's preview)
  // is the main surface where alarms are reviewed, so "request clip" must be
  // offered here too. deviceProtocolById resolves which notifications belong to
  // a device with a camera -- limit:1000, same pragmatic ceiling as
  // MapView/LiveView; this page does not have the full Device at hand the way
  // DeviceDetailPanel does.
  const [deviceProtocolById, setDeviceProtocolById] = useState<Record<string, DeviceProtocol>>({});
  const [clips, setClips] = useState<Record<string, AlarmVideoClip>>({});
  const [clipBusyId, setClipBusyId] = useState<string | null>(null);
  const [openClipId, setOpenClipId] = useState<string | null>(null);
  const [clipError, setClipError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listDevices({ limit: 1000 })
      .then(({ items: devices }) => {
        setDeviceProtocolById(Object.fromEntries(devices.map((d) => [d.id, d.protocol])));
      })
      .catch(() => {
        // Silent on purpose: without this, the "request clip" button simply does
        // not appear (fail-closed) -- a transient error must not cover the rest
        // of the page with an intrusive message.
      });
  }, []);

  // When the visible list changes, resolve the real state of each clip we do not
  // know yet -- same pattern as DeviceAlarmsPreview.
  useEffect(() => {
    const pending = items.filter(
      (n) =>
        n.alarm_id &&
        n.device_id &&
        alarmCanHaveClip(deviceProtocolById[n.device_id], alarmTypeFromNotificationTitle(n.title)) &&
        !(n.alarm_id in clips)
    );
    if (pending.length === 0) return;
    let cancelled = false;
    Promise.all(pending.map(async (n) => [n.alarm_id as string, await api.getAlarmClip(n.alarm_id as string)] as const)).then(
      (fetched) => {
        if (cancelled) return;
        setClips((prev) => {
          const next = { ...prev };
          for (const [alarmId, clip] of fetched) if (clip) next[alarmId] = clip;
          return next;
        });
      }
    );
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, deviceProtocolById]);

  // Short polling ONLY while some requested clip is still in progress -- same as
  // DeviceAlarmsPreview (never runs if nothing was requested).
  useEffect(() => {
    const pending = Object.values(clips).filter((c) => c.status === "requested" || c.status === "uploading");
    if (pending.length === 0) return;
    const t = setInterval(async () => {
      const updated = await Promise.all(pending.map(async (c) => [c.alarm_id, await api.getAlarmClip(c.alarm_id)] as const));
      setClips((prev) => {
        const next = { ...prev };
        for (const [alarmId, clip] of updated) if (clip) next[alarmId] = clip;
        return next;
      });
    }, 4000);
    return () => clearInterval(t);
  }, [clips]);

  async function requestClip(alarmId: string) {
    setClipBusyId(alarmId);
    try {
      const clip = await api.requestAlarmClip(alarmId);
      setClips((prev) => ({ ...prev, [alarmId]: clip }));
      setClipError(null);
    } catch (err) {
      setClipError(err instanceof ApiError ? err.message : "error pidiendo el clip");
    } finally {
      setClipBusyId(null);
    }
  }

  return (
    <PageContainer>
      <PageHeader
        title="Notificaciones"
        description={
          deviceFilterId
            ? `Solo ${deviceFilterLabel ?? "esta unidad"} — ${unreadDisplay} sin leer`
            : connectionState === "open"
              ? `${unreadDisplay} sin leer`
              : connectionState === "reconnecting"
                ? "Reconectando..."
                : "Conectando..."
        }
        action={
          deviceFilterId ? (
            <Button variant="secondary" onClick={() => setSearchParams({})}>
              Quitar filtro de unidad
            </Button>
          ) : undefined
        }
      />

      {filteredError && <Alert>{filteredError}</Alert>}
      {clipError && <Alert>{clipError}</Alert>}

      <div className="flex gap-1.5">
        {(
          [
            ["all", "Todas"],
            ["unread", `Sin leer${unreadDisplay ? ` · ${unreadDisplay}` : ""}`],
            ["critical", "Críticas"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setListFilter(key)}
            className={`rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
              listFilter === key ? "border-brand-500/60 bg-brand-600/15 text-ink" : "border-line-strong text-ink-dim hover:text-ink"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Platform: operational device problems (compact, deduplicated). */}
      {isPlatform && !deviceFilterId && <DeviceHealthPanel />}

      <Card className="p-2 sm:p-2">
        {items.length === 0 ? (
          <EmptyState>{listFilter === "all" ? "Sin notificaciones todavía." : "Nada en este filtro."}</EmptyState>
        ) : (
          <ul className="space-y-1">
            {items.map((n) => {
              const clip = n.alarm_id ? clips[n.alarm_id] : undefined;
              const clipsSupported = !!n.device_id && alarmCanHaveClip(deviceProtocolById[n.device_id], alarmTypeFromNotificationTitle(n.title));
              const color = n.severity ? SEVERITY_COLOR[n.severity] : "#7e8796";
              return (
                <li key={n.id} className={`rounded-2xl p-3 transition-colors ${n.read_at ? "" : "bg-fg/[0.035]"}`}>
                  <div className="flex gap-3">
                    <span
                      className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl"
                      style={{ background: `${color}22`, color }}
                      aria-hidden
                    >
                      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9">
                        <path d="M12 9v4M12 17h.01" strokeLinecap="round" />
                        <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" strokeLinejoin="round" />
                      </svg>
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-start justify-between gap-2">
                        <p className={`text-sm leading-snug ${n.read_at ? "font-medium text-ink-dim" : "font-semibold text-ink"}`}>
                          {notificationTitleLabel(n.title)}
                        </p>
                        {!n.read_at && <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-brand-500" aria-label="sin leer" />}
                      </div>
                      {n.body && <p className="mt-0.5 text-sm text-ink-dim">{n.body}</p>}
                      <p className="mt-1 text-xs text-ink-faint">
                        {n.severity && <span style={{ color }}>{SEVERITY_LABEL[n.severity]}</span>}
                        {n.severity && " · "}
                        {relativeTime(n.created_at)}
                      </p>

                      <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                        {n.device_id && (
                          <Button
                            variant="secondary"
                            className="px-3 py-1.5 text-xs"
                            onClick={() => {
                              if (!n.read_at) handleMarkRead(n.id);
                              navigate(`/map?device=${n.device_id}`);
                            }}
                          >
                            Ver en el mapa
                          </Button>
                        )}
                        {!n.read_at && (
                          <Button variant="ghost" className="px-3 py-1.5 text-xs" onClick={() => handleMarkRead(n.id)}>
                            Marcar leída
                          </Button>
                        )}
                        {clipsSupported && n.alarm_id && (!clip || clip.status === "failed") && (
                          // Offer a retry on 'failed' (the button must not
                          // disappear forever after a failure); 'unsupported'
                          // offers no retry (device protocol limitation).
                          <Button
                            variant="ghost"
                            className="px-3 py-1.5 text-xs"
                            disabled={clipBusyId === n.alarm_id}
                            onClick={() => requestClip(n.alarm_id!)}
                          >
                            {clip ? "Reintentar clip" : "Pedir clip (~1 min)"}
                          </Button>
                        )}
                        {clip && clip.status === "ready" && clip.url && (
                          <Button
                            variant="ghost"
                            className="px-3 py-1.5 text-xs"
                            onClick={() => setOpenClipId((cur) => (cur === n.alarm_id ? null : n.alarm_id!))}
                          >
                            {openClipId === n.alarm_id ? "Ocultar clip" : "Ver clip"}
                          </Button>
                        )}
                        {clip && (clip.status === "requested" || clip.status === "uploading") && (
                          <span className="text-xs text-ink-faint">{CLIP_STATUS_LABEL[clip.status]}</span>
                        )}
                        {clip && clip.status === "ready" && !clip.url && <span className="text-xs text-ink-faint">Este clip ya no está disponible</span>}
                      </div>
                      {clip && (clip.status === "failed" || clip.status === "unsupported") && (
                        <p className="mt-1.5 text-xs text-rose-300">{clip.error_detail ?? CLIP_STATUS_LABEL[clip.status]}</p>
                      )}
                      {clip && clip.status === "ready" && clip.url && openClipId === n.alarm_id && (
                        <div className="mt-2.5">
                          <AlarmClipPlayer url={clip.url} secondaryUrl={clip.url_secondary} />
                        </div>
                      )}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </PageContainer>
  );
}
