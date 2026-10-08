-- CONFIGURATION commands to GT06 devices (UPLOAD/FILELIST/SERVER/APN/TIMEZONE/
-- UPLOADSW/TIMER/ANGLEREP/SOSALM, see api/app/gt06_config_commands.py).
-- Distinct from device_commands (0029, engine cut/resume): that one is
-- protocol-agnostic and available to tenant_admin; this one is 100%
-- GT06-specific (builds raw text via SendRawCommand) and available ONLY to
-- the platform (super_admin/support). These are provisioning/technical support
-- commands, not tenant self-service. A separate table instead of widening
-- device_commands: its command_type CHECK is deliberately narrow (engine only),
-- and the raw text sent here can be much longer (full URLs).
--
-- The raw text sent to the device is ALWAYS built server-side
-- (api/app/routers/device_config_commands.py) from `command_key` + `params`
-- validated by Pydantic. The client NEVER sends free text that goes straight to
-- the GT06 socket (prevents an extra `#` in a parameter from terminating the
-- command early and injecting a second one).
CREATE TABLE device_config_commands (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    device_id     UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    -- command_key identifies WHICH builder in api/app/gt06_config_commands.py
    -- was used. Adding a command is one more catalog entry + one more value
    -- here, never a redesign (same as device_commands.command_type).
    command_key   TEXT NOT NULL CHECK (command_key IN (
        'corekitsw', 'server', 'apn', 'upload_url', 'filelist_url',
        'uploadsw', 'timezone', 'timer', 'anglerep', 'sosalm'
    )),
    -- Structured parameters entered by the operator (host/port/APN/etc), kept
    -- apart from the raw text so the history shows "what was configured" in a
    -- readable way, not only the GT06 text.
    params        JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- The EXACT text sent to the device (e.g. "SERVER,1,203.0.113.10,5023#"),
    -- length-capped for the same reason as device_reply.
    raw_text      TEXT NOT NULL CHECK (char_length(raw_text) <= 300),
    requested_by  UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'success', 'failed', 'timeout', 'device_offline')),
    device_reply  TEXT CHECK (char_length(device_reply) <= 500),
    completed_at  TIMESTAMPTZ
);

CREATE INDEX device_config_commands_tenant_id_idx ON device_config_commands (tenant_id);
CREATE INDEX device_config_commands_device_id_idx ON device_config_commands (device_id, requested_at DESC);

-- Same two protections as device_commands (0029): a finalized command never
-- returns to pending, and tenant_id must always match the referenced device's
-- real tenant.
CREATE FUNCTION enforce_device_config_command_pending_transition() RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'cannot modify an already finalized configuration command (status=%)', OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER device_config_commands_enforce_pending_transition
    BEFORE UPDATE ON device_config_commands
    FOR EACH ROW EXECUTE FUNCTION enforce_device_config_command_pending_transition();

CREATE FUNCTION enforce_device_config_command_tenant_matches_device() RETURNS TRIGGER AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM devices WHERE id = NEW.device_id AND tenant_id = NEW.tenant_id
    ) THEN
        RAISE EXCEPTION 'device_config_commands.tenant_id does not match the referenced device''s tenant';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER device_config_commands_enforce_tenant_matches_device
    BEFORE INSERT OR UPDATE ON device_config_commands
    FOR EACH ROW EXECUTE FUNCTION enforce_device_config_command_tenant_matches_device();

ALTER TABLE device_config_commands ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_config_commands FORCE ROW LEVEL SECURITY;

-- Unlike device_commands (tenant_admin can read/trigger), SELECT/INSERT/UPDATE
-- here are bypass-only in all three directions: not even read access for a
-- tenant_admin. The FINER distinction (super_admin vs. support) never lives in
-- RLS in this project (same as billing_plans/platform_map_settings); it lives
-- in the API dependencies.
CREATE POLICY device_config_commands_select ON device_config_commands
    FOR SELECT USING (app_bypass_rls());
CREATE POLICY device_config_commands_insert ON device_config_commands
    FOR INSERT WITH CHECK (app_bypass_rls());
CREATE POLICY device_config_commands_update ON device_config_commands
    FOR UPDATE USING (app_bypass_rls()) WITH CHECK (app_bypass_rls());
-- No DELETE policy: audit record.

GRANT SELECT, INSERT, UPDATE ON device_config_commands TO app_user;

COMMENT ON TABLE device_config_commands IS 'Audit of GT06 configuration commands (UPLOAD/FILELIST/SERVER/APN/TIMEZONE/etc). Platform only (super_admin/support), never tenant_admin.';
