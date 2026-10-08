-- Driver login (role `driver`) + shift events (clock in/out, meal start/end).
-- HIGH-RISK change: adds a new auth role and a new RLS dimension; this file
-- (and the accompanying api/ code) requires an independent security review
-- before merging.
--
-- Design: drivers ARE `users` rows (reusing the existing, reviewed
-- JWT/bcrypt/timing-safe login instead of a parallel auth mechanism) with a
-- `driver_id` linking them to their `drivers` row (migration 0014). Each driver
-- login sets a new session GUC (`app.driver_id`, same mechanism as
-- `app.tenant_id` in 0003_rls_helpers.sql) that the `driver_shift_events` RLS
-- policy uses so a driver only sees/inserts THEIR OWN events. tenant_admin/
-- operator/viewer (or bypass) still see all events of the tenant, as with any
-- other table.

-- ---------------------------------------------------------------------------
-- 1. `driver` role in the enum + `users.driver_id`
-- ---------------------------------------------------------------------------

-- ADD VALUE cannot be used in the SAME explicit transaction that adds it, but
-- as a standalone statement (autocommit, as with the rest of this file under
-- `psql -f`) it is available to the following statements of this script.
ALTER TYPE user_role ADD VALUE 'driver';

ALTER TABLE users ADD COLUMN driver_id UUID REFERENCES drivers(id) ON DELETE RESTRICT;

-- A driver has at most one login account.
CREATE UNIQUE INDEX users_driver_id_unique ON users (driver_id) WHERE driver_id IS NOT NULL;

-- Replaces the original CHECK (0005_users.sql) to add a third branch: driver_id
-- NOT NULL only when role='driver', NULL otherwise. Prevents, e.g., a
-- tenant_admin with a leftover driver_id from a botched role change.
ALTER TABLE users DROP CONSTRAINT users_tenant_role_consistency;
ALTER TABLE users ADD CONSTRAINT users_tenant_role_consistency CHECK (
    (tenant_id IS NULL AND role IN ('super_admin', 'support') AND is_platform_bypass = true AND driver_id IS NULL)
    OR
    (tenant_id IS NOT NULL AND role IN ('tenant_admin', 'tenant_operator', 'tenant_viewer')
        AND is_platform_bypass = false AND driver_id IS NULL)
    OR
    (tenant_id IS NOT NULL AND role = 'driver' AND is_platform_bypass = false AND driver_id IS NOT NULL)
);

-- enforce_driver_tenant_match() already exists (migration 0014, validates
-- NEW.driver_id against NEW.tenant_id); reused as-is since users has the same
-- two column names.
CREATE TRIGGER users_enforce_driver_tenant
    BEFORE INSERT OR UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION enforce_driver_tenant_match();

COMMENT ON COLUMN users.driver_id IS 'Driver this login belongs to. Required when role=driver, NULL for any other role (see CHECK users_tenant_role_consistency).';

-- ---------------------------------------------------------------------------
-- 2. Session GUC app.driver_id, same mechanism as app.tenant_id/
--    app.bypass_rls (0003_rls_helpers.sql). Fail-closed: if the API does not
--    set it (current_setting with missing_ok=true), it returns NULL and the
--    policy below grants nothing by default.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_current_driver_id() RETURNS UUID
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT NULLIF(current_setting('app.driver_id', true), '')::uuid
$$;

COMMENT ON FUNCTION app_current_driver_id() IS
    'driver_id of the current authenticated request IF the session is a driver login. NULL for any other role. The API must set it from the JWT driver_id claim, never infer it.';

REVOKE ALL ON FUNCTION app_current_driver_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_current_driver_id() TO app_user;

-- ---------------------------------------------------------------------------
-- 3. driver_shift_events: normal low-volume table (a few events per driver per
--    day), NOT a hypertable. The security_barrier view/SECURITY DEFINER pattern
--    is only for hypertables (see 0009_timeseries_access.sql); standard RLS +
--    GRANT is enough here. Append-only (no UPDATE, DELETE bypass-only), like
--    usage_events: correcting a mistake means inserting a new event, not
--    editing history.
-- ---------------------------------------------------------------------------
CREATE TYPE shift_event_type AS ENUM ('clock_in', 'clock_out', 'meal_start', 'meal_end');
CREATE TYPE shift_event_source AS ENUM ('driver_app', 'manual_admin');

CREATE TABLE driver_shift_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- RESTRICT: history, like every event table referencing
    -- drivers/devices/users in this schema.
    driver_id   UUID NOT NULL REFERENCES drivers(id) ON DELETE RESTRICT,
    event_type  shift_event_type NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lat         DOUBLE PRECISION CHECK (lat BETWEEN -90 AND 90),
    lon         DOUBLE PRECISION CHECK (lon BETWEEN -180 AND 180),
    source      shift_event_source NOT NULL DEFAULT 'driver_app',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX driver_shift_events_tenant_id_idx ON driver_shift_events (tenant_id);
CREATE INDEX driver_shift_events_driver_time_idx ON driver_shift_events (driver_id, occurred_at DESC);

CREATE TRIGGER driver_shift_events_enforce_driver_tenant
    BEFORE INSERT OR UPDATE ON driver_shift_events
    FOR EACH ROW EXECUTE FUNCTION enforce_driver_tenant_match();

COMMENT ON TABLE driver_shift_events IS 'Driver shift events (clock in/out, meal). Append-only; basis for the hours-worked report.';

ALTER TABLE driver_shift_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE driver_shift_events FORCE ROW LEVEL SECURITY;

-- New RLS dimension: on top of the usual tenant isolation, a driver
-- (app_current_driver_id() IS NOT NULL) only sees/inserts THEIR OWN events. A
-- non-driver session (tenant_admin/operator/viewer, or bypass) does not set
-- app.driver_id, so current_setting returns NULL, "app_current_driver_id() IS
-- NULL" holds, and it sees the whole tenant. This relies on the API ALWAYS
-- setting app.driver_id correctly for a driver session (see api/app/deps.py);
-- the API additionally rejects with an explicit 500 any role=driver session
-- whose JWT lacks driver_id, as defense in depth against a token-issuing bug.
CREATE POLICY driver_shift_events_select ON driver_shift_events
    FOR SELECT
    USING (
        app_bypass_rls()
        OR (
            tenant_id = app_current_tenant_id()
            AND (app_current_driver_id() IS NULL OR driver_id = app_current_driver_id())
        )
    );

CREATE POLICY driver_shift_events_insert ON driver_shift_events
    FOR INSERT
    WITH CHECK (
        app_bypass_rls()
        OR (
            tenant_id = app_current_tenant_id()
            AND (app_current_driver_id() IS NULL OR driver_id = app_current_driver_id())
        )
    );

-- No UPDATE policy (nobody can modify a written event, not even bypass).
-- DELETE bypass-only for exceptional cleanup/correction; never driver or
-- tenant_admin self-service.
CREATE POLICY driver_shift_events_delete ON driver_shift_events
    FOR DELETE
    USING (app_bypass_rls());

GRANT SELECT, INSERT, DELETE ON driver_shift_events TO app_user;
