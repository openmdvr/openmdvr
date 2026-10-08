-- Splits vehicle and driver out of `devices` as entities of their own.
--
-- Previously devices.vehicle_plate/vehicle_make/vehicle_model/vehicle_year/
-- driver_name were free text on the camera itself, conflating "the installed
-- JT808 hardware" with "the vehicle carrying it" and "who drives it". That
-- breaks as soon as a driver changes trucks or a device is reinstalled in
-- another vehicle: there was no way to know "who drove unit X on March 3".
-- `driver_vehicle_assignments` is the time-ranged history that solves this;
-- `ended_at IS NULL` = active assignment.
--
-- vehicles/drivers/driver_vehicle_assignments are normal low-volume tables (not
-- hypertables). The security_barrier view + SECURITY DEFINER pattern (see
-- 0009_timeseries_access.sql) is ONLY for hypertables, where TimescaleDB
-- propagates GRANTs to chunks but not FORCE ROW LEVEL SECURITY. Here RLS +
-- direct GRANT (same as `devices`/`users`) is sufficient and correct.
--
-- Unlike `devices` (bypass-only creation: real hardware installation, with a
-- globally unique jt808_terminal_id that could be a cross-tenant oracle),
-- vehicles/drivers/assignments ARE tenant_admin self-service: no globally
-- unique identifier is involved; it is the customer's own inventory.

CREATE TYPE vehicle_status AS ENUM ('active', 'inactive');

CREATE TABLE vehicles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plate       TEXT,
    make        TEXT,
    model       TEXT,
    year        SMALLINT CHECK (year BETWEEN 1980 AND 2100),
    status      vehicle_status NOT NULL DEFAULT 'active',
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX vehicles_tenant_id_idx ON vehicles (tenant_id);
-- Unique per tenant only when a plate exists (a vehicle may be registered before
-- it has a plate); same partial-unique-index idiom as users_tenant_email_unique
-- in 0005_users.sql.
CREATE UNIQUE INDEX vehicles_tenant_plate_unique ON vehicles (tenant_id, plate) WHERE plate IS NOT NULL;

CREATE TRIGGER vehicles_set_updated_at
    BEFORE UPDATE ON vehicles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE vehicles IS 'A vehicle in a tenant''s fleet, separate from devices: a device can be reinstalled in another vehicle, and a vehicle can be temporarily without a device.';

CREATE TYPE driver_status AS ENUM ('active', 'inactive');

CREATE TABLE drivers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    license_number  TEXT,
    phone           TEXT,
    status          driver_status NOT NULL DEFAULT 'active',
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX drivers_tenant_id_idx ON drivers (tenant_id);

CREATE TRIGGER drivers_set_updated_at
    BEFORE UPDATE ON drivers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- A record only (name/license/phone). Driver login (role `driver` in users) is
-- added later in 0015 with its own security review; this table grants no app
-- access by itself.
COMMENT ON TABLE drivers IS 'A driver in a tenant''s fleet. Does not imply a login account.';

-- ---------------------------------------------------------------------------
-- driver_vehicle_assignments: time-ranged history of which driver drove which
-- vehicle. ended_at NULL = active assignment. Partial unique indexes enforce
-- "at most one active driver per vehicle, and at most one active vehicle per
-- driver" at the schema level, not just by API convention.
-- ---------------------------------------------------------------------------
CREATE TABLE driver_vehicle_assignments (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- RESTRICT (not CASCADE/SET NULL): this table is history, same as
    -- gps_positions/alarms/usage_events towards devices/users in
    -- 0007_timeseries_tables.sql. In practice it never fires because
    -- vehicles/drivers are never physically deleted, only change status.
    driver_id   UUID NOT NULL REFERENCES drivers(id) ON DELETE RESTRICT,
    vehicle_id  UUID NOT NULL REFERENCES vehicles(id) ON DELETE RESTRICT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT driver_vehicle_assignments_valid_range CHECK (ended_at IS NULL OR ended_at > started_at)
);

CREATE INDEX driver_vehicle_assignments_tenant_id_idx ON driver_vehicle_assignments (tenant_id);
CREATE INDEX driver_vehicle_assignments_driver_id_idx ON driver_vehicle_assignments (driver_id);
CREATE UNIQUE INDEX driver_vehicle_assignments_active_vehicle_unique
    ON driver_vehicle_assignments (vehicle_id) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX driver_vehicle_assignments_active_driver_unique
    ON driver_vehicle_assignments (driver_id) WHERE ended_at IS NULL;

COMMENT ON TABLE driver_vehicle_assignments IS 'History of which driver drove which vehicle and when. ended_at NULL = current assignment.';

-- ---------------------------------------------------------------------------
-- Cross-tenant consistency triggers, same reason as enforce_device_tenant_match()
-- in 0007_timeseries_tables.sql: RLS alone does not stop a bypass session (or an
-- application bug) from mixing tenant_id with ANOTHER tenant's
-- vehicle_id/driver_id. search_path ends in pg_temp for the same reason
-- documented there (defense against temp-table shadowing).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION enforce_vehicle_tenant_match() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    vehicle_tenant UUID;
BEGIN
    IF NEW.vehicle_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT tenant_id INTO vehicle_tenant FROM vehicles WHERE id = NEW.vehicle_id;
    IF vehicle_tenant IS NULL OR vehicle_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'vehicle_id % is not valid for tenant_id %', NEW.vehicle_id, NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION enforce_driver_tenant_match() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    driver_tenant UUID;
