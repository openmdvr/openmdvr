-- 1) Recovery of clips that arrived LATE.
-- 2) Device health events for the platform (super_admin/support).
--
-- Context: a camera may keep retrying the upload of the SAME event clip on
-- every reconnection and every few minutes, because the server rejected it
-- with 404 ("no pending request": the request had already been marked failed
-- by the 5-10 min timeout) and the device treats any error as "retry later".
-- That wastes large amounts of cellular data. That file is exactly the one
-- requested for that alarm: real evidence that arrived late, not garbage.
BEGIN;

-- 1. A 'failed' clip that NEVER received video may move to 'ready' when the
--    EXACT requested file arrives late. Everything else stays forbidden: a clip
--    that already had video (non-null storage_key) or a 'ready'/'unsupported'
--    clip is never reopened.
CREATE OR REPLACE FUNCTION enforce_alarm_video_clip_status_transition() RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status = 'failed' AND NEW.status = 'ready'
       AND OLD.storage_key IS NULL AND NEW.storage_key IS NOT NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.status IN ('ready', 'failed', 'unsupported') AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'cannot change the status of an already finalized clip request (status=%)', OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION recover_alarm_clip_late(
    p_clip_id UUID,
    p_storage_key TEXT
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_alarm_id UUID;
BEGIN
    IF NOT app_bypass_rls() THEN
        RAISE EXCEPTION 'recovering a clip requires a platform session' USING ERRCODE = '42501';
    END IF;
    UPDATE alarm_video_clips
    SET status = 'ready', completed_at = now(), storage_key = p_storage_key,
        error_detail = 'recovered: the device uploaded the file after the timeout'
    WHERE id = p_clip_id AND status = 'failed' AND storage_key IS NULL
    RETURNING alarm_id INTO v_alarm_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'clip % not recoverable (not failed or already has video)', p_clip_id USING ERRCODE = '42501';
    END IF;
    UPDATE alarms SET video_evidence_key = p_storage_key WHERE id = v_alarm_id;
END;
$$;
REVOKE ALL ON FUNCTION recover_alarm_clip_late(UUID, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION recover_alarm_clip_late(UUID, TEXT) TO app_user;

-- 2. Device health: OPERATIONAL problems of a device (data wasted on retries,
--    native photo that fell back to the expensive method, etc.), distinct from
--    tenant alarms. Platform only.
--    Deduplicated: the same problem on the same device is ONE row with a
--    counter (occurrences) and the last time it happened, not one row per
--    occurrence, so it never becomes a huge list.
CREATE TABLE device_health_events (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    device_id    UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind ~ '^[a-z_]{3,60}$'),
    dedupe_key   TEXT NOT NULL DEFAULT '' CHECK (length(dedupe_key) <= 200),
    severity     TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    title        TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurrences  INTEGER NOT NULL DEFAULT 1 CHECK (occurrences >= 1),
    bytes_wasted BIGINT NOT NULL DEFAULT 0 CHECK (bytes_wasted >= 0),
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ,
    resolved_by  UUID REFERENCES users(id) ON DELETE SET NULL
);
-- A single OPEN event per (device, kind, key): the upsert accumulates there.
CREATE UNIQUE INDEX device_health_events_open_uniq
    ON device_health_events (device_id, kind, dedupe_key) WHERE resolved_at IS NULL;
CREATE INDEX device_health_events_open_recent_idx
    ON device_health_events (last_seen DESC) WHERE resolved_at IS NULL;

ALTER TABLE device_health_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_health_events FORCE ROW LEVEL SECURITY;
CREATE POLICY device_health_events_select ON device_health_events FOR SELECT USING (app_bypass_rls());
CREATE POLICY device_health_events_update ON device_health_events FOR UPDATE USING (app_bypass_rls()) WITH CHECK (app_bypass_rls());
GRANT SELECT ON device_health_events TO app_user;
-- Can only be marked resolved (the event itself is never rewritten).
GRANT UPDATE (resolved_at, resolved_by) ON device_health_events TO app_user;

-- Recording (and deduplication) from jt808-server. SECURITY DEFINER: app_user
-- has no direct INSERT.
CREATE OR REPLACE FUNCTION record_device_health_event(
    p_device_id  UUID,
    p_kind       TEXT,
    p_dedupe_key TEXT,
    p_severity   TEXT,
    p_title      TEXT,
    p_detail     JSONB,
    p_bytes      BIGINT
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_tenant UUID;
BEGIN
    IF NOT app_bypass_rls() THEN
        RAISE EXCEPTION 'recording device health requires a platform session' USING ERRCODE = '42501';
    END IF;
    SELECT tenant_id INTO v_tenant FROM devices WHERE id = p_device_id;
    IF v_tenant IS NULL THEN
        RETURN;
    END IF;
    INSERT INTO device_health_events (tenant_id, device_id, kind, dedupe_key, severity, title, detail, bytes_wasted)
    VALUES (v_tenant, p_device_id, p_kind, coalesce(p_dedupe_key, ''), p_severity, p_title,
            coalesce(p_detail, '{}'::jsonb), greatest(coalesce(p_bytes, 0), 0))
    ON CONFLICT (device_id, kind, dedupe_key) WHERE resolved_at IS NULL
    DO UPDATE SET occurrences = device_health_events.occurrences + 1,
                  bytes_wasted = device_health_events.bytes_wasted + greatest(coalesce(p_bytes, 0), 0),
                  last_seen = now(),
                  severity = EXCLUDED.severity,
                  title = EXCLUDED.title,
                  detail = EXCLUDED.detail;
END;
$$;
REVOKE ALL ON FUNCTION record_device_health_event(UUID, TEXT, TEXT, TEXT, TEXT, JSONB, BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_device_health_event(UUID, TEXT, TEXT, TEXT, TEXT, JSONB, BIGINT) TO app_user;

COMMIT;
