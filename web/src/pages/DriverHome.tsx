import { useEffect, useRef, useState } from "react";
import { api, ApiError, type Route, type ShiftEvent, type ShiftEventType } from "../lib/api";
import { useAuth } from "../lib/auth";
import { todayLocalIso } from "../lib/localDate";
import { Alert, Badge, Button, Card } from "../components/ui";

// Driver view -- mobile-first from the start (not a retrofit of the desktop
// dashboard): large clock-in/out/meal buttons + the day's assigned route. GET
// /shifts and GET /routes without an explicit driver_id already return ONLY the
// driver's own data -- RLS (driver_shift_events_select/routes_select, migrations
// 0015/0016) filters it server side before this component receives anything; it
// is not a UI filter that could be bypassed.
const ROUTE_STATUS_LABEL: Record<Route["status"], string> = {
  planned: "Planeada",
  in_progress: "En curso",
  completed: "Completada",
  cancelled: "Cancelada",
};
const EVENT_LABEL: Record<ShiftEventType, string> = {
  clock_in: "Entrada",
  clock_out: "Salida",
  meal_start: "Salida a comer",
  meal_end: "Regreso de comer",
};

function nextActions(lastEvent: ShiftEventType | null): { type: ShiftEventType; label: string }[] {
  if (lastEvent === null || lastEvent === "clock_out") {
    return [{ type: "clock_in", label: "Iniciar turno" }];
  }
  if (lastEvent === "meal_start") {
    return [{ type: "meal_end", label: "Volver de comer" }];
  }
  // clock_in or meal_end: on shift, can go to lunch or finish.
  return [
    { type: "meal_start", label: "Salir a comer" },
    { type: "clock_out", label: "Terminar turno" },
  ];
}

// How long a confirmation stays armed before auto-cancelling -- prevents an
// EARLIER accidental tap from staying "ready to confirm" if the driver comes
// back to the phone minutes later without meaning to touch anything.
const CONFIRM_TIMEOUT_MS = 8000;

export default function DriverHome() {
  const { logout, tenantDisplayName, tenantLogoUrl } = useAuth();
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
            <h1 className="text-lg leading-tight font-semibold text-ink">Mi turno</h1>
          </div>
        </div>
        <Button variant="ghost" onClick={logout} className="px-2 py-1 text-xs">
          Salir
        </Button>
      </div>

      {error && <Alert>{error}</Alert>}

      {todayRoute && (
        <Card className="space-y-1">
          <div className="flex items-center justify-between">
            <p className="text-xs font-semibold tracking-wide text-ink-dim uppercase">Ruta de hoy</p>
            <Badge tone={todayRoute.status === "completed" ? "success" : "brand"}>
              {ROUTE_STATUS_LABEL[todayRoute.status]}
            </Badge>
          </div>
          <p className="text-base font-medium text-ink">{todayRoute.name}</p>
          {todayRoute.description && <p className="text-sm text-ink-dim">{todayRoute.description}</p>}
          {todayRoute.vehicle_plate && (
            <p className="font-data text-xs text-ink-faint">Vehículo: {todayRoute.vehicle_plate}</p>
          )}
        </Card>
      )}

      <Card className="space-y-3 text-center">
        <p className="text-xs tracking-wide text-ink-faint uppercase">Estado actual</p>
        <p className="text-2xl font-semibold text-ink">
          {lastEvent ? EVENT_LABEL[lastEvent] : "Sin registrar hoy"}
        </p>
        {events[0] && (
          <p className="font-data text-xs text-ink-faint">{new Date(events[0].occurred_at).toLocaleTimeString()}</p>
        )}
      </Card>

      <div className="flex flex-col gap-3">
        {pending ? (
          <Card className="space-y-3 text-center">
            <p className="text-sm text-ink-dim">¿Confirmar “{pending.label}”?</p>
            <div className="flex gap-3">
              <Button disabled={busy} onClick={confirmPendingAction} className="flex-1 py-3 text-base">
                Sí, confirmar
              </Button>
              <Button variant="secondary" onClick={cancelConfirm} className="flex-1 py-3 text-base">
                Cancelar
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
        <p className="mb-2 text-xs font-semibold tracking-wide text-ink-dim uppercase">Hoy</p>
        {events.length === 0 ? (
          <p className="text-sm text-ink-faint">Sin eventos registrados todavía.</p>
        ) : (
          <ul className="space-y-1.5 text-sm">
            {events.map((e) => (
              <li key={e.id} className="flex items-center justify-between text-ink-dim">
                <span>{EVENT_LABEL[e.event_type]}</span>
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
