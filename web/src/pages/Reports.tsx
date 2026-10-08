import { useEffect, useMemo, useState } from "react";
import {
  api,
  ApiError,
  type Device,
  type Driver,
  type DriverHoursReport,
  type Geofence,
  type GeofenceReport,
  type Vehicle,
  type VehicleDistanceReport,
  type VehicleEngineHoursReport,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import { Alert, Badge, Button, Card, EmptyState, Field, PageContainer, PageHeader, Select } from "../components/ui";
import { daysAgoLocalIso, startOfLocalDayIso, startOfNextLocalDayIso, todayLocalIso as todayIso } from "../lib/localDate";
import { formatDuration } from "../lib/geofences";

// Reports page -- distance driven and hours worked, both computed from data
// already captured (GPS positions, shift events). Date range with a native
// <input type="date"> instead of a date-picker library -- not worth a new
// dependency.
const daysAgoIso = daysAgoLocalIso;

// CSV generated and downloaded client side -- no backend PDF generation for now.
// This is not certified against any specific regulator's format (e.g. FMCSA/ELD
// in the US, Mexican transport regulations); CSV is something a
// customer/accountant can reprocess in the meantime.
//
// Two safeguards: (1) formula injection -- a unit/geofence label written as
// `=HYPERLINK(...)` or `@SUM(...)` would EXECUTE when the CSV is opened in
// Excel/Sheets; it is neutralized with a leading apostrophe (OWASP
// recommendation) without touching legitimate negative numbers (lat/lon); (2)
// without a UTF-8 BOM, Spanish-locale Excel opens the file as Latin-1 and breaks
// accents.
function csvCell(cell: string): string {
  const risky = /^[=+@\t\r]/.test(cell) || (cell.startsWith("-") && !/^-?\d+(\.\d+)?$/.test(cell));
  const safe = risky ? `'${cell}` : cell;
  return `"${safe.replace(/"/g, '""')}"`;
}

function downloadCsv(filename: string, rows: string[][]) {
  const csv = String.fromCharCode(0xfeff) + rows.map((r) => r.map(csvCell).join(",")).join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

const dateInputClass =
  "block w-full rounded-sm border border-line-strong bg-surface-2 px-2.5 py-1.5 text-sm text-ink outline-none focus:border-brand-600 focus:ring-1 focus:ring-brand-600";

export default function Reports() {
  return (
    <PageContainer>
      <PageHeader
        title="Reportes"
        description="Kilómetros recorridos, horas trabajadas y visitas a geocercas, calculados a partir de las posiciones GPS y los eventos ya capturados."
      />
      <RetentionInfoSection />
      <DistanceReportSection />
      <EngineHoursReportSection />
      <HoursReportSection />
      <GeofenceReportSection />
    </PageContainer>
  );
}

// Informational, read-only -- the retention window is a plan/billing attribute
// only support/platform edits (bypass-only PATCH /tenants/{id}, see
// Administration → Tenants), not self-service. A platform session has no tenant
// of "its own", so this card does not apply. GET /tenants already returns only
// the caller's row for a regular tenant session (RLS) -- no new endpoint needed.
function RetentionInfoSection() {
  const { isPlatform } = useAuth();
  const [days, setDays] = useState<number | null>(null);

  useEffect(() => {
    if (isPlatform) return;
    api
      .listTenants({ limit: 1 })
      .then(({ items }) => setDays(items[0]?.gps_retention_days ?? null))
      .catch(() => setDays(null));
  }, [isPlatform]);

  if (isPlatform || days == null) return null;

  return (
    <Card>
      <p className="text-sm text-ink-dim">
        Tus posiciones GPS se conservan durante los últimos{" "}
        <span className="font-data font-medium text-ink">{days} días</span>.
      </p>
    </Card>
  );
}

function DistanceReportSection() {
  const [vehicles, setVehicles] = useState<Vehicle[]>([]);
  const [vehicleId, setVehicleId] = useState("");
  const [dateFrom, setDateFrom] = useState(daysAgoIso(7));
  const [dateTo, setDateTo] = useState(todayIso());
  const [report, setReport] = useState<VehicleDistanceReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .listVehicles({ limit: 1000 })
      .then(({ items }) => {
        setVehicles(items);
        if (items.length > 0) setVehicleId(items[0].id);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "error cargando vehículos"));
  }, []);

  async function runReport() {
    if (!vehicleId) return;
    setBusy(true);
    setError(null);
    try {
      setReport(await api.vehicleDistanceReport(vehicleId, dateFrom, dateTo));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error generando el reporte");
      setReport(null);
    } finally {
      setBusy(false);
    }
  }

  function exportCsv() {
    if (!report) return;
    const vehicle = vehicles.find((v) => v.id === report.vehicle_id);
    const rows = [
      ["fecha", "km_recorridos", "posiciones_gps"],
      ...report.days.map((d) => [d.date, d.distance_km.toFixed(2), String(d.position_count)]),
      ["total", report.total_distance_km.toFixed(2), ""],
    ];
    downloadCsv(`km-recorridos-${vehicle?.plate ?? report.vehicle_id}-${report.date_from}_${report.date_to}.csv`, rows);
  }

  return (
    <>
      <Card className="space-y-3">
        <p className="text-xs font-semibold tracking-wide text-ink-dim uppercase">Kilómetros recorridos</p>
        {error && <Alert>{error}</Alert>}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
          <Field label="Vehículo">
            <Select value={vehicleId} onChange={(e) => setVehicleId(e.target.value)}>
              <option value="" disabled>
                Selecciona...
              </option>
              {vehicles.map((v) => (
                <option key={v.id} value={v.id}>
                  {[v.plate, v.make, v.model].filter(Boolean).join(" ") || v.id}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Desde">
            <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className={dateInputClass} />
          </Field>
          <Field label="Hasta">
            <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className={dateInputClass} />
          </Field>
          <div className="flex items-end">
            <Button disabled={busy || !vehicleId} onClick={runReport} className="w-full">
              {busy ? "Calculando..." : "Generar"}
            </Button>
          </div>
        </div>
        <p className="text-xs text-ink-faint">
          Máximo 31 días por consulta — el cálculo suma distancia entre puntos GPS consecutivos, no hay una tabla de
          resumen precalculada todavía.
        </p>
      </Card>

      {report && (
        <Card>
          <div className="mb-3 flex items-center justify-between">
            <p className="text-sm text-ink-dim">
              Total del período: <span className="font-data font-medium text-ink">{report.total_distance_km} km</span>
              {report.device_id == null && (
                <span className="ml-2 text-ink-faint">(sin dispositivo instalado en este vehículo)</span>
              )}
            </p>
            {report.days.length > 0 && (
              <Button variant="secondary" onClick={exportCsv} className="px-2 py-1 text-xs">
                Exportar CSV
              </Button>
            )}
          </div>

          {report.days.length === 0 ? (
            <EmptyState>Sin posiciones GPS en este rango de fechas.</EmptyState>
          ) : (
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                  <th className="py-1.5 pr-2 font-medium">Fecha</th>
                  <th className="py-1.5 pr-2 font-medium">Km recorridos</th>
                  <th className="py-1.5 font-medium">Posiciones GPS</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {report.days.map((d) => (
                  <tr key={d.date}>
                    <td className="font-data py-2 pr-2 text-ink">{d.date}</td>
                    <td className="font-data py-2 pr-2 text-ink-dim">{d.distance_km.toFixed(2)}</td>
                    <td className="font-data py-2 text-ink-faint">{d.position_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      )}
    </>
  );
}

// Driving hours / engine on while stopped (idling) / engine off. Crosses
// ignition_on (alarms) with GPS speed (gps_positions) -- see
// vehicles.py::vehicle_engine_hours_report. A 5 km/h threshold for "stopped"
// (real GPS almost never reports exactly 0 with the vehicle parked), and it only
// counts from when ignition started being recorded -- no attempt to reconstruct
// earlier history.
function EngineHoursReportSection() {
  const [vehicles, setVehicles] = useState<Vehicle[]>([]);
  const [vehicleId, setVehicleId] = useState("");
  const [dateFrom, setDateFrom] = useState(daysAgoIso(7));
  const [dateTo, setDateTo] = useState(todayIso());
  const [report, setReport] = useState<VehicleEngineHoursReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .listVehicles({ limit: 1000 })
      .then(({ items }) => {
        setVehicles(items);
        if (items.length > 0) setVehicleId(items[0].id);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "error cargando vehículos"));
  }, []);

  async function runReport() {
    if (!vehicleId) return;
    setBusy(true);
    setError(null);
    try {
      setReport(await api.vehicleEngineHoursReport(vehicleId, dateFrom, dateTo));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error generando el reporte");
      setReport(null);
    } finally {
      setBusy(false);
    }
  }

  function exportCsv() {
    if (!report) return;
    const vehicle = vehicles.find((v) => v.id === report.vehicle_id);
    const rows = [
      ["fecha", "horas_conduciendo", "horas_detenido_encendido", "horas_apagado"],
      ...report.days.map((d) => [d.date, d.driving_hours.toFixed(2), d.idle_hours.toFixed(2), d.engine_off_hours.toFixed(2)]),
      [
        "total",
        report.total_driving_hours.toFixed(2),
        report.total_idle_hours.toFixed(2),
        report.total_engine_off_hours.toFixed(2),
      ],
    ];
    downloadCsv(`horas-motor-${vehicle?.plate ?? report.vehicle_id}-${report.date_from}_${report.date_to}.csv`, rows);
  }

  return (
    <>
      <Card className="space-y-3">
        <p className="text-xs font-semibold tracking-wide text-ink-dim uppercase">Horas de conducción / motor</p>
        {error && <Alert>{error}</Alert>}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
          <Field label="Vehículo">
            <Select value={vehicleId} onChange={(e) => setVehicleId(e.target.value)}>
              <option value="" disabled>
                Selecciona...
              </option>
              {vehicles.map((v) => (
                <option key={v.id} value={v.id}>
                  {[v.plate, v.make, v.model].filter(Boolean).join(" ") || v.id}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Desde">
            <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className={dateInputClass} />
          </Field>
          <Field label="Hasta">
            <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className={dateInputClass} />
          </Field>
          <div className="flex items-end">
            <Button disabled={busy || !vehicleId} onClick={runReport} className="w-full">
              {busy ? "Calculando..." : "Generar"}
            </Button>
          </div>
        </div>
        <p className="text-xs text-ink-faint">
          Máximo 31 días por consulta. Cruza el estado de ignición con la velocidad GPS (menos de 5 km/h cuenta como
          detenido) — solo cuenta desde que este reporte empezó a registrar ignición (18/9/2026), días anteriores
          salen en cero.
        </p>
      </Card>

      {report && (
        <Card>
          <div className="mb-3 flex items-center justify-between">
            <p className="text-sm text-ink-dim">
              Total del período:{" "}
              <span className="font-data font-medium text-ink">{report.total_driving_hours} h conduciendo</span>
              {" · "}
              <span className="font-data font-medium text-ink">{report.total_idle_hours} h detenido</span>
              {" · "}
              <span className="font-data font-medium text-ink">{report.total_engine_off_hours} h apagado</span>
              {report.device_id == null && (
                <span className="ml-2 text-ink-faint">(sin dispositivo instalado en este vehículo)</span>
              )}
            </p>
            {report.days.length > 0 && (
              <Button variant="secondary" onClick={exportCsv} className="px-2 py-1 text-xs">
                Exportar CSV
              </Button>
            )}
          </div>

          {report.days.length === 0 ? (
            <EmptyState>Sin datos de ignición en este rango de fechas.</EmptyState>
          ) : (
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                  <th className="py-1.5 pr-2 font-medium">Fecha</th>
                  <th className="py-1.5 pr-2 font-medium">Conduciendo</th>
                  <th className="py-1.5 pr-2 font-medium">Detenido (encendido)</th>
                  <th className="py-1.5 font-medium">Apagado</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {report.days.map((d) => (
                  <tr key={d.date}>
                    <td className="font-data py-2 pr-2 text-ink">{d.date}</td>
                    <td className="font-data py-2 pr-2 text-ink-dim">{d.driving_hours.toFixed(2)}</td>
                    <td className="font-data py-2 pr-2 text-ink-dim">{d.idle_hours.toFixed(2)}</td>
                    <td className="font-data py-2 text-ink-faint">{d.engine_off_hours.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      )}
    </>
  );
}

function HoursReportSection() {
  const [drivers, setDrivers] = useState<Driver[]>([]);
  const [driverId, setDriverId] = useState("");
  const [dateFrom, setDateFrom] = useState(daysAgoIso(7));
  const [dateTo, setDateTo] = useState(todayIso());
  const [report, setReport] = useState<DriverHoursReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .listDrivers({ limit: 1000 })
      .then(({ items }) => {
        setDrivers(items);
        if (items.length > 0) setDriverId(items[0].id);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "error cargando choferes"));
  }, []);

  async function runReport() {
    if (!driverId) return;
    setBusy(true);
    setError(null);
    try {
      setReport(await api.driverHoursReport(driverId, dateFrom, dateTo));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error generando el reporte");
      setReport(null);
    } finally {
      setBusy(false);
    }
  }

  function exportCsv() {
    if (!report) return;
    const driver = drivers.find((d) => d.id === report.driver_id);
    const rows = [
      ["fecha", "horas_trabajadas", "turnos_completos"],
      ...report.days.map((d) => [d.date, d.hours_worked.toFixed(2), String(d.completed_shifts)]),
      ["total", report.total_hours.toFixed(2), ""],
    ];
    downloadCsv(`horas-trabajadas-${driver?.name ?? report.driver_id}-${report.date_from}_${report.date_to}.csv`, rows);
  }

  return (
    <>
      <Card className="space-y-3">
        <p className="text-xs font-semibold tracking-wide text-ink-dim uppercase">Horas trabajadas</p>
        {error && <Alert>{error}</Alert>}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
          <Field label="Chofer">
            <Select value={driverId} onChange={(e) => setDriverId(e.target.value)}>
              <option value="" disabled>
                Selecciona...
              </option>
              {drivers.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Desde">
            <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className={dateInputClass} />
          </Field>
          <Field label="Hasta">
            <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className={dateInputClass} />
          </Field>
          <div className="flex items-end">
            <Button disabled={busy || !driverId} onClick={runReport} className="w-full">
              {busy ? "Calculando..." : "Generar"}
            </Button>
          </div>
        </div>
        <p className="text-xs text-ink-faint">
          Máximo 31 días por consulta. Empareja entrada/salida (restando el tiempo de comida de en medio) por turno
          COMPLETO — un turno todavía abierto (sin salida registrada) no cuenta todavía. Esta es la base de datos
          para un reporte de horas, no un formato certificado para ningún regulador específico.
        </p>
      </Card>

      {report && (
        <Card>
          <div className="mb-3 flex items-center justify-between">
            <p className="text-sm text-ink-dim">
              Total del período: <span className="font-data font-medium text-ink">{report.total_hours} h</span>
            </p>
            {report.days.length > 0 && (
              <Button variant="secondary" onClick={exportCsv} className="px-2 py-1 text-xs">
                Exportar CSV
              </Button>
            )}
          </div>

          {report.days.length === 0 ? (
            <EmptyState>Sin turnos completos en este rango de fechas.</EmptyState>
          ) : (
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                  <th className="py-1.5 pr-2 font-medium">Fecha</th>
                  <th className="py-1.5 pr-2 font-medium">Horas trabajadas</th>
                  <th className="py-1.5 font-medium">Turnos completos</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {report.days.map((d) => (
                  <tr key={d.date}>
                    <td className="font-data py-2 pr-2 text-ink">{d.date}</td>
                    <td className="font-data py-2 pr-2 text-ink-dim">{d.hours_worked.toFixed(2)}</td>
                    <td className="font-data py-2 text-ink-faint">{d.completed_shifts}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      )}
    </>
  );
}

// Geofence visits (0052_geofences.sql) -- entries, exits, dwells and time inside
// per geofence, plus each visit's detail. Protocol-agnostic: comes from
// geofence_events, filled for any device that reports GPS. 93-day cap per query
// (enforced by the backend), and an operator only sees visits of THEIR assigned
// units (RLS with app_can_view_device).
const VISITS_PAGE = 50;

function GeofenceReportSection() {
  const [geofences, setGeofences] = useState<Geofence[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [geofenceId, setGeofenceId] = useState("");
  const [deviceId, setDeviceId] = useState("");
  const [dateFrom, setDateFrom] = useState(daysAgoIso(7));
  const [dateTo, setDateTo] = useState(todayIso());
  const [report, setReport] = useState<GeofenceReport | null>(null);
  const [visibleVisits, setVisibleVisits] = useState(VISITS_PAGE);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.listGeofences({ limit: 500 }).then(({ items }) => setGeofences(items)).catch(() => {});
    api.listDevices({ limit: 1000 }).then(({ items }) => setDevices(items)).catch(() => {});
  }, []);

  const totals = useMemo(() => {
    if (!report) return null;
    return report.geofences.reduce(
      (acc, r) => ({ enters: acc.enters + r.enters, exits: acc.exits + r.exits, inside: acc.inside + r.total_inside_s }),
      { enters: 0, exits: 0, inside: 0 },
    );
  }, [report]);

  async function runReport() {
    setBusy(true);
    setError(null);
    try {
      setReport(
        await api.geofenceReport({
          from: startOfLocalDayIso(dateFrom),
          to: startOfNextLocalDayIso(dateTo),
          geofenceId: geofenceId || undefined,
          deviceId: deviceId || undefined,
        }),
      );
      setVisibleVisits(VISITS_PAGE);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error generando el reporte");
      setReport(null);
    } finally {
      setBusy(false);
    }
  }

  function exportCsv() {
    if (!report) return;
    const rows = [
      ["geocerca", "unidad", "entrada", "salida", "duracion_min", "entrada_estimada", "sigue_adentro"],
      ...report.visits.map((v) => [
        v.geofence_name,
        v.device_label,
        v.entered_at ? new Date(v.entered_at).toLocaleString() : "",
        v.exited_at ? new Date(v.exited_at).toLocaleString() : "",
        v.duration_s != null ? (v.duration_s / 60).toFixed(1) : "",
        v.entry_estimated ? "si" : "no",
        v.open ? "si" : "no",
      ]),
    ];
    downloadCsv(`visitas-geocercas-${dateFrom}_${dateTo}.csv`, rows);
  }

  return (
    <>
      <Card className="space-y-3">
        <p className="text-xs font-semibold tracking-wide text-ink-dim uppercase">Visitas a geocercas</p>
        {error && <Alert>{error}</Alert>}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Field label="Geocerca">
            <Select value={geofenceId} onChange={(e) => setGeofenceId(e.target.value)}>
              <option value="">Todas</option>
              {geofences.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Unidad">
            <Select value={deviceId} onChange={(e) => setDeviceId(e.target.value)}>
              <option value="">Todas</option>
              {devices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.label}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Desde">
            <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className={dateInputClass} />
          </Field>
          <Field label="Hasta">
            <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className={dateInputClass} />
          </Field>
          <div className="flex items-end">
            <Button disabled={busy} onClick={runReport} className="w-full">
              {busy ? "Calculando..." : "Generar"}
            </Button>
          </div>
        </div>
        <p className="text-xs text-ink-dim">Máximo 93 días por consulta.</p>
      </Card>

      {report && totals && (
        <Card className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="grid grid-cols-3 gap-4 text-sm">
              <div>
                <p className="text-xs text-ink-dim">Entradas</p>
                <p className="font-data text-lg font-semibold text-ink">{totals.enters}</p>
              </div>
              <div>
                <p className="text-xs text-ink-dim">Salidas</p>
                <p className="font-data text-lg font-semibold text-ink">{totals.exits}</p>
              </div>
              <div>
                <p className="text-xs text-ink-dim">Tiempo adentro</p>
                <p className="font-data text-lg font-semibold text-ink">{formatDuration(totals.inside)}</p>
              </div>
            </div>
            {report.visits.length > 0 && (
              <Button variant="secondary" onClick={exportCsv} className="px-2 py-1 text-xs">
                Exportar CSV
              </Button>
            )}
          </div>

          {report.geofences.length === 0 && report.visits.length === 0 ? (
            <EmptyState>Sin eventos de geocerca en este rango.</EmptyState>
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[520px] text-left text-sm">
                  <thead>
                    <tr className="border-b border-line text-xs tracking-wide text-ink-dim uppercase">
                      <th className="py-1.5 pr-2 font-medium">Geocerca</th>
                      <th className="py-1.5 pr-2 font-medium">Entradas</th>
                      <th className="py-1.5 pr-2 font-medium">Salidas</th>
                      <th className="py-1.5 pr-2 font-medium">Unidades</th>
                      <th className="py-1.5 pr-2 font-medium">Tiempo adentro</th>
                      <th className="py-1.5 font-medium">Visita promedio</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line">
                    {report.geofences.map((r) => (
                      <tr key={r.geofence_id ?? r.geofence_name}>
                        <td className="py-2 pr-2 text-ink">
                          {r.geofence_name}
                          {r.geofence_id == null && <span className="ml-1 text-xs text-ink-dim">(borrada)</span>}
                        </td>
                        <td className="font-data py-2 pr-2 text-ink-dim">{r.enters}</td>
                        <td className="font-data py-2 pr-2 text-ink-dim">{r.exits}</td>
                        <td className="font-data py-2 pr-2 text-ink-dim">{r.unique_devices}</td>
                        <td className="font-data py-2 pr-2 text-ink-dim">{formatDuration(r.total_inside_s)}</td>
                        <td className="font-data py-2 text-ink-dim">{formatDuration(r.avg_visit_s)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="space-y-2">
                <p className="text-xs font-semibold tracking-wide text-ink-dim uppercase">Visitas ({report.visits.length})</p>
                {report.visits_truncated && (
                  <Alert variant="info">Hay más visitas de las que se muestran -- acota el rango o filtra por geocerca/unidad.</Alert>
                )}
                <ul className="divide-y divide-line">
                  {report.visits.slice(0, visibleVisits).map((v, i) => (
                    <li
                      key={`${v.device_id}-${v.geofence_id}-${v.entered_at}-${i}`}
                      className="flex flex-wrap items-center justify-between gap-2 py-2"
                    >
                      <div className="min-w-0">
                        <p className="truncate text-sm text-ink">
                          <span className="font-medium">{v.device_label}</span>
                          <span className="text-ink-dim"> · {v.geofence_name}</span>
                        </p>
                        <p className="text-xs text-ink-dim">
                          {v.entered_at ? new Date(v.entered_at).toLocaleString() : "—"}
                          {" → "}
                          {v.exited_at ? new Date(v.exited_at).toLocaleString() : "sigue adentro"}
                        </p>
                      </div>
                      <span className="flex items-center gap-2">
                        {v.open && <Badge tone="success">adentro</Badge>}
                        <span className="font-data text-sm text-ink">
                          {v.entry_estimated ? "≥ " : ""}
                          {formatDuration(v.duration_s)}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
                {report.visits.length > visibleVisits && (
                  <Button variant="ghost" className="w-full" onClick={() => setVisibleVisits((n) => n + VISITS_PAGE)}>
                    Ver más ({report.visits.length - visibleVisits} restantes)
                  </Button>
                )}
                {report.visits.some((v) => v.entry_estimated) && (
                  <p className="text-xs text-ink-dim">
                    “≥” = la unidad ya estaba adentro cuando se creó o redibujó la geocerca; la duración real es mayor.
                  </p>
                )}
              </div>
            </>
          )}
        </Card>
      )}
    </>
  );
}
