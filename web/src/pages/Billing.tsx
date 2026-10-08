import { useEffect, useState, type FormEvent } from "react";
import {
  api,
  ApiError,
  type BillingPeriod,
  type BillingPlan,
  type BillingPlanCategory,
  type DeviceDataUsage,
  type Invoice,
  type InvoiceStatus,
  type PageResult,
  type Payment,
  type PaymentMethod,
  type PlatformBillingSettings,
  type PromotionDiscountType,
  type Tenant,
  type TenantProfitability,
  type TenantPromotion,
  type TenantSubscriptionItem,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import { Alert, Badge, Button, Card, CardTitle, EmptyState, Field, Input, PageContainer, PageHeader, Pagination, Select } from "../components/ui";

// Billing page, with two completely different views depending on role:
// - Platform (super_admin/support): catalog, subscriptions, invoices, payments,
//   promotions and profitability.
// - tenant_admin: "My billing" -- its own resolved subscription (GET
//   /billing/my-subscription, a narrow view), its invoices and its payments,
//   both through the same GET /billing/invoices and GET /billing/payments
//   (require_tenant_admin; RLS resolves "mine" without a parameter).
//   tenant_operator/tenant_viewer reach neither view (require_tenant_admin in
//   the backend) -- they get a message instead of a broken or unexplained empty
//   screen.
export default function Billing() {
  const { isPlatform, role } = useAuth();

  return (
    <PageContainer>
      <PageHeader
        title="Facturación"
        description={isPlatform ? "Catálogo, suscripciones, facturas, pagos, promociones y rentabilidad." : "Tu plan, tus facturas y tus pagos."}
      />
      {isPlatform ? (
        <PlatformBillingView />
      ) : role === "tenant_admin" ? (
        <MyBillingSection />
      ) : (
        <EmptyState>Esta sección es solo para administradores de tenant o de plataforma.</EmptyState>
      )}
    </PageContainer>
  );
}

function PlatformBillingView() {
  const [tenants, setTenants] = useState<Tenant[]>([]);

  useEffect(() => {
    // Full roster (same pragmatic 1000 ceiling as Dashboard.tsx, see
    // web/README.md), only for the tenant pickers below.
    api
      .listTenants({ limit: 1000 })
      .then((r) => setTenants(r.items))
      .catch(() => setTenants([]));
  }, []);

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
      <BillingPlansSection />
      <div className="lg:col-span-2">
        <TenantSubscriptionSection tenants={tenants} />
      </div>
      <div className="lg:col-span-2">
        <TenantInvoicingSection tenants={tenants} />
      </div>
      <div className="lg:col-span-2">
        <ProfitabilitySection />
      </div>
      <div className="lg:col-span-2">
        <SimUsageSection />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// "My billing" -- tenant_admin view. No tenant picker (unlike the platform
// sections below): always resolves the caller's own session.
// ---------------------------------------------------------------------------
function MyBillingSection() {
  const [items, setItems] = useState<TenantSubscriptionItem[]>([]);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [payments, setPayments] = useState<Payment[]>([]);
  const [error, setError] = useState<string | null>(null);

  async function reload() {
    try {
      const [subs, inv, pay] = await Promise.all([
        api.getMySubscription(),
        api.listInvoices({ limit: 100 }),
        api.listPayments({ limit: 100 }),
      ]);
      setItems(subs);
      setInvoices(inv.items);
      setPayments(pay.items);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando tu facturación");
    }
  }

  useEffect(() => {
    reload();
  }, []);

  const activeItems = items.filter((i) => i.ended_at === null);

  return (
    <div className="space-y-6">
      {error && <Alert>{error}</Alert>}

      <Card>
        <CardTitle>Tu plan contratado</CardTitle>
        {activeItems.length === 0 ? (
          <EmptyState>No tienes ninguna línea contratada todavía.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                  <th className="py-1.5 pr-2 font-medium">Descripción</th>
                  <th className="py-1.5 pr-2 font-medium">Cantidad</th>
                  <th className="py-1.5 font-medium">Precio unitario</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {activeItems.map((item) => (
                  <tr key={item.id}>
                    <td className="py-2 pr-2 align-top text-ink">
                      {item.plan_name ?? item.custom_description}
                      {item.plan_sku && <span className="ml-1 font-data text-xs text-ink-faint">({item.plan_sku})</span>}
                    </td>
                    <td className="py-2 pr-2 align-top font-data text-ink-dim">{item.quantity}</td>
                    <td className="py-2 align-top font-data text-ink-dim">${item.effective_unit_price.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card>
        <CardTitle>Tus facturas</CardTitle>
        {invoices.length === 0 ? (
          <EmptyState>No tienes ninguna factura todavía.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                  <th className="py-1.5 pr-2 font-medium">Período</th>
                  <th className="py-1.5 pr-2 font-medium">Total</th>
                  <th className="py-1.5 pr-2 font-medium">Vence</th>
                  <th className="py-1.5 font-medium">Estado</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {invoices.map((inv) => (
                  <tr key={inv.id}>
                    <td className="py-2 pr-2 align-top font-data text-xs text-ink-dim">
                      {inv.period_start} → {inv.period_end}
                    </td>
                    <td className="py-2 pr-2 align-top font-data text-ink">${inv.total.toFixed(2)}</td>
                    <td className="py-2 pr-2 align-top font-data text-xs text-ink-faint">{inv.due_date}</td>
                    <td className="py-2 align-top">
                      <Badge tone={INVOICE_STATUS_TONE[inv.status]}>{INVOICE_STATUS_LABEL[inv.status]}</Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card>
        <CardTitle>Tus pagos</CardTitle>
        {payments.length === 0 ? (
          <EmptyState>No tienes ningún pago registrado todavía.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                  <th className="py-1.5 pr-2 font-medium">Fecha</th>
                  <th className="py-1.5 pr-2 font-medium">Monto</th>
                  <th className="py-1.5 pr-2 font-medium">Método</th>
                  <th className="py-1.5 font-medium">Referencia</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {payments.map((p) => (
                  <tr key={p.id}>
                    <td className="py-2 pr-2 align-top font-data text-xs text-ink-dim">{p.received_at.slice(0, 10)}</td>
                    <td className="py-2 pr-2 align-top font-data text-ink">${p.amount.toFixed(2)}</td>
                    <td className="py-2 pr-2 align-top text-ink-dim">{PAYMENT_METHOD_LABEL[p.method]}</td>
                    <td className="py-2 align-top text-ink-faint">{p.reference_note ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Platform view (super_admin/support).
// ---------------------------------------------------------------------------

const BILLING_CATEGORY_LABEL: Record<BillingPlanCategory, string> = {
  gps: "GPS",
  camera: "Cámara",
  addon: "Addon",
};
const BILLING_PERIOD_LABEL: Record<BillingPeriod, string> = {
  monthly: "Mensual",
  semiannual: "Semestral",
  annual: "Anual",
};
const BILLING_PERIODS: BillingPeriod[] = ["monthly", "semiannual", "annual"];
const BILLING_CATEGORIES: BillingPlanCategory[] = ["gps", "camera", "addon"];

function BillingPlansSection() {
  const [plans, setPlans] = useState<BillingPlan[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);

  async function reload() {
    try {
      setPlans((await api.listBillingPlans({ limit: 200 })).items);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando planes");
    }
  }

  useEffect(() => {
    reload();
  }, []);

  return (
    <Card>
      <CardTitle
        action={
          <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setShowCreate((v) => !v)}>
            {showCreate ? "Cancelar" : "+ Nuevo plan"}
          </Button>
        }
      >
        Catálogo de facturación
      </CardTitle>

      {showCreate && (
        <div className="mb-3">
          <BillingPlanCreateForm
            onCreated={() => {
              setShowCreate(false);
              reload();
            }}
          />
        </div>
      )}

      {error && <Alert>{error}</Alert>}

      {plans.length === 0 ? (
        <EmptyState>Sin planes en el catálogo todavía.</EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                <th className="py-1.5 pr-2 font-medium">SKU</th>
                <th className="py-1.5 pr-2 font-medium">Nombre</th>
                <th className="py-1.5 pr-2 font-medium">Categoría</th>
                <th className="py-1.5 pr-2 font-medium">Precio</th>
                <th className="py-1.5 pr-2 font-medium">Período</th>
                <th className="py-1.5 font-medium">Activo</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {plans.map((p) => (
                <BillingPlanRow key={p.id} plan={p} onChanged={reload} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function BillingPlanCreateForm({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [sku, setSku] = useState("");
  const [category, setCategory] = useState<BillingPlanCategory>("gps");
  const [unitPrice, setUnitPrice] = useState("");
  const [billingPeriod, setBillingPeriod] = useState<BillingPeriod>("monthly");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const price = Number(unitPrice);
    if (!Number.isFinite(price) || price < 0) {
      setError("precio inválido");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.createBillingPlan({ name, sku, category, unit_price: price, billing_period: billingPeriod });
      setName("");
      setSku("");
      setUnitPrice("");
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error creando plan");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-2 border border-line bg-surface-2 p-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        <Field label="Nombre">
          <Input required value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="SKU">
          <Input required value={sku} onChange={(e) => setSku(e.target.value)} />
        </Field>
        <Field label="Categoría">
          <Select value={category} onChange={(e) => setCategory(e.target.value as BillingPlanCategory)}>
            {BILLING_CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {BILLING_CATEGORY_LABEL[c]}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Precio (MXN)">
          <Input required type="number" min="0" step="0.01" value={unitPrice} onChange={(e) => setUnitPrice(e.target.value)} />
        </Field>
        <Field label="Período">
          <Select value={billingPeriod} onChange={(e) => setBillingPeriod(e.target.value as BillingPeriod)}>
            {BILLING_PERIODS.map((p) => (
              <option key={p} value={p}>
                {BILLING_PERIOD_LABEL[p]}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={busy} className="px-3 py-1 text-xs">
          Crear plan
        </Button>
        {error && <span className="text-xs text-red-300">{error}</span>}
      </div>
    </form>
  );
}

function BillingPlanRow({ plan, onChanged }: { plan: BillingPlan; onChanged: () => void }) {
  const [price, setPrice] = useState(String(plan.unit_price));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dirty = price !== String(plan.unit_price);

  async function savePrice() {
    const value = Number(price);
    if (!Number.isFinite(value) || value < 0) {
      setError("precio inválido");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.updateBillingPlan(plan.id, { unit_price: value });
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error guardando");
    } finally {
      setBusy(false);
    }
  }

  async function toggleActive() {
    setBusy(true);
    try {
      await api.updateBillingPlan(plan.id, { active: !plan.active });
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error guardando");
    } finally {
      setBusy(false);
    }
  }

  return (
    <tr>
      <td className="py-2 pr-2 align-top font-data text-ink-dim">{plan.sku}</td>
      <td className="py-2 pr-2 align-top text-ink">{plan.name}</td>
      <td className="py-2 pr-2 align-top text-ink-dim">{BILLING_CATEGORY_LABEL[plan.category]}</td>
      <td className="py-2 pr-2 align-top">
        <div className="flex items-center gap-1.5">
          <Input
            type="number"
            min="0"
            step="0.01"
            value={price}
            onChange={(e) => setPrice(e.target.value)}
            className="w-24 py-1 text-xs"
          />
          {dirty && (
            <Button variant="secondary" disabled={busy} onClick={savePrice} className="px-2 py-1 text-xs">
              Guardar
            </Button>
          )}
        </div>
        {error && <p className="mt-1 text-xs text-red-300">{error}</p>}
      </td>
      <td className="py-2 pr-2 align-top text-ink-dim">{BILLING_PERIOD_LABEL[plan.billing_period]}</td>
      <td className="py-2 align-top">
        <Button variant="secondary" disabled={busy} onClick={toggleActive} className="px-2 py-1 text-xs">
          {plan.active ? "Desactivar" : "Activar"}
        </Button>
      </td>
    </tr>
  );
}

export function TenantSubscriptionSection({
  tenants,
  fixedTenantId,
}: {
  tenants: Tenant[];
  // When pinned (TenantWorkspace.tsx: already inside ONE tenant's workspace),
  // the redundant picker is hidden instead of showing a single-option <select>.
  fixedTenantId?: string;
}) {
  const [selectedId, setSelectedId] = useState(fixedTenantId ?? "");
  useEffect(() => {
    if (!fixedTenantId && !selectedId && tenants.length > 0) setSelectedId(tenants[0].id);
  }, [selectedId, tenants, fixedTenantId]);

  const [items, setItems] = useState<TenantSubscriptionItem[]>([]);
  const [plans, setPlans] = useState<BillingPlan[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);

  async function reload() {
    if (!selectedId) return;
    try {
      setItems((await api.listSubscriptionItems({ tenant_id: selectedId, limit: 200 })).items);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando suscripción");
    }
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  useEffect(() => {
    api
      .listBillingPlans({ active_only: true, limit: 200 })
      .then(({ items }) => setPlans(items))
      .catch(() => setPlans([]));
  }, []);

  const activeItems = items.filter((i) => i.ended_at === null);
  const endedItems = items.filter((i) => i.ended_at !== null);

  return (
    <Card>
      <CardTitle
        action={
          <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setShowCreate((v) => !v)}>
            {showCreate ? "Cancelar" : "+ Nueva línea"}
          </Button>
        }
      >
        Suscripción por tenant
      </CardTitle>

      {!fixedTenantId && (
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

      {error && <Alert>{error}</Alert>}

      {showCreate && selectedId && (
        <div className="mb-3">
          <SubscriptionItemCreateForm
            tenantId={selectedId}
            plans={plans}
            onCreated={() => {
              setShowCreate(false);
              reload();
            }}
          />
        </div>
      )}

      {!selectedId ? (
        <EmptyState>Sin tenant seleccionado.</EmptyState>
      ) : activeItems.length === 0 ? (
        <EmptyState>Este tenant no tiene ninguna línea contratada todavía.</EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                <th className="py-1.5 pr-2 font-medium">Descripción</th>
                <th className="py-1.5 pr-2 font-medium">Cantidad</th>
                <th className="py-1.5 pr-2 font-medium">Precio unitario</th>
                <th className="py-1.5 font-medium" />
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {activeItems.map((item) => (
                <SubscriptionItemRow key={item.id} item={item} onChanged={reload} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {endedItems.length > 0 && (
        <p className="mt-3 text-xs text-ink-faint">{endedItems.length} línea(s) terminada(s) en el historial.</p>
      )}
    </Card>
  );
}

function SubscriptionItemCreateForm({
  tenantId,
  plans,
  onCreated,
}: {
  tenantId: string;
  plans: BillingPlan[];
  onCreated: () => void;
}) {
  const [billingPlanId, setBillingPlanId] = useState("");
  const [customDescription, setCustomDescription] = useState("");
  // Required only when NO plan is chosen -- a line with a plan inherits its
  // category from billing_plans.category (see TenantSubscriptionItemCreate in
  // schemas.py).
  const [category, setCategory] = useState<BillingPlanCategory>("camera");
  const [quantity, setQuantity] = useState("1");
  const [priceOverride, setPriceOverride] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const qty = Number(quantity);
    if (!Number.isInteger(qty) || qty < 1) {
      setError("cantidad inválida");
      return;
    }
    if (!billingPlanId && !customDescription.trim()) {
      setError("elegí un plan o escribí una descripción personalizada");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.createSubscriptionItem({
        tenant_id: tenantId,
        billing_plan_id: billingPlanId || undefined,
        custom_description: customDescription.trim() || undefined,
        category: billingPlanId ? undefined : category,
        quantity: qty,
        unit_price_override: priceOverride.trim() ? Number(priceOverride) : undefined,
      });
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error creando línea");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-2 border border-line bg-surface-2 p-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        <Field label="Plan del catálogo">
          <Select value={billingPlanId} onChange={(e) => setBillingPlanId(e.target.value)}>
            <option value="">(línea personalizada)</option>
            {plans.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} — {p.unit_price} {p.currency}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Descripción personalizada">
          <Input
            placeholder="Solo si no eligió un plan"
            value={customDescription}
            onChange={(e) => setCustomDescription(e.target.value)}
            disabled={!!billingPlanId}
          />
        </Field>
        <Field label="Categoría">
          <Select
            value={category}
            onChange={(e) => setCategory(e.target.value as BillingPlanCategory)}
            disabled={!!billingPlanId}
          >
            {BILLING_CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {BILLING_CATEGORY_LABEL[c]}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Cantidad">
          <Input type="number" min="1" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
        </Field>
        <Field label="Precio distinto (opcional)">
          <Input
            type="number"
            min="0"
            step="0.01"
            placeholder="usa el de lista"
            value={priceOverride}
            onChange={(e) => setPriceOverride(e.target.value)}
          />
        </Field>
      </div>
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={busy} className="px-3 py-1 text-xs">
          Agregar línea
        </Button>
        {error && <span className="text-xs text-red-300">{error}</span>}
      </div>
    </form>
  );
}

function SubscriptionItemRow({ item, onChanged }: { item: TenantSubscriptionItem; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function endItem() {
    setBusy(true);
    setError(null);
    try {
      await api.updateSubscriptionItem(item.id, { end_now: true });
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error guardando");
    } finally {
      setBusy(false);
    }
  }

  return (
    <tr>
      <td className="py-2 pr-2 align-top text-ink">
        {item.plan_name ?? item.custom_description}
        {item.plan_sku && <span className="ml-1 font-data text-xs text-ink-faint">({item.plan_sku})</span>}
      </td>
      <td className="py-2 pr-2 align-top font-data text-ink-dim">{item.quantity}</td>
      <td className="py-2 pr-2 align-top font-data text-ink-dim">
        ${item.effective_unit_price.toFixed(2)}
        {item.unit_price_override != null && <Badge tone="brand">personalizado</Badge>}
      </td>
      <td className="py-2 align-top">
        <Button variant="secondary" disabled={busy} onClick={endItem} className="px-2 py-1 text-xs">
          Terminar
        </Button>
        {error && <p className="mt-1 text-xs text-red-300">{error}</p>}
      </td>
    </tr>
  );
}

const INVOICE_STATUS_LABEL: Record<InvoiceStatus, string> = {
  draft: "Borrador",
  issued: "Emitida",
  paid: "Pagada",
  overdue: "Vencida",
  void: "Anulada",
};

const INVOICE_STATUS_TONE: Record<InvoiceStatus, "neutral" | "success" | "muted" | "warning" | "danger"> = {
  draft: "muted",
  issued: "warning",
  paid: "success",
  overdue: "danger",
  void: "neutral",
};

const PROMOTION_TYPE_LABEL: Record<PromotionDiscountType, string> = {
  full_waiver: "Exención total",
  percentage: "Porcentaje",
  fixed_amount: "Monto fijo",
};

export function TenantInvoicingSection({
  tenants,
  fixedTenantId,
}: {
  tenants: Tenant[];
  fixedTenantId?: string;
}) {
  const [selectedId, setSelectedId] = useState(fixedTenantId ?? "");
  useEffect(() => {
    if (!fixedTenantId && !selectedId && tenants.length > 0) setSelectedId(tenants[0].id);
  }, [selectedId, tenants, fixedTenantId]);

  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [promotions, setPromotions] = useState<TenantPromotion[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showPromoForm, setShowPromoForm] = useState(false);

  async function reload() {
    if (!selectedId) return;
    try {
      const [inv, promo] = await Promise.all([
        api.listInvoices({ tenant_id: selectedId, limit: 100 }),
        api.listPromotions({ tenant_id: selectedId, limit: 100 }),
      ]);
      setInvoices(inv.items);
      setPromotions(promo.items);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando facturación");
    }
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  return (
    <Card>
      <CardTitle
        action={
          <Button variant="secondary" className="px-2 py-1 text-xs" onClick={() => setShowPromoForm((v) => !v)}>
            {showPromoForm ? "Cancelar" : "+ Nueva promoción"}
          </Button>
        }
      >
        Promociones y facturas
      </CardTitle>

      {!fixedTenantId && (
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

      {error && <Alert>{error}</Alert>}

      {showPromoForm && selectedId && (
        <div className="mb-4">
          <PromotionCreateForm
            tenantId={selectedId}
            onCreated={() => {
              setShowPromoForm(false);
              reload();
            }}
          />
        </div>
      )}

      {!selectedId ? (
        <EmptyState>Sin tenant seleccionado.</EmptyState>
      ) : (
        <>
          <h3 className="mb-2 text-xs font-semibold tracking-wide text-ink-faint uppercase">Facturas</h3>
          {invoices.length === 0 ? (
            <EmptyState>Este tenant no tiene ninguna factura todavía.</EmptyState>
          ) : (
            <div className="mb-4 overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                    <th className="py-1.5 pr-2 font-medium">Período</th>
                    <th className="py-1.5 pr-2 font-medium">Subtotal</th>
                    <th className="py-1.5 pr-2 font-medium">Descuento</th>
                    <th className="py-1.5 pr-2 font-medium">Total</th>
                    <th className="py-1.5 pr-2 font-medium">Vence</th>
                    <th className="py-1.5 pr-2 font-medium">Estado</th>
                    <th className="py-1.5 font-medium" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {invoices.map((inv) => (
                    <InvoiceRow key={inv.id} invoice={inv} onChanged={reload} />
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h3 className="mb-2 text-xs font-semibold tracking-wide text-ink-faint uppercase">Promociones</h3>
          {promotions.length === 0 ? (
            <EmptyState>Este tenant no tiene ninguna promoción registrada.</EmptyState>
          ) : (
            <ul className="space-y-1 text-sm text-ink-dim">
              {promotions.map((p) => (
                <li key={p.id}>
                  {p.description} — {PROMOTION_TYPE_LABEL[p.discount_type]}
                  {p.discount_type !== "full_waiver" && (
                    <> ({p.discount_type === "percentage" ? `${p.discount_value}%` : `$${p.discount_value.toFixed(2)}`})</>
                  )}{" "}
                  <span className="font-data text-xs text-ink-faint">
                    {p.starts_at} → {p.ends_at}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </Card>
  );
}

const PAYMENT_METHOD_LABEL: Record<PaymentMethod, string> = {
  cash: "Efectivo",
  bank_transfer: "Transferencia",
  stripe: "Stripe",
  mercado_pago: "Mercado Pago",
  other: "Otro",
};

function InvoiceRow({ invoice, onChanged }: { invoice: Invoice; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showPay, setShowPay] = useState(false);

  async function voidIt() {
    setBusy(true);
    setError(null);
    try {
      await api.voidInvoice(invoice.id);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error anulando");
    } finally {
      setBusy(false);
    }
  }

  const canVoid = invoice.status !== "paid" && invoice.status !== "void";
  const canPay = invoice.status === "issued" || invoice.status === "overdue";

  return (
    <>
      <tr>
        <td className="py-2 pr-2 align-top font-data text-xs text-ink-dim">
          {invoice.period_start} → {invoice.period_end}
        </td>
        <td className="py-2 pr-2 align-top font-data text-ink-dim">${invoice.subtotal.toFixed(2)}</td>
        <td className="py-2 pr-2 align-top font-data text-ink-dim">${invoice.discount_total.toFixed(2)}</td>
        <td className="py-2 pr-2 align-top font-data text-ink">${invoice.total.toFixed(2)}</td>
        <td className="py-2 pr-2 align-top font-data text-xs text-ink-faint">{invoice.due_date}</td>
        <td className="py-2 pr-2 align-top">
          <Badge tone={INVOICE_STATUS_TONE[invoice.status]}>{INVOICE_STATUS_LABEL[invoice.status]}</Badge>
        </td>
        <td className="py-2 align-top">
          <div className="flex gap-2">
            {canPay && (
              <Button variant="secondary" disabled={busy} onClick={() => setShowPay((v) => !v)} className="px-2 py-1 text-xs">
                {showPay ? "Cancelar" : "Registrar pago"}
              </Button>
            )}
            {canVoid && (
              <Button variant="secondary" disabled={busy} onClick={voidIt} className="px-2 py-1 text-xs">
                Anular
              </Button>
            )}
          </div>
          {error && <p className="mt-1 text-xs text-red-300">{error}</p>}
        </td>
      </tr>
      {showPay && (
        <tr>
          <td colSpan={7} className="pb-2">
            <PaymentCreateForm
              invoiceId={invoice.id}
              suggestedAmount={invoice.total}
              onCreated={() => {
                setShowPay(false);
                onChanged();
              }}
            />
          </td>
        </tr>
      )}
    </>
  );
}

function PaymentCreateForm({
  invoiceId,
  suggestedAmount,
  onCreated,
}: {
  invoiceId: string;
  suggestedAmount: number;
  onCreated: () => void;
}) {
  const [amount, setAmount] = useState(String(suggestedAmount));
  const [method, setMethod] = useState<PaymentMethod>("cash");
  const [referenceNote, setReferenceNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createPayment({
        invoice_id: invoiceId,
        amount: Number(amount),
        method,
        reference_note: referenceNote || undefined,
      });
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error registrando pago");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-3 rounded-sm border border-line bg-surface-2 p-3">
      <Field label="Monto">
        <Input type="number" min={0.01} step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} required />
      </Field>
      <Field label="Método">
        <Select value={method} onChange={(e) => setMethod(e.target.value as PaymentMethod)}>
          {Object.entries(PAYMENT_METHOD_LABEL).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="Referencia (opcional)">
        <Input value={referenceNote} onChange={(e) => setReferenceNote(e.target.value)} placeholder="folio de depósito, etc." />
      </Field>
      <Button type="submit" disabled={busy} className="px-3 py-1 text-xs">
        Confirmar pago
      </Button>
      {error && <span className="text-xs text-red-300">{error}</span>}
    </form>
  );
}

function PromotionCreateForm({ tenantId, onCreated }: { tenantId: string; onCreated: () => void }) {
  const [description, setDescription] = useState("");
  const [startsAt, setStartsAt] = useState("");
  const [endsAt, setEndsAt] = useState("");
  const [discountType, setDiscountType] = useState<PromotionDiscountType>("full_waiver");
  const [discountValue, setDiscountValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createPromotion({
        tenant_id: tenantId,
        description,
        starts_at: startsAt,
        ends_at: endsAt,
        discount_type: discountType,
        discount_value: discountType === "full_waiver" ? undefined : Number(discountValue),
      });
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error creando promoción");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-3 rounded-sm border border-line bg-surface-2 p-3">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Descripción">
          <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="1 año gratis, cliente piloto" required />
        </Field>
        <Field label="Tipo de descuento">
          <Select value={discountType} onChange={(e) => setDiscountType(e.target.value as PromotionDiscountType)}>
            <option value="full_waiver">Exención total</option>
            <option value="percentage">Porcentaje</option>
            <option value="fixed_amount">Monto fijo</option>
          </Select>
        </Field>
        <Field label="Desde">
          <Input type="date" value={startsAt} onChange={(e) => setStartsAt(e.target.value)} required />
        </Field>
        <Field label="Hasta">
          <Input type="date" value={endsAt} onChange={(e) => setEndsAt(e.target.value)} required />
        </Field>
        {discountType !== "full_waiver" && (
          <Field label={discountType === "percentage" ? "Porcentaje (0-100)" : "Monto"}>
            <Input
              type="number"
              min={0}
              max={discountType === "percentage" ? 100 : undefined}
              step="0.01"
              value={discountValue}
              onChange={(e) => setDiscountValue(e.target.value)}
              required
            />
          </Field>
        )}
      </div>
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={busy} className="px-3 py-1 text-xs">
          Crear promoción
        </Button>
        {error && <span className="text-xs text-red-300">{error}</span>}
      </div>
    </form>
  );
}

function bytesToGb(bytes: number): string {
  return (bytes / 1073741824).toFixed(2);
}

function bytesToMb(bytes: number): string {
  return (bytes / 1048576).toFixed(1);
}

// Real data usage per SIM line (device_data_usage_monthly) -- REAL TCP-level
// bytes of each connection (jt808server/gt06server), never estimated, unlike
// "Profitability" above (which measures bytes served to the BROWSER). Platform
// only -- tenant_admin only sees sim_number/sim_carrier in Administration →
// Devices, never this section.
function SimUsageSection() {
  const [page, setPage] = useState<PageResult<DeviceDataUsage> | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const limit = 25;

  useEffect(() => {
    api
      .listDeviceDataUsage({ limit, offset })
      .then(setPage)
      .catch((err) => setError(err instanceof ApiError ? err.message : "error cargando consumo de SIM"));
  }, [offset]);

  return (
    <Card>
      <CardTitle>Consumo de datos por línea SIM</CardTitle>
      <p className="mb-3 text-xs text-ink-faint">
        Bytes REALES transferidos por cada dispositivo en su propia conexión celular (heartbeats, posiciones,
        alarmas, comandos de video, subida de clips) -- últimos 12 meses, promedio mensual sobre los meses con
        datos.
      </p>

      {error && <Alert>{error}</Alert>}

      {page && page.items.length === 0 ? (
        <EmptyState>Sin dispositivos para mostrar.</EmptyState>
      ) : page ? (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                <th className="py-1.5 pr-2 font-medium">Unidad</th>
                <th className="py-1.5 pr-2 font-medium">Tenant</th>
                <th className="py-1.5 pr-2 font-medium">Modelo</th>
                <th className="py-1.5 pr-2 font-medium">SIM</th>
                <th className="py-1.5 pr-2 font-medium">Consumo/mes (prom., MB)</th>
                <th className="py-1.5 pr-2 font-medium">Total 12m (MB)</th>
                <th className="py-1.5 pr-2 font-medium">Costo del plan</th>
                <th className="py-1.5 pr-2 font-medium">Tope contratado (MB)</th>
                <th className="py-1.5 font-medium">Estado</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {page.items.map((row) => (
                <tr key={row.device_id}>
                  <td className="py-2 pr-2 align-top text-ink">{row.label}</td>
                  <td className="py-2 pr-2 align-top text-ink-dim">{row.tenant_name}</td>
                  <td className="py-2 pr-2 align-top text-ink-dim">{row.device_model_name ?? "—"}</td>
                  <td className="py-2 pr-2 align-top font-data text-ink-dim">
                    {row.sim_number ?? "—"}
                    {row.sim_carrier && <span className="text-ink-faint"> ({row.sim_carrier})</span>}
                  </td>
                  <td className="py-2 pr-2 align-top font-data text-ink-dim">
                    {row.avg_monthly_bytes !== null ? bytesToMb(row.avg_monthly_bytes) : "—"}
                  </td>
                  <td className="py-2 pr-2 align-top font-data text-ink-dim">{bytesToMb(row.total_bytes_12m)}</td>
                  <td className="py-2 pr-2 align-top font-data text-ink-dim">
                    {row.sim_plan_cost_mxn_month !== null ? `$${row.sim_plan_cost_mxn_month.toFixed(2)}` : "—"}
                  </td>
                  <td className="py-2 pr-2 align-top font-data text-ink-dim">{row.sim_plan_data_cap_mb ?? "—"}</td>
                  <td className="py-2 align-top">
                    {row.sim_plan_data_cap_mb === null ? (
                      <span className="text-ink-faint">sin tope</span>
                    ) : row.over_cap ? (
                      <Badge tone="danger">sobre el tope</Badge>
                    ) : (
                      <Badge tone="success">dentro del tope</Badge>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-3">
            <Pagination total={page.total} limit={page.limit} offset={page.offset} onOffsetChange={setOffset} />
          </div>
        </div>
      ) : null}
    </Card>
  );
}

function ProfitabilitySection() {
  const [settings, setSettings] = useState<PlatformBillingSettings | null>(null);
  const [page, setPage] = useState<PageResult<TenantProfitability> | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const limit = 25;

  async function reload() {
    try {
      const [s, p] = await Promise.all([
        api.getBillingSettings(),
        api.listTenantProfitability({ limit, offset }),
      ]);
      setSettings(s);
      setPage(p);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando rentabilidad");
    }
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offset]);

  return (
    <Card>
      <CardTitle>Rentabilidad por tenant (estimada)</CardTitle>
      <p className="mb-3 text-xs text-ink-faint">
        Costo ESTIMADO a partir de supuestos configurables, no contabilidad exacta — no hay facturación de
        infraestructura desglosada por tenant todavía.
      </p>

      {error && <Alert>{error}</Alert>}

      {settings && <BillingSettingsForm settings={settings} onSaved={reload} />}

      {page && page.items.length === 0 ? (
        <EmptyState>Sin tenants para mostrar.</EmptyState>
      ) : page ? (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-line text-xs tracking-wide text-ink-faint uppercase">
                <th className="py-1.5 pr-2 font-medium">Tenant</th>
                <th className="py-1.5 pr-2 font-medium">Cámaras activas</th>
                <th className="py-1.5 pr-2 font-medium">Tráfico/mes (GB)</th>
                <th className="py-1.5 pr-2 font-medium">Costo est. (USD)</th>
                <th className="py-1.5 pr-2 font-medium">Costo est. (MXN)</th>
                <th className="py-1.5 pr-2 font-medium">Ingreso/mes (MXN)</th>
                <th className="py-1.5 pr-2 font-medium">Margen (MXN)</th>
                <th className="py-1.5 font-medium">Margen %</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {page.items.map((row) => (
                <tr key={row.tenant_id}>
                  <td className="py-2 pr-2 align-top text-ink">{row.tenant_name}</td>
                  <td className="py-2 pr-2 align-top font-data text-ink-dim">{row.active_devices}</td>
                  <td className="py-2 pr-2 align-top font-data text-ink-dim">{bytesToGb(row.bytes_this_month)}</td>
                  <td className="py-2 pr-2 align-top font-data text-ink-dim">${row.estimated_cost_usd.toFixed(2)}</td>
                  <td className="py-2 pr-2 align-top font-data text-ink-dim">${row.estimated_cost_mxn.toFixed(2)}</td>
                  <td className="py-2 pr-2 align-top font-data text-ink-dim">${row.monthly_revenue_mxn.toFixed(2)}</td>
                  <td className="py-2 pr-2 align-top font-data text-ink">${row.margin_mxn.toFixed(2)}</td>
                  <td className="py-2 align-top">
                    {row.margin_pct === null ? (
                      <span className="text-ink-faint">—</span>
                    ) : (
                      <Badge tone={row.margin_pct < 0 ? "danger" : row.margin_pct < 30 ? "warning" : "success"}>
                        {row.margin_pct.toFixed(1)}%
                      </Badge>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-3">
            <Pagination total={page.total} limit={page.limit} offset={page.offset} onOffsetChange={setOffset} />
          </div>
        </div>
      ) : null}
    </Card>
  );
}

function BillingSettingsForm({ settings, onSaved }: { settings: PlatformBillingSettings; onSaved: () => void }) {
  const [costPerDevice, setCostPerDevice] = useState(String(settings.cost_usd_per_device_month));
  // Separate rate for GT06 (GPS-only, no video), see
  // 0027_billing_category_quota.sql. Deliberately lower than the camera rate.
  const [costPerGpsDevice, setCostPerGpsDevice] = useState(String(settings.cost_usd_per_gps_device_month));
  const [costPerGb, setCostPerGb] = useState(String(settings.cost_usd_per_gb));
  const [exchangeRate, setExchangeRate] = useState(String(settings.exchange_rate_mxn_per_usd));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const dirty =
    costPerDevice !== String(settings.cost_usd_per_device_month) ||
    costPerGpsDevice !== String(settings.cost_usd_per_gps_device_month) ||
    costPerGb !== String(settings.cost_usd_per_gb) ||
    exchangeRate !== String(settings.exchange_rate_mxn_per_usd);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await api.updateBillingSettings({
        cost_usd_per_device_month: Number(costPerDevice),
        cost_usd_per_gps_device_month: Number(costPerGpsDevice),
        cost_usd_per_gb: Number(costPerGb),
        exchange_rate_mxn_per_usd: Number(exchangeRate),
      });
      onSaved();
    } catch (err) {
      // Support can READ the assumptions but not edit them (require_super_admin)
      // -- 403 expected.
      setError(err instanceof ApiError ? err.message : "error guardando");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mb-4 flex flex-wrap items-end gap-3 rounded-sm border border-line bg-surface-2 p-3">
      <Field label="Costo USD/cámara/mes">
        <Input type="number" min={0} step="0.01" value={costPerDevice} onChange={(e) => setCostPerDevice(e.target.value)} className="w-28" />
      </Field>
      <Field label="Costo USD/GPS/mes">
        <Input
          type="number"
          min={0}
          step="0.01"
          value={costPerGpsDevice}
          onChange={(e) => setCostPerGpsDevice(e.target.value)}
          className="w-28"
        />
      </Field>
      <Field label="Costo USD/GB">
        <Input type="number" min={0} step="0.0001" value={costPerGb} onChange={(e) => setCostPerGb(e.target.value)} className="w-28" />
      </Field>
      <Field label="Tipo de cambio MXN/USD">
        <Input type="number" min={0.01} step="0.01" value={exchangeRate} onChange={(e) => setExchangeRate(e.target.value)} className="w-28" />
      </Field>
      {dirty && (
        <Button disabled={busy} onClick={save} className="px-3 py-1 text-xs">
          Guardar supuestos
        </Button>
      )}
      {error && <span className="text-xs text-red-300">{error}</span>}
    </div>
  );
}
