import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, ApiError, type Tenant, type TenantProfitability, type Vehicle } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Alert, Badge, Card, CardTitle, EmptyState, PageContainer, PageHeader } from "../components/ui";
import {
  BrandingSection,
  DeviceGroupsSection,
  DevicesSection,
  DriversSection,
  UsersSection,
  VehiclesSection,
  WebhookEndpointsSection,
} from "./Dashboard";
import { TenantInvoicingSection, TenantSubscriptionSection } from "./Billing";

// Per-tenant "workspace" page: select a tenant and configure everything about it
// in one place, without a tenant picker repeated in every section (Users,
// Vehicles, Drivers, Devices, Subscription, Invoices).
//
// Reuses the same sections exported from Dashboard.tsx/Billing.tsx -- they are
// invoked with `ownTenantId`/`fixedTenantId` pinned to the URL's tenant instead
// of each showing its own picker. It is also the basis for tenant
// self-management: for a tenant_admin session, `/admin` (App.tsx) renders this
// SAME component pinned to its own tenant, without a tenant list.
//
// Organized in tabs. The Summary (quota/profitability) stays ALWAYS visible on
// top -- lightweight context (a few badges), not a work module -- and the rest
// is grouped into tabs by real task, not by database table: Vehicles and Drivers
// live together (a driver is assigned TO the vehicle, never to the device), and
// Device groups live next to Users (both are "who sees/receives what", not "what
// the device is"). Each tab mounts/unmounts on change (never hidden with CSS) --
// sections already refresh on mount, so switching tabs always brings fresh data
// without extra plumbing.
type WorkspaceTab = "devices" | "fleet" | "access" | "billing" | "branding";
const WORKSPACE_TAB_IDS: WorkspaceTab[] = ["devices", "fleet", "access", "billing", "branding"];

