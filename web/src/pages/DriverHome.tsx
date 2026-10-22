import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, ApiError, type Route, type ShiftEvent, type ShiftEventType } from "../lib/api";
import { useAuth } from "../lib/auth";
import { todayLocalIso } from "../lib/localDate";
import { Alert, Badge, Button, Card } from "../components/ui";

// How long a confirmation stays armed before auto-cancelling -- prevents an
// EARLIER accidental tap from staying "ready to confirm" if the driver comes
// back to the phone minutes later without meaning to touch anything.
const CONFIRM_TIMEOUT_MS = 8000;

function useDriverTranslations() {
  const { t } = useTranslation();
  const routeStatusLabel = (s: Route["status"]) =>
    t(`driver_home.route_${s === "in_progress" ? "in_progress" : s === "planned" ? "planned" : s === "completed" ? "completed" : "cancelled"}`);
  const eventLabel = (e: ShiftEventType) => t(`driver_home.event_${e}`);
  const nextActions = (lastEvent: ShiftEventType | null): { type: ShiftEventType; label: string }[] => {
    if (lastEvent === null || lastEvent === "clock_out")
      return [{ type: "clock_in", label: t("driver_home.action_clock_in") }];
    if (lastEvent === "meal_start")
      return [{ type: "meal_end", label: t("driver_home.action_meal_end") }];
    return [
      { type: "meal_start", label: t("driver_home.action_meal_start") },
      { type: "clock_out", label: t("driver_home.action_clock_out") },
    ];
  };
  return { t, routeStatusLabel, eventLabel, nextActions };
}

