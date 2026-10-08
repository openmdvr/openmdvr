-- Per-tenant/user/device alerting and visibility. A tenant_admin sees every
-- device of their tenant without explicit assignment; a tenant_operator/
-- tenant_viewer only sees (and receives alerts for) devices assigned to them,
-- directly or via a group. Follows the common industry pattern "permission to
-- view the device (direct or via group) AND subscription to the alert type",
-- evaluated at event time, without a full rules engine.
--
-- This migration is ADDITIVE:
--   - The 5 new tables are low risk (same tenant-wide pattern as
--     driver_vehicle_assignments, 0014_vehicles_drivers.sql).
--   - app_current_user_id()/app_device_recipients()/app_can_view_device()
--     are defined here and wired into the devices/alarms_v/gps_positions_v
--     RLS policies by 0032.
--   - The backfill below seeds the assignments needed so that wiring them
--     in does not cut off access for existing accounts.

-- ---------------------------------------------------------------------------
-- app_current_user_id(): same mechanism as app_current_driver_id()
-- (0015_driver_shift_events.sql): a session GUC set by the API in EVERY
-- transaction (api/app/db.py::tenant_scoped_connection).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_current_user_id() RETURNS UUID
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT NULLIF(current_setting('app.user_id', true), '')::uuid
$$;

COMMENT ON FUNCTION app_current_user_id() IS
    'user_id of the current authenticated request (any role, not only driver). NULL if the API did not set it.';

REVOKE ALL ON FUNCTION app_current_user_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_current_user_id() TO app_user;

-- ---------------------------------------------------------------------------
-- device_groups
-- ---------------------------------------------------------------------------
CREATE TABLE device_groups (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        TEXT NOT NULL CHECK (btrim(name) <> ''),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT device_groups_tenant_name_unique UNIQUE (tenant_id, name)
);

CREATE INDEX device_groups_tenant_id_idx ON device_groups (tenant_id);

CREATE TRIGGER device_groups_set_updated_at
    BEFORE UPDATE ON device_groups
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE device_groups IS 'Group of devices within a tenant, used to assign visibility/notifications to users without repeating each device_id.';

-- ---------------------------------------------------------------------------
-- device_group_members
-- ---------------------------------------------------------------------------
CREATE TABLE device_group_members (
    device_group_id UUID NOT NULL REFERENCES device_groups(id) ON DELETE CASCADE,
    device_id       UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (device_group_id, device_id)
);

CREATE INDEX device_group_members_tenant_id_idx ON device_group_members (tenant_id);
CREATE INDEX device_group_members_device_id_idx ON device_group_members (device_id);

-- Same pattern as enforce_vehicle_tenant_match/enforce_driver_tenant_match
-- (0014_vehicles_drivers.sql): both FKs must belong to the row's tenant_id,
-- regardless of which role or session writes.
CREATE OR REPLACE FUNCTION enforce_device_group_member_tenant_match() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    group_tenant  UUID;
    device_tenant UUID;
BEGIN
    SELECT tenant_id INTO group_tenant FROM device_groups WHERE id = NEW.device_group_id;
    IF group_tenant IS NULL OR group_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'device_group_id % is not valid for tenant_id %', NEW.device_group_id, NEW.tenant_id;
    END IF;
    SELECT tenant_id INTO device_tenant FROM devices WHERE id = NEW.device_id;
    IF device_tenant IS NULL OR device_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'device_id % is not valid for tenant_id %', NEW.device_id, NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER device_group_members_enforce_tenant
    BEFORE INSERT OR UPDATE ON device_group_members
    FOR EACH ROW EXECUTE FUNCTION enforce_device_group_member_tenant_match();

COMMENT ON TABLE device_group_members IS 'Devices that belong to each group.';

-- ---------------------------------------------------------------------------
-- user_device_assignments: direct device -> user assignment
-- ---------------------------------------------------------------------------
CREATE TABLE user_device_assignments (
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id   UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, device_id)
);

CREATE INDEX user_device_assignments_tenant_id_idx ON user_device_assignments (tenant_id);
CREATE INDEX user_device_assignments_device_id_idx ON user_device_assignments (device_id);

