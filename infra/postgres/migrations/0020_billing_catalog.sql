-- Billing: plan catalog + per-tenant subscriptions.
--
-- HIGH-RISK change: a new surface of sensitive data (prices, subscriptions)
-- with its own RLS dimension; requires an independent security review like
-- any new table with tenant isolation.
--
-- `billing_plans` is a GLOBAL catalog (no tenant_id), never readable by a
-- non-bypass session: neither the internal price catalog nor the per-tenant
-- estimated cost (0023) may ever reach a tenant_admin, even by accident. A
-- tenant CAN see its own subscription lines (tenant_subscription_items,
-- standard RLS by tenant_id), but a JOIN against billing_plans from that
-- session returns nothing (RLS blocks it). Showing the tenant the NAME/PRICE
-- of what it subscribes to is solved separately with a narrow function
-- (0024_billing_resolve_plan.sql).

CREATE TYPE billing_period AS ENUM ('monthly', 'semiannual', 'annual');
CREATE TYPE billing_plan_category AS ENUM ('gps', 'camera', 'addon');

-- ---------------------------------------------------------------------------
-- billing_plans: reusable price catalog (priced per SKU/line item, not a
-- single number per customer).
-- ---------------------------------------------------------------------------
CREATE TABLE billing_plans (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL CHECK (btrim(name) <> ''),
    sku             TEXT NOT NULL UNIQUE CHECK (btrim(sku) <> ''),
    category        billing_plan_category NOT NULL,
    unit_price      NUMERIC(12, 2) NOT NULL CHECK (unit_price >= 0),
    currency        TEXT NOT NULL DEFAULT 'MXN',
    billing_period  billing_period NOT NULL DEFAULT 'monthly',
    active          BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER billing_plans_set_updated_at
    BEFORE UPDATE ON billing_plans
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE billing_plans IS 'Global price catalog (SKUs), never readable by a non-bypass session. Real prices charged to a tenant live in tenant_subscription_items (unit_price_override) and are SNAPSHOTTED into invoice_line_items when invoicing; they never reference this catalog live.';

ALTER TABLE billing_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_plans FORCE ROW LEVEL SECURITY;

-- No tenant_id: the only access dimension is bypass or not. A tenant_admin must
-- never see the internal price catalog.
CREATE POLICY billing_plans_select ON billing_plans FOR SELECT USING (app_bypass_rls());
CREATE POLICY billing_plans_insert ON billing_plans FOR INSERT WITH CHECK (app_bypass_rls());
CREATE POLICY billing_plans_update ON billing_plans FOR UPDATE USING (app_bypass_rls()) WITH CHECK (app_bypass_rls());
CREATE POLICY billing_plans_delete ON billing_plans FOR DELETE USING (app_bypass_rls());

GRANT SELECT, INSERT, UPDATE, DELETE ON billing_plans TO app_user;

-- ---------------------------------------------------------------------------
-- tenants: billing cycle, ONE per tenant (not per line); all subscribed lines
-- are invoiced together that day. next_invoice_date is nullable: the invoicing
-- job (0021) sets/advances it; a tenant with no subscription line genuinely has
-- no "next invoice".
-- ---------------------------------------------------------------------------
ALTER TABLE tenants
    ADD COLUMN billing_period billing_period NOT NULL DEFAULT 'monthly',
    ADD COLUMN next_invoice_date DATE;

COMMENT ON COLUMN tenants.billing_period IS 'Billing cycle of this tenant. All its tenant_subscription_items lines are invoiced together on this period, not one date per line.';
COMMENT ON COLUMN tenants.next_invoice_date IS 'Date on which the next invoice for this tenant is due to be generated (generate_invoices). NULL while it has no active subscription line.';

-- ---------------------------------------------------------------------------
-- tenant_subscription_items: what each tenant subscribes to. Either references
-- a catalog plan (billing_plan_id) or is a fully custom line
-- (custom_description), never neither. unit_price_override lets a platform
-- admin charge THIS tenant a price different from the plan's list price
-- without forking the catalog.
-- ---------------------------------------------------------------------------
CREATE TABLE tenant_subscription_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- RESTRICT: a catalog plan with active lines referencing it cannot be
    -- deleted (deactivate it with `active = false` instead).
    billing_plan_id     UUID REFERENCES billing_plans(id) ON DELETE RESTRICT,
    custom_description  TEXT,
    quantity            INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    unit_price_override NUMERIC(12, 2) CHECK (unit_price_override >= 0),
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL = active line. Ending a line sets ended_at, never deletes it
    -- (history of what a tenant had and when).
    ended_at            TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (billing_plan_id IS NOT NULL OR btrim(coalesce(custom_description, '')) <> ''),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE INDEX tenant_subscription_items_tenant_id_idx ON tenant_subscription_items (tenant_id);
CREATE INDEX tenant_subscription_items_active_idx ON tenant_subscription_items (tenant_id) WHERE ended_at IS NULL;

CREATE TRIGGER tenant_subscription_items_set_updated_at
    BEFORE UPDATE ON tenant_subscription_items
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE tenant_subscription_items IS 'Lines of what a tenant subscribes to (cameras, GPS, add-ons). Snapshotted into invoice_line_items when invoicing, never edited retroactively to alter an issued invoice.';

ALTER TABLE tenant_subscription_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_subscription_items FORCE ROW LEVEL SECURITY;

-- SELECT: a tenant sees ITS OWN lines (to know what it subscribes to), but since
-- billing_plans is off-limits, a JOIN to resolve list name/price returns
-- nothing; see the note at the top of this file.
CREATE POLICY tenant_subscription_items_select ON tenant_subscription_items
    FOR SELECT USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());
-- INSERT/UPDATE/DELETE: bypass-only. A tenant never edits its own
-- subscription/price; it is operational support work, like adjusting a
-- tenant's max_live_view_seconds.
CREATE POLICY tenant_subscription_items_insert ON tenant_subscription_items
    FOR INSERT WITH CHECK (app_bypass_rls());
CREATE POLICY tenant_subscription_items_update ON tenant_subscription_items
    FOR UPDATE USING (app_bypass_rls()) WITH CHECK (app_bypass_rls());
CREATE POLICY tenant_subscription_items_delete ON tenant_subscription_items
    FOR DELETE USING (app_bypass_rls());

GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_subscription_items TO app_user;
