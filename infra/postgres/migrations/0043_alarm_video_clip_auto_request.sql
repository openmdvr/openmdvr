-- Automatic clip request when a camera event (GT06 0x95) is detected: the
-- request fires immediately on the event instead of waiting for a human to
-- click later. The trigger lives in jt808-server (Go), the same process that
-- detects 0x95, to minimize latency between the event and the request, with
-- no round-trip to the Python API.
--
-- requested_by becomes NULLABLE: an automatic request has no user behind it.
-- The manual path ("Request clip"/"Retry" in the UI) still sends a real
-- requested_by. NULL means "triggered by the system", never missing data.
ALTER TABLE alarm_video_clips ALTER COLUMN requested_by DROP NOT NULL;

COMMENT ON COLUMN alarm_video_clips.requested_by IS
    'User who requested the clip manually. NULL = triggered automatically by the system when the camera event (0x95) was detected.';
