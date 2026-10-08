-- Retrieval of video clips linked to an alarm. Requested on demand (to save
-- cellular data); 1 clip = 1 video segment recorded on the device (for
-- GT06/JC261: a fixed 1-minute segment, a real hardware limitation, not an
-- arbitrary "10s before/10s after" window).
--
-- Same audit pattern as remote commands (0029_device_commands.sql): who
-- requested what, when, and what REALLY happened. Protocol-agnostic on purpose:
-- this table knows nothing about GT06 commands or HTTP uploads; that lives
-- entirely inside jt808-server.
--
-- alarm_id/alarm_time are stored WITHOUT a foreign key to alarms: alarms is a
-- hypertable (composite PK (id, "time"), chunk-partitioned) and this schema
-- never uses a hypertable as an FK target. The real "this alarm exists and
-- belongs to this tenant" validation is done by alarms_v (security_barrier +
-- RLS) in the API before inserting this row, just as device ownership is
-- checked before accepting a remote command.
CREATE TABLE alarm_video_clips (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    alarm_id          UUID NOT NULL,
    alarm_time        TIMESTAMPTZ NOT NULL,
    device_id         UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    -- 'jt808' can be added later; the CHECK is widened by a new migration, same
    -- as command_type in device_commands.
    protocol          TEXT NOT NULL CHECK (protocol IN ('gt06_video')),
    status            TEXT NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested', 'uploading', 'ready', 'failed', 'unsupported')),
    -- ON DELETE RESTRICT, same as device_commands.requested_by and
    -- alarms.acknowledged_by: a clip request is a real audit record and must
    -- never disappear because the requesting account was deleted.
    requested_by      UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ,
    -- Real range of the segment the device confirmed (may not match alarm_time
    -- exactly; for GT06/JC261, the 1-minute segment containing it), set only
    -- when status becomes 'ready'.
    clip_started_at   TIMESTAMPTZ,
    clip_ended_at     TIMESTAMPTZ,
    -- Same mandatory prefix as alarms.video_evidence_key
    -- (0007_timeseries_tables.sql): same storage convention, one shared bucket.
    storage_key       TEXT CHECK (storage_key IS NULL OR storage_key LIKE 'tenants/' || tenant_id || '/%'),
    -- Defensive cap: may come from a device error message (untrusted input);
    -- never executed/interpreted, only displayed.
    error_detail      TEXT CHECK (char_length(error_detail) <= 500)
);

CREATE INDEX alarm_video_clips_tenant_id_idx ON alarm_video_clips (tenant_id);
CREATE INDEX alarm_video_clips_device_id_idx ON alarm_video_clips (device_id, requested_at DESC);
-- An index on alarm_id for the common "is there already a request for this
-- alarm?" path (idempotency of POST .../request-clip).
CREATE INDEX alarm_video_clips_alarm_id_idx ON alarm_video_clips (alarm_id);

-- Like device_commands: only pending ("requested"/"uploading") -> terminal is
-- allowed; a resolved request is never reopened.
CREATE FUNCTION enforce_alarm_video_clip_status_transition() RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status IN ('ready', 'failed', 'unsupported') THEN
        RAISE EXCEPTION 'cannot modify an already finalized clip request (status=%)', OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER alarm_video_clips_enforce_status_transition
    BEFORE UPDATE ON alarm_video_clips
    FOR EACH ROW EXECUTE FUNCTION enforce_alarm_video_clip_status_transition();

-- Same reason as enforce_device_command_tenant_matches_device
-- (0029_device_commands.sql): tenant_id is not derived from any database
-- relation (the API sets it from devices.tenant_id already validated by RLS),
-- so without this trigger nothing stops a future bug from inserting mismatched
-- tenant_id/device_id.
CREATE FUNCTION enforce_alarm_video_clip_tenant_matches_device() RETURNS TRIGGER AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM devices WHERE id = NEW.device_id AND tenant_id = NEW.tenant_id
    ) THEN
        RAISE EXCEPTION 'alarm_video_clips.tenant_id does not match the referenced device''s tenant';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER alarm_video_clips_enforce_tenant_matches_device
    BEFORE INSERT OR UPDATE ON alarm_video_clips
    FOR EACH ROW EXECUTE FUNCTION enforce_alarm_video_clip_tenant_matches_device();