BEGIN
    IF NEW.driver_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT tenant_id INTO driver_tenant FROM drivers WHERE id = NEW.driver_id;
    IF driver_tenant IS NULL OR driver_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'driver_id % is not valid for tenant_id %', NEW.driver_id, NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER driver_vehicle_assignments_enforce_vehicle_tenant
    BEFORE INSERT OR UPDATE ON driver_vehicle_assignments
    FOR EACH ROW EXECUTE FUNCTION enforce_vehicle_tenant_match();

CREATE TRIGGER driver_vehicle_assignments_enforce_driver_tenant
    BEFORE INSERT OR UPDATE ON driver_vehicle_assignments
    FOR EACH ROW EXECUTE FUNCTION enforce_driver_tenant_match();

-- ---------------------------------------------------------------------------
-- devices.vehicle_id: the device installed in a vehicle. Nullable: a device may
-- exist without a vehicle yet (freshly provisioned) and a vehicle may have no
-- device installed.
-- ---------------------------------------------------------------------------
ALTER TABLE devices ADD COLUMN vehicle_id UUID REFERENCES vehicles(id) ON DELETE RESTRICT;
CREATE INDEX devices_vehicle_id_idx ON devices (vehicle_id);

CREATE TRIGGER devices_enforce_vehicle_tenant
    BEFORE INSERT OR UPDATE ON devices
    FOR EACH ROW EXECUTE FUNCTION enforce_vehicle_tenant_match();

-- ---------------------------------------------------------------------------
-- Data migration: existing devices with free-text vehicle_plate/make/model/
-- year/driver_name become real vehicles/drivers rows + an active assignment. A
-- 1:1 device->vehicle mapping is correct for existing data and loses nothing.
-- ---------------------------------------------------------------------------
INSERT INTO vehicles (tenant_id, plate, make, model, year)
SELECT tenant_id, vehicle_plate, vehicle_make, vehicle_model, vehicle_year
FROM devices
WHERE vehicle_plate IS NOT NULL OR vehicle_make IS NOT NULL
   OR vehicle_model IS NOT NULL OR vehicle_year IS NOT NULL;

UPDATE devices d
SET vehicle_id = v.id
FROM vehicles v
WHERE v.tenant_id = d.tenant_id
  AND v.plate IS NOT DISTINCT FROM d.vehicle_plate
  AND v.make IS NOT DISTINCT FROM d.vehicle_make
  AND v.model IS NOT DISTINCT FROM d.vehicle_model
  AND v.year IS NOT DISTINCT FROM d.vehicle_year
  AND (d.vehicle_plate IS NOT NULL OR d.vehicle_make IS NOT NULL
       OR d.vehicle_model IS NOT NULL OR d.vehicle_year IS NOT NULL);

INSERT INTO drivers (tenant_id, name)
SELECT DISTINCT tenant_id, driver_name FROM devices WHERE driver_name IS NOT NULL AND driver_name <> '';

INSERT INTO driver_vehicle_assignments (tenant_id, driver_id, vehicle_id)
SELECT d.tenant_id, dr.id, d.vehicle_id
FROM devices d
JOIN drivers dr ON dr.tenant_id = d.tenant_id AND dr.name = d.driver_name
WHERE d.driver_name IS NOT NULL AND d.driver_name <> '' AND d.vehicle_id IS NOT NULL;

ALTER TABLE devices
    DROP COLUMN vehicle_plate,
    DROP COLUMN vehicle_make,
    DROP COLUMN vehicle_model,
    DROP COLUMN vehicle_year,
    DROP COLUMN driver_name;

-- ---------------------------------------------------------------------------
-- RLS: same pattern as `users` (self-service within the own tenant, or bypass
-- for the platform), one policy per command, FORCE in addition to ENABLE.
-- ---------------------------------------------------------------------------
ALTER TABLE vehicles ENABLE ROW LEVEL SECURITY;
ALTER TABLE vehicles FORCE ROW LEVEL SECURITY;

CREATE POLICY vehicles_select ON vehicles
    FOR SELECT USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY vehicles_insert ON vehicles
    FOR INSERT WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY vehicles_update ON vehicles
    FOR UPDATE USING (app_bypass_rls() OR tenant_id = app_current_tenant_id())
    WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY vehicles_delete ON vehicles
    FOR DELETE USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON vehicles TO app_user;

ALTER TABLE drivers ENABLE ROW LEVEL SECURITY;
ALTER TABLE drivers FORCE ROW LEVEL SECURITY;

CREATE POLICY drivers_select ON drivers
    FOR SELECT USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY drivers_insert ON drivers
    FOR INSERT WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY drivers_update ON drivers
    FOR UPDATE USING (app_bypass_rls() OR tenant_id = app_current_tenant_id())
    WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY drivers_delete ON drivers
    FOR DELETE USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON drivers TO app_user;

ALTER TABLE driver_vehicle_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE driver_vehicle_assignments FORCE ROW LEVEL SECURITY;

CREATE POLICY driver_vehicle_assignments_select ON driver_vehicle_assignments
    FOR SELECT USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY driver_vehicle_assignments_insert ON driver_vehicle_assignments
    FOR INSERT WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY driver_vehicle_assignments_update ON driver_vehicle_assignments
    FOR UPDATE USING (app_bypass_rls() OR tenant_id = app_current_tenant_id())
    WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY driver_vehicle_assignments_delete ON driver_vehicle_assignments
    FOR DELETE USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON driver_vehicle_assignments TO app_user;
