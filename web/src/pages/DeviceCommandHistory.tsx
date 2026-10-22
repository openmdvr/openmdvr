import { useEffect, useState } from "react";
import { useParams, useSearchParams, Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { api, ApiError, type DeviceCommand, type DeviceCommandStatus } from "../lib/api";
import { Alert, Badge, Card, EmptyState, Field, PageContainer, PageHeader, Pagination, type BadgeTone } from "../components/ui";

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
  const { t } = useTranslation();
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
        title={t("cmd_history.title")}
        description={deviceLabel ? `Unidad: ${deviceLabel}` : undefined}
      />

      <Card className="space-y-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label={t("common.from")}>
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
          <Field label={t("common.to")}>
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
            {t("cmd_history.clear_filter")}
          </button>
        )}
      </Card>

      {error && <Alert>{error}</Alert>}

      <Card>
        {page == null ? (
          <p className="text-sm text-ink-faint">{t("common.loading")}</p>
        ) : page.items.length === 0 ? (
          <EmptyState>
            {dateFrom || dateTo ? t("cmd_history.no_results_filtered") : t("cmd_history.no_results")}
          </EmptyState>
        ) : (
          <>
            <ul className="divide-y divide-line">
              {page.items.map((c) => (
                <li key={c.id} className="space-y-1 py-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium text-ink">{t(`cmd_history.cmd_${c.command_type}`)}</span>
                    <Badge tone={COMMAND_STATUS_TONE[c.status]}>{t(`cmd_history.status_${c.status === "device_offline" ? "offline" : c.status}`)}</Badge>
                  </div>
                  <p className="text-xs text-ink-dim">
                    {t("cmd_history.requested_by")} {c.requested_by_email} · {new Date(c.requested_at).toLocaleString()}
                  </p>
                  <p className="text-xs text-ink-dim">
                    {c.completed_at ? <>{t("cmd_history.resolved_at")} {new Date(c.completed_at).toLocaleString()}</> : <>{t("cmd_history.in_progress")}</>}
                  </p>
                  <p className="text-xs text-ink-dim">
                    {t("cmd_history.device_reply")}{" "}
                    {c.device_reply ? <span className="font-data text-ink">{c.device_reply}</span> : t("cmd_history.no_reply")}
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
        {t("cmd_history.back_to_map")}
      </Link>
    </PageContainer>
  );
}
