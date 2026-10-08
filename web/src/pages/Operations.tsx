import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import {
  api,
  ApiError,
  type Driver,
  type DriverShiftAlert,
  type DriverShiftStatus,
  type Route,
  type ShiftEventType,
  type Tenant,
  type Vehicle,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardTitle,
  EmptyState,
  Input,
  PageContainer,
  PageHeader,
  Select,
  type BadgeTone,
} from "../components/ui";
import { todayLocalIso } from "../lib/localDate";

// Live operations panel -- what a tenant admin needs to run day-to-day
// operations without checking table by table in Administration: who is on shift
// now, routes, and recent policy alerts. Everything reuses existing
// endpoints/RLS (the usual require_non_driver), no new security surface.
//
// Routes live HERE rather than in Administration: creating/assigning a route is
// day-to-day operational work, not configuration.

const EVENT_LABEL: Record<ShiftEventType, string> = {
  clock_in: "En turno",
  clock_out: "Fuera de turno",
  meal_start: "Comiendo",
  meal_end: "En turno",
};

const EVENT_TONE: Record<ShiftEventType, BadgeTone> = {
  clock_in: "success",
  clock_out: "muted",
  meal_start: "warning",
  meal_end: "success",
};

export default function Operations() {
  const { tenantId, isPlatform, role } = useAuth();
  // Creating/editing routes is require_tenant_admin in the backend (platform
  // bypass included). For a tenant_admin session the tenant is already fixed (no
  // ambiguity); for the platform this page has no single tenant --
  // RouteCreateForm gets its own picker (visible ONLY to the platform, see
  // tenants below) instead of hiding the capability entirely.
  const canManageRoutes = isPlatform || role === "tenant_admin";
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [drivers, setDrivers] = useState<Driver[]>([]);
  const [vehicles, setVehicles] = useState<Vehicle[]>([]);

  useEffect(() => {
    if (tenantId) {
      // tenant_admin session: a single tenant, no ambiguity -- no need for the
      // full tenant list.
      api.listDrivers({ tenant_id: tenantId, limit: 1000 }).then((r) => setDrivers(r.items)).catch(() => setDrivers([]));
      api.listVehicles({ tenant_id: tenantId, limit: 1000 }).then((r) => setVehicles(r.items)).catch(() => setVehicles([]));
      return;
    }
    if (isPlatform) {
      // Platform session without a fixed tenant: "on shift
      // now"/"routes"/"alerts" already mix data from ALL tenants unfiltered (RLS
      // bypass) -- full drivers/vehicles/tenants are loaded (same pragmatic 1000
      // ceiling as MapView/TenantWorkspace) so the form's tenant picker and the
      // table's "Tenant" column can be resolved client side.
      api.listTenants({ limit: 1000 }).then((r) => setTenants(r.items)).catch(() => setTenants([]));
      api.listDrivers({ limit: 1000 }).then((r) => setDrivers(r.items)).catch(() => setDrivers([]));
      api.listVehicles({ limit: 1000 }).then((r) => setVehicles(r.items)).catch(() => setVehicles([]));
    }
  }, [tenantId, isPlatform]);

  return (
    <PageContainer>
      <PageHeader
        title="Operación"
        description="Estado de turno de cada chofer, rutas y alertas recientes de política."
      />
      <div className="space-y-6">
        <ShiftStatusSection />
        <RoutesSection
          tenants={tenants}
          drivers={drivers}
          vehicles={vehicles}
          canManage={canManageRoutes}
          ownTenantId={tenantId}
        />
        <ShiftAlertsSection />
      </div>
    </PageContainer>
  );
}