ALTER TABLE alarm_video_clips ENABLE ROW LEVEL SECURITY;
ALTER TABLE alarm_video_clips FORCE ROW LEVEL SECURITY;

-- SELECT: any session of the tenant (same as device_commands_select;
-- require_non_driver is the real barrier in the API). INSERT/UPDATE: no extra
-- role policy here on purpose; require_non_driver on POST .../request-clip is
-- the real barrier, RLS only isolates by tenant.
CREATE POLICY alarm_video_clips_select ON alarm_video_clips
    FOR SELECT USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY alarm_video_clips_insert ON alarm_video_clips
    FOR INSERT WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY alarm_video_clips_update ON alarm_video_clips
    FOR UPDATE USING (app_bypass_rls() OR tenant_id = app_current_tenant_id())
    WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());
-- No DELETE policy: audit record, never deleted.

GRANT SELECT, INSERT, UPDATE ON alarm_video_clips TO app_user;

COMMENT ON TABLE alarm_video_clips IS 'Audit of alarm-linked video clip requests (who, when, what the device replied). Protocol-agnostic by design.';

-- mark_alarm_clip_ready: the only "write" path that marks a request as ready.
-- It touches ONLY status/completed_at/clip_started_at/clip_ended_at/storage_key,
-- and also updates alarms.video_evidence_key (denormalized so the alarm preview
-- does not need to join this table). Same approach as acknowledge_alarm: a
-- narrow function instead of a whole-row UPDATE. SECURITY DEFINER because
-- jt808-server runs in a bypass session (no real user app.tenant_id in that
-- context), like insert_alarm/insert_usage_event.
CREATE OR REPLACE FUNCTION mark_alarm_clip_ready(
    p_clip_id UUID,
    p_storage_key TEXT,
    p_clip_started_at TIMESTAMPTZ,
    p_clip_ended_at TIMESTAMPTZ
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_alarm_id UUID;
BEGIN
    -- Defense in depth: only jt808-server (bypass session) marks a clip ready,
    -- never a tenant session, even though nothing calls it that way today.
    -- Same as delete_alarm.
    IF NOT app_bypass_rls() THEN
        RAISE EXCEPTION 'marking a clip as ready requires a platform session' USING ERRCODE = '42501';
    END IF;
    UPDATE alarm_video_clips
    SET status = 'ready', completed_at = now(), storage_key = p_storage_key,
        clip_started_at = p_clip_started_at, clip_ended_at = p_clip_ended_at
    WHERE id = p_clip_id AND status IN ('requested', 'uploading')
    RETURNING alarm_id INTO v_alarm_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'clip request % not found or already finalized', p_clip_id USING ERRCODE = '42501';
    END IF;

    UPDATE alarms SET video_evidence_key = p_storage_key WHERE id = v_alarm_id;
END;
$$;

-- mark_alarm_clip_failed: same approach, for the error path (timeout, error
-- device_reply, unsupported format).
CREATE OR REPLACE FUNCTION mark_alarm_clip_failed(
    p_clip_id UUID,
    p_status TEXT,
    p_error_detail TEXT
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NOT app_bypass_rls() THEN
        RAISE EXCEPTION 'marking a clip as failed requires a platform session' USING ERRCODE = '42501';
    END IF;
    IF p_status NOT IN ('failed', 'unsupported') THEN
        RAISE EXCEPTION 'mark_alarm_clip_failed: invalid status %', p_status;
    END IF;
    UPDATE alarm_video_clips
    SET status = p_status, completed_at = now(), error_detail = left(p_error_detail, 500)
    WHERE id = p_clip_id AND status IN ('requested', 'uploading');

    IF NOT FOUND THEN
        RAISE EXCEPTION 'clip request % not found or already finalized', p_clip_id USING ERRCODE = '42501';
    END IF;
END;
$$;

GRANT EXECUTE ON FUNCTION mark_alarm_clip_ready(UUID, TEXT, TIMESTAMPTZ, TIMESTAMPTZ) TO app_user;
GRANT EXECUTE ON FUNCTION mark_alarm_clip_failed(UUID, TEXT, TEXT) TO app_user;
