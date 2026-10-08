-- Billing: GET /billing/my-subscription must resolve the name/sku/price of a
-- plan the tenant already subscribes to. `billing_plans_select`
-- (0020_billing_catalog.sql) is bypass-only ON PURPOSE (the full internal
-- price catalog must never be readable by a tenant), so a plain JOIN from a
-- tenant session returns NULLs for `bp.name`/`bp.unit_price`.
--
-- This SECURITY DEFINER function resolves EXACTLY one plan by id without
-- exposing the catalog. It is only safe because its caller
-- (api/app/routers/billing.py::my_subscription) first filters
-- tenant_subscription_items through NORMAL RLS (tenant_id =
-- app_current_tenant_id(), no bypass): the billing_plan_id passed in always
-- belongs to a line that tenant legitimately has, never an arbitrary id a
-- client could invent to browse other plans.
CREATE OR REPLACE FUNCTION resolve_billing_plan_public(p_plan_id UUID)
RETURNS TABLE(name TEXT, sku TEXT, unit_price NUMERIC)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT name, sku, unit_price FROM billing_plans WHERE id = p_plan_id;
$$;

COMMENT ON FUNCTION resolve_billing_plan_public(UUID) IS
    'Resolves name/sku/price of ONE plan by id, bypassing billing_plans RLS. Safe only because its sole caller (GET /billing/my-subscription) has already verified via normal RLS that the plan belongs to a line of THAT tenant.';

REVOKE ALL ON FUNCTION resolve_billing_plan_public(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_billing_plan_public(UUID) TO app_user;
