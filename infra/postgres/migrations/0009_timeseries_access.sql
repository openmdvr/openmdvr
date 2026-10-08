-- Real app_user access layer to gps_positions, alarms and usage_events.
--
-- Why this file exists (see also the headers of 0007_timeseries_tables.sql and
-- 0008_rls_policies.sql): these three tables are TimescaleDB hypertables.
-- TimescaleDB automatically propagates the hypertable's GRANTs to each of its
-- physical chunks (real tables under the _timescaledb_internal schema),
-- including chunks created AFTER the GRANT. But it does NOT propagate FORCE ROW
-- LEVEL SECURITY to chunks (Postgres does not even allow it: "operation not
-- supported on chunk tables"). Since app_user has USAGE on
-- _timescaledb_internal (needed for TimescaleDB to work), any role with a direct
-- GRANT on the hypertable can name a chunk directly (`SELECT * FROM
-- _timescaledb_internal."_hyper_1_1_chunk"`) and read/write it without any RLS
-- policy ever being evaluated, however well the 0008 policies are written. This
-- was confirmed empirically during a security review: one tenant's sessions
-- could read, update and delete other tenants' rows this way.
--
-- The only durable mitigation is to NEVER grant app_user a direct table
-- privilege on these three hypertables. Instead:
--   - READS: `security_barrier` views (gps_positions_v, alarms_v,
--     usage_events_v), owned by the migration role (not app_user), with the
--     tenant filter embedded in the view. Postgres resolves access to the
--     underlying table with the view OWNER's privileges, not the querying
--     role's, so app_user never needs (nor has) any privilege on the hypertable
--     or its chunks to read through the view.
--   - WRITES: SECURITY DEFINER functions (insert_gps_position, insert_alarm,
--     insert_usage_event, acknowledge_alarm, delete_gps_positions_before,
--     delete_alarm), also owned by the migration role. Each function EXPLICITLY
--     repeats the bypass/tenant condition that used to live in the WITH CHECK
--     policy, because a SECURITY DEFINER function no longer inherits the
--     caller's RLS restriction (it runs with the owner's privileges).
--     acknowledge_alarm() also replaces the former whole-row UPDATE: it can only
--     touch acknowledged_at/acknowledged_by, never
--     video_evidence_key/alarm_type/severity/"time" (a whole-table UPDATE
--     allowed evidence tampering).
--
-- Every function sets search_path explicitly, and that path ALWAYS ends in
-- pg_temp. This is not cosmetic: if pg_temp does not appear LITERALLY in the
-- list, Postgres still searches it, but BEFORE any listed schema (including
-- pg_catalog); omitting it does not exclude it, it gives it the highest
-- priority. app_user has the TEMP privilege on the database (granted to PUBLIC
-- by default), so any session can create a temporary table named "devices",
-- "users", "alarms" or "usage_events" and, without pg_temp pinned at the end,
-- that temp table would resolve BEFORE the real one inside these function bodies
-- -- voiding enforce_device_tenant_match() and friends even in a bypass session.
-- This was confirmed exploitable during review (it allowed phantom
-- usage_events/alarms that never reached the real table, and broke the
-- tenant/device invariant). "pg_catalog, public, pg_temp" makes pg_temp
-- searched last, closing the vector.
--
-- TimescaleDB's own internal functions use search_path='pg_catalog, pg_temp'
-- for the same reason.

-- ---------------------------------------------------------------------------
-- Read views
-- ---------------------------------------------------------------------------

CREATE VIEW gps_positions_v WITH (security_barrier = true) AS
    SELECT * FROM gps_positions
    WHERE app_bypass_rls() OR tenant_id = app_current_tenant_id();

CREATE VIEW alarms_v WITH (security_barrier = true) AS
    SELECT * FROM alarms
    WHERE app_bypass_rls() OR tenant_id = app_current_tenant_id();

CREATE VIEW usage_events_v WITH (security_barrier = true) AS
    SELECT * FROM usage_events
    WHERE app_bypass_rls() OR tenant_id = app_current_tenant_id();