export default function TenantWorkspace() {
  const { tenantId: routeTenantId } = useParams<{ tenantId: string }>();
  const { isPlatform, tenantId: ownSessionTenantId, role } = useAuth();
  const navigate = useNavigate();

  // Without a route parameter (mounted at /admin for a tenant_admin session, see
  // App.tsx) the session's OWN tenant is used -- exactly the same component as
  // the platform workspace, except the tenant never comes from the URL.
  const tenantId = routeTenantId ?? ownSessionTenantId ?? undefined;

  // A tenant_admin session must only reach ITS OWN workspace -- RLS would
  // already block any other tenant's data, but a clear message and a redirect
  // are better UX than an unexplained empty screen (same rule as other gates in
  // this project, e.g. DriverHome/RequireDriver in App.tsx).
  const blockedCrossTenant = !isPlatform && !!routeTenantId && routeTenantId !== ownSessionTenantId;
  useEffect(() => {
    if (blockedCrossTenant) {
      navigate("/admin", { replace: true });
    }
  }, [blockedCrossTenant, navigate]);

  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [vehicles, setVehicles] = useState<Vehicle[]>([]);
  const [profitability, setProfitability] = useState<TenantProfitability | null>(null);
  const [error, setError] = useState<string | null>(null);

  function reloadVehicles() {
    if (!tenantId) return;
    api
      .listVehicles({ tenant_id: tenantId, limit: 1000 })
      .then((r) => setVehicles(r.items))
      .catch(() => setVehicles([]));
  }

  useEffect(() => {
    // Do not fire any query for a tenant you are about to leave through the
    // redirect above -- RLS would return them empty (no real leak), but it would
    // be pointless network traffic toward another tenant. The redirect itself is
    // still UX, not the guarantee.
    if (!tenantId || blockedCrossTenant) return;
    api
      .listTenants({ limit: 1000 })
      .then((r) => setTenants(r.items))
      .catch((err) => setError(err instanceof ApiError ? err.message : "error cargando tenant"));
    reloadVehicles();
    // Quota summary -- only meaningful for a platform session (require_bypass in
    // the backend); for a real tenant_admin this call would 403, so it is
    // skipped entirely.
    if (isPlatform) {
      api
        .listTenantProfitability({ tenant_id: tenantId, limit: 1 })
        .then((r) => setProfitability(r.items[0] ?? null))
        .catch(() => setProfitability(null));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, isPlatform, blockedCrossTenant]);

  const tenant = tenants.find((t) => t.id === tenantId) ?? null;
  // canManage is derived from the role: the backend rejects every write with 403
  // for tenant_operator/tenant_viewer (require_tenant_admin), so the UI must not
  // show "+ new vehicle"/"+ new driver"/"edit" to them -- that would
  // misrepresent permissions on screen.
  const canManage = isPlatform || role === "tenant_admin";
  const canSeeAccessTab = isPlatform || role === "tenant_admin";
  const canSeeBrandingTab = isPlatform || role === "tenant_admin";

  // Count split by protocol -- TenantOut's camera_device_quota/gps_device_quota
  // are two independent pools, so "N of M contracted" needs its own count per
  // category, not the combined total. limit:1000 is the same pragmatic ceiling
  // as MapView/LiveView, not a real scaling solution.
  const [cameraDeviceCount, setCameraDeviceCount] = useState<number | null>(null);
  const [gpsDeviceCount, setGpsDeviceCount] = useState<number | null>(null);

  function reloadDeviceCounts() {
    if (!tenantId) return;
    api
      .listDevices({ tenant_id: tenantId, limit: 1000 })
      .then((r) => {
        // gt06_video (JIMI JC261/JC400) counts as a camera ('camera' category,
        // shares quota with jt808) -- see _PROTOCOL_TO_CATEGORY in
        // api/app/routers/devices.py. Only status==='active' devices count: a
        // deactivated device (soft-delete, the row still exists) must not count
        // against the quota shown here, matching the backend
        // (_assert_device_quota_not_exceeded).
        const active = r.items.filter((d) => d.status === "active");
        setCameraDeviceCount(active.filter((d) => d.protocol === "jt808" || d.protocol === "gt06_video").length);
        setGpsDeviceCount(active.filter((d) => d.protocol === "gt06").length);
      })
      .catch(() => {
        setCameraDeviceCount(null);
        setGpsDeviceCount(null);
      });
  }

  useEffect(() => {
    if (!tenantId || blockedCrossTenant) return;
    reloadDeviceCounts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, blockedCrossTenant]);

  // `?tab=fleet` (link from DeviceDetailPanel.tsx: the speed limit lives on the
  // VEHICLE, not the device, so the unit panel links straight to this tab) --
  // initial value only, never synced back to the URL when switching tabs by
  // click (this view is not meant to be shared by link).
  const [searchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const [activeTab, setActiveTab] = useState<WorkspaceTab>(
    WORKSPACE_TAB_IDS.includes(requestedTab as WorkspaceTab) ? (requestedTab as WorkspaceTab) : "devices",
  );

  const allTabs: { id: WorkspaceTab; label: string; visible: boolean }[] = [
    { id: "devices", label: "Dispositivos", visible: true },
    { id: "fleet", label: "Vehículos y choferes", visible: true },
    { id: "access", label: "Usuarios y accesos", visible: canSeeAccessTab },
    { id: "billing", label: "Facturación", visible: isPlatform },
    { id: "branding", label: "Marca y política", visible: canSeeBrandingTab },
  ];
  const tabs = allTabs.filter((t) => t.visible);

  // If the role loses access to the active tab (e.g. the session changes in the
  // same browser tab), fall back to "Devices" -- always visible to any role --
  // instead of staying on an empty tab with no way out.
  useEffect(() => {
    if (!tabs.some((t) => t.id === activeTab)) setActiveTab("devices");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs.map((t) => t.id).join(",")]);

  if (!tenantId) return null;

  return (
    <PageContainer>
      {isPlatform && (
        <Link to="/admin" className="mb-2 inline-block text-xs text-ink-dim hover:text-ink hover:underline">
          ← Volver a Tenants
        </Link>
      )}
      <PageHeader
        title={tenant?.name ?? "Cargando…"}
        description={tenant ? `Estado: ${tenant.status}` : undefined}
      />

      {error && <Alert>{error}</Alert>}

      {!tenant ? (
        <EmptyState>Cargando…</EmptyState>
      ) : (
        <div className="space-y-4">
          <Card>
            <CardTitle>Resumen</CardTitle>
            <div className="flex flex-wrap gap-6 text-sm">
              <div>
                <div className="text-xs text-ink-faint uppercase">Cámaras (JT808)</div>
                <div className="mt-1">
                  {cameraDeviceCount === null ? (
                    "—"
                  ) : (
                    <Badge
                      tone={
                        cameraDeviceCount > tenant.camera_device_quota
                          ? "danger"
                          : cameraDeviceCount === tenant.camera_device_quota
                            ? "warning"
                            : "success"
                      }
                    >
                      {cameraDeviceCount} de {tenant.camera_device_quota} contratados
                    </Badge>
                  )}
                </div>
              </div>
              <div>
                <div className="text-xs text-ink-faint uppercase">GPS (GT06)</div>
                <div className="mt-1">
                  {gpsDeviceCount === null ? (
                    "—"
                  ) : (
                    <Badge
                      tone={
                        gpsDeviceCount > tenant.gps_device_quota
                          ? "danger"
                          : gpsDeviceCount === tenant.gps_device_quota
                            ? "warning"
                            : "success"
                      }
                    >
                      {gpsDeviceCount} de {tenant.gps_device_quota} contratados
                    </Badge>
                  )}
                </div>
              </div>
              {isPlatform && (
                <div>
                  <div className="text-xs text-ink-faint uppercase">Rentabilidad estimada</div>
                  <div className="mt-1">
                    {profitability ? (
                      <Badge
                        tone={
                          profitability.margin_pct === null
                            ? "muted"
                            : profitability.margin_pct < 0
                              ? "danger"
                              : profitability.margin_pct < 30
                                ? "warning"
                                : "success"
                        }
                      >
                        {profitability.margin_pct === null ? "sin ingreso" : `${profitability.margin_pct.toFixed(1)}% margen`}
                      </Badge>
                    ) : (
                      "—"
                    )}
                  </div>
                </div>
              )}
            </div>
          </Card>

          <div className="flex flex-wrap gap-1 border-b border-line">
            {tabs.map((t) => (
              <button
                key={t.id}
                onClick={() => setActiveTab(t.id)}
                className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
                  activeTab === t.id
                    ? "border-brand-600 text-brand-500"
                    : "border-transparent text-ink-dim hover:text-ink"
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>

          {activeTab === "devices" && (
            <DevicesSection
              tenants={[tenant]}
              vehicles={vehicles}
              isPlatform={isPlatform}
              ownTenantId={tenant.id}
              onVehicleCreated={reloadVehicles}
              onDeviceCreated={reloadDeviceCounts}
            />
          )}

          {activeTab === "fleet" && (
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <VehiclesSection tenants={[tenant]} canManage={canManage} ownTenantId={tenant.id} onChanged={reloadVehicles} />
              <DriversSection tenants={[tenant]} canManage={canManage} ownTenantId={tenant.id} />
            </div>
          )}

          {activeTab === "access" && canSeeAccessTab && (
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <UsersSection
                tenants={[tenant]}
                isPlatform={false}
                ownTenantId={tenant.id}
                defaultTenantId={null}
                canManage={canManage}
              />
              <DeviceGroupsSection tenantId={tenant.id} canManage={canManage} />

              {/*
               * Outbound webhooks -- requires PLATFORM approval
               * (tenant.webhooks_enabled, see WebhooksEnabledToggle in
               * Administration -> Tenants). Without it the section is not even
               * shown -- creating an endpoint would fail on the backend anyway
               * (trigger enforce_webhook_endpoint_tenant_enabled), but showing
               * the button would misrepresent what this tenant can do today.
               */}
              {tenant.webhooks_enabled && (
                <div className="lg:col-span-2">
                  <WebhookEndpointsSection tenantId={tenant.id} canManage={canManage} />
                </div>
              )}
            </div>
          )}

          {activeTab === "billing" && isPlatform && (
            <div className="space-y-6">
              <TenantSubscriptionSection tenants={[tenant]} fixedTenantId={tenant.id} />
              <TenantInvoicingSection tenants={[tenant]} fixedTenantId={tenant.id} />
            </div>
          )}

          {activeTab === "branding" && canSeeBrandingTab && (
            <BrandingSection tenants={[tenant]} isPlatform={false} ownTenantId={tenant.id} />
          )}
        </div>
      )}
    </PageContainer>
  );
}
