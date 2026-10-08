import { useEffect, useState } from "react";
import { useParams, useSearchParams, Link } from "react-router-dom";
import { api, ApiError, type DeviceCommand, type DeviceCommandStatus, type DeviceCommandType } from "../lib/api";
import { Alert, Badge, Card, EmptyState, Field, PageContainer, PageHeader, Pagination, type BadgeTone } from "../components/ui";

// Full engine command history of ONE unit -- linked from "view full history" in
// DeviceDetailPanel.tsx. Paginated with a date filter. Same native date input
// pattern (<input type="date">, no library) as Reports.tsx, and the same
// Pagination component as the Administration tables.
const ENGINE_COMMAND_LABEL: Record<DeviceCommandType, string> = {
  engine_stop: "Cortar motor",
  engine_resume: "Reconectar motor",
};

const COMMAND_STATUS_LABEL: Record<DeviceCommandStatus, string> = {
  pending: "en curso",
  success: "éxito",
  failed: "falló",
  timeout: "sin respuesta",
  device_offline: "sin conexión",
};

const COMMAND_STATUS_TONE: Record<DeviceCommandStatus, BadgeTone> = {
  pending: "brand",
  success: "success",
  failed: "danger",
  timeout: "warning",
  device_offline: "muted",
};

const PAGE_LIMIT = 20;

// <input type="date"> yields a "timezone-less" date (YYYY-MM-DD) representing a
// day in the browser's LOCAL timezone -- it is resolved here to a real UTC
// instant (start/end of THAT local day) before sending it to the backend.
// Postgres runs in UTC; sending the raw date and letting the backend interpret
// it as UTC would make "search today" return commands up to several hours
// before/after the day the user perceives.
function startOfLocalDayIso(dateStr: string): string {
  return new Date(`${dateStr}T00:00:00`).toISOString();
}

function endOfLocalDayIso(dateStr: string): string {
  return new Date(`${dateStr}T23:59:59.999`).toISOString();
}

const dateInputClass =
  "block w-full rounded-sm border border-line-strong bg-surface-2 px-2.5 py-1.5 text-sm text-ink outline-none focus:border-brand-600 focus:ring-1 focus:ring-brand-600";

export default function DeviceCommandHistory() {
  const { deviceId } = useParams<{ deviceId: string }>();
  const [searchParams] = useSearchParams();
  const deviceLabel = searchParams.get("device_label");

  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<{ items: DeviceCommand[]; total: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!deviceId) return;
    api
      .listDeviceCommands(deviceId, {
        limit: PAGE_LIMIT,
        offset,
        dateFrom: dateFrom ? startOfLocalDayIso(dateFrom) : undefined,
        dateTo: dateTo ? endOfLocalDayIso(dateTo) : undefined,
      })
      .then(setPage)
      .catch((err) => setError(err instanceof ApiError ? err.message : "error cargando el historial"));
  }, [deviceId, offset, dateFrom, dateTo]);

  if (!deviceId) return null;

  return (
    <PageContainer>
      <PageHeader
        title="Historial de comandos"
        description={deviceLabel ? `Unidad: ${deviceLabel}` : "Comandos de motor enviados a esta unidad."}
      />

      <Card className="space-y-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label="Desde">
            <input
              type="date"
              value={dateFrom}
              onChange={(e) => {
                setOffset(0);
                setDateFrom(e.target.value);
              }}
              className={dateInputClass}
            />
          </Field>
          <Field label="Hasta">
            <input
              type="date"
              value={dateTo}
              onChange={(e) => {
                setOffset(0);
                setDateTo(e.target.value);
              }}
              className={dateInputClass}
            />
          </Field>
        </div>
        {(dateFrom || dateTo) && (
          <button
            type="button"
            className="text-xs text-brand-500 hover:underline"
            onClick={() => {
              setOffset(0);
              setDateFrom("");
              setDateTo("");
            }}
          >
            Quitar filtro de fecha
          </button>
        )}
      </Card>

      {error && <Alert>{error}</Alert>}

      <Card>
        {page == null ? (
          <p className="text-sm text-ink-faint">Cargando…</p>
        ) : page.items.length === 0 ? (
          <EmptyState>
            {dateFrom || dateTo ? "Sin comandos en ese rango de fechas." : "Sin comandos enviados todavía."}
          </EmptyState>
        ) : (
          <>
            <ul className="divide-y divide-line">
              {page.items.map((c) => (
                <li key={c.id} className="space-y-1 py-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium text-ink">{ENGINE_COMMAND_LABEL[c.command_type]}</span>
                    <Badge tone={COMMAND_STATUS_TONE[c.status]}>{COMMAND_STATUS_LABEL[c.status]}</Badge>
                  </div>
                  <p className="text-xs text-ink-dim">
                    Pedido por {c.requested_by_email} · {new Date(c.requested_at).toLocaleString()}
                  </p>
                  <p className="text-xs text-ink-dim">
                    {c.completed_at ? <>Resuelto: {new Date(c.completed_at).toLocaleString()}</> : <>Aún en curso, sin resolver</>}
                  </p>
                  <p className="text-xs text-ink-dim">
                    Respuesta del dispositivo:{" "}
                    {c.device_reply ? <span className="font-data text-ink">{c.device_reply}</span> : "(sin texto)"}
                  </p>
                </li>
              ))}
            </ul>
            <div className="mt-3">
              <Pagination total={page.total} limit={PAGE_LIMIT} offset={offset} onOffsetChange={setOffset} />
            </div>
          </>
        )}
      </Card>

      <Link to="/map" className="text-sm font-medium text-brand-700 hover:underline">
        ← Volver al mapa
      </Link>
    </PageContainer>
  );
}
