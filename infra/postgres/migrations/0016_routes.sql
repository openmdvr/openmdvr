-- Route assignment. v1 is deliberately simple: one route = one driver + one
-- vehicle + one day, no stops or optimization. Standard RLS pattern, EXCEPT
-- that it reuses the SAME driver dimension as driver_shift_events (migration
-- 0015) so a driver only sees THEIR OWN assigned routes, never those of
-- another driver in the same tenant. This is what lets the driver view show
-- "your route today" without the backend handing out the rest of the
-- operation.

CREATE TYPE route_status AS ENUM ('planned', 'in_progress', 'completed', 'cancelled');

CREATE TABLE routes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    date        DATE NOT NULL,
    -- Nullable: a route can be planned before driver/vehicle are confirmed.
    -- RESTRICT for the same reason as devices.vehicle_id (migration 0014):
    -- never CASCADE towards an entity that may have referenced history.
    driver_id   UUID REFERENCES drivers(id) ON DELETE RESTRICT,
    vehicle_id  UUID REFERENCES vehicles(id) ON DELETE RESTRICT,
    status      route_status NOT NULL DEFAULT 'planned',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX routes_tenant_id_idx ON routes (tenant_id);
CREATE INDEX routes_driver_date_idx ON routes (driver_id, date);

CREATE TRIGGER routes_set_updated_at
    BEFORE UPDATE ON routes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- enforce_driver_tenant_match()/enforce_vehicle_tenant_match() already exist
-- (migrations 0014/0015) and are generic over NEW.driver_id/NEW.vehicle_id +
-- NEW.tenant_id; reused as-is.
CREATE TRIGGER routes_enforce_driver_tenant
    BEFORE INSERT OR UPDATE ON routes
    FOR EACH ROW EXECUTE FUNCTION enforce_driver_tenant_match();

CREATE TRIGGER routes_enforce_vehicle_tenant
    BEFORE INSERT OR UPDATE ON routes
    FOR EACH ROW EXECUTE FUNCTION enforce_vehicle_tenant_match();

COMMENT ON TABLE routes IS 'Route assigned to a driver/vehicle for one day. v1: no stops or optimization.';

ALTER TABLE routes ENABLE ROW LEVEL SECURITY;
ALTER TABLE routes FORCE ROW LEVEL SECURITY;

-- SELECT: same dimension as driver_shift_events_select (0015). A driver
-- (app_current_driver_id() IS NOT NULL) only sees routes where they ARE the
-- assigned driver; any other session (tenant_admin/operator/viewer, bypass)
-- sees all routes of the tenant. Unlike fleet endpoints, drivers ARE
-- intentionally allowed to read here (GET /routes uses get_current_user, not
-- require_non_driver): it is how the driver view knows "their" route.
CREATE POLICY routes_select ON routes
    FOR SELECT
    USING (
        app_bypass_rls()
        OR (
            tenant_id = app_current_tenant_id()
            AND (app_current_driver_id() IS NULL OR driver_id = app_current_driver_id())
        )
    );

-- INSERT/UPDATE/DELETE: tenant isolation only, like vehicles/drivers. The real
-- "only tenant_admin can create/edit routes" restriction lives in the API
-- (require_tenant_admin): RLS isolates by tenant, it does not replace
-- role-based authorization (see 0008_rls_policies.sql). A driver session would
-- technically pass this WITH CHECK by tenant_id, but never gets here: the
-- write endpoint requires tenant_admin before touching the database.
CREATE POLICY routes_insert ON routes
    FOR INSERT WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY routes_update ON routes
    FOR UPDATE
    USING (app_bypass_rls() OR tenant_id = app_current_tenant_id())
    WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY routes_delete ON routes
    FOR DELETE USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON routes TO app_user;