CREATE OR REPLACE FUNCTION enforce_user_device_assignment_tenant_match() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    u_tenant UUID;
    d_tenant UUID;
BEGIN
    SELECT tenant_id INTO u_tenant FROM users WHERE id = NEW.user_id;
    IF u_tenant IS NULL OR u_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'user_id % is not valid for tenant_id %', NEW.user_id, NEW.tenant_id;
    END IF;
    SELECT tenant_id INTO d_tenant FROM devices WHERE id = NEW.device_id;
    IF d_tenant IS NULL OR d_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'device_id % is not valid for tenant_id %', NEW.device_id, NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER user_device_assignments_enforce_tenant
    BEFORE INSERT OR UPDATE ON user_device_assignments
    FOR EACH ROW EXECUTE FUNCTION enforce_user_device_assignment_tenant_match();

COMMENT ON TABLE user_device_assignments IS 'Direct assignment of a device to a user, used to restrict visibility and route notifications. tenant_admin needs no row here: it always sees its whole tenant (see app_can_view_device).';

-- ---------------------------------------------------------------------------
-- user_device_group_assignments
-- ---------------------------------------------------------------------------
CREATE TABLE user_device_group_assignments (
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_group_id UUID NOT NULL REFERENCES device_groups(id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, device_group_id)
);

CREATE INDEX user_device_group_assignments_tenant_id_idx ON user_device_group_assignments (tenant_id);

CREATE OR REPLACE FUNCTION enforce_user_device_group_assignment_tenant_match() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    u_tenant UUID;
    g_tenant UUID;
BEGIN
    SELECT tenant_id INTO u_tenant FROM users WHERE id = NEW.user_id;
    IF u_tenant IS NULL OR u_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'user_id % is not valid for tenant_id %', NEW.user_id, NEW.tenant_id;
    END IF;
    SELECT tenant_id INTO g_tenant FROM device_groups WHERE id = NEW.device_group_id;
    IF g_tenant IS NULL OR g_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'device_group_id % is not valid for tenant_id %', NEW.device_group_id, NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER user_device_group_assignments_enforce_tenant
    BEFORE INSERT OR UPDATE ON user_device_group_assignments
    FOR EACH ROW EXECUTE FUNCTION enforce_user_device_group_assignment_tenant_match();

COMMENT ON TABLE user_device_group_assignments IS 'Assignment of a whole device group to a user.';