function ShiftStatusSection() {
  const [rows, setRows] = useState<DriverShiftStatus[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listDriverShiftStatus()
      .then(setRows)
      .catch((err) => setError(err instanceof ApiError ? err.message : "error cargando estado de turno"));
  }, []);

  return (
    <Card>
      <CardTitle>En turno ahora</CardTitle>
      {error && <Alert>{error}</Alert>}
      {rows.length === 0 ? (
        <EmptyState>Sin choferes dados de alta todavía.</EmptyState>
      ) : (
        <ul className="divide-y divide-line">
          {rows.map((r) => (
            <li key={r.driver_id} className="flex items-center justify-between gap-4 py-2.5">
              <span className="text-sm font-medium text-ink">{r.driver_name}</span>
              <div className="text-right">
                <Badge tone={r.last_event_type ? EVENT_TONE[r.last_event_type] : "neutral"}>
                  {r.last_event_type ? EVENT_LABEL[r.last_event_type] : "Sin registrar"}
                </Badge>
                {r.last_event_at && (
                  <p className="mt-0.5 text-xs text-ink-faint">{new Date(r.last_event_at).toLocaleString()}</p>
                )}
                {/*
                 * Show on the map where the driver clocked in -- best-effort
                 * coordinates captured by DriverHome.tsx
                 * (navigator.geolocation). A historical point, not a live unit
                 * -- MapView.tsx shows it with its own icon
                 * (?lat=&lon=&label=).
                 */}
                {r.last_lat != null && r.last_lon != null && (
                  <Link
                    to={`/map?lat=${r.last_lat}&lon=${r.last_lon}&label=${encodeURIComponent(r.driver_name)}`}
                    className="mt-0.5 block text-xs text-brand-600 hover:underline"
                  >
                    Ver en el mapa
                  </Link>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function ShiftAlertsSection() {
  const [alerts, setAlerts] = useState<DriverShiftAlert[]>([]);
  const [onlyUnacknowledged, setOnlyUnacknowledged] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  async function reload() {
    try {
      setAlerts((await api.listDriverShiftAlerts({ unacknowledged_only: onlyUnacknowledged, limit: 100 })).items);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando alertas");
    }
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onlyUnacknowledged]);

  async function acknowledge(id: string) {
    setBusyId(id);
    try {
      await api.acknowledgeDriverShiftAlert(id);
      await reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error reconociendo alerta");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <Card>
      <CardTitle
        action={
          <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setOnlyUnacknowledged((v) => !v)}>
            {onlyUnacknowledged ? "Ver todas" : "Ver solo sin reconocer"}
          </Button>
        }
      >
        Alertas de choferes
      </CardTitle>
      {error && <Alert>{error}</Alert>}
      {alerts.length === 0 ? (
        <EmptyState>
          {onlyUnacknowledged ? "No hay alertas sin reconocer." : "Todavía no hay alertas registradas."}
        </EmptyState>
      ) : (
        <ul className="divide-y divide-line">
          {alerts.map((a) => (
            <li key={a.id} className="flex items-center justify-between gap-4 py-2.5">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <Badge tone="warning">
                    {a.alert_type === "meal_outside_window" ? "comida fuera de horario" : "turno excedido"}
                  </Badge>
                  <span className="truncate text-sm font-medium text-ink">{a.driver_name}</span>
                </div>
                <p className="mt-0.5 text-xs text-ink-dim">{new Date(a.occurred_at).toLocaleString()}</p>
              </div>
              {a.acknowledged_at ? (
                <Badge tone="muted">reconocida</Badge>
              ) : (
                <Button variant="secondary" disabled={busyId === a.id} onClick={() => acknowledge(a.id)}>
                  Reconocer
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Routes (driver + vehicle + day assignment, v1 without stops).
// ---------------------------------------------------------------------------

const ROUTE_STATUSES: Route["status"][] = ["planned", "in_progress", "completed", "cancelled"];

function RoutesSection({
  tenants,
  drivers,
  vehicles,
  canManage,
  ownTenantId,
}: {
  tenants: Tenant[];
  drivers: Driver[];
  vehicles: Vehicle[];
  canManage: boolean;
  ownTenantId: string | null;
}) {
  const [routes, setRoutes] = useState<Route[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  async function reload() {
    try {
      setRoutes((await api.listRoutes({ limit: 200, tenant_id: ownTenantId ?? undefined })).items);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando rutas");
    }
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ownTenantId]);

  const driverName = (id: string | null) => drivers.find((d) => d.id === id)?.name ?? "—";
  const tenantName = (id: string) => tenants.find((t) => t.id === id)?.name ?? "—";
  // "Tenant" column ONLY when there is no fixed tenant (platform session without
  // a picker) -- redundant for a real tenant_admin session, which already knows
  // whose rows they all are.
  const showTenantColumn = !ownTenantId;
  const columnCount = 5 + (showTenantColumn ? 1 : 0) + (canManage ? 1 : 0);

  return (
    <Card>
      <CardTitle
        action={
          canManage && (
            <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setShowCreate((v) => !v)}>
              {showCreate ? "Cancelar" : "+ Nueva ruta"}
            </Button>
          )
        }
      >
        Rutas
      </CardTitle>

      {showCreate && canManage && (
        <div className="mb-3">
          <RouteCreateForm
            tenants={tenants}
            drivers={drivers}
            vehicles={vehicles}
            ownTenantId={ownTenantId}
            onCreated={() => {
              setShowCreate(false);
              reload();
            }}
          />
        </div>
      )}

      {error && <Alert>{error}</Alert>}

      {routes.length === 0 ? (
        <EmptyState>Sin rutas registradas todavía.</EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                <th className="py-1.5 pr-2 font-medium">Fecha</th>
                <th className="py-1.5 pr-2 font-medium">Ruta</th>
                {showTenantColumn && <th className="py-1.5 pr-2 font-medium">Tenant</th>}
                <th className="py-1.5 pr-2 font-medium">Chofer</th>
                <th className="py-1.5 pr-2 font-medium">Vehículo</th>
                <th className="py-1.5 pr-2 font-medium">Estado</th>
                {canManage && <th className="py-1.5 font-medium" />}
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {routes.map((r) =>
                editingId === r.id ? (
                  <RouteEditRow
                    key={r.id}
                    route={r}
                    drivers={drivers}
                    vehicles={vehicles}
                    colSpan={columnCount}
                    onDone={() => setEditingId(null)}
                    onSaved={reload}
                  />
                ) : (
                  <tr key={r.id}>
                    <td className="font-data py-2 pr-2 text-xs text-ink-dim">{r.date}</td>
                    <td className="py-2 pr-2 text-ink">{r.name}</td>
                    {showTenantColumn && (
                      <td className="py-2 pr-2 text-xs text-ink-dim">{tenantName(r.tenant_id)}</td>
                    )}
                    <td className="py-2 pr-2 text-xs text-ink-dim">{r.driver_name ?? driverName(r.driver_id)}</td>
                    <td className="font-data py-2 pr-2 text-xs text-ink-dim">{r.vehicle_plate ?? "—"}</td>
                    <td className="py-2 pr-2">
                      <Badge tone={r.status === "completed" ? "success" : r.status === "cancelled" ? "muted" : "brand"}>
                        {r.status}
                      </Badge>
                    </td>
                    {canManage && (
                      <td className="py-2">
                        <Button variant="ghost" onClick={() => setEditingId(r.id)} className="px-2 py-1 text-xs">
                          Editar
                        </Button>
                      </td>
                    )}
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function RouteCreateForm({
  tenants,
  drivers,
  vehicles,
  ownTenantId,
  onCreated,
}: {
  tenants: Tenant[];
  drivers: Driver[];
  vehicles: Vehicle[];
  ownTenantId: string | null;
  onCreated: () => void;
}) {
  const [tenantId, setTenantId] = useState(ownTenantId ?? "");
  const [name, setName] = useState("");
  const [date, setDate] = useState(todayLocalIso());
  const [driverId, setDriverId] = useState("");
  const [vehicleId, setVehicleId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const tenantDrivers = drivers.filter((d) => d.tenant_id === tenantId);
  const tenantVehicles = vehicles.filter((v) => v.tenant_id === tenantId);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createRoute({
        tenant_id: tenantId,
        name,
        date,
        driver_id: driverId || undefined,
        vehicle_id: vehicleId || undefined,
      });
      setName("");
      setDriverId("");
      setVehicleId("");
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error creando ruta");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="grid grid-cols-2 gap-2 border border-line bg-surface-2 p-3 sm:grid-cols-5">
      {!ownTenantId && (
        <Select required value={tenantId} onChange={(e) => setTenantId(e.target.value)} className="col-span-2 sm:col-span-1">
          <option value="" disabled>
            Tenant...
          </option>
          {tenants.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </Select>
      )}
      <Input
        placeholder="Nombre de la ruta"
        required
        value={name}
        onChange={(e) => setName(e.target.value)}
        className="col-span-2 sm:col-span-1"
      />
      <Input type="date" required value={date} onChange={(e) => setDate(e.target.value)} />
      <Select value={driverId} onChange={(e) => setDriverId(e.target.value)}>
        <option value="">Sin chofer</option>
        {tenantDrivers.map((d) => (
          <option key={d.id} value={d.id}>
            {d.name}
          </option>
        ))}
      </Select>
      <Select value={vehicleId} onChange={(e) => setVehicleId(e.target.value)}>
        <option value="">Sin vehículo</option>
        {tenantVehicles.map((v) => (
          <option key={v.id} value={v.id}>
            {v.plate || v.id}
          </option>
        ))}
      </Select>
      <Button type="submit" disabled={busy} className="col-span-2 sm:col-span-5">
        Crear ruta
      </Button>
      {error && (
        <div className="col-span-2 sm:col-span-5">
          <Alert>{error}</Alert>
        </div>
      )}
    </form>
  );
}

function RouteEditRow({
  route,
  drivers,
  vehicles,
  colSpan,
  onDone,
  onSaved,
}: {
  route: Route;
  drivers: Driver[];
  vehicles: Vehicle[];
  colSpan: number;
  onDone: () => void;
  onSaved: () => void;
}) {
  const [driverId, setDriverId] = useState(route.driver_id ?? "");
  const [vehicleId, setVehicleId] = useState(route.vehicle_id ?? "");
  const [status, setStatus] = useState<Route["status"]>(route.status);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const tenantDrivers = drivers.filter((d) => d.tenant_id === route.tenant_id);
  const tenantVehicles = vehicles.filter((v) => v.tenant_id === route.tenant_id);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await api.updateRoute(route.id, {
        driver_id: driverId || undefined,
        vehicle_id: vehicleId || undefined,
        status,
      });
      onSaved();
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error guardando");
    } finally {
      setBusy(false);
    }
  }

  return (
    <tr>
      <td colSpan={colSpan} className="py-2">
        <div className="space-y-2 border border-brand-600/30 bg-surface-2 p-3">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <Select value={driverId} onChange={(e) => setDriverId(e.target.value)}>
              <option value="">Sin chofer</option>
              {tenantDrivers.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </Select>
            <Select value={vehicleId} onChange={(e) => setVehicleId(e.target.value)}>
              <option value="">Sin vehículo</option>
              {tenantVehicles.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.plate || v.id}
                </option>
              ))}
            </Select>
            <Select value={status} onChange={(e) => setStatus(e.target.value as Route["status"])}>
              {ROUTE_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </Select>
          </div>
          <div className="flex items-center gap-2">
            <Button disabled={busy} onClick={save} className="px-3 py-1 text-xs">
              Guardar
            </Button>
            <Button variant="secondary" onClick={onDone} className="px-3 py-1 text-xs">
              Cancelar
            </Button>
            {error && <span className="text-xs text-red-300">{error}</span>}
          </div>
        </div>
      </td>
    </tr>
  );
}
