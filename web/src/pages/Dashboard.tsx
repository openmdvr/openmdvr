import { Fragment, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  api,
  ApiError,
  type ApiKey,
  type ApiKeyCreated,
  type BillingPeriod,
  type Device,
  type DeviceGroup,
  type DeviceModel,
  type DeviceProtocol,
  type Driver,
  type MapProvider,
  type PageResult,
  type Tenant,
  type TenantRole,
  type User,
  type UserDeviceAssignments,
  type UserNotificationSettings,
  usesGT06Imei,
  type Vehicle,
  type WebhookDelivery,
  type WebhookTestResult,
  type WebhookEndpoint,
  type WebhookEndpointCreated,
} from "../lib/api";

// Readable label per protocol -- gt06_video (JIMI JC261/JC400) is GT06 with a
// camera, distinct from both GPS-only GT06 and JT808.
const PROTOCOL_LABEL: Record<DeviceProtocol, string> = {
  jt808: "JT808 (cámara)",
  gt06: "GT06 (GPS)",
  gt06_video: "GT06 + video (JC261/JC400)",
};
import { isDeviceRecent, lastSeenLabel, livenessTooltip, useDeviceOfflineThreshold } from "../lib/deviceStatus";
import { AUTO_FAILOVER_ORDER, MAP_PROVIDERS } from "../lib/mapProviders";
import { useAuth } from "../lib/auth";
import {
  Alert,
  Badge,
  type BadgeTone,
  Button,
  Card,
  CardTitle,
  EmptyState,
  Field,
  Input,
  PageContainer,
  PageHeader,
  Pagination,
  Select,
  Tooltip,
} from "../components/ui";

// Fixed, small page size on purpose -- an admin table for reviewing/searching,
// not a full report. Prev/next (Pagination in ui.tsx) is enough at this scale.
const PAGE_LIMIT = 10;