GRANT SELECT ON gps_positions_v TO app_user;
GRANT SELECT ON alarms_v TO app_user;
GRANT SELECT ON usage_events_v TO app_user;

-- ---------------------------------------------------------------------------
-- Writes: gps_positions
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION insert_gps_position(
    p_tenant_id UUID,
    p_device_id UUID,
    p_time      TIMESTAMPTZ,
    p_lat       DOUBLE PRECISION,
    p_lon       DOUBLE PRECISION,
    p_speed_kmh REAL DEFAULT NULL,
    p_heading   REAL DEFAULT NULL,
    p_altitude  REAL DEFAULT NULL,
    p_raw       JSONB DEFAULT NULL
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NOT (app_bypass_rls() OR p_tenant_id = app_current_tenant_id()) THEN
        RAISE EXCEPTION 'tenant_id % not authorized for this session', p_tenant_id
            USING ERRCODE = '42501';
    END IF;
    INSERT INTO gps_positions (time, tenant_id, device_id, lat, lon, speed_kmh, heading, altitude, raw)
    VALUES (p_time, p_tenant_id, p_device_id, p_lat, p_lon, p_speed_kmh, p_heading, p_altitude, p_raw);
END;
$$;

CREATE OR REPLACE FUNCTION delete_gps_positions_before(
    p_device_id UUID,
    p_before    TIMESTAMPTZ
) RETURNS BIGINT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    deleted_count BIGINT;
BEGIN
    IF NOT app_bypass_rls() THEN
        RAISE EXCEPTION 'deleting GPS positions requires a platform session' USING ERRCODE = '42501';
    END IF;
    DELETE FROM gps_positions WHERE device_id = p_device_id AND "time" < p_before;
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$;

REVOKE ALL ON FUNCTION insert_gps_position(UUID, UUID, TIMESTAMPTZ, DOUBLE PRECISION, DOUBLE PRECISION, REAL, REAL, REAL, JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION delete_gps_positions_before(UUID, TIMESTAMPTZ) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION insert_gps_position(UUID, UUID, TIMESTAMPTZ, DOUBLE PRECISION, DOUBLE PRECISION, REAL, REAL, REAL, JSONB) TO app_user;
GRANT EXECUTE ON FUNCTION delete_gps_positions_before(UUID, TIMESTAMPTZ) TO app_user;

-- ---------------------------------------------------------------------------
-- Writes: alarms
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION insert_alarm(
    p_tenant_id          UUID,
    p_device_id          UUID,
    p_time               TIMESTAMPTZ,
    p_alarm_type         TEXT,
    p_severity           alarm_severity DEFAULT 'warning',
    p_details            JSONB DEFAULT NULL,
    -- p_video_evidence_key is not validated here: the alarms table has a CHECK
    -- (see 0007_timeseries_tables.sql) requiring the prefix
    -- 'tenants/<row tenant_id>/...'. Without it, a tenant could create its own
    -- alarm pointing at another tenant's real video_evidence_key and then
    -- request it through the legitimate "my alarm, give me its video" path. The
    -- CHECK closes this regardless of which function or role inserts.
    p_video_evidence_key TEXT DEFAULT NULL
) RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    new_id UUID;
BEGIN
    IF NOT (app_bypass_rls() OR p_tenant_id = app_current_tenant_id()) THEN
        RAISE EXCEPTION 'tenant_id % not authorized for this session', p_tenant_id
            USING ERRCODE = '42501';
    END IF;
    INSERT INTO alarms (time, tenant_id, device_id, alarm_type, severity, details, video_evidence_key)
    VALUES (p_time, p_tenant_id, p_device_id, p_alarm_type, p_severity, p_details, p_video_evidence_key)
    RETURNING id INTO new_id;
    RETURN new_id;
END;
$$;

-- The only "write" path a tenant has on an existing alarm. It touches ONLY
-- acknowledged_at/acknowledged_by by construction; there is no way through this
-- path for a tenant to rewrite video_evidence_key, alarm_type, severity, details
-- or "time" of its own alarm.
CREATE OR REPLACE FUNCTION acknowledge_alarm(
    p_alarm_id UUID,
    p_acknowledged_by UUID
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    alarm_tenant UUID;
BEGIN
    SELECT tenant_id INTO alarm_tenant FROM alarms WHERE id = p_alarm_id;
    -- Generic, identical message whether the alarm does not exist or belongs to
    -- another tenant: neither confirms nor denies a foreign id (same as
    -- enforce_device_tenant_match in 0007).
    --
    -- NOTE: this function is REDEFINED in migration 0032 to also check
    -- app_can_view_device(). It is not edited here because
    -- app_can_view_device() (0031) does not exist yet at this point of the
    -- migration sequence (a fresh database running files in order would fail).
    IF NOT FOUND OR NOT (app_bypass_rls() OR alarm_tenant = app_current_tenant_id()) THEN
        RAISE EXCEPTION 'alarm % not valid for this session', p_alarm_id USING ERRCODE = '42501';
    END IF;
    UPDATE alarms SET acknowledged_at = now(), acknowledged_by = p_acknowledged_by
    WHERE id = p_alarm_id;
END;
$$;

CREATE OR REPLACE FUNCTION delete_alarm(p_alarm_id UUID) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NOT app_bypass_rls() THEN
        RAISE EXCEPTION 'deleting alarms requires a platform session' USING ERRCODE = '42501';
    END IF;
    DELETE FROM alarms WHERE id = p_alarm_id;
END;
$$;

REVOKE ALL ON FUNCTION insert_alarm(UUID, UUID, TIMESTAMPTZ, TEXT, alarm_severity, JSONB, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION acknowledge_alarm(UUID, UUID) FROM PUBLIC;
REVOKE ALL ON FUNCTION delete_alarm(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION insert_alarm(UUID, UUID, TIMESTAMPTZ, TEXT, alarm_severity, JSONB, TEXT) TO app_user;
GRANT EXECUTE ON FUNCTION acknowledge_alarm(UUID, UUID) TO app_user;
GRANT EXECUTE ON FUNCTION delete_alarm(UUID) TO app_user;

-- ---------------------------------------------------------------------------
-- Writes: usage_events (INSERT only; never UPDATE/DELETE, not even via a
-- function: the billing ledger is append-only without exception).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION insert_usage_event(
    p_tenant_id         UUID,
    p_device_id         UUID,
    p_time              TIMESTAMPTZ,
    p_user_id           UUID,
    p_event_type        usage_event_type,
    p_bytes_transferred BIGINT,
    p_metadata          JSONB DEFAULT NULL
) RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    new_id UUID;
BEGIN
    IF NOT (app_bypass_rls() OR p_tenant_id = app_current_tenant_id()) THEN
        RAISE EXCEPTION 'tenant_id % not authorized for this session', p_tenant_id
            USING ERRCODE = '42501';
    END IF;
    INSERT INTO usage_events (time, tenant_id, device_id, user_id, event_type, bytes_transferred, metadata)
    VALUES (p_time, p_tenant_id, p_device_id, p_user_id, p_event_type, p_bytes_transferred, p_metadata)
    RETURNING id INTO new_id;
    RETURN new_id;
END;
$$;

REVOKE ALL ON FUNCTION insert_usage_event(UUID, UUID, TIMESTAMPTZ, UUID, usage_event_type, BIGINT, JSONB) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION insert_usage_event(UUID, UUID, TIMESTAMPTZ, UUID, usage_event_type, BIGINT, JSONB) TO app_user;

-- ---------------------------------------------------------------------------
-- Belt and braces: explicitly revoke any direct privilege app_user might have on
-- the base hypertables, in case a future migration reintroduces one by mistake.
-- A no-op today (never granted); documented so the absence of GRANTs in this
-- file is explicitly intentional, not an oversight.
-- ---------------------------------------------------------------------------
REVOKE ALL ON gps_positions FROM app_user;
REVOKE ALL ON alarms FROM app_user;
REVOKE ALL ON usage_events FROM app_user;