-- ---------------------------------------------------------------------------
-- user_notification_settings: per-user channel preference. OPTIONAL row:
-- when missing, the API assumes in_app_enabled=true/email_enabled=false via
-- LEFT JOIN + COALESCE (same defaults as the columns below).
-- ---------------------------------------------------------------------------
CREATE TABLE user_notification_settings (
    user_id         UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    in_app_enabled  BOOLEAN NOT NULL DEFAULT true,
    email_enabled   BOOLEAN NOT NULL DEFAULT false,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX user_notification_settings_tenant_id_idx ON user_notification_settings (tenant_id);

CREATE TRIGGER user_notification_settings_set_updated_at
    BEFORE UPDATE ON user_notification_settings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE FUNCTION enforce_notification_settings_tenant_match() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    u_tenant UUID;
BEGIN
    SELECT tenant_id INTO u_tenant FROM users WHERE id = NEW.user_id;
    IF u_tenant IS NULL OR u_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'user_id % is not valid for tenant_id %', NEW.user_id, NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER user_notification_settings_enforce_tenant
    BEFORE INSERT OR UPDATE ON user_notification_settings
    FOR EACH ROW EXECUTE FUNCTION enforce_notification_settings_tenant_match();

COMMENT ON TABLE user_notification_settings IS 'Per-user channel preference, configured by tenant_admin. Optional row; absence means column defaults.';

-- ---------------------------------------------------------------------------
-- Backfill: preserve the existing behavior of every tenant_operator/
-- tenant_viewer (who previously saw every device of their tenant) by
-- explicitly assigning what they already saw. Without it, enabling
-- per-device visibility would silently cut off access for existing
-- accounts. tenant_admin is intentionally excluded: it always sees all.
-- ---------------------------------------------------------------------------
INSERT INTO user_device_assignments (user_id, device_id, tenant_id)
SELECT u.id, d.id, u.tenant_id
FROM users u
JOIN devices d ON d.tenant_id = u.tenant_id
WHERE u.role IN ('tenant_operator', 'tenant_viewer')
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- Core functions: ONE source of truth for "who may view this device",
-- expressed twice for performance reasons only (never different rules; if a
-- condition changes, change BOTH):
--
--   app_device_recipients(device_id) -> SETOF user_id
--     SECURITY DEFINER, independent of the current session. Used by the
--     notification fan-out (insert_alarm()) for one device, evaluated ONCE
--     per alarm.
--
--   app_can_view_device(device_id) -> BOOLEAN
--     Per-row RLS predicate (devices_select/alarms_v/gps_positions_v),
--     evaluated once PER ROW returned; a SETOF there would be far more
--     expensive than a short-circuit EXISTS.
--
-- Both: active tenant_admin of the device's tenant (always) + direct
-- assignment + group assignment. Drivers are excluded implicitly: they never
-- have assignment rows nor role='tenant_admin'.
-- ---------------------------------------------------------------------------
-- app_is_tenant_admin(): shared helper for the write policies below. Without
-- it, RLS on the 4 assignment tables was tenant-wide and ANY tenant session
-- could, via direct SQL, add itself to another user's group/assignment and
-- widen its own visibility (the API's require_tenant_admin blocked this over
-- HTTP, but that was a single layer of defense). Now enforced in RLS too.
CREATE OR REPLACE FUNCTION app_is_tenant_admin() RETURNS BOOLEAN
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT EXISTS (
        SELECT 1 FROM users u
        WHERE u.id = app_current_user_id() AND u.role = 'tenant_admin' AND u.status = 'active'
    )
$$;

REVOKE ALL ON FUNCTION app_is_tenant_admin() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_is_tenant_admin() TO app_user;

CREATE OR REPLACE FUNCTION app_device_recipients(target_device_id UUID) RETURNS SETOF UUID
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT u.id
    FROM users u
    JOIN devices d ON d.tenant_id = u.tenant_id
    WHERE d.id = target_device_id
      AND u.status = 'active'
      AND u.role = 'tenant_admin'
    UNION
    SELECT a.user_id
    FROM user_device_assignments a
    JOIN users u ON u.id = a.user_id
    WHERE a.device_id = target_device_id AND u.status = 'active'
    UNION
    SELECT uga.user_id
    FROM user_device_group_assignments uga
    JOIN device_group_members dgm ON dgm.device_group_id = uga.device_group_id
    JOIN users u ON u.id = uga.user_id
    WHERE dgm.device_id = target_device_id AND u.status = 'active'
$$;

-- Intentionally NO GRANT to app_user (security finding): it is SECURITY
-- DEFINER and, unlike insert_gps_position/insert_alarm, does not check the
-- CALLING session's tenant, by design (it enumerates recipients for any
-- device; insert_alarm() calls it internally). Granting EXECUTE to app_user
-- would let any authenticated session enumerate user ids of other tenants.
-- The owner (migration role) can still call it from other SECURITY DEFINER
-- functions it owns.
REVOKE ALL ON FUNCTION app_device_recipients(UUID) FROM PUBLIC;

