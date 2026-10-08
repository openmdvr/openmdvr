-- Time-series tables: gps_positions, alarms, usage_events.
-- Each carries its own tenant_id (not derived by a JOIN on every query) so RLS
-- policies can filter directly on the table without touching devices.
--
-- Because tenant_id is stored denormalized next to device_id, a row could be
-- inserted with a tenant_id that does NOT match the real owner of device_id
-- (through an application bug, not an external attack: RLS already stops a
-- foreign tenant_id from passing its own session's WITH CHECK, but it does not
-- stop a bypass session, or a bug, from mixing tenant_id and device_id from two
-- tenants). The enforce_device_tenant_match() trigger closes that gap at the
-- schema level, regardless of which role or session is writing.
--
-- IMPORTANT about app_user access to these three tables: NO direct grants are
-- given (see 0008_rls_policies.sql and 0009_timeseries_access.sql). They are
-- TimescaleDB hypertables; TimescaleDB propagates hypertable GRANTs to every
-- physical chunk but does NOT propagate FORCE ROW LEVEL SECURITY (unsupported on
-- chunks), so a role with a direct GRANT on the hypertable could read/write its
-- chunks (_timescaledb_internal.*) by name without any RLS policy. app_user
-- accesses these tables exclusively through security_barrier views and
-- SECURITY DEFINER functions, never with a direct privilege on the base table.
--
-- FKs to devices/users are ON DELETE RESTRICT (not CASCADE/SET NULL): these
-- three tables are history/audit/billing and must never lose rows or
-- attribution as a side effect of deleting a device or user. A referential
-- action runs with the table owner's privileges and IGNORES RLS, so a
-- CASCADE/SET NULL here would let a normal tenant session (allowed to delete its
-- own devices/users) alter these "protected" tables as a side effect. Retiring a
-- device or user with history is modeled as a soft delete
-- (status='inactive'/'disabled'), never a physical DELETE.

-- search_path is set explicitly, ending in pg_temp: without it, a session able
-- to run arbitrary SQL (e.g. via a future SQL injection in the API) could create
-- a temporary table named "devices" and make this trigger read it instead of the
-- real table, voiding the validation entirely. See the comment in
-- 0009_timeseries_access.sql (same mechanism).
CREATE OR REPLACE FUNCTION enforce_device_tenant_match() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    device_tenant UUID;
BEGIN
    SELECT tenant_id INTO device_tenant FROM devices WHERE id = NEW.device_id;
    -- If RLS hides the device (not visible in this session) this is also NULL;
    -- the error message is intentionally generic so it neither confirms nor
    -- denies the existence of a device_id that belongs to another tenant.
    IF device_tenant IS NULL THEN
        RAISE EXCEPTION 'device_id % is not valid for tenant_id %', NEW.device_id, NEW.tenant_id;
    END IF;
    IF device_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'device_id % is not valid for tenant_id %', NEW.device_id, NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TYPE usage_event_type AS ENUM ('live_view', 'playback', 'download');
CREATE TYPE alarm_severity AS ENUM ('info', 'warning', 'critical');

-- ---------------------------------------------------------------------------
-- gps_positions
-- ---------------------------------------------------------------------------
CREATE TABLE gps_positions (
    "time"      TIMESTAMPTZ NOT NULL,
    tenant_id   UUID NOT NULL,
    device_id   UUID NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
    lat         DOUBLE PRECISION NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon         DOUBLE PRECISION NOT NULL CHECK (lon BETWEEN -180 AND 180),
    speed_kmh   REAL,
    heading     REAL,
    altitude    REAL,
    raw         JSONB,
    PRIMARY KEY (device_id, "time")
);

SELECT create_hypertable('gps_positions', 'time');
CREATE INDEX gps_positions_tenant_time_idx ON gps_positions (tenant_id, "time" DESC);

CREATE TRIGGER gps_positions_enforce_tenant
    BEFORE INSERT OR UPDATE ON gps_positions
    FOR EACH ROW EXECUTE FUNCTION enforce_device_tenant_match();

COMMENT ON TABLE gps_positions IS 'GPS positions reported by each device. Immutable from the app (no UPDATE/DELETE via app_user except retention cleanup by a bypass role).';

-- ---------------------------------------------------------------------------
-- alarms
-- ---------------------------------------------------------------------------
CREATE TABLE alarms (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    "time"              TIMESTAMPTZ NOT NULL,
    tenant_id           UUID NOT NULL,
    device_id           UUID NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
    alarm_type          TEXT NOT NULL,
    severity            alarm_severity NOT NULL DEFAULT 'warning',
    details             JSONB,
    -- Prefix CHECK: the storage key of an evidence video MUST always start with
    -- "tenants/<this row's tenant_id>/". Without it, a tenant could insert its
    -- own alarm (correct tenant_id, passes RLS and the device trigger) pointing
    -- at ANOTHER tenant's real video_evidence_key (obtained some other way:
    -- guessed, leaked in a log) and then request "the video of my alarm" through
    -- the legitimate app path. Same impact as rewriting video_evidence_key on a
    -- foreign alarm (already closed: acknowledge_alarm() cannot touch this
    -- column), but via INSERT instead of UPDATE. The storage layer must ALWAYS
    -- generate keys with this exact prefix.
    video_evidence_key  TEXT
        CHECK (video_evidence_key IS NULL OR video_evidence_key LIKE 'tenants/' || tenant_id || '/%'),
    acknowledged_at     TIMESTAMPTZ,
    -- RESTRICT (not CASCADE/SET NULL): a user who acknowledged an alarm cannot be
    -- deleted without explicitly dissociating it first (soft-deleting the user
    -- via status='disabled' is the normal path).
    acknowledged_by     UUID REFERENCES users(id) ON DELETE RESTRICT,
    PRIMARY KEY (id, "time")
);