// Hook shared by the three sections (tenants/users/devices): debounced search
// (300ms, avoids one request per keystroke) + offset + reload. Each section
// passes its own fetch function -- the pagination/search logic is identical,
// only what is requested changes.
function usePaginatedResource<T>(
  fetchPage: (params: { search: string; limit: number; offset: number }) => Promise<PageResult<T>>,
) {
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<PageResult<T> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const t = setTimeout(() => {
      setSearch(searchInput);
      setOffset(0);
    }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const reload = useCallback(async () => {
    try {
      setPage(await fetchPage({ search, limit: PAGE_LIMIT, offset }));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando datos");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, offset]);

  useEffect(() => {
    reload();
  }, [reload]);

  return { searchInput, setSearchInput, offset, setOffset, page, error, reload };
}

export default function Dashboard() {
  // Only PLATFORM sessions (super_admin/support) see this page -- AdminRoot in
  // App.tsx sends a tenant_admin straight to TenantWorkspace pinned to its own
  // tenant. That is why users/vehicles/drivers/devices of ALL tenants are not
  // mixed here: Administration is just the Tenants table, and managing one
  // tenant's fleet means entering its workspace.
  const navigate = useNavigate();

  return (
    <PageContainer>
      <PageHeader title="Administración" description="Tenants de la plataforma — entra a uno para administrar todo lo suyo." />

      <section className="space-y-3">
        <h2 className="text-xs font-semibold tracking-wide text-ink-faint uppercase">Plataforma</h2>
        {/*
         * Billing catalog, subscriptions, invoices/payments/promotions and
         * estimated profitability live on their own page (/billing,
         * "Facturación" in the rail).
         */}
        <TenantsSection onTenantCreated={(t) => navigate(`/admin/tenants/${t.id}`)} />
        <MonitoringSettingsSection />
        <MapProviderSection />
      </section>
    </PageContainer>
  );
}

// ---------------------------------------------------------------------------
// Map -- tile provider (platform_map_settings, migration 0030). Automatic
// failover on tile errors runs in each user's browser (see lib/mapProviders.ts).
// What is edited HERE is only the manual override: "auto" (the expected state,
// each browser decides) or forcing a specific provider for the WHOLE platform.
// Unlike Monitoring (bypass-only, routine support work), this is super_admin
// only (see require_super_admin in platform.py) -- forcing the map for the whole
// platform is an infrastructure decision, not a routine task.
function MapProviderSection() {
  const { role } = useAuth();
  const canEdit = role === "super_admin";
  const [settings, setSettings] = useState<Awaited<ReturnType<typeof api.getMapSettings>> | null>(null);
  const [selected, setSelected] = useState<MapProvider>("auto");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api
      .getMapSettings()
      .then((s) => {
        setSettings(s);
        setSelected(s.active_provider);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "error cargando el ajuste"));
  }, []);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const updated = await api.updateMapSettings(selected);
      setSettings(updated);
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error guardando el ajuste");
    } finally {
      setBusy(false);
    }
  }

  // Only providers that are actually usable today can be forced
  // (AUTO_FAILOVER_ORDER already excludes CARTO without an API key) -- never a
  // button that would produce a broken map stamped "API KEY REQUIRED".
  const forceOptions = AUTO_FAILOVER_ORDER;

  return (
    <Card>
      <CardTitle>Mapa</CardTitle>
      <p className="mb-3 text-xs text-ink-dim">
        Proveedor de tiles del mapa. "Automático" es el estado esperado — cada navegador conmuta solo a otro
        proveedor si detecta errores reales cargando tiles. Forzar uno específico aquí es una anulación manual
        para toda la plataforma, pensada solo para cuando un proveedor falla de una forma que la detección
        automática no cubre; en operación normal nunca debería hacer falta.
      </p>
      {error && <Alert>{error}</Alert>}
      {settings == null ? (
        <p className="text-xs text-ink-faint">Cargando…</p>
      ) : (
        <>
          <p className="mb-3 text-xs text-ink-dim">
            Estado actual:{" "}
            {settings.active_provider === "auto" ? (
              <span className="text-ink">automático</span>
            ) : (
              <span className="text-ink">
                forzado a {MAP_PROVIDERS[settings.active_provider].label}
                {settings.forced_by_email && ` · por ${settings.forced_by_email}`}
                {settings.forced_at && ` · ${new Date(settings.forced_at).toLocaleString()}`}
              </span>
            )}
          </p>
          {canEdit ? (
            <form onSubmit={onSubmit} className="flex flex-wrap items-end gap-2">
              <Field label="Proveedor">
                <Select
                  value={selected}
                  onChange={(e) => {
                    setSaved(false);
                    setSelected(e.target.value as MapProvider);
                  }}
                >
                  <option value="auto">Automático (recomendado)</option>
                  {forceOptions.map((id) => (
                    <option key={id} value={id}>
                      Forzar: {MAP_PROVIDERS[id].label}
                    </option>
                  ))}
                </Select>
              </Field>
              <Button type="submit" disabled={busy}>
                Guardar
              </Button>
              {saved && <span className="text-xs text-emerald-500">Guardado.</span>}
            </form>
          ) : (
            <p className="text-xs text-ink-faint">Solo super_admin puede cambiar esta anulación.</p>
          )}
        </>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Monitoring -- the "device alive/seen now" threshold
// (platform_monitoring_settings, migration 0028). Bypass-only to edit (same
// operational rule as a tenant's max_live_view_seconds, not a super_admin
// decision). READING it is open to any authenticated session (see
// useDeviceOfflineThreshold), which is why this editor lives only here and needs
// no "who can see" check beyond "who can open this page".
function MonitoringSettingsSection() {
  const [thresholdMinutes, setThresholdMinutes] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api
      .getMonitoringSettings()
      .then((s) => setThresholdMinutes(Math.round(s.device_offline_threshold_seconds / 60)))
      .catch((err) => setError(err instanceof ApiError ? err.message : "error cargando el ajuste"));
  }, []);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (thresholdMinutes == null) return;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const updated = await api.updateMonitoringSettings(thresholdMinutes * 60);
      setThresholdMinutes(Math.round(updated.device_offline_threshold_seconds / 60));
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error guardando el ajuste");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardTitle>Monitoreo</CardTitle>
      <p className="mb-3 text-xs text-ink-dim">
        Minutos sin reportar (posición o heartbeat) antes de que un dispositivo deje de verse como "visto ahora" en el
        mapa y los listados. Aplica a toda la plataforma, cámaras y GPS por igual.
      </p>
      {error && <Alert>{error}</Alert>}
      {thresholdMinutes == null ? (
        <p className="text-xs text-ink-faint">Cargando…</p>
      ) : (
        <form onSubmit={onSubmit} className="flex flex-wrap items-end gap-2">
          <Field label="Umbral (minutos)">
            <Input
              type="number"
              min={1}
              max={1440}
              value={thresholdMinutes}
              onChange={(e) => {
                setSaved(false);
                setThresholdMinutes(Number(e.target.value));
              }}
              className="w-28"
            />
          </Field>
          <Button type="submit" disabled={busy}>
            Guardar
          </Button>
          {saved && <span className="text-xs text-emerald-500">Guardado.</span>}
        </form>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Tenants
// ---------------------------------------------------------------------------

function TenantsSection({ onTenantCreated }: { onTenantCreated: (t: Tenant) => void }) {
  const { searchInput, setSearchInput, setOffset, page, error, reload } = usePaginatedResource<Tenant>((p) =>
    api.listTenants(p),
  );
  const [showCreate, setShowCreate] = useState(false);

  return (
    <Card>
      <CardTitle
        action={
          <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setShowCreate((v) => !v)}>
            {showCreate ? "Cancelar" : "+ Nuevo tenant"}
          </Button>
        }
      >
        Tenants
      </CardTitle>

      <Input
        placeholder="Buscar por nombre..."
        value={searchInput}
        onChange={(e) => setSearchInput(e.target.value)}
        className="mb-3"
      />

      {showCreate && (
        <div className="mb-3">
          <TenantCreateForm
            onCreated={(t) => {
              setShowCreate(false);
              onTenantCreated(t);
              reload();
            }}
          />
        </div>
      )}

      {error && <Alert>{error}</Alert>}

      {!page || page.items.length === 0 ? (
        <EmptyState>Sin resultados.</EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                <th className="py-1.5 pr-2 font-medium">Nombre</th>
                <th className="py-1.5 pr-2 font-medium">Estado</th>
                <th className="py-1.5 pr-2 font-medium">Límite/sesión (s)</th>
                <th className="py-1.5 pr-2 font-medium">Cuota/mes (min)</th>
                <th className="py-1.5 pr-2 font-medium">Retención GPS (días)</th>
                <th className="py-1.5 pr-2 font-medium">Ciclo de facturación</th>
                <th className="py-1.5 font-medium">Webhooks</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {page.items.map((t) => (
                <TenantRow key={t.id} tenant={t} onChanged={reload} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {page && (
        <div className="mt-3">
          <Pagination total={page.total} limit={page.limit} offset={page.offset} onOffsetChange={setOffset} />
        </div>
      )}
    </Card>
  );
}

function TenantCreateForm({ onCreated }: { onCreated: (t: Tenant) => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const tenant = await api.createTenant(name);
      setName("");
      onCreated(tenant);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error creando tenant");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-wrap items-end gap-2 border border-line bg-surface-2 p-3">
      <div className="min-w-[200px] flex-1">
        <Field label="Nombre del nuevo tenant">
          <Input required autoFocus value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
      </div>
      <Button type="submit" disabled={busy}>
        Crear tenant
      </Button>
      {error && <Alert>{error}</Alert>}
    </form>
  );
}

function TenantRow({ tenant, onChanged }: { tenant: Tenant; onChanged: () => void }) {
  const [limit, setLimit] = useState(String(tenant.max_live_view_seconds));
  const [busyLimit, setBusyLimit] = useState(false);
  const [limitError, setLimitError] = useState<string | null>(null);
  const dirtyLimit = limit !== String(tenant.max_live_view_seconds);

  // Accumulated MONTHLY quota (different from the per-session limit) -- edited
  // in minutes in the UI (more readable than raw seconds for a monthly total),
  // converted to seconds on save.
  const quotaMinutes = Math.round(tenant.live_view_monthly_quota_seconds / 60);
  const [quota, setQuota] = useState(String(quotaMinutes));
  const [busyQuota, setBusyQuota] = useState(false);
  const [quotaError, setQuotaError] = useState<string | null>(null);
  const dirtyQuota = quota !== String(quotaMinutes);

  async function saveLimit() {
    const seconds = Number(limit);
    if (!Number.isInteger(seconds) || seconds < 5 || seconds > 3600) {
      setLimitError("entero entre 5 y 3600");
      return;
    }
    setBusyLimit(true);
    setLimitError(null);
    try {
      await api.updateTenant(tenant.id, { max_live_view_seconds: seconds });
      onChanged();
    } catch (err) {
      setLimitError(err instanceof ApiError ? err.message : "error guardando");
    } finally {
      setBusyLimit(false);
    }
  }

  async function saveQuota() {
    const minutes = Number(quota);
    if (!Number.isInteger(minutes) || minutes < 1 || minutes > 1_666_666) {
      setQuotaError("entero entre 1 y 1,666,666");
      return;
    }
    setBusyQuota(true);
    setQuotaError(null);
    try {
      await api.updateTenant(tenant.id, { live_view_monthly_quota_seconds: minutes * 60 });
      onChanged();
    } catch (err) {
      setQuotaError(err instanceof ApiError ? err.message : "error guardando");
    } finally {
      setBusyQuota(false);
    }
  }

  // GPS position retention -- a plan attribute, same pattern as the limit/quota
  // above (bypass-only, PATCH /tenants/{id}).
  const [retention, setRetention] = useState(String(tenant.gps_retention_days));
  const [busyRetention, setBusyRetention] = useState(false);
  const [retentionError, setRetentionError] = useState<string | null>(null);
  const dirtyRetention = retention !== String(tenant.gps_retention_days);

  async function saveRetention() {
    const days = Number(retention);
    if (!Number.isInteger(days) || days < 7 || days > 730) {
      setRetentionError("entero entre 7 y 730");
      return;
    }
    setBusyRetention(true);
    setRetentionError(null);
    try {
      await api.updateTenant(tenant.id, { gps_retention_days: days });
      onChanged();
    } catch (err) {
      setRetentionError(err instanceof ApiError ? err.message : "error guardando");
    } finally {
      setBusyRetention(false);
    }
  }

  return (
    <tr>
      <td className="py-2 pr-2 align-top">
        {/*
         * Opens this tenant's workspace instead of managing everything mixed
         * together from this table.
         */}
        <Link to={`/admin/tenants/${tenant.id}`} className="font-medium text-brand-700 hover:underline">
          {tenant.name}
        </Link>
      </td>
      <td className="py-2 pr-2 align-top">
        <Badge tone={tenant.status === "active" ? "success" : "muted"}>{tenant.status}</Badge>
      </td>
      <td className="py-2 pr-2 align-top">
        <div className="flex items-center gap-1.5">
          <Input
            type="number"
            min={5}
            max={3600}
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
            className="w-20 py-1 text-xs"
          />
          {dirtyLimit && (
            <Button variant="secondary" disabled={busyLimit} onClick={saveLimit} className="px-2 py-1 text-xs">
              Guardar
            </Button>
          )}
        </div>
        {limitError && <p className="mt-1 text-xs text-red-300">{limitError}</p>}
      </td>
      <td className="py-2 pr-2 align-top">
        <div className="flex items-center gap-1.5">
          <Input
            type="number"
            min={1}
            value={quota}
            onChange={(e) => setQuota(e.target.value)}
            className="w-20 py-1 text-xs"
          />
          {dirtyQuota && (
            <Button variant="secondary" disabled={busyQuota} onClick={saveQuota} className="px-2 py-1 text-xs">
              Guardar
            </Button>
          )}
        </div>
        {quotaError && <p className="mt-1 text-xs text-red-300">{quotaError}</p>}
      </td>
      <td className="py-2 align-top">
        <div className="flex items-center gap-1.5">
          <Input
            type="number"
            min={7}
            max={730}
            value={retention}
            onChange={(e) => setRetention(e.target.value)}
            className="w-20 py-1 text-xs"
          />
          {dirtyRetention && (
            <Button variant="secondary" disabled={busyRetention} onClick={saveRetention} className="px-2 py-1 text-xs">
              Guardar
            </Button>
          )}
        </div>
        {retentionError && <p className="mt-1 text-xs text-red-300">{retentionError}</p>}
      </td>
      <td className="py-2 pr-2 align-top">
        <BillingPeriodSelect tenant={tenant} onChanged={onChanged} />
      </td>
      <td className="py-2 align-top">
        <WebhooksEnabledToggle tenant={tenant} onChanged={onChanged} />
      </td>
    </tr>
  );
}

// Platform approval for the outbound webhooks feature -- ONLY super_admin can
// change this (see PATCH /tenants/{id} in tenants.py: support gets 403 even with
// RLS bypass, same as "support cannot create users/API keys"). A platform role
// that is not super_admin sees the state but NEVER a control the backend would
// reject anyway (same rule as canManage in TenantWorkspace.tsx).
function WebhooksEnabledToggle({ tenant, onChanged }: { tenant: Tenant; onChanged: () => void }) {
  const { role } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    setBusy(true);
    setError(null);
    try {
      await api.updateTenant(tenant.id, { webhooks_enabled: !tenant.webhooks_enabled });
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error guardando");
    } finally {
      setBusy(false);
    }
  }

  if (role !== "super_admin") {
    return <Badge tone={tenant.webhooks_enabled ? "success" : "muted"}>{tenant.webhooks_enabled ? "sí" : "no"}</Badge>;
  }

  return (
    <div>
      <label className="flex items-center gap-1.5 text-xs text-ink">
        <input type="checkbox" checked={tenant.webhooks_enabled} disabled={busy} onChange={toggle} />
        {tenant.webhooks_enabled ? "Habilitado" : "Deshabilitado"}
      </label>
      {error && <p className="mt-1 text-xs text-red-300">{error}</p>}
    </div>
  );
}

// Billing cycle -- editable through PATCH /tenants/{id} (see api/README.md),
// since plans are sold monthly, semiannually or annually.
const BILLING_PERIOD_LABEL: Record<BillingPeriod, string> = {
  monthly: "Mensual",
  semiannual: "Semestral",
  annual: "Anual",
};

function BillingPeriodSelect({ tenant, onChanged }: { tenant: Tenant; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(period: BillingPeriod) {
    if (period === tenant.billing_period) return;
    setBusy(true);
    setError(null);
    try {
      await api.updateTenant(tenant.id, { billing_period: period });
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error guardando");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <Select
        value={tenant.billing_period}
        disabled={busy}
        onChange={(e) => save(e.target.value as BillingPeriod)}
        className="w-32 py-1 text-xs"
      >
        {Object.entries(BILLING_PERIOD_LABEL).map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </Select>
      {error && <p className="mt-1 text-xs text-red-300">{error}</p>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Users
// ---------------------------------------------------------------------------

const TENANT_ROLES: { value: TenantRole; label: string }[] = [
  { value: "tenant_admin", label: "tenant_admin" },
  { value: "tenant_operator", label: "tenant_operator" },
  { value: "tenant_viewer", label: "tenant_viewer" },
  { value: "driver", label: "driver" },
];

// Roles to which device/group assignment (alerts) applies -- tenant_admin always
// sees its whole tenant (see app_can_view_device, migration 0031); assigning
// anything would have no effect and the backend rejects it explicitly (422).
const _ASSIGNABLE_ROLES = new Set(["tenant_operator", "tenant_viewer"]);

export function UsersSection({
  tenants,
  isPlatform,
  ownTenantId,
  defaultTenantId,
  canManage = true,
}: {
  tenants: Tenant[];
  isPlatform: boolean;
  ownTenantId: string | null;
  defaultTenantId: string | null;
  // The only caller (TenantWorkspace.tsx) decides this from the role: the
  // backend rejects writes with 403 for non-admins, so "+ new user"/"assignment"
  // must not be shown to them -- see TenantWorkspace.tsx::canManage.
  canManage?: boolean;
}) {
  // ownTenantId also narrows the query (not just the UI) -- needed so a PLATFORM
  // session viewing a specific tenant's workspace (TenantWorkspace.tsx) only
  // sees THAT tenant's users, not the global bypass. Redundant for a real
  // tenant_admin (RLS already limits it) but harmless.
  const { searchInput, setSearchInput, setOffset, page, error, reload } = usePaginatedResource<User>((p) =>
    api.listUsers({ ...p, tenant_id: ownTenantId ?? undefined }),
  );
  const [showCreate, setShowCreate] = useState(false);
  const [expandedUserId, setExpandedUserId] = useState<string | null>(null);
  const tenantName = (id: string) => tenants.find((t) => t.id === id)?.name ?? id;
  const { userId: ownUserId, role: ownRole, isPlatform: ownIsPlatform } = useAuth();
  const [statusBusyId, setStatusBusyId] = useState<string | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [resetPasswordUserId, setResetPasswordUserId] = useState<string | null>(null);
  const [resetPasswordValue, setResetPasswordValue] = useState("");
  const [resetBusyId, setResetBusyId] = useState<string | null>(null);
  const [resetDoneId, setResetDoneId] = useState<string | null>(null);

  // Orderly, auditable user removal: deactivate/reactivate (soft-delete, never a
  // real DELETE, see migration 0038_user_device_status_audit.sql). Same
  // permission as creating users (canManage), never on one's own account (the
  // backend already rejects it with 422; hiding the button avoids a useless
  // round trip).
  async function toggleUserStatus(u: User) {
    setStatusError(null);
    setStatusBusyId(u.id);
    try {
      await api.updateUserStatus(u.id, u.status === "active" ? "disabled" : "active");
      reload();
    } catch (e) {
      setStatusError(e instanceof Error ? e.message : "no se pudo cambiar el estado");
    } finally {
      setStatusBusyId(null);
    }
  }

  // Admin password reset. Mirrors on the frontend the SAME rules the backend
  // enforces (users.py::reset_user_password) -- never show a button the backend
  // would reject, same rule as canManage in TenantWorkspace.tsx.
  function canResetPassword(u: User): boolean {
    if (u.id === ownUserId) return false;
    if (u.role === "super_admin" || u.role === "support") return ownRole === "super_admin";
    if (ownIsPlatform) return true;
    if (ownRole !== "tenant_admin") return false;
    return u.role !== "tenant_admin";
  }

  async function submitResetPassword(userId: string) {
    setStatusError(null);
    setResetBusyId(userId);
    try {
      await api.resetUserPassword(userId, resetPasswordValue);
      setResetPasswordUserId(null);
      setResetPasswordValue("");
      setResetDoneId(userId);
      setTimeout(() => setResetDoneId((id) => (id === userId ? null : id)), 5000);
    } catch (e) {
      setStatusError(e instanceof ApiError ? e.message : "no se pudo restablecer la contraseña");
    } finally {
      setResetBusyId(null);
    }
  }

  return (
    <Card>
      <CardTitle
        action={
          canManage && (
            <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setShowCreate((v) => !v)}>
              {showCreate ? "Cancelar" : "+ Nuevo usuario"}
            </Button>
          )
        }
      >
        Usuarios
      </CardTitle>

      <Input
        placeholder="Buscar por email..."
        value={searchInput}
        onChange={(e) => setSearchInput(e.target.value)}
        className="mb-3"
      />

      {showCreate && canManage && (
        <div className="mb-3">
          <UserCreateForm
            tenants={tenants}
            isPlatform={isPlatform}
            ownTenantId={ownTenantId}
            defaultTenantId={defaultTenantId}
            onCreated={() => {
              setShowCreate(false);
              reload();
            }}
          />
        </div>
      )}

      {error && <Alert>{error}</Alert>}
      {statusError && <Alert>{statusError}</Alert>}

      {!page || page.items.length === 0 ? (
        <EmptyState>Sin resultados.</EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                <th className="py-1.5 pr-2 font-medium">Email</th>
                {isPlatform && <th className="py-1.5 pr-2 font-medium">Tenant</th>}
                <th className="py-1.5 font-medium">Rol</th>
                <th className="py-1.5 pl-2 font-medium">Estado</th>
                {ownTenantId && <th className="py-1.5 pl-2 font-medium" />}
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {page.items.map((u) => (
                <Fragment key={u.id}>
                  <tr>
                    <td className="py-2 pr-2 text-ink">{u.email}</td>
                    {isPlatform && (
                      <td className="py-2 pr-2 text-xs text-ink-dim">{u.tenant_id ? tenantName(u.tenant_id) : "—"}</td>
                    )}
                    <td className="py-2">
                      <Badge>{u.role}</Badge>
                    </td>
                    <td className="py-2 pl-2">
                      <Badge tone={u.status === "active" ? "success" : "muted"}>
                        {u.status === "active" ? "Activo" : "Desactivado"}
                      </Badge>
                    </td>
                    {ownTenantId && (
                      <td className="py-2 pl-2 text-right">
                        <div className="flex items-center justify-end gap-2">
                          {canManage && u.id !== ownUserId && (
                            <Button
                              variant="secondary"
                              className="px-2 py-1 text-xs"
                              disabled={statusBusyId === u.id}
                              onClick={() => toggleUserStatus(u)}
                            >
                              {u.status === "active" ? "Desactivar" : "Reactivar"}
                            </Button>
                          )}
                          {canResetPassword(u) && (
                            <Button
                              variant="secondary"
                              className="px-2 py-1 text-xs"
                              onClick={() => {
                                setResetPasswordUserId((id) => (id === u.id ? null : u.id));
                                setResetPasswordValue("");
                              }}
                            >
                              {resetDoneId === u.id
                                ? "Contraseña actualizada"
                                : resetPasswordUserId === u.id
                                  ? "Cancelar"
                                  : "Restablecer contraseña"}
                            </Button>
                          )}
                          {_ASSIGNABLE_ROLES.has(u.role) && (
                            <Button
                              variant="secondary"
                              className="px-2 py-1 text-xs"
                              onClick={() => setExpandedUserId((id) => (id === u.id ? null : u.id))}
                            >
                              {expandedUserId === u.id ? "Cerrar" : "Asignación"}
                            </Button>
                          )}
                        </div>
                      </td>
                    )}
                  </tr>
                  {resetPasswordUserId === u.id && ownTenantId && (
                    <tr>
                      <td colSpan={isPlatform ? 5 : 4} className="bg-surface-2 p-3">
                        <div className="flex items-center gap-2">
                          <Input
                            type="password"
                            autoFocus
                            placeholder="Nueva contraseña (mínimo 8 caracteres)"
                            value={resetPasswordValue}
                            onChange={(e) => setResetPasswordValue(e.target.value)}
                            className="max-w-xs"
                          />
                          <Button
                            className="px-2 py-1 text-xs"
                            disabled={resetBusyId === u.id || resetPasswordValue.length < 8}
                            onClick={() => submitResetPassword(u.id)}
                          >
                            Confirmar
                          </Button>
                        </div>
                        <p className="mt-1 text-xs text-ink-faint">
                          Comunícale la nueva contraseña a {u.email} por fuera de la plataforma -- no hay envío de correo todavía.
                        </p>
                      </td>
                    </tr>
                  )}
                  {expandedUserId === u.id && ownTenantId && (
                    <tr>
                      <td colSpan={isPlatform ? 5 : 4} className="bg-surface-2 p-3">
                        <UserAssignmentPanel userId={u.id} tenantId={ownTenantId} canManage={canManage} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {page && (
        <div className="mt-3">
          <Pagination total={page.total} limit={page.limit} offset={page.offset} onOffsetChange={setOffset} />
        </div>
      )}
    </Card>
  );
}

// Device/group assignment panel + channel preference for ONE user
// (tenant_operator/tenant_viewer) -- expanded inline in the UsersSection row
// instead of a modal. FULL replacement on save (same pattern as
// TenantSettingsUpdate/DeviceGroupMembersUpdate), not incremental add/remove --
// the admin sees the complete current state and corrects it.
function UserAssignmentPanel({ userId, tenantId, canManage }: { userId: string; tenantId: string; canManage: boolean }) {
  const [devices, setDevices] = useState<Device[]>([]);
  const [groups, setGroups] = useState<DeviceGroup[]>([]);
  const [assignment, setAssignment] = useState<UserDeviceAssignments | null>(null);
  const [settings, setSettings] = useState<UserNotificationSettings | null>(null);
  const [selectedDevices, setSelectedDevices] = useState<Set<string>>(new Set());
  const [selectedGroups, setSelectedGroups] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setError(null);
    Promise.all([
      api.listDevices({ tenant_id: tenantId, limit: 1000 }),
      api.listDeviceGroups({ tenant_id: tenantId, limit: 1000 }),
      api.getUserDeviceAssignments(userId),
      api.getUserNotificationSettings(userId),
    ])
      .then(([d, g, a, s]) => {
        setDevices(d.items);
        setGroups(g.items);
        setAssignment(a);
        setSettings(s);
        setSelectedDevices(new Set(a.device_ids));
        setSelectedGroups(new Set(a.device_group_ids));
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "error cargando asignación"));
  }, [userId, tenantId]);

  function toggle(set: Set<string>, id: string, setter: (s: Set<string>) => void) {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setter(next);
  }

  async function onSaveAssignment() {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const result = await api.replaceUserDeviceAssignments(userId, {
        device_ids: Array.from(selectedDevices),
        device_group_ids: Array.from(selectedGroups),
      });
      setAssignment(result);
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error guardando asignación");
    } finally {
      setBusy(false);
    }
  }

  async function onToggleChannel(field: keyof UserNotificationSettings, value: boolean) {
    if (!settings) return;
    const prev = settings;
    setSettings({ ...settings, [field]: value }); // optimistic, reverted on failure
    try {
      const result = await api.updateUserNotificationSettings(userId, { [field]: value });
      setSettings(result);
    } catch (err) {
      setSettings(prev);
      setError(err instanceof ApiError ? err.message : "error guardando preferencia");
    }
  }

  if (error) return <Alert>{error}</Alert>;
  if (!assignment || !settings) return <div className="text-xs text-ink-faint">Cargando…</div>;

  return (
    <div className="space-y-3">
      <div>
        <div className="mb-1 text-xs font-semibold tracking-wide text-ink-faint uppercase">Dispositivos</div>
        {devices.length === 0 ? (
          <div className="text-xs text-ink-faint">Este tenant no tiene dispositivos.</div>
        ) : (
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {devices.map((d) => (
              <label key={d.id} className="flex items-center gap-1.5 text-sm text-ink">
                <input
                  type="checkbox"
                  disabled={!canManage}
                  checked={selectedDevices.has(d.id)}
                  onChange={() => toggle(selectedDevices, d.id, setSelectedDevices)}
                />
                {d.label}
              </label>
            ))}
          </div>
        )}
      </div>

      <div>
        <div className="mb-1 text-xs font-semibold tracking-wide text-ink-faint uppercase">Grupos</div>
        {groups.length === 0 ? (
          <div className="text-xs text-ink-faint">Este tenant no tiene grupos de dispositivos todavía.</div>
        ) : (
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {groups.map((g) => (
              <label key={g.id} className="flex items-center gap-1.5 text-sm text-ink">
                <input
                  type="checkbox"
                  disabled={!canManage}
                  checked={selectedGroups.has(g.id)}
                  onChange={() => toggle(selectedGroups, g.id, setSelectedGroups)}
                />
                {g.name} ({g.device_count})
              </label>
            ))}
          </div>
        )}
      </div>

      {canManage && (
        <Button variant="secondary" className="px-2 py-1 text-xs" onClick={onSaveAssignment} disabled={busy}>
          Guardar asignación
        </Button>
      )}
      {saved && <span className="ml-2 text-xs text-ink-faint">Guardado.</span>}

      <div className="border-t border-line pt-3">
        <div className="mb-1 text-xs font-semibold tracking-wide text-ink-faint uppercase">Notificaciones</div>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <label className="flex items-center gap-1.5 text-sm text-ink">
            <input
              type="checkbox"
              disabled={!canManage}
              checked={settings.in_app_enabled}
              onChange={(e) => onToggleChannel("in_app_enabled", e.target.checked)}
            />
            Plataforma
          </label>
          <label className="flex items-center gap-1.5 text-sm text-ink">
            <input
              type="checkbox"
              disabled={!canManage}
              checked={settings.email_enabled}
              onChange={(e) => onToggleChannel("email_enabled", e.target.checked)}
            />
            Correo
          </label>
        </div>
      </div>

      <ApiKeysSubsection userId={userId} devices={devices} canManage={canManage} />
    </div>
  );
}

// API keys (0034_api_keys.sql) -- M2M integrations that authenticate AS this
// user, narrowed by can_write (read-only by default) and optionally by
// allowed_device_ids (a subset of the devices already visible above). Same rule
// as the rest of this panel: only tenant_admin/platform manages them
// (canManage), never self-service.
function ApiKeysSubsection({ userId, devices, canManage }: { userId: string; devices: Device[]; canManage: boolean }) {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  // The only moment the full key lives in browser memory -- it is never
  // requested from the backend again (ApiKeyOut, unlike ApiKeyCreatedOut,
  // carries no raw_key). Discarded when the notice is closed or the panel
  // unmounts (collapsing "Assignment").
  const [justCreated, setJustCreated] = useState<ApiKeyCreated | null>(null);

  async function reload() {
    try {
      setKeys((await api.listApiKeys(userId, { limit: 100 })).items);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando claves de API");
    }
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  async function onRevoke(keyId: string) {
    try {
      await api.revokeApiKey(userId, keyId);
      await reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error revocando la clave");
    }
  }

  return (
    <div className="border-t border-line pt-3">
      <div className="mb-1 flex items-center justify-between">
        <div className="text-xs font-semibold tracking-wide text-ink-faint uppercase">Claves de API</div>
        {canManage && !showCreate && !justCreated && (
          <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setShowCreate(true)}>
            + Nueva clave
          </Button>
        )}
      </div>
      {error && <p className="text-xs text-red-400">{error}</p>}

      {justCreated && (
        <div className="mb-2 space-y-1.5 rounded-md border border-brand-600/40 bg-brand-600/10 p-2 text-xs">
          <p className="font-medium text-ink">Copia esta clave ahora — no se puede volver a mostrar.</p>
          <code className="block overflow-x-auto rounded bg-surface-2 p-1.5 text-[11px] break-all text-ink select-all">
            {justCreated.raw_key}
          </code>
          <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setJustCreated(null)}>
            Ya la copié
          </Button>
        </div>
      )}

      {showCreate && canManage && (
        <ApiKeyCreateForm
          userId={userId}
          devices={devices}
          onCreated={(created) => {
            setShowCreate(false);
            setJustCreated(created);
            reload();
          }}
          onCancel={() => setShowCreate(false)}
        />
      )}

      {keys.length === 0 ? (
        <p className="text-xs text-ink-faint">Sin claves de API todavía.</p>
      ) : (
        <ul className="divide-y divide-line text-xs">
          {keys.map((k) => {
            const isRevoked = !!k.revoked_at;
            const isExpired = !isRevoked && new Date(k.expires_at).getTime() < Date.now();
            return (
              <li key={k.id} className="flex items-center justify-between gap-2 py-1.5">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="truncate font-medium text-ink">{k.name}</span>
                    <Badge tone={k.can_write ? "warning" : "muted"}>
                      {k.can_write ? "lectura/escritura" : "solo lectura"}
                    </Badge>
                    {isRevoked && <Badge tone="danger">revocada</Badge>}
                    {isExpired && <Badge tone="danger">expirada</Badge>}
                  </div>
                  <p className="text-[11px] text-ink-faint">
                    {k.key_prefix}… ·{" "}
                    {k.allowed_device_ids === null
                      ? "todos los dispositivos"
                      : `${k.allowed_device_ids.length} dispositivo(s)`}{" "}
                    · {k.last_used_at ? `usada ${new Date(k.last_used_at).toLocaleString()}` : "nunca usada"}
                  </p>
                </div>
                {canManage && !isRevoked && (
                  <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => onRevoke(k.id)}>
                    Revocar
                  </Button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function ApiKeyCreateForm({
  userId,
  devices,
  onCreated,
  onCancel,
}: {
  userId: string;
  devices: Device[];
  onCreated: (created: ApiKeyCreated) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [canWrite, setCanWrite] = useState(false);
  const [restrictDevices, setRestrictDevices] = useState(false);
  const [selectedDevices, setSelectedDevices] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function toggleDevice(id: string) {
    const next = new Set(selectedDevices);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelectedDevices(next);
  }

  async function onSubmit() {
    if (!name.trim()) {
      setError("ponle un nombre a la clave (para identificarla después)");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await api.createApiKey(userId, {
        name: name.trim(),
        can_write: canWrite,
        allowed_device_ids: restrictDevices ? Array.from(selectedDevices) : null,
      });
      onCreated(created);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error creando la clave");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mb-2 space-y-2 rounded-md border border-line p-2">
      {error && <p className="text-xs text-red-400">{error}</p>}
      <Input
        placeholder="Nombre (ej. Integración con sistema XYZ)"
        value={name}
        onChange={(e) => setName(e.target.value)}
        className="text-xs"
      />
      <label className="flex items-center gap-1.5 text-xs text-ink">
        <input type="checkbox" checked={canWrite} onChange={(e) => setCanWrite(e.target.checked)} />
        Permitir escritura (además de lectura) — sin marcar, la clave es de solo lectura
      </label>
      <label className="flex items-center gap-1.5 text-xs text-ink">
        <input type="checkbox" checked={restrictDevices} onChange={(e) => setRestrictDevices(e.target.checked)} />
        Acotar a dispositivos específicos (sin marcar, ve todo lo que este usuario ya ve)
      </label>
      {/*
       * The field name could suggest a full sandbox, but a key "scoped to 0
       * devices" still sees vehicles/drivers/routes in full (including a
       * driver's last GPS check-in position) -- those resources are not
       * modeled per device for ANY role in this system. Clarified here instead
       * of pretending a scope that does not exist.
       */}
      <p className="pl-4 text-[11px] text-ink-faint">
        Esto acota solo telemetría (dispositivos, posiciones, alarmas, notificaciones) — vehículos, choferes y rutas
        del tenant siguen visibles según el rol del usuario, sin importar esta selección.
      </p>
      {restrictDevices && (
        <div className="flex flex-wrap gap-x-3 gap-y-1 pl-4">
          {devices.length === 0 ? (
            <span className="text-xs text-ink-faint">Este tenant no tiene dispositivos.</span>
          ) : (
            devices.map((d) => (
              <label key={d.id} className="flex items-center gap-1.5 text-xs text-ink">
                <input type="checkbox" checked={selectedDevices.has(d.id)} onChange={() => toggleDevice(d.id)} />
                {d.label}
              </label>
            ))
          )}
        </div>
      )}
      <div className="flex gap-2">
        <Button variant="secondary" className="px-2 py-1 text-xs" onClick={onSubmit} disabled={busy}>
          Crear clave
        </Button>
        <Button variant="ghost" className="px-2 py-1 text-xs" onClick={onCancel}>
          Cancelar
        </Button>
      </div>
    </div>
  );
}

// Device groups (see
// infra/postgres/migrations/0031_device_groups_and_assignments.sql) --
// tenant_admin self-service, same as Vehicles/Drivers.
export function DeviceGroupsSection({ tenantId, canManage }: { tenantId: string; canManage: boolean }) {
  const { searchInput, setSearchInput, setOffset, page, error, reload } = usePaginatedResource<DeviceGroup>((p) =>
    api.listDeviceGroups({ ...p, tenant_id: tenantId }),
  );
  const [devices, setDevices] = useState<Device[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [expandedGroupId, setExpandedGroupId] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listDevices({ tenant_id: tenantId, limit: 1000 })
      .then((r) => setDevices(r.items))
      .catch(() => setDevices([]));
  }, [tenantId]);

  async function onCreate(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setFormError(null);
    try {
      await api.createDeviceGroup({ tenant_id: tenantId, name: newName });
      setNewName("");
      setShowCreate(false);
      reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "error creando el grupo");
    } finally {
      setBusy(false);
    }
  }

  async function onRename(id: string) {
    setBusy(true);
    try {
      await api.updateDeviceGroup(id, renameValue);
      setRenamingId(null);
      reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "error renombrando el grupo");
    } finally {
      setBusy(false);
    }
  }

  async function onDelete(id: string) {
    setBusy(true);
    try {
      await api.deleteDeviceGroup(id);
      if (expandedGroupId === id) setExpandedGroupId(null);
      reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "error borrando el grupo");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardTitle
        action={
          canManage && (
            <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setShowCreate((v) => !v)}>
              {showCreate ? "Cancelar" : "+ Nuevo grupo"}
            </Button>
          )
        }
      >
        Grupos de dispositivos
      </CardTitle>

      <Input
        placeholder="Buscar por nombre..."
        value={searchInput}
        onChange={(e) => setSearchInput(e.target.value)}
        className="mb-3"
      />

      {showCreate && canManage && (
        <form onSubmit={onCreate} className="mb-3 flex gap-2 border border-line bg-surface-2 p-3">
          <Input
            placeholder="nombre del grupo"
            required
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            className="flex-1"
          />
          <Button type="submit" disabled={busy}>
            Crear
          </Button>
        </form>
      )}

      {(error || formError) && <Alert>{error ?? formError}</Alert>}

      {!page || page.items.length === 0 ? (
        <EmptyState>Sin grupos todavía.</EmptyState>
      ) : (
        <div className="divide-y divide-line">
          {page.items.map((g) => (
            <div key={g.id} className="py-2">
              <div className="flex items-center justify-between gap-2">
                {renamingId === g.id ? (
                  <div className="flex flex-1 gap-2">
                    <Input value={renameValue} onChange={(e) => setRenameValue(e.target.value)} className="flex-1" />
                    <Button className="px-2 py-1 text-xs" onClick={() => onRename(g.id)} disabled={busy}>
                      Guardar
                    </Button>
                    <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setRenamingId(null)}>
                      Cancelar
                    </Button>
                  </div>
                ) : (
                  <>
                    <span className="text-sm text-ink">
                      {g.name} <span className="text-xs text-ink-faint">({g.device_count} dispositivos)</span>
                    </span>
                    <div className="flex gap-2">
                      <Button
                        variant="secondary"
                        className="px-2 py-1 text-xs"
                        onClick={() => setExpandedGroupId((id) => (id === g.id ? null : g.id))}
                      >
                        {expandedGroupId === g.id ? "Cerrar" : "Miembros"}
                      </Button>
                      {canManage && (
                        <>
                          <Button
                            variant="secondary"
                            className="px-2 py-1 text-xs"
                            onClick={() => {
                              setRenamingId(g.id);
                              setRenameValue(g.name);
                            }}
                          >
                            Renombrar
                          </Button>
                          <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => onDelete(g.id)} disabled={busy}>
                            Borrar
                          </Button>
                        </>
                      )}
                    </div>
                  </>
                )}
              </div>
              {expandedGroupId === g.id && (
                <div className="mt-2">
                  <DeviceGroupMembersPicker
                    groupId={g.id}
                    devices={devices}
                    canManage={canManage}
                    onSaved={reload}
                  />
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {page && (
        <div className="mt-3">
          <Pagination total={page.total} limit={page.limit} offset={page.offset} onOffsetChange={setOffset} />
        </div>
      )}
    </Card>
  );
}

function DeviceGroupMembersPicker({
  groupId,
  devices,
  canManage,
  onSaved,
}: {
  groupId: string;
  devices: Device[];
  canManage: boolean;
  onSaved: () => void;
}) {
  const [selected, setSelected] = useState<Set<string> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listDeviceGroupMembers(groupId)
      .then((ids) => setSelected(new Set(ids)))
      .catch((err) => setError(err instanceof ApiError ? err.message : "error cargando miembros"));
  }, [groupId]);

  function toggle(id: string) {
    if (!selected) return;
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  }

  async function onSave() {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      await api.replaceDeviceGroupMembers(groupId, Array.from(selected));
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error guardando miembros");
    } finally {
      setBusy(false);
    }
  }

  if (error) return <Alert>{error}</Alert>;
  if (!selected) return <div className="text-xs text-ink-faint">Cargando…</div>;

  return (
    <div>
      {devices.length === 0 ? (
        <div className="text-xs text-ink-faint">Este tenant no tiene dispositivos.</div>
      ) : (
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {devices.map((d) => (
            <label key={d.id} className="flex items-center gap-1.5 text-sm text-ink">
              <input type="checkbox" disabled={!canManage} checked={selected.has(d.id)} onChange={() => toggle(d.id)} />
              {d.label}
            </label>
          ))}
        </div>
      )}
      {canManage && (
        <Button variant="secondary" className="mt-2 px-2 py-1 text-xs" onClick={onSave} disabled={busy}>
          Guardar miembros
        </Button>
      )}
    </div>
  );
}

function UserCreateForm({
  tenants,
  isPlatform,
  ownTenantId,
  defaultTenantId,
  onCreated,
}: {
  tenants: Tenant[];
  isPlatform: boolean;
  ownTenantId: string | null;
  defaultTenantId: string | null;
  onCreated: () => void;
}) {
  // tenant_admin can only create users in its own tenant (the backend already
  // enforces it, see api/app/routers/users.py) -- locking the picker here is
  // just better UX, not the only barrier.
  const [tenantId, setTenantId] = useState(defaultTenantId ?? ownTenantId ?? "");
  const [role, setRole] = useState<TenantRole>("tenant_admin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [driverId, setDriverId] = useState("");
  const [drivers, setDrivers] = useState<Driver[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (defaultTenantId) setTenantId(defaultTenantId);
  }, [defaultTenantId]);

  // Roster of drivers WITHOUT a login account yet, for the selected tenant --
  // only needed when the chosen role is "driver", so it is fetched on demand
  // instead of always alongside tenants/vehicles.
  useEffect(() => {
    if (role !== "driver" || !tenantId) {
      setDrivers([]);
      return;
    }
    api
      .listDrivers({ limit: 1000 })
      .then(({ items }) => setDrivers(items.filter((d) => d.tenant_id === tenantId)))
      .catch(() => setDrivers([]));
  }, [role, tenantId]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createUser({
        email,
        password,
        role,
        tenant_id: tenantId,
        driver_id: role === "driver" ? driverId : undefined,
      });
      setEmail("");
      setPassword("");
      setDriverId("");
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error creando usuario");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="grid grid-cols-2 gap-2 border border-line bg-surface-2 p-3 sm:grid-cols-4">
      {isPlatform && (
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
      <Select value={role} onChange={(e) => setRole(e.target.value as TenantRole)} className="col-span-2 sm:col-span-1">
        {TENANT_ROLES.map((r) => (
          <option key={r.value} value={r.value}>
            {r.label}
          </option>
        ))}
      </Select>
      <Input
        type="email"
        placeholder="email"
        required
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        className="col-span-2 sm:col-span-1"
      />
      <Input
        type="password"
        placeholder="contraseña"
        required
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        className="col-span-2 sm:col-span-1"
      />
      {role === "driver" && (
        <Select required value={driverId} onChange={(e) => setDriverId(e.target.value)} className="col-span-2 sm:col-span-1">
          <option value="" disabled>
            Chofer...
          </option>
          {drivers.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name}
            </option>
          ))}
        </Select>
      )}
      <Button type="submit" disabled={busy} className="col-span-2 sm:col-span-4">
        Crear usuario
      </Button>
      {error && (
        <div className="col-span-2 sm:col-span-4">
          <Alert>{error}</Alert>
        </div>
      )}
    </form>
  );
}

// ---------------------------------------------------------------------------
// Outbound webhooks (0035_webhooks.sql) -- the "push" counterpart of API keys.
// Requires tenants.webhooks_enabled=true (approved by the platform, see
// WebhooksEnabledToggle above) -- TenantWorkspace.tsx decides whether this
// section is shown at all.
// ---------------------------------------------------------------------------

// Same set as WEBHOOK_EVENT_TYPES in api/app/schemas.py -- no endpoint exposes
// it (over-engineering for a one-element list today), so it is kept in sync by
// hand; adding a new event type requires touching both sides anyway (the
// dispatcher in webhooks.py must also know how to emit it).
const WEBHOOK_EVENT_TYPE_LABELS: Record<string, string> = {
  device_alarm: "Alarma de dispositivo",
};

export function WebhookEndpointsSection({ tenantId, canManage }: { tenantId: string; canManage: boolean }) {
  const { page, error, reload, setOffset } = usePaginatedResource<WebhookEndpoint>((p) =>
    api.listWebhookEndpoints({ ...p, tenant_id: tenantId }),
  );
  const [showCreate, setShowCreate] = useState(false);
  // The only moment the signing secret lives in browser memory -- it is never
  // requested again (WebhookEndpointOut, unlike WebhookEndpointCreatedOut,
  // carries no `secret`).
  const [justCreated, setJustCreated] = useState<WebhookEndpointCreated | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // Result of the last "send test" per endpoint -- lets the UI diagnose why a
  // receiver gets nothing (HTTP status, latency, real error) without waiting for
  // a real alarm.
  const [testResults, setTestResults] = useState<Record<string, WebhookTestResult | "running">>({});
  const [deleteArmedId, setDeleteArmedId] = useState<string | null>(null);

  async function onTest(id: string) {
    setActionError(null);
    setTestResults((prev) => ({ ...prev, [id]: "running" }));
    try {
      const result = await api.testWebhookEndpoint(id);
      setTestResults((prev) => ({ ...prev, [id]: result }));
    } catch (err) {
      setTestResults((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      setActionError(err instanceof ApiError ? err.message : "error enviando la prueba");
    }
  }

  async function onToggleEnabled(endpoint: WebhookEndpoint) {
    setActionError(null);
    try {
      await api.updateWebhookEndpoint(endpoint.id, { enabled: !endpoint.enabled });
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "error guardando");
    }
  }

  async function onRotate(id: string) {
    setActionError(null);
    try {
      const rotated = await api.rotateWebhookSecret(id);
      setJustCreated(rotated);
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "error rotando el secreto");
    }
  }

  async function onDelete(id: string) {
    setActionError(null);
    // Confirmation armed on the same button (no native confirm()): a single
    // click must not delete the webhook -- and its delivery history with it.
    if (deleteArmedId !== id) {
      setDeleteArmedId(id);
      return;
    }
    setDeleteArmedId(null);
    try {
      await api.deleteWebhookEndpoint(id);
      if (expandedId === id) setExpandedId(null);
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "error borrando el webhook");
    }
  }

  return (
    <Card>
      <CardTitle
        action={
          canManage &&
          !showCreate && (
            <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setShowCreate(true)}>
              + Nuevo webhook
            </Button>
          )
        }
      >
        Webhooks
      </CardTitle>

      {error && <Alert>{error}</Alert>}
      {actionError && <Alert>{actionError}</Alert>}

      {justCreated && (
        <div className="mb-3 space-y-1.5 rounded-md border border-brand-600/40 bg-brand-600/10 p-2 text-xs">
          <p className="font-medium text-ink">
            Copia este secreto ahora — no se puede volver a mostrar. Úsalo para verificar la firma HMAC-SHA256 (header{" "}
            <code>X-OpenMDVR-Signature</code>) de cada entrega.
          </p>
          <code className="block overflow-x-auto rounded bg-surface-2 p-1.5 text-[11px] break-all text-ink select-all">
            {justCreated.secret}
          </code>
          <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setJustCreated(null)}>
            Ya lo copié
          </Button>
        </div>
      )}

      {showCreate && canManage && (
        <WebhookEndpointCreateForm
          tenantId={tenantId}
          onCreated={(created) => {
            setShowCreate(false);
            setJustCreated(created);
            reload();
          }}
          onCancel={() => setShowCreate(false)}
        />
      )}

      {!page || page.items.length === 0 ? (
        <EmptyState>Sin webhooks todavía.</EmptyState>
      ) : (
        <ul className="divide-y divide-line text-sm">
          {page.items.map((w) => (
            <li key={w.id} className="space-y-1.5 py-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate font-medium text-ink">{w.url}</p>
                  <p className="text-xs text-ink-faint">
                    {w.event_types.map((t) => WEBHOOK_EVENT_TYPE_LABELS[t] ?? t).join(", ")}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  <Badge tone={w.enabled ? "success" : "muted"}>{w.enabled ? "activo" : "desactivado"}</Badge>
                  {w.consecutive_failures > 0 && (
                    <Badge tone="warning">{w.consecutive_failures} fallo(s) seguido(s)</Badge>
                  )}
                </div>
              </div>
              {w.disabled_reason && <p className="text-xs text-red-300">{w.disabled_reason}</p>}
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <Button
                  variant="secondary"
                  className="px-2 py-1 text-xs"
                  onClick={() => setExpandedId(expandedId === w.id ? null : w.id)}
                >
                  {expandedId === w.id ? "Ocultar entregas" : "Ver entregas"}
                </Button>
                {canManage && (
                  <>
                    <Button
                      variant="secondary"
                      className="px-2 py-1 text-xs"
                      disabled={testResults[w.id] === "running"}
                      onClick={() => onTest(w.id)}
                    >
                      {testResults[w.id] === "running" ? "Enviando..." : "Enviar prueba"}
                    </Button>
                    <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => onToggleEnabled(w)}>
                      {w.enabled ? "Desactivar" : "Activar"}
                    </Button>
                    <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => onRotate(w.id)}>
                      Rotar secreto
                    </Button>
                    <Button
                      variant="ghost"
                      className={`px-2 py-1 text-xs ${deleteArmedId === w.id ? "text-red-400" : ""}`}
                      onClick={() => onDelete(w.id)}
                    >
                      {deleteArmedId === w.id ? "¿Borrar? Confirmar" : "Borrar"}
                    </Button>
                  </>
                )}
              </div>
              {(() => {
                const r = testResults[w.id];
                if (!r || r === "running") return null;
                return (
                  <div
                    className={`rounded-md border px-2 py-1.5 text-xs ${
                      r.success ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300" : "border-red-500/40 bg-red-500/10 text-red-300"
                    }`}
                  >
                    {r.success
                      ? `Prueba entregada: HTTP ${r.status_code} en ${r.elapsed_ms} ms.`
                      : `La prueba falló${r.status_code != null ? ` (HTTP ${r.status_code})` : ""}: ${r.error}`}
                  </div>
                );
              })()}
              {expandedId === w.id && <WebhookDeliveriesList endpointId={w.id} />}
            </li>
          ))}
        </ul>
      )}

      {page && (
        <div className="mt-3">
          <Pagination total={page.total} limit={page.limit} offset={page.offset} onOffsetChange={setOffset} />
        </div>
      )}
    </Card>
  );
}

function WebhookEndpointCreateForm({
  tenantId,
  onCreated,
  onCancel,
}: {
  tenantId: string;
  onCreated: (created: WebhookEndpointCreated) => void;
  onCancel: () => void;
}) {
  const [url, setUrl] = useState("");
  const [selectedEvents, setSelectedEvents] = useState<Set<string>>(new Set(Object.keys(WEBHOOK_EVENT_TYPE_LABELS)));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function toggleEvent(type: string) {
    const next = new Set(selectedEvents);
    if (next.has(type)) next.delete(type);
    else next.add(type);
    setSelectedEvents(next);
  }

  async function onSubmit() {
    if (!url.trim()) {
      setError("la URL es obligatoria");
      return;
    }
    if (selectedEvents.size === 0) {
      setError("selecciona al menos un tipo de evento");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await api.createWebhookEndpoint({
        tenant_id: tenantId,
        url: url.trim(),
        event_types: Array.from(selectedEvents),
      });
      onCreated(created);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error creando el webhook");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mb-3 space-y-2 rounded-md border border-line p-2">
      {error && <p className="text-xs text-red-400">{error}</p>}
      <Input
        placeholder="https://tu-sistema.com/webhooks/openmdvr"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        className="text-xs"
      />
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {Object.entries(WEBHOOK_EVENT_TYPE_LABELS).map(([type, label]) => (
          <label key={type} className="flex items-center gap-1.5 text-xs text-ink">
            <input type="checkbox" checked={selectedEvents.has(type)} onChange={() => toggleEvent(type)} />
            {label}
          </label>
        ))}
      </div>
      <div className="flex gap-2">
        <Button variant="secondary" className="px-2 py-1 text-xs" onClick={onSubmit} disabled={busy}>
          Crear webhook
        </Button>
        <Button variant="ghost" className="px-2 py-1 text-xs" onClick={onCancel}>
          Cancelar
        </Button>
      </div>
    </div>
  );
}

const WEBHOOK_DELIVERY_STATUS_TONE: Record<WebhookDelivery["status"], BadgeTone> = {
  pending: "brand",
  success: "success",
  failed: "warning",
  exhausted: "danger",
};

function WebhookDeliveriesList({ endpointId }: { endpointId: string }) {
  const [deliveries, setDeliveries] = useState<WebhookDelivery[]>([]);
  const [error, setError] = useState<string | null>(null);

  // Refreshes on its own while the list is open (a pending delivery turns into
  // success/failure within seconds).
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api
        .listWebhookDeliveries(endpointId, { limit: 20 })
        .then((r) => {
          if (!cancelled) setDeliveries(r.items);
        })
        .catch((err) => {
          if (!cancelled) setError(err instanceof ApiError ? err.message : "error cargando entregas");
        });
    load();
    const timer = setInterval(load, 10_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [endpointId]);

  if (error) return <p className="text-xs text-red-400">{error}</p>;
  if (deliveries.length === 0) return <p className="text-xs text-ink-faint">Sin entregas todavía.</p>;

  return (
    <ul className="divide-y divide-line rounded-md border border-line text-xs">
      {deliveries.map((d) => (
        <li key={d.id} className="flex items-center justify-between gap-2 p-2">
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <Badge tone={WEBHOOK_DELIVERY_STATUS_TONE[d.status]}>{d.status}</Badge>
              <span className="text-ink-faint">{d.event_type}</span>
              {d.response_status_code != null && <span className="text-ink-faint">HTTP {d.response_status_code}</span>}
            </div>
            <p className="text-[11px] text-ink-dim">
              {new Date(d.created_at).toLocaleString()} · intento {d.attempt_count}
              {d.status === "pending" && d.attempt_count > 0 && ` · reintento ${new Date(d.next_attempt_at).toLocaleTimeString()}`}
            </p>
            {d.last_error && <p className="mt-0.5 text-[11px] break-words text-red-300">{d.last_error}</p>}
          </div>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Vehicles
// ---------------------------------------------------------------------------

interface VehicleFormValues {
  plate: string;
  make: string;
  model: string;
  year: string;
  notes: string;
  // Unit speed limit (0050_vehicle_max_speed.sql) -- empty text = no limit
  // configured, the overspeed alarm never fires. Protocol-agnostic: evaluated in
  // the database for every device type.
  maxSpeedKmh: string;
}

const EMPTY_VEHICLE_FORM: VehicleFormValues = {
  plate: "", make: "", model: "", year: "", notes: "", maxSpeedKmh: "",
};

export function VehiclesSection({
  tenants,
  canManage,
  ownTenantId,
  onChanged,
}: {
  tenants: Tenant[];
  canManage: boolean;
  ownTenantId: string | null;
  onChanged: () => void;
}) {
  const { searchInput, setSearchInput, setOffset, page, error, reload } = usePaginatedResource<Vehicle>((p) =>
    api.listVehicles({ ...p, tenant_id: ownTenantId ?? undefined }),
  );
  const [drivers, setDrivers] = useState<Driver[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  useEffect(() => {
    api
      .listDrivers({ limit: 1000, tenant_id: ownTenantId ?? undefined })
      .then(({ items }) => setDrivers(items))
      .catch(() => {
        /* the "assign driver" picker simply stays empty */
      });
  }, [page, ownTenantId]);

  function reloadAll() {
    reload();
    onChanged();
  }

  return (
    <Card>
      <CardTitle
        action={
          canManage && (
            <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setShowCreate((v) => !v)}>
              {showCreate ? "Cancelar" : "+ Nuevo vehículo"}
            </Button>
          )
        }
      >
        Vehículos
      </CardTitle>

      <Input
        placeholder="Buscar por placa, marca o modelo..."
        value={searchInput}
        onChange={(e) => setSearchInput(e.target.value)}
        className="mb-3"
      />

      {showCreate && canManage && (
        <div className="mb-3">
          <VehicleCreateForm
            tenants={tenants}
            ownTenantId={ownTenantId}
            onCreated={() => {
              setShowCreate(false);
              reloadAll();
            }}
          />
        </div>
      )}

      {error && <Alert>{error}</Alert>}

      {!page || page.items.length === 0 ? (
        <EmptyState>Sin resultados.</EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                <th className="py-1.5 pr-2 font-medium">Placa</th>
                <th className="py-1.5 pr-2 font-medium">Vehículo</th>
                <th className="py-1.5 pr-2 font-medium">Chofer actual</th>
                {canManage && <th className="py-1.5 font-medium" />}
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {page.items.map((v) =>
                editingId === v.id ? (
                  <VehicleEditRow key={v.id} vehicle={v} onDone={() => setEditingId(null)} onSaved={reloadAll} />
                ) : (
                  <tr key={v.id}>
                    <td className="font-data py-2 pr-2 text-ink">{v.plate || "—"}</td>
                    <td className="py-2 pr-2 text-xs text-ink-dim">
                      {[v.make, v.model, v.year].filter(Boolean).join(" ") || "—"}
                    </td>
                    <td className="py-2 pr-2 text-xs text-ink-dim">
                      {canManage ? (
                        <div className="flex items-center gap-1.5">
                          <Select
                            value={v.current_driver_id ?? ""}
                            onChange={async (e) => {
                              const driverId = e.target.value;
                              if (driverId) await api.assignDriver(v.id, driverId);
                              else await api.unassignDriver(v.id);
                              reloadAll();
                            }}
                            className="py-1 text-xs"
                          >
                            <option value="">— sin asignar —</option>
                            {drivers.map((d) => (
                              <option key={d.id} value={d.id}>
                                {d.name}
                              </option>
                            ))}
                          </Select>
                        </div>
                      ) : (
                        v.current_driver_name || "—"
                      )}
                    </td>
                    {canManage && (
                      <td className="py-2">
                        <Button variant="ghost" onClick={() => setEditingId(v.id)} className="px-2 py-1 text-xs">
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

      {page && (
        <div className="mt-3">
          <Pagination total={page.total} limit={page.limit} offset={page.offset} onOffsetChange={setOffset} />
        </div>
      )}
    </Card>
  );
}

function VehicleCreateForm({
  tenants,
  ownTenantId,
  onCreated,
}: {
  tenants: Tenant[];
  ownTenantId: string | null;
  onCreated: () => void;
}) {
  const [tenantId, setTenantId] = useState(ownTenantId ?? "");
  const [fields, setFields] = useState<VehicleFormValues>(EMPTY_VEHICLE_FORM);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function set<K extends keyof VehicleFormValues>(key: K, value: string) {
    setFields((f) => ({ ...f, [key]: value }));
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createVehicle({
        tenant_id: tenantId,
        plate: fields.plate || undefined,
        make: fields.make || undefined,
        model: fields.model || undefined,
        year: fields.year ? Number(fields.year) : undefined,
        notes: fields.notes || undefined,
        max_speed_kmh: fields.maxSpeedKmh ? Number(fields.maxSpeedKmh) : undefined,
      });
      setFields(EMPTY_VEHICLE_FORM);
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error creando vehículo");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-2 border border-line bg-surface-2 p-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
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
        <Input placeholder="Placa" value={fields.plate} onChange={(e) => set("plate", e.target.value)} />
        <Input placeholder="Marca" value={fields.make} onChange={(e) => set("make", e.target.value)} />
        <Input placeholder="Modelo" value={fields.model} onChange={(e) => set("model", e.target.value)} />
        <Input type="number" placeholder="Año" value={fields.year} onChange={(e) => set("year", e.target.value)} />
        <Input
          type="number"
          placeholder="Vel. máxima km/h (opcional)"
          value={fields.maxSpeedKmh}
          onChange={(e) => set("maxSpeedKmh", e.target.value)}
        />
      </div>
      <Button type="submit" disabled={busy}>
        Dar de alta
      </Button>
      {error && <Alert>{error}</Alert>}
    </form>
  );
}

function VehicleEditRow({ vehicle, onDone, onSaved }: { vehicle: Vehicle; onDone: () => void; onSaved: () => void }) {
  const [fields, setFields] = useState<VehicleFormValues>({
    plate: vehicle.plate ?? "",
    make: vehicle.make ?? "",
    model: vehicle.model ?? "",
    year: vehicle.year != null ? String(vehicle.year) : "",
    notes: vehicle.notes ?? "",
    maxSpeedKmh: vehicle.max_speed_kmh != null ? String(vehicle.max_speed_kmh) : "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function set<K extends keyof VehicleFormValues>(key: K, value: string) {
    setFields((f) => ({ ...f, [key]: value }));
  }

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await api.updateVehicle(vehicle.id, {
        plate: fields.plate,
        make: fields.make,
        model: fields.model,
        year: fields.year ? Number(fields.year) : undefined,
        notes: fields.notes,
        max_speed_kmh: fields.maxSpeedKmh ? Number(fields.maxSpeedKmh) : undefined,
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
      <td colSpan={4} className="py-2">
        <div className="space-y-2 border border-brand-600/30 bg-surface-2 p-3">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Input placeholder="Placa" value={fields.plate} onChange={(e) => set("plate", e.target.value)} />
            <Input placeholder="Marca" value={fields.make} onChange={(e) => set("make", e.target.value)} />
            <Input placeholder="Modelo" value={fields.model} onChange={(e) => set("model", e.target.value)} />
            <Input type="number" placeholder="Año" value={fields.year} onChange={(e) => set("year", e.target.value)} />
            <Input
              type="number"
              placeholder="Vel. máxima km/h"
              value={fields.maxSpeedKmh}
              onChange={(e) => set("maxSpeedKmh", e.target.value)}
            />
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

// ---------------------------------------------------------------------------
// Drivers
// ---------------------------------------------------------------------------

interface DriverFormValues {
  name: string;
  license_number: string;
  phone: string;
}

const EMPTY_DRIVER_FORM: DriverFormValues = { name: "", license_number: "", phone: "" };

export function DriversSection({
  tenants,
  canManage,
  ownTenantId,
}: {
  tenants: Tenant[];
  canManage: boolean;
  ownTenantId: string | null;
}) {
  const { searchInput, setSearchInput, setOffset, page, error, reload } = usePaginatedResource<Driver>((p) =>
    api.listDrivers({ ...p, tenant_id: ownTenantId ?? undefined }),
  );
  const [showCreate, setShowCreate] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  return (
    <Card>
      <CardTitle
        action={
          canManage && (
            <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setShowCreate((v) => !v)}>
              {showCreate ? "Cancelar" : "+ Nuevo chofer"}
            </Button>
          )
        }
      >
        Choferes
      </CardTitle>

      <Input
        placeholder="Buscar por nombre, licencia o teléfono..."
        value={searchInput}
        onChange={(e) => setSearchInput(e.target.value)}
        className="mb-3"
      />

      {showCreate && canManage && (
        <div className="mb-3">
          <DriverCreateForm
            tenants={tenants}
            ownTenantId={ownTenantId}
            onCreated={() => {
              setShowCreate(false);
              reload();
            }}
          />
        </div>
      )}

      {error && <Alert>{error}</Alert>}

      {!page || page.items.length === 0 ? (
        <EmptyState>Sin resultados.</EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                <th className="py-1.5 pr-2 font-medium">Nombre</th>
                <th className="py-1.5 pr-2 font-medium">Teléfono</th>
                <th className="py-1.5 pr-2 font-medium">Vehículo actual</th>
                {canManage && <th className="py-1.5 font-medium" />}
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {page.items.map((d) =>
                editingId === d.id ? (
                  <DriverEditRow key={d.id} driver={d} onDone={() => setEditingId(null)} onSaved={reload} />
                ) : (
                  <tr key={d.id}>
                    <td className="py-2 pr-2 text-ink">{d.name}</td>
                    <td className="py-2 pr-2 text-xs text-ink-dim">{d.phone || "—"}</td>
                    <td className="font-data py-2 pr-2 text-xs text-ink-dim">{d.current_vehicle_plate || "—"}</td>
                    {canManage && (
                      <td className="py-2">
                        <Button variant="ghost" onClick={() => setEditingId(d.id)} className="px-2 py-1 text-xs">
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

      {page && (
        <div className="mt-3">
          <Pagination total={page.total} limit={page.limit} offset={page.offset} onOffsetChange={setOffset} />
        </div>
      )}
    </Card>
  );
}

function DriverCreateForm({
  tenants,
  ownTenantId,
  onCreated,
}: {
  tenants: Tenant[];
  ownTenantId: string | null;
  onCreated: () => void;
}) {
  const [tenantId, setTenantId] = useState(ownTenantId ?? "");
  const [fields, setFields] = useState<DriverFormValues>(EMPTY_DRIVER_FORM);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function set<K extends keyof DriverFormValues>(key: K, value: string) {
    setFields((f) => ({ ...f, [key]: value }));
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createDriver({
        tenant_id: tenantId,
        name: fields.name,
        license_number: fields.license_number || undefined,
        phone: fields.phone || undefined,
      });
      setFields(EMPTY_DRIVER_FORM);
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error creando chofer");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-2 border border-line bg-surface-2 p-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
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
          placeholder="Nombre completo"
          required
          value={fields.name}
          onChange={(e) => set("name", e.target.value)}
          className="col-span-2 sm:col-span-1"
        />
        <Input placeholder="Licencia" value={fields.license_number} onChange={(e) => set("license_number", e.target.value)} />
        <Input placeholder="Teléfono" value={fields.phone} onChange={(e) => set("phone", e.target.value)} />
      </div>
      <Button type="submit" disabled={busy}>
        Dar de alta
      </Button>
      {error && <Alert>{error}</Alert>}
    </form>
  );
}

function DriverEditRow({ driver, onDone, onSaved }: { driver: Driver; onDone: () => void; onSaved: () => void }) {
  const [fields, setFields] = useState<DriverFormValues>({
    name: driver.name,
    license_number: driver.license_number ?? "",
    phone: driver.phone ?? "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function set<K extends keyof DriverFormValues>(key: K, value: string) {
    setFields((f) => ({ ...f, [key]: value }));
  }

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await api.updateDriver(driver.id, {
        name: fields.name,
        license_number: fields.license_number,
        phone: fields.phone,
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
      <td colSpan={4} className="py-2">
        <div className="space-y-2 border border-brand-600/30 bg-surface-2 p-3">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Input placeholder="Nombre" value={fields.name} onChange={(e) => set("name", e.target.value)} />
            <Input placeholder="Licencia" value={fields.license_number} onChange={(e) => set("license_number", e.target.value)} />
            <Input placeholder="Teléfono" value={fields.phone} onChange={(e) => set("phone", e.target.value)} />
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

// ---------------------------------------------------------------------------
// Devices
// ---------------------------------------------------------------------------

export function DevicesSection({
  tenants,
  vehicles,
  isPlatform,
  ownTenantId = null,
  onVehicleCreated,
  onDeviceCreated,
}: {
  tenants: Tenant[];
  vehicles: Vehicle[];
  isPlatform: boolean;
  // Creating/editing devices is still require_bypass in the backend (isPlatform
  // is the only real permission check) -- ownTenantId is only about SCOPE: it
  // narrows the query and hides the redundant Tenant column/picker when already
  // inside a specific tenant's workspace (TenantWorkspace.tsx).
  ownTenantId?: string | null;
  // The onboarding wizard (see DeviceOnboardingWizard) can create a NEW vehicle
  // as part of the flow -- this notifies the parent (TenantWorkspace.tsx) so it
  // refreshes ITS vehicle list, same mechanism as VehiclesSection's `onChanged`.
  onVehicleCreated?: () => void;
  // Same for the quota shown in TenantWorkspace.tsx's "Summary" card --
  // otherwise creating a unit would add it to the table below while "N of M
  // contracted" above kept the old number until a full reload.
  onDeviceCreated?: () => void;
}) {
  // Deactivated devices are hidden by default; the toggle shows them so they can
  // be reactivated. Ref: reload() is memoized by search/page and must read the
  // current value.
  const [showInactive, setShowInactive] = useState(false);
  const showInactiveRef = useRef(false);
  showInactiveRef.current = showInactive;
  const { searchInput, setSearchInput, setOffset, page, error, reload } = usePaginatedResource<Device>((p) =>
    api.listDevices({ ...p, tenant_id: ownTenantId ?? undefined, exclude_inactive: !showInactiveRef.current }),
  );
  const firstToggle = useRef(true);
  useEffect(() => {
    if (firstToggle.current) {
      firstToggle.current = false;
      return;
    }
    setOffset(0);
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showInactive]);
  const [showCreate, setShowCreate] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const tenantName = (id: string) => tenants.find((t) => t.id === id)?.name ?? id;
  const vehicleById = (id: string | null) => (id ? vehicles.find((v) => v.id === id) : undefined);
  const offlineThresholdSeconds = useDeviceOfflineThreshold();

  // GET /devices/models is bypass-only on the backend -- only requested for
  // platform sessions, never for a tenant_admin (it would 403 with no real use:
  // the resolved name already comes in Device.device_model_name).
  const [deviceModels, setDeviceModels] = useState<DeviceModel[]>([]);
  useEffect(() => {
    if (!isPlatform) return;
    api.listDeviceModels().then(setDeviceModels).catch(() => {});
  }, [isPlatform]);

  return (
    <Card>
      <CardTitle
        action={
          isPlatform && (
            <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setShowCreate((v) => !v)}>
              {showCreate ? "Cancelar" : "+ Nueva unidad"}
            </Button>
          )
        }
      >
        Dispositivos
      </CardTitle>

      <div className="mb-3 flex flex-wrap items-center gap-3">
        <Input
          placeholder="Buscar por etiqueta o terminal..."
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          className="min-w-0 flex-1"
        />
        <label className="flex shrink-0 cursor-pointer items-center gap-2 text-xs text-ink-dim select-none">
          <input type="checkbox" checked={showInactive} onChange={(e) => setShowInactive(e.target.checked)} className="h-4 w-4 accent-brand-600" />
          Mostrar desactivados
        </label>
      </div>

      {showCreate && isPlatform && (
        <div className="mb-3">
          <DeviceOnboardingWizard
            tenants={tenants}
            vehicles={vehicles}
            deviceModels={deviceModels}
            ownTenantId={ownTenantId}
            onVehicleCreated={onVehicleCreated}
            onCreated={() => {
              setShowCreate(false);
              reload();
              onDeviceCreated?.();
            }}
          />
        </div>
      )}

      {error && <Alert>{error}</Alert>}

      {!page || page.items.length === 0 ? (
        <EmptyState>Sin resultados.</EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                <th className="py-1.5 pr-2 font-medium">Unidad</th>
                {!ownTenantId && <th className="py-1.5 pr-2 font-medium">Tenant</th>}
                <th className="py-1.5 pr-2 font-medium">Vehículo</th>
                <th className="py-1.5 pr-2 font-medium">Conductor</th>
                <th className="py-1.5 pr-2 font-medium">Protocolo</th>
                <th className="py-1.5 pr-2 font-medium">Identificador</th>
                <th className="py-1.5 pr-2 font-medium">Modelo</th>
                <th className="py-1.5 pr-2 font-medium">SIM</th>
                <th className="py-1.5 pr-2 font-medium">
                  <span className="inline-flex items-center gap-1">
                    Visto
                    <Tooltip label={livenessTooltip(offlineThresholdSeconds)}>
                      <span className="text-[10px] normal-case text-ink-faint">ⓘ</span>
                    </Tooltip>
                  </span>
                </th>
                {isPlatform && <th className="py-1.5 font-medium" />}
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {page.items.map((d) => {
                const vehicle = vehicleById(d.vehicle_id);
                return editingId === d.id ? (
                  <DeviceEditRow
                    key={d.id}
                    device={d}
                    vehicles={vehicles}
                    deviceModels={deviceModels}
                    onDone={() => setEditingId(null)}
                    onSaved={reload}
                  />
                ) : (
                  <tr key={d.id}>
                    <td className="py-2 pr-2">
                      <Link to={`/devices/${d.id}?protocol=${d.protocol}`} className="font-medium text-brand-700 hover:underline">
                        {d.label}
                      </Link>
                      {d.status !== "active" && (
                        <div>
                          <Badge tone={d.status === "maintenance" ? "warning" : "muted"}>
                            {d.status === "maintenance" ? "En mantenimiento" : "Inactivo"}
                          </Badge>
                        </div>
                      )}
                      {vehicle?.plate && <div className="font-data text-xs text-ink-faint">{vehicle.plate}</div>}
                    </td>
                    {!ownTenantId && <td className="py-2 pr-2 text-xs text-ink-dim">{tenantName(d.tenant_id)}</td>}
                    <td className="py-2 pr-2 text-xs text-ink-dim">
                      {vehicle ? [vehicle.make, vehicle.model, vehicle.year].filter(Boolean).join(" ") || "—" : "—"}
                    </td>
                    <td className="py-2 pr-2 text-xs text-ink-dim">{vehicle?.current_driver_name || "—"}</td>
                    <td className="py-2 pr-2 text-xs text-ink-dim">{PROTOCOL_LABEL[d.protocol]}</td>
                    <td className="font-data py-2 pr-2 text-xs text-ink-dim">
                      {usesGT06Imei(d.protocol) ? d.gt06_imei : d.jt808_terminal_id}
                    </td>
                    <td className="py-2 pr-2 text-xs text-ink-dim">{d.device_model_name ?? "—"}</td>
                    <td className="font-data py-2 pr-2 text-xs text-ink-dim">
                      {d.sim_number ?? "—"}
                      {d.sim_carrier && <span className="text-ink-faint"> ({d.sim_carrier})</span>}
                    </td>
                    <td className="py-2 pr-2 text-xs text-ink-faint">
                      <span className="inline-flex items-center gap-1.5">
                        <span
                          className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                            isDeviceRecent(d.last_seen_at, offlineThresholdSeconds) ? "bg-emerald-500" : "bg-slate-600"
                          }`}
                        />
                        {lastSeenLabel(d.last_seen_at, offlineThresholdSeconds)}
                      </span>
                    </td>
                    {isPlatform && (
                      <td className="py-2">
                        <div className="flex items-center gap-1">
                          <Button variant="ghost" onClick={() => setEditingId(d.id)} className="px-2 py-1 text-xs">
                            Editar
                          </Button>
                          {/*
                           * GT06 configuration commands
                           * (SERVER/APN/TIMEZONE/etc) -- platform ONLY.
                           * Meaningless for jt808 (it does not speak GT06 at
                           * all).
                           */}
                          {(d.protocol === "gt06" || d.protocol === "gt06_video") && (
                            <Link
                              to={`/admin/devices/${d.id}/config-commands?device_label=${encodeURIComponent(d.label)}`}
                              className="px-2 py-1 text-xs font-medium text-brand-500 hover:underline"
                            >
                              Configurar
                            </Link>
                          )}
                        </div>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {page && (
        <div className="mt-3">
          <Pagination total={page.total} limit={page.limit} offset={page.offset} onOffsetChange={setOffset} />
        </div>
      )}
    </Card>
  );
}

// Device onboarding wizard: registering a device with vehicle + driver + group
// in one 4-step flow instead of jumping between four separate sections (Devices,
// Vehicles, Drivers via the vehicle, Device groups). It executes the real
// actions (createVehicle/assignDriver/createDeviceGroup) AS you advance, not at
// the end -- each step is optional ("no X for now"), so a quick registration
// without a vehicle stays as simple as a plain form.
type OnboardStep = "device" | "vehicle" | "driver" | "group";
type OnboardChoice = "none" | "existing" | "new";

const ONBOARD_STEPS: { id: OnboardStep; label: string }[] = [
  { id: "device", label: "Dispositivo" },
  { id: "vehicle", label: "Vehículo" },
  { id: "driver", label: "Chofer" },
  { id: "group", label: "Grupo" },
];

function OnboardStepPicker({
  value,
  onChange,
  noneLabel,
  existingLabel,
  newLabel,
}: {
  value: OnboardChoice;
  onChange: (choice: OnboardChoice) => void;
  noneLabel: string;
  existingLabel: string;
  newLabel: string;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {(
        [
          ["none", noneLabel],
          ["existing", existingLabel],
          ["new", newLabel],
        ] as const
      ).map(([choice, text]) => (
        <Button
          key={choice}
          type="button"
          variant={value === choice ? "primary" : "secondary"}
          className="px-2 py-1 text-xs"
          onClick={() => onChange(choice)}
        >
          {text}
        </Button>
      ))}
    </div>
  );
}

function DeviceOnboardingWizard({
  tenants,
  vehicles,
  deviceModels,
  ownTenantId,
  onCreated,
  onVehicleCreated,
}: {
  tenants: Tenant[];
  vehicles: Vehicle[];
  deviceModels: DeviceModel[];
  ownTenantId?: string | null;
  onCreated: () => void;
  onVehicleCreated?: () => void;
}) {
  const [step, setStep] = useState<OnboardStep>("device");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Step 1 -- device fields. Nothing is created yet: the real device is
  // registered at the end (step 4), after resolving vehicle/driver/group, so
  // vehicle_id can be sent already resolved in the SAME POST /devices (no extra
  // PATCH).
  const [tenantId, setTenantId] = useState(ownTenantId ?? "");
  const [protocol, setProtocol] = useState<DeviceProtocol>("jt808");
  const [identifier, setIdentifier] = useState("");
  const [label, setLabel] = useState("");
  const [deviceModelId, setDeviceModelId] = useState("");
  const [simNumber, setSimNumber] = useState("");
  const [simCarrier, setSimCarrier] = useState("");
  const [notes, setNotes] = useState("");

  // Step 2 -- "new vehicle" really creates it (api.createVehicle) as soon as you
  // advance, not a draft -- so the vehicle exists whether or not the user
  // finishes the rest of the wizard.
  const [vehicleChoice, setVehicleChoice] = useState<OnboardChoice>("none");
  const [vehicleId, setVehicleId] = useState("");
  const [localVehicles, setLocalVehicles] = useState<Vehicle[]>(vehicles);
  const [newVehiclePlate, setNewVehiclePlate] = useState("");
  const [newVehicleMake, setNewVehicleMake] = useState("");
  const [newVehicleModel, setNewVehicleModel] = useState("");
  const [newVehicleYear, setNewVehicleYear] = useState("");

  // Step 3 -- a driver is assigned to the VEHICLE (assignDriver), never directly
  // to the device -- the project's data model (migration 0014,
  // driver_vehicle_assignments). That is why this step performs the assignment
  // immediately as you advance, without waiting for the device to exist.
  const [driverChoice, setDriverChoice] = useState<OnboardChoice>("none");
  const [driverId, setDriverId] = useState("");
  const [drivers, setDrivers] = useState<Driver[]>([]);
  const [newDriverName, setNewDriverName] = useState("");
  const [newDriverLicense, setNewDriverLicense] = useState("");
  const [newDriverPhone, setNewDriverPhone] = useState("");

  // Step 4 -- the real membership (replaceDeviceGroupMembers) needs the device
  // id, which only exists after `finish()`; a NEW group is created as soon as
  // you advance (like vehicle/driver), the membership is added afterwards.
  const [groupChoice, setGroupChoice] = useState<OnboardChoice>("none");
  const [groupId, setGroupId] = useState("");
  const [groups, setGroups] = useState<DeviceGroup[]>([]);
  const [newGroupName, setNewGroupName] = useState("");

  const protocolModels = deviceModels.filter((m) => m.protocol === protocol);
  const tenantVehicles = localVehicles.filter((v) => v.tenant_id === tenantId);

  useEffect(() => {
    if (step !== "driver" || !tenantId) return;
    api
      .listDrivers({ tenant_id: tenantId, limit: 1000 })
      .then((r) => setDrivers(r.items))
      .catch(() => setDrivers([]));
  }, [step, tenantId]);

  useEffect(() => {
    if (step !== "group" || !tenantId) return;
    api
      .listDeviceGroups({ tenant_id: tenantId, limit: 1000 })
      .then((r) => setGroups(r.items))
      .catch(() => setGroups([]));
  }, [step, tenantId]);

  async function confirmVehicleStep() {
    setError(null);
    if (vehicleChoice === "new") {
      if (!newVehiclePlate && !newVehicleMake && !newVehicleModel) {
        setError('Completa al menos un dato del vehículo, o elige "Sin vehículo por ahora".');
        return;
      }
      setBusy(true);
      try {
        const created = await api.createVehicle({
          tenant_id: tenantId,
          plate: newVehiclePlate || undefined,
          make: newVehicleMake || undefined,
          model: newVehicleModel || undefined,
          year: newVehicleYear ? Number(newVehicleYear) : undefined,
        });
        setLocalVehicles((v) => [...v, created]);
        setVehicleId(created.id);
        onVehicleCreated?.();
        setStep("driver");
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "error creando vehículo");
      } finally {
        setBusy(false);
      }
      return;
    }
    if (vehicleChoice === "existing" && !vehicleId) {
      setError('Elige un vehículo, o cambia a "Sin vehículo por ahora".');
      return;
    }
    setStep("driver");
  }

  async function confirmDriverStep() {
    setError(null);
    if (!vehicleId) {
      setStep("group");
      return;
    }
    if (driverChoice === "new") {
      if (!newDriverName) {
        setError('El chofer necesita al menos un nombre, o elige "Sin chofer por ahora".');
        return;
      }
      setBusy(true);
      try {
        const created = await api.createDriver({
          tenant_id: tenantId,
          name: newDriverName,
          license_number: newDriverLicense || undefined,
          phone: newDriverPhone || undefined,
        });
        await api.assignDriver(vehicleId, created.id);
        // The vehicle chosen/created in step 2 changed its current_driver_name
        // -- notify the parent (TenantWorkspace.tsx) so the "Driver" column of
        // the Devices table does not keep the stale value until a reload.
        onVehicleCreated?.();
        setStep("group");
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "error creando/asignando chofer");
      } finally {
        setBusy(false);
      }
      return;
    }
    if (driverChoice === "existing") {
      if (!driverId) {
        setError('Elige un chofer, o cambia a "Sin chofer por ahora".');
        return;
      }
      setBusy(true);
      try {
        await api.assignDriver(vehicleId, driverId);
        onVehicleCreated?.(); // same reason as above
        setStep("group");
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "error asignando chofer");
      } finally {
        setBusy(false);
      }
      return;
    }
    setStep("group");
  }

  async function finish() {
    setError(null);
    if (groupChoice === "existing" && !groupId) {
      setError('Elige un grupo, o cambia a "Sin grupo por ahora".');
      return;
    }
    if (groupChoice === "new" && !newGroupName) {
      setError('El grupo necesita un nombre, o elige "Sin grupo por ahora".');
      return;
    }
    setBusy(true);
    try {
      const device = await api.createDevice({
        tenant_id: tenantId,
        protocol,
        jt808_terminal_id: protocol === "jt808" ? identifier : undefined,
        gt06_imei: usesGT06Imei(protocol) ? identifier : undefined,
        label,
        vehicle_id: vehicleId || undefined,
        notes: notes || undefined,
        device_model_id: deviceModelId || undefined,
        sim_number: simNumber || undefined,
        sim_carrier: simCarrier || undefined,
      });

      let targetGroupId = groupId;
      if (groupChoice === "new") {
        const created = await api.createDeviceGroup({ tenant_id: tenantId, name: newGroupName });
        targetGroupId = created.id;
      }
      if (targetGroupId) {
        const members = await api.listDeviceGroupMembers(targetGroupId);
        await api.replaceDeviceGroupMembers(targetGroupId, [...members, device.id]);
      }

      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error creando dispositivo");
    } finally {
      setBusy(false);
    }
  }

  const stepIndex = ONBOARD_STEPS.findIndex((s) => s.id === step);

  return (
    <div className="space-y-3 border border-line bg-surface-2 p-3">
      <div className="flex flex-wrap items-center gap-1 text-xs text-ink-faint">
        {ONBOARD_STEPS.map((s, i) => (
          <span key={s.id} className={`flex items-center gap-1 ${i === stepIndex ? "font-semibold text-brand-500" : ""}`}>
            {i > 0 && <span className="px-0.5 text-ink-faint">→</span>}
            {i + 1}. {s.label}
          </span>
        ))}
      </div>

      {error && <Alert>{error}</Alert>}

      {step === "device" && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setError(null);
            setStep("vehicle");
          }}
          className="space-y-2"
        >
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {!ownTenantId && (
              <Select
                required
                value={tenantId}
                onChange={(e) => setTenantId(e.target.value)}
                className="col-span-2 sm:col-span-1"
              >
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
            <Select
              value={protocol}
              onChange={(e) => {
                setProtocol(e.target.value as DeviceProtocol);
                setIdentifier(""); // the identifier format changes completely between protocols
              }}
              className="col-span-2 sm:col-span-1"
            >
              <option value="jt808">{PROTOCOL_LABEL.jt808}</option>
              <option value="gt06">{PROTOCOL_LABEL.gt06}</option>
              <option value="gt06_video">{PROTOCOL_LABEL.gt06_video}</option>
            </Select>
            <Input
              placeholder={protocol === "jt808" ? "jt808_terminal_id" : "IMEI (15 dígitos)"}
              required
              value={identifier}
              onChange={(e) => setIdentifier(e.target.value)}
              pattern={usesGT06Imei(protocol) ? "[0-9]{15}" : undefined}
              title={usesGT06Imei(protocol) ? "15 dígitos" : undefined}
              className="col-span-2 sm:col-span-1"
            />
            <Input
              placeholder="Etiqueta (p.ej. Camión 12)"
              required
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              className="col-span-2 sm:col-span-1"
            />
            <Select value={deviceModelId} onChange={(e) => setDeviceModelId(e.target.value)} className="col-span-2 sm:col-span-1">
              <option value="">Sin modelo</option>
              {protocolModels.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </Select>
            <Input
              placeholder="Número de SIM"
              value={simNumber}
              onChange={(e) => setSimNumber(e.target.value)}
              className="col-span-2 sm:col-span-1"
            />
            <Input
              placeholder="Operadora (AT&T, Verizon...)"
              value={simCarrier}
              onChange={(e) => setSimCarrier(e.target.value)}
              className="col-span-2 sm:col-span-1"
            />
          </div>

          <Field label="Notas">
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={2}
              className="block w-full rounded-sm border border-line-strong bg-surface-2 px-2.5 py-1.5 text-sm text-ink outline-none focus:border-brand-600 focus:ring-1 focus:ring-brand-600"
            />
          </Field>

          <Button type="submit" disabled={!tenantId} className="px-3 py-1 text-xs">
            Siguiente: vehículo →
          </Button>
        </form>
      )}

      {step === "vehicle" && (
        <div className="space-y-2">
          <p className="text-xs text-ink-dim">¿A qué vehículo pertenece esta unidad?</p>
          <OnboardStepPicker
            value={vehicleChoice}
            onChange={(c) => {
              setVehicleChoice(c);
              if (c !== "existing") setVehicleId("");
            }}
            noneLabel="Sin vehículo por ahora"
            existingLabel="Elegir existente"
            newLabel="+ Nuevo vehículo"
          />

          {vehicleChoice === "existing" && (
            <Select value={vehicleId} onChange={(e) => setVehicleId(e.target.value)}>
              <option value="">Selecciona un vehículo...</option>
              {tenantVehicles.map((v) => (
                <option key={v.id} value={v.id}>
                  {[v.plate, v.make, v.model].filter(Boolean).join(" ") || v.id}
                </option>
              ))}
            </Select>
          )}

          {vehicleChoice === "new" && (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Input placeholder="Placa" value={newVehiclePlate} onChange={(e) => setNewVehiclePlate(e.target.value)} />
              <Input placeholder="Marca" value={newVehicleMake} onChange={(e) => setNewVehicleMake(e.target.value)} />
              <Input placeholder="Modelo" value={newVehicleModel} onChange={(e) => setNewVehicleModel(e.target.value)} />
              <Input placeholder="Año" type="number" value={newVehicleYear} onChange={(e) => setNewVehicleYear(e.target.value)} />
            </div>
          )}

          <div className="flex gap-2">
            <Button variant="secondary" className="px-3 py-1 text-xs" onClick={() => setStep("device")}>
              ← Atrás
            </Button>
            <Button disabled={busy} className="px-3 py-1 text-xs" onClick={confirmVehicleStep}>
              {vehicleChoice === "new" ? "Crear vehículo y continuar →" : "Siguiente: chofer →"}
            </Button>
          </div>
        </div>
      )}

      {step === "driver" && (
        <div className="space-y-2">
          {!vehicleId ? (
            <p className="text-xs text-ink-faint">
              Sin vehículo asignado todavía -- un chofer se asigna al vehículo, no directo a la unidad. Vuelve al paso
              anterior si quieres asignar un vehículo primero.
            </p>
          ) : (
            <>
              <p className="text-xs text-ink-dim">¿Quién conduce este vehículo?</p>
              <OnboardStepPicker
                value={driverChoice}
                onChange={(c) => {
                  setDriverChoice(c);
                  if (c !== "existing") setDriverId("");
                }}
                noneLabel="Sin chofer por ahora"
                existingLabel="Elegir existente"
                newLabel="+ Nuevo chofer"
              />
              {driverChoice === "existing" && (
                <Select value={driverId} onChange={(e) => setDriverId(e.target.value)}>
                  <option value="">Selecciona un chofer...</option>
                  {drivers.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </Select>
              )}
              {driverChoice === "new" && (
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                  <Input placeholder="Nombre completo" value={newDriverName} onChange={(e) => setNewDriverName(e.target.value)} />
                  <Input placeholder="Licencia" value={newDriverLicense} onChange={(e) => setNewDriverLicense(e.target.value)} />
                  <Input placeholder="Teléfono" value={newDriverPhone} onChange={(e) => setNewDriverPhone(e.target.value)} />
                </div>
              )}
            </>
          )}
          <div className="flex gap-2">
            <Button variant="secondary" className="px-3 py-1 text-xs" onClick={() => setStep("vehicle")}>
              ← Atrás
            </Button>
            <Button disabled={busy} className="px-3 py-1 text-xs" onClick={confirmDriverStep}>
              Siguiente: grupo →
            </Button>
          </div>
        </div>
      )}

      {step === "group" && (
        <div className="space-y-2">
          <p className="text-xs text-ink-dim">¿Pertenece a algún grupo de dispositivos?</p>
          <OnboardStepPicker
            value={groupChoice}
            onChange={(c) => {
              setGroupChoice(c);
              if (c !== "existing") setGroupId("");
            }}
            noneLabel="Sin grupo por ahora"
            existingLabel="Elegir existente"
            newLabel="+ Nuevo grupo"
          />
          {groupChoice === "existing" && (
            <Select value={groupId} onChange={(e) => setGroupId(e.target.value)}>
              <option value="">Selecciona un grupo...</option>
              {groups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name} ({g.device_count})
                </option>
              ))}
            </Select>
          )}
          {groupChoice === "new" && (
            <Input placeholder="Nombre del grupo" value={newGroupName} onChange={(e) => setNewGroupName(e.target.value)} />
          )}
          <div className="flex gap-2">
            <Button variant="secondary" className="px-3 py-1 text-xs" onClick={() => setStep("driver")}>
              ← Atrás
            </Button>
            <Button disabled={busy} className="px-3 py-1 text-xs" onClick={finish}>
              Crear dispositivo
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function DeviceEditRow({
  device,
  vehicles,
  deviceModels,
  onDone,
  onSaved,
}: {
  device: Device;
  vehicles: Vehicle[];
  deviceModels: DeviceModel[];
  onDone: () => void;
  onSaved: () => void;
}) {
  const [label, setLabel] = useState(device.label);
  const [vehicleId, setVehicleId] = useState(device.vehicle_id ?? "");
  const [notes, setNotes] = useState(device.notes ?? "");
  const [devStatus, setDevStatus] = useState(device.status);
  const [deviceModelId, setDeviceModelId] = useState(device.device_model_id ?? "");
  const [simNumber, setSimNumber] = useState(device.sim_number ?? "");
  const [simCarrier, setSimCarrier] = useState(device.sim_carrier ?? "");
  // sim_plan_cost_mxn_month/sim_plan_data_cap_mb NEVER come in `device` (see
  // Device in lib/api.ts -- platform only, read back only in Billing → SIM
  // usage) -- they always start empty; "save" without touching them leaves the
  // real value intact (COALESCE on the backend).
  const [simPlanCost, setSimPlanCost] = useState("");
  const [simPlanCapMb, setSimPlanCapMb] = useState("");
  // Protocol change gt06<->gt06_video (same IMEI, see DeviceUpdate.protocol) --
  // only applies to devices that ALREADY are one of these two, never jt808 (that
  // still requires removing and re-adding the device).
  const canChangeProtocol = device.protocol === "gt06" || device.protocol === "gt06_video";
  const [protocol, setProtocol] = useState<"gt06" | "gt06_video">(
    device.protocol === "gt06_video" ? "gt06_video" : "gt06",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const tenantVehicles = vehicles.filter((v) => v.tenant_id === device.tenant_id);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await api.updateDevice(device.id, {
        label,
        vehicle_id: vehicleId || undefined,
        notes,
        status: devStatus as "active" | "inactive" | "maintenance",
        ...(canChangeProtocol ? { protocol } : {}),
        ...(deviceModelId ? { device_model_id: deviceModelId } : {}),
        ...(simNumber ? { sim_number: simNumber } : {}),
        ...(simCarrier ? { sim_carrier: simCarrier } : {}),
        ...(simPlanCost ? { sim_plan_cost_mxn_month: Number(simPlanCost) } : {}),
        ...(simPlanCapMb ? { sim_plan_data_cap_mb: Number(simPlanCapMb) } : {}),
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
      <td colSpan={9} className="py-2">
        <div className="space-y-2 border border-brand-600/30 bg-surface-2 p-3">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Input placeholder="Etiqueta" value={label} onChange={(e) => setLabel(e.target.value)} />
            <Select value={vehicleId} onChange={(e) => setVehicleId(e.target.value)} className="col-span-2 sm:col-span-1">
              <option value="">Sin vehículo</option>
              {tenantVehicles.map((v) => (
                <option key={v.id} value={v.id}>
                  {[v.plate, v.make, v.model].filter(Boolean).join(" ") || v.id}
                </option>
              ))}
            </Select>
            <Select value={devStatus} onChange={(e) => setDevStatus(e.target.value)}>
              <option value="active">Activo</option>
              <option value="inactive">Inactivo (desactivado)</option>
              <option value="maintenance">En mantenimiento</option>
            </Select>
            <Select value={deviceModelId} onChange={(e) => setDeviceModelId(e.target.value)}>
              <option value="">Sin modelo</option>
              {deviceModels
                .filter((m) => m.protocol === device.protocol)
                .map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                  </option>
                ))}
            </Select>
            <Input placeholder="Número de SIM" value={simNumber} onChange={(e) => setSimNumber(e.target.value)} />
            <Input placeholder="Operadora (AT&T, Verizon...)" value={simCarrier} onChange={(e) => setSimCarrier(e.target.value)} />
            <Input
              type="number"
              min={0}
              step="0.01"
              placeholder="Costo del plan (MXN/mes)"
              value={simPlanCost}
              onChange={(e) => setSimPlanCost(e.target.value)}
            />
            <Input
              type="number"
              min={0}
              placeholder="Tope contratado (MB/mes)"
              value={simPlanCapMb}
              onChange={(e) => setSimPlanCapMb(e.target.value)}
            />
          </div>
          <p className="text-xs text-ink-faint">
            Costo y tope de la línea son exclusivos de plataforma -- se leen en Facturación → Consumo de SIM, nunca
            visibles para el tenant.
          </p>
          {devStatus !== "active" && (
            <p className="text-xs text-ink-faint">
              Un dispositivo no activo no puede iniciar video en vivo ni recibir comandos remotos, y libera su cupo de
              facturación para dar de alta otro en su lugar.
            </p>
          )}
          {canChangeProtocol && (
            <div className="flex items-center gap-2">
              <Select value={protocol} onChange={(e) => setProtocol(e.target.value as "gt06" | "gt06_video")} className="max-w-xs">
                <option value="gt06">GT06 (GPS)</option>
                <option value="gt06_video">GT06 + video (JC261/JC400)</option>
              </Select>
              {protocol !== device.protocol && (
                <p className="text-xs text-ink-faint">
                  Mismo IMEI ({device.gt06_imei}) — solo cambia si cuenta como cámara o como GPS para el cupo/facturación.
                </p>
              )}
            </div>
          )}
          <textarea
            placeholder="Notas"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={2}
            className="block w-full rounded-sm border border-line-strong bg-surface-2 px-2.5 py-1.5 text-sm text-ink outline-none focus:border-brand-600 focus:ring-1 focus:ring-brand-600"
          />
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

// ---------------------------------------------------------------------------
// Brand and policy -- brand logo/name + driver meal window/max hours,
// self-service for the tenant_admin itself (PATCH /tenants/{id}/settings, never
// touches quota/billing).
// ---------------------------------------------------------------------------

export function BrandingSection({
  tenants,
  isPlatform,
  ownTenantId,
}: {
  tenants: Tenant[];
  isPlatform: boolean;
  ownTenantId: string | null;
}) {
  // A tenant_admin only edits ITS OWN tenant (no picker); the platform chooses
  // which tenant to configure -- same as UsersSection choosing which tenant to
  // add a user to. `tenants` arrives empty on the first render (loaded async in
  // Dashboard()), so auto-selecting the first tenant for the platform cannot
  // happen in the useState initializer (the picker would stay on "Select..."
  // forever) -- it must be re-evaluated when `tenants` stops being empty.
  const [selectedId, setSelectedId] = useState(ownTenantId ?? "");
  useEffect(() => {
    if (!isPlatform && ownTenantId) {
      setSelectedId(ownTenantId);
    } else if (isPlatform && !selectedId && tenants.length > 0) {
      setSelectedId(tenants[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPlatform, ownTenantId, tenants]);

  const tenant = tenants.find((t) => t.id === selectedId) ?? null;

  return (
    <Card>
      <CardTitle>Marca y política</CardTitle>
      {isPlatform && (
        <div className="mb-3">
          <Field label="Tenant">
            <Select value={selectedId} onChange={(e) => setSelectedId(e.target.value)}>
              <option value="" disabled>
                Selecciona...
              </option>
              {tenants.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </Select>
          </Field>
        </div>
      )}
      {tenant ? <BrandingForm key={tenant.id} tenant={tenant} /> : <EmptyState>Sin tenant seleccionado.</EmptyState>}
    </Card>
  );
}

function BrandingForm({ tenant }: { tenant: Tenant }) {
  const [displayName, setDisplayName] = useState(tenant.display_name ?? "");
  const [logoUrl, setLogoUrl] = useState(tenant.logo_url ?? "");
  const [hasMealWindow, setHasMealWindow] = useState(
    tenant.meal_break_window_start != null && tenant.meal_break_window_end != null,
  );
  const [mealStart, setMealStart] = useState(tenant.meal_break_window_start ?? "12:00");
  const [mealEnd, setMealEnd] = useState(tenant.meal_break_window_end ?? "13:00");
  const [hasMaxHours, setHasMaxHours] = useState(tenant.max_shift_hours != null);
  const [maxHours, setMaxHours] = useState(String(tenant.max_shift_hours ?? 8));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  async function save() {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await api.updateTenantSettings(tenant.id, {
        display_name: displayName.trim() || null,
        logo_url: logoUrl.trim() || null,
        meal_break_window_start: hasMealWindow ? mealStart : null,
        meal_break_window_end: hasMealWindow ? mealEnd : null,
        max_shift_hours: hasMaxHours ? Number(maxHours) : null,
      });
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error guardando la configuración");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      {error && <Alert>{error}</Alert>}
      {saved && !error && <Alert variant="info">Guardado.</Alert>}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Nombre de marca">
          <Input
            placeholder="OpenMDVR (por defecto)"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </Field>
        <Field label="URL del logo">
          <Input placeholder="https://..." value={logoUrl} onChange={(e) => setLogoUrl(e.target.value)} />
        </Field>
      </div>
      <p className="text-xs text-ink-faint">
        Se muestran en el rail de escritorio y en la app de chofer una vez que el usuario inicia sesión — sin subida
        de archivo todavía, pega la URL de una imagen que ya tengas alojada en otro lado.
      </p>

      <div className="space-y-2 border-t border-line pt-3">
        <label className="flex items-center gap-2 text-sm text-ink-dim">
          <input type="checkbox" checked={hasMealWindow} onChange={(e) => setHasMealWindow(e.target.checked)} />
          Restringir horario para salir a comer
        </label>
        {hasMealWindow && (
          <div className="grid grid-cols-2 gap-3 sm:max-w-xs">
            <Field label="Desde (UTC)">
              <input
                type="time"
                value={mealStart}
                onChange={(e) => setMealStart(e.target.value)}
                className="block w-full rounded-sm border border-line-strong bg-surface-2 px-2.5 py-1.5 text-sm text-ink outline-none focus:border-brand-600 focus:ring-1 focus:ring-brand-600"
              />
            </Field>
            <Field label="Hasta (UTC)">
              <input
                type="time"
                value={mealEnd}
                onChange={(e) => setMealEnd(e.target.value)}
                className="block w-full rounded-sm border border-line-strong bg-surface-2 px-2.5 py-1.5 text-sm text-ink outline-none focus:border-brand-600 focus:ring-1 focus:ring-brand-600"
              />
            </Field>
          </div>
        )}
      </div>

      <div className="space-y-2 border-t border-line pt-3">
        <label className="flex items-center gap-2 text-sm text-ink-dim">
          <input type="checkbox" checked={hasMaxHours} onChange={(e) => setHasMaxHours(e.target.checked)} />
          Máximo de horas por turno
        </label>
        {hasMaxHours && (
          <div className="sm:max-w-[140px]">
            <Input
              type="number"
              min="0.5"
              max="48"
              step="0.5"
              value={maxHours}
              onChange={(e) => setMaxHours(e.target.value)}
            />
          </div>
        )}
      </div>

      <p className="text-xs text-ink-faint">
        Fuera de rango o pasado el máximo NUNCA bloquea el registro real de turno de un chofer — genera una alerta
        para revisar en Operación.
      </p>

      <Button disabled={busy} onClick={save} className="px-3 py-1 text-xs">
        {busy ? "Guardando..." : "Guardar"}
      </Button>
    </div>
  );
}

