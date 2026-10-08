-- Closes clip requests stuck in 'requested'/'uploading'. A device may accept
-- the request and never upload the file (there is no upload confirmation
-- protocol implemented yet; see gt06Sender in alarmclip.go), which would leave
-- the row "uploading clip..." in the UI forever.
--
-- Same add_job() mechanism used elsewhere (enforce_gps_position_retention,
-- enforce_billing_suspension, enforce_webhook_delivery_retention), no new
-- infrastructure. Short interval (5 min, not daily): a hung request degrades
-- the user experience within minutes, not days.
--
-- 10 minutes of grace: generous for a cellular upload of a 1-minute clip
-- (the device can take well over 15s just to search its SD card), without
-- leaving the user waiting indefinitely.
CREATE OR REPLACE PROCEDURE enforce_alarm_video_clip_timeout(job_id INT, config JSONB)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    UPDATE alarm_video_clips
    SET status = 'failed',
        completed_at = now(),
        error_detail = 'timed out: the device never confirmed the clip upload'
    WHERE status IN ('requested', 'uploading')
      AND requested_at < now() - interval '10 minutes';
END;
$$;

COMMENT ON PROCEDURE enforce_alarm_video_clip_timeout(INT, JSONB) IS
    'Runs every 5 minutes: marks as failed any clip request stuck in requested/uploading for more than 10 minutes. Without it the UI would show "uploading clip..." forever if the device never uploads the file.';

REVOKE ALL ON PROCEDURE enforce_alarm_video_clip_timeout(INT, JSONB) FROM PUBLIC;
GRANT EXECUTE ON PROCEDURE enforce_alarm_video_clip_timeout(INT, JSONB) TO app_user;

SELECT add_job('enforce_alarm_video_clip_timeout', '5 minutes');