export default function DriverHome() {
  const { logout, tenantDisplayName, tenantLogoUrl } = useAuth();
  const { t, routeStatusLabel, eventLabel, nextActions } = useDriverTranslations();
  const [events, setEvents] = useState<ShiftEvent[]>([]);
  const [todayRoute, setTodayRoute] = useState<Route | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Accidental-tap protection: a click on a shift button arms a confirmation in
  // place instead of firing the event directly -- a native browser `confirm()`
  // is avoided on purpose (poor experience in a mobile PWA, and it can interfere
  // with the timing of the geolocation prompt that `clock()` triggers).
  const [pendingAction, setPendingAction] = useState<ShiftEventType | null>(null);
  const confirmTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (confirmTimeoutRef.current) clearTimeout(confirmTimeoutRef.current);
    };
  }, []);

  async function reload() {
    const today = todayLocalIso();
    try {
      const { items } = await api.listShiftEvents({ from: today, to: today, limit: 100 });
      setEvents(items); // already ORDER BY occurred_at DESC from the API
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando tus eventos de hoy");
    }
    try {
      const { items } = await api.listRoutes({ from: today, to: today, limit: 5 });
      setTodayRoute(items[0] ?? null);
    } catch {
      // No assigned route or a load error -- the view works the same without
      // this card; clocking in does not depend on having a route.
      setTodayRoute(null);
    }
  }

  useEffect(() => {
    reload();
  }, []);

  async function clock(eventType: ShiftEventType) {
    setBusy(true);
    setError(null);
    try {
      // Best-effort geolocation: if the browser denies it or it is unavailable,
      // the event is still recorded without coordinates -- it is not required to
      // clock in.
      const coords = await new Promise<{ lat: number; lon: number } | undefined>((resolve) => {
        if (!navigator.geolocation) return resolve(undefined);
        navigator.geolocation.getCurrentPosition(
          (pos) => resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude }),
          () => resolve(undefined),
          { timeout: 3000 },
        );
      });
      await api.clockShiftEvent(eventType, coords);
      await reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error registrando el evento");
    } finally {
      setBusy(false);
    }
  }

  function armConfirm(eventType: ShiftEventType) {
    setPendingAction(eventType);
    if (confirmTimeoutRef.current) clearTimeout(confirmTimeoutRef.current);
    confirmTimeoutRef.current = setTimeout(() => setPendingAction(null), CONFIRM_TIMEOUT_MS);
  }

  function cancelConfirm() {
    if (confirmTimeoutRef.current) clearTimeout(confirmTimeoutRef.current);
    setPendingAction(null);
  }

  async function confirmPendingAction() {
    const eventType = pendingAction;
    cancelConfirm();
    if (eventType) await clock(eventType);
  }

  const lastEvent = events[0]?.event_type ?? null;
  const actions = nextActions(lastEvent);
  const pending = actions.find((a) => a.type === pendingAction);

  return (
    // pt/pb-[calc(...)] add the notch/dynamic island and home indicator safe
    // areas to the normal p-4 (1rem) padding -- this screen does not live inside
    // Layout.tsx (standalone, no app header/tab bar), so it is itself the real
    // top/bottom edge.
    <div className="mx-auto flex min-h-screen max-w-md flex-col gap-4 px-4 pt-[calc(1rem+env(safe-area-inset-top))] pb-[calc(1rem+env(safe-area-inset-bottom))]">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          {/*
           * White label -- a driver is the user most likely to see this as
           * "the company app", not OpenMDVR; falls back to the default logo if
           * the tenant has not configured one (PATCH /tenants/{id}/settings).
           */}
          <img src={tenantLogoUrl ?? "/logo-mark.png"} alt="" className="h-6 w-auto shrink-0" />
          <div>
            {tenantDisplayName && <p className="text-xs text-ink-faint">{tenantDisplayName}</p>}
            <h1 className="text-lg leading-tight font-semibold text-ink">{t("driver_home.title")}</h1>
          </div>
        </div>
        <Button variant="ghost" onClick={logout} className="px-2 py-1 text-xs">
          {t("driver_home.logout")}
        </Button>
      </div>

      {error && <Alert>{error}</Alert>}

      {todayRoute && (
        <Card className="space-y-1">
          <div className="flex items-center justify-between">
            <p className="text-xs font-semibold tracking-wide text-ink-dim uppercase">{t("driver_home.route_today")}</p>
            <Badge tone={todayRoute.status === "completed" ? "success" : "brand"}>
              {routeStatusLabel(todayRoute.status)}
            </Badge>
          </div>
          <p className="text-base font-medium text-ink">{todayRoute.name}</p>
          {todayRoute.description && <p className="text-sm text-ink-dim">{todayRoute.description}</p>}
          {todayRoute.vehicle_plate && (
            <p className="font-data text-xs text-ink-faint">{t("driver_home.vehicle_prefix")} {todayRoute.vehicle_plate}</p>
          )}
        </Card>
      )}

      <Card className="space-y-3 text-center">
        <p className="text-xs tracking-wide text-ink-faint uppercase">{t("driver_home.current_status")}</p>
        <p className="text-2xl font-semibold text-ink">
          {lastEvent ? eventLabel(lastEvent) : t("driver_home.no_events_today")}
        </p>
        {events[0] && (
          <p className="font-data text-xs text-ink-faint">{new Date(events[0].occurred_at).toLocaleTimeString()}</p>
        )}
      </Card>

      <div className="flex flex-col gap-3">
        {pending ? (
          <Card className="space-y-3 text-center">
            <p className="text-sm text-ink-dim">{t("driver_home.confirm_action", { label: pending.label })}</p>
            <div className="flex gap-3">
              <Button disabled={busy} onClick={confirmPendingAction} className="flex-1 py-3 text-base">
                {t("driver_home.yes_confirm")}
              </Button>
              <Button variant="secondary" onClick={cancelConfirm} className="flex-1 py-3 text-base">
                {t("device_detail.cancel")}
              </Button>
            </div>
          </Card>
        ) : (
          actions.map((action) => (
            <Button
              key={action.type}
              disabled={busy}
              onClick={() => armConfirm(action.type)}
              variant={action.type === "clock_out" ? "secondary" : "primary"}
              className="py-4 text-base"
            >
              {action.label}
            </Button>
          ))
        )}
      </div>

      <Card>
        <p className="mb-2 text-xs font-semibold tracking-wide text-ink-dim uppercase">{t("driver_home.today_section")}</p>
        {events.length === 0 ? (
          <p className="text-sm text-ink-faint">{t("driver_home.no_events_yet")}</p>
        ) : (
          <ul className="space-y-1.5 text-sm">
            {events.map((e) => (
              <li key={e.id} className="flex items-center justify-between text-ink-dim">
                <span>{eventLabel(e.event_type)}</span>
                <span className="font-data text-xs text-ink-faint">
                  {new Date(e.occurred_at).toLocaleTimeString()}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
