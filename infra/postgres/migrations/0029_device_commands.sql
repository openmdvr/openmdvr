-- Remote commands to devices (currently: GT06 engine cut/resume). Audits "who
-- requested what, when, and what the device REALLY replied", not just "it was
-- sent". Design: a protocol-agnostic layer (API/DB/frontend) separate from a
-- per-protocol layer inside jt808-server.
--
-- command_type is protocol-agnostic on purpose (common fleet-platform
-- vocabulary): this table knows nothing about GT06/0x80/DYD#; that lives
-- entirely in jt808-server/internal/gt06server. Adding a command_type (from any
-- future protocol) is one more CHECK value here, never a redesign.
--
-- Normal low-volume table (not a hypertable), like vehicles/drivers
-- (0014_vehicles_drivers.sql): RLS + direct GRANT is sufficient and correct.
-- The security_barrier view + SECURITY DEFINER pattern is only needed for
-- hypertables (see 0009_timeseries_access.sql).
CREATE TABLE device_commands (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    device_id     UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    command_type  TEXT NOT NULL CHECK (command_type IN ('engine_stop', 'engine_resume')),
    -- ON DELETE RESTRICT, like alarms.acknowledged_by: a command that cut a real
    -- engine is an audit record and must never disappear because someone
    -- deleted the requesting account.
    requested_by  UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'success', 'failed', 'timeout', 'device_offline')),
    -- Defensive length cap: device_reply is ASCII text sent by the DEVICE
    -- (untrusted input; GT06 has no cryptographic auth beyond the IMEI). It is
    -- never executed or interpreted, only displayed in the history, but a cap
    -- stops a malformed/compromised device from bloating this table.
    device_reply  TEXT CHECK (char_length(device_reply) <= 500),
    completed_at  TIMESTAMPTZ
);

CREATE INDEX device_commands_tenant_id_idx ON device_commands (tenant_id);
CREATE INDEX device_commands_device_id_idx ON device_commands (device_id, requested_at DESC);

-- A finalized command (success/failed/timeout/device_offline) never returns to
-- pending nor changes its result: it is an audit record, not editable state.
-- Only the pending -> terminal transition is allowed, once.
CREATE FUNCTION enforce_device_command_pending_transition() RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'cannot modify an already finalized command (status=%)', OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER device_commands_enforce_pending_transition
    BEFORE UPDATE ON device_commands
    FOR EACH ROW EXECUTE FUNCTION enforce_device_command_pending_transition();

-- device_commands.tenant_id is not derived from any database relation; the
-- router sets it (device_commands.py, taken from devices.tenant_id already
-- validated by RLS). Without this trigger nothing would stop a future bug in
-- that layer from inserting a row whose tenant_id does not match the
-- referenced device (RLS alone only requires tenant_id to match the SESSION,
-- never the DEVICE). Not exploitable via the API today, but defense in depth:
-- never rely on a single layer.
CREATE FUNCTION enforce_device_command_tenant_matches_device() RETURNS TRIGGER AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM devices WHERE id = NEW.device_id AND tenant_id = NEW.tenant_id
    ) THEN
        RAISE EXCEPTION 'device_commands.tenant_id does not match the referenced device''s tenant';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER device_commands_enforce_tenant_matches_device
    BEFORE INSERT OR UPDATE ON device_commands
    FOR EACH ROW EXECUTE FUNCTION enforce_device_command_tenant_matches_device();

ALTER TABLE device_commands ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_commands FORCE ROW LEVEL SECURITY;

-- SELECT: any session of the tenant may see the history (GET
-- /devices/{id}/commands is require_non_driver in the API). INSERT/UPDATE: no
-- extra role policy here on purpose; require_tenant_admin is the real barrier
-- in the API (POST /devices/{id}/commands). RLS only isolates by tenant, like
-- vehicles/drivers.
CREATE POLICY device_commands_select ON device_commands
    FOR SELECT USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY device_commands_insert ON device_commands
    FOR INSERT WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY device_commands_update ON device_commands
    FOR UPDATE USING (app_bypass_rls() OR tenant_id = app_current_tenant_id())
    WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());
-- No DELETE policy: audit record, never deleted.

GRANT SELECT, INSERT, UPDATE ON device_commands TO app_user;

COMMENT ON TABLE device_commands IS 'Audit of remote commands to devices (who, when, what the device replied). Protocol-agnostic by design.';
