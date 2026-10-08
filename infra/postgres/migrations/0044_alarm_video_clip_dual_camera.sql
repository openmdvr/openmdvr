-- The JC261 records EACH event with two cameras (front + cabin, channels I/F
-- in the 0x95 report), but only the front clip was requested/shown.
-- alarm_video_clips stays ONE row per alarm (never two): a secondary column is
-- much simpler than modeling "N clips per alarm" (correlating concurrent
-- uploads, a list UI instead of a fixed player) for a case that is at most 2.
--
-- front_file_name/cabin_file_name: the EXACT name the device reported for each
-- channel (from 0x95, videoEventGroup.frontFile()/cabinFile() in gt06server),
-- stored when the request is CREATED. They are used both to request the file
-- (UPLOADFILE,<name>#, see alarmclip.go) and to CORRELATE the real upload when
-- it arrives: the device sends a "filename" field in the multipart body, so
-- the clip is matched by exact file name instead of "the most recent pending
-- request for this device", which also distinguishes concurrent requests for
-- the same device.
ALTER TABLE alarm_video_clips
    ADD COLUMN front_file_name TEXT,
    ADD COLUMN cabin_file_name TEXT,
    ADD COLUMN storage_key_secondary TEXT
        CHECK (storage_key_secondary IS NULL OR storage_key_secondary LIKE 'tenants/' || tenant_id || '/%');

COMMENT ON COLUMN alarm_video_clips.front_file_name IS
    'Exact name the device reported for the front camera file (0x95). Used to request it (UPLOADFILE) and to correlate the real upload.';
COMMENT ON COLUMN alarm_video_clips.cabin_file_name IS
    'Same as front_file_name, for the cabin camera. NULL if the event only carried one channel.';
COMMENT ON COLUMN alarm_video_clips.storage_key_secondary IS
    'Storage key of the cabin camera clip, if it arrived. NULL is not an error: the front camera is the primary one and enough for the clip to count as "ready".';

-- enforce_alarm_video_clip_status_transition (0040) blocked ANY UPDATE on an
-- already-terminal row, regardless of the column changed. Attaching the
-- secondary camera to a clip ALREADY 'ready' (the most common real case; the
-- front usually arrives first) failed even though attach_alarm_clip_secondary
-- below NEVER touches status: the trigger is BEFORE UPDATE ... FOR EACH ROW
-- and fires on ANY update of the row. The intent was always to protect the
-- state machine (never reopen it), not freeze the whole row. Now it blocks only
-- when NEW.status tries to CHANGE away from a terminal state; other columns
-- (like storage_key_secondary) can still be updated on a 'ready'/'failed'/
-- 'unsupported' row.
CREATE OR REPLACE FUNCTION enforce_alarm_video_clip_status_transition() RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status IN ('ready', 'failed', 'unsupported') AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'cannot change the status of an already finalized clip request (status=%)', OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- attach_alarm_clip_secondary: deliberately NEVER touches status/completed_at.
-- The row may already be 'ready' via the front camera (mark_alarm_clip_ready,
-- unchanged); the cabin clip is a best-effort attachment that may arrive
-- BEFORE or AFTER that. It works on a terminal row thanks to the trigger fix
-- above (it never changes status, so it is never blocked).
CREATE OR REPLACE FUNCTION attach_alarm_clip_secondary(
    p_clip_id UUID,
    p_storage_key TEXT
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NOT app_bypass_rls() THEN
        RAISE EXCEPTION 'attaching a clip''s secondary camera requires a platform session' USING ERRCODE = '42501';
    END IF;
    UPDATE alarm_video_clips
    SET storage_key_secondary = p_storage_key
    WHERE id = p_clip_id AND storage_key_secondary IS NULL;
END;
$$;

GRANT EXECUTE ON FUNCTION attach_alarm_clip_secondary(UUID, TEXT) TO app_user;