-- SECURITY DEFINER on purpose: this function is called by the devices_select
-- RLS policy (0032_device_visibility_rls.sql) on `devices`. Without SECURITY
-- DEFINER, the JOIN to `devices` in the tenant_admin branch below triggers
-- devices_select AGAIN for that row, which calls app_can_view_device()
-- again: infinite recursion (Postgres aborts with "StatementTooComplex", so
-- every devices/alarms/positions read for a tenant_admin failed with 500).
-- As SECURITY DEFINER the body runs as the owner (RLS-exempt migration role)
-- and the subqueries never re-evaluate their own policies. No new leak: each
-- subquery is bound to app_current_user_id()/target_device_id explicitly,
-- never "fetch everything and filter later".
CREATE OR REPLACE FUNCTION app_can_view_device(target_device_id UUID) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT
        app_bypass_rls()
        OR EXISTS (
            -- Bound to the DEVICE's tenant, not just the user's role
            -- (security finding): without the JOIN to devices this branch
            -- returned TRUE for a tenant_admin regardless of the device's
            -- tenant. Harmless while every caller also checks
            -- tenant_id = app_current_tenant_id(), but a trap for future
            -- callers. Same condition as app_device_recipients() above.
            SELECT 1 FROM users u
            JOIN devices d ON d.id = target_device_id AND d.tenant_id = u.tenant_id
            WHERE u.id = app_current_user_id() AND u.role = 'tenant_admin' AND u.status = 'active'
        )
        OR EXISTS (
            SELECT 1 FROM user_device_assignments a
            WHERE a.user_id = app_current_user_id() AND a.device_id = target_device_id
        )
        OR EXISTS (
            SELECT 1 FROM user_device_group_assignments uga
            JOIN device_group_members dgm ON dgm.device_group_id = uga.device_group_id
            WHERE uga.user_id = app_current_user_id() AND dgm.device_id = target_device_id
        )
$$;

REVOKE ALL ON FUNCTION app_can_view_device(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_can_view_device(UUID) TO app_user;

-- ---------------------------------------------------------------------------
-- RLS. SELECT stays tenant-wide (any tenant user must be able to READ
-- groups/assignments for the UI to work; the real read restriction on
-- sensitive data is app_can_view_device on devices/alarms/positions).
-- INSERT/UPDATE/DELETE on the 4 assignment tables require
-- app_is_tenant_admin(), so a tenant session cannot self-assign another
-- device/group via direct SQL. Two layers (RLS + require_tenant_admin in
-- api/app/deps.py), not one.
-- ---------------------------------------------------------------------------
ALTER TABLE device_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_groups FORCE ROW LEVEL SECURITY;
CREATE POLICY device_groups_select ON device_groups
    FOR SELECT USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY device_groups_insert ON device_groups
    FOR INSERT WITH CHECK (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
CREATE POLICY device_groups_update ON device_groups
    FOR UPDATE USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()))
    WITH CHECK (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
CREATE POLICY device_groups_delete ON device_groups
    FOR DELETE USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
GRANT SELECT, INSERT, UPDATE, DELETE ON device_groups TO app_user;

ALTER TABLE device_group_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_group_members FORCE ROW LEVEL SECURITY;
CREATE POLICY device_group_members_select ON device_group_members
    FOR SELECT USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY device_group_members_insert ON device_group_members
    FOR INSERT WITH CHECK (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
CREATE POLICY device_group_members_delete ON device_group_members
    FOR DELETE USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
GRANT SELECT, INSERT, DELETE ON device_group_members TO app_user;

ALTER TABLE user_device_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_device_assignments FORCE ROW LEVEL SECURITY;
CREATE POLICY user_device_assignments_select ON user_device_assignments
    FOR SELECT USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY user_device_assignments_insert ON user_device_assignments
    FOR INSERT WITH CHECK (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
CREATE POLICY user_device_assignments_delete ON user_device_assignments
    FOR DELETE USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
GRANT SELECT, INSERT, DELETE ON user_device_assignments TO app_user;

ALTER TABLE user_device_group_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_device_group_assignments FORCE ROW LEVEL SECURITY;
CREATE POLICY user_device_group_assignments_select ON user_device_group_assignments
    FOR SELECT USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY user_device_group_assignments_insert ON user_device_group_assignments
    FOR INSERT WITH CHECK (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
CREATE POLICY user_device_group_assignments_delete ON user_device_group_assignments
    FOR DELETE USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
GRANT SELECT, INSERT, DELETE ON user_device_group_assignments TO app_user;

ALTER TABLE user_notification_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_notification_settings FORCE ROW LEVEL SECURITY;
CREATE POLICY user_notification_settings_select ON user_notification_settings
    FOR SELECT USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY user_notification_settings_insert ON user_notification_settings
    FOR INSERT WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY user_notification_settings_update ON user_notification_settings
    FOR UPDATE USING (app_bypass_rls() OR tenant_id = app_current_tenant_id())
    WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());
GRANT SELECT, INSERT, UPDATE ON user_notification_settings TO app_user;