SELECT create_hypertable('alarms', 'time');
CREATE INDEX alarms_tenant_time_idx ON alarms (tenant_id, "time" DESC);
CREATE INDEX alarms_device_time_idx ON alarms (device_id, "time" DESC);

CREATE TRIGGER alarms_enforce_device_tenant
    BEFORE INSERT OR UPDATE ON alarms
    FOR EACH ROW EXECUTE FUNCTION enforce_device_tenant_match();

-- acknowledged_by must belong to the same tenant as the alarm, or be a platform
-- (support) user. Prevents an alarm from being "acknowledged" by a user of a
-- different tenant (cross-tenant identity confusion).
CREATE OR REPLACE FUNCTION enforce_acknowledger_tenant_match() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    ack_tenant  UUID;
    ack_bypass  BOOLEAN;
BEGIN
    IF NEW.acknowledged_by IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT tenant_id, is_platform_bypass INTO ack_tenant, ack_bypass
    FROM users WHERE id = NEW.acknowledged_by;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'acknowledged_by % is not valid', NEW.acknowledged_by;
    END IF;
    IF NOT ack_bypass AND ack_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'acknowledged_by % is not valid', NEW.acknowledged_by;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER alarms_enforce_acknowledger_tenant
    BEFORE INSERT OR UPDATE ON alarms
    FOR EACH ROW EXECUTE FUNCTION enforce_acknowledger_tenant_match();

COMMENT ON TABLE alarms IS 'ADAS/DMS alarms reported by each device.';

-- ---------------------------------------------------------------------------
-- usage_events: ledger of bytes served. Source of truth for pricing and
-- infrastructure scaling. Deliberately no UPDATE/DELETE policy for any role (see
-- 0008_rls_policies.sql): it is an insert-only ledger.
-- ---------------------------------------------------------------------------
CREATE TABLE usage_events (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    "time"              TIMESTAMPTZ NOT NULL,
    tenant_id           UUID NOT NULL,
    device_id           UUID NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
    -- RESTRICT (not CASCADE/SET NULL): usage_events is the billing source of
    -- truth; neither the device nor the user that generated an event can be
    -- physically deleted while the event exists, so nothing can alter the
    -- attribution of an already written record.
    user_id             UUID REFERENCES users(id) ON DELETE RESTRICT,
    event_type          usage_event_type NOT NULL,
    bytes_transferred   BIGINT NOT NULL CHECK (bytes_transferred >= 0),
    metadata            JSONB,
    PRIMARY KEY (id, "time")
);

SELECT create_hypertable('usage_events', 'time');
CREATE INDEX usage_events_tenant_time_idx ON usage_events (tenant_id, "time" DESC);
CREATE INDEX usage_events_device_time_idx ON usage_events (device_id, "time" DESC);

-- BEFORE INSERT OR UPDATE (not just INSERT), symmetric with gps_positions/alarms,
-- even though app_user has no UPDATE path to this table today: cheap to keep the
-- cross-validation ready for a future billing-correction flow.
CREATE TRIGGER usage_events_enforce_device_tenant
    BEFORE INSERT OR UPDATE ON usage_events
    FOR EACH ROW EXECUTE FUNCTION enforce_device_tenant_match();

-- enforce_acknowledger_tenant_match() is written for alarms.acknowledged_by;
-- usage_events uses user_id, so it gets its own function with the same rule
-- instead of overloading one meant for another column. It must be defined BEFORE
-- the CREATE TRIGGER that uses it.
CREATE OR REPLACE FUNCTION enforce_user_tenant_match() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    u_tenant  UUID;
    u_bypass  BOOLEAN;
BEGIN
    IF NEW.user_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT tenant_id, is_platform_bypass INTO u_tenant, u_bypass
    FROM users WHERE id = NEW.user_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'user_id % is not valid', NEW.user_id;
    END IF;
    IF NOT u_bypass AND u_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'user_id % is not valid', NEW.user_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER usage_events_enforce_user_tenant
    BEFORE INSERT OR UPDATE ON usage_events
    FOR EACH ROW EXECUTE FUNCTION enforce_user_tenant_match();

COMMENT ON TABLE usage_events IS 'Ledger of bytes served to clients (live_view/playback/download). Insert-only.';
