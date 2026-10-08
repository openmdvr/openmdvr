-- JC261/JC400 (JIMI IoT/Concox) support: dashcams that speak GT06 for
-- telemetry (same Go server as GPS-only trackers) but push video over RTMP
-- (NOT JT1078).
-- A new protocol value `gt06_video` instead of reusing `gt06`: a plain `gt06`
-- tracker still has no camera (POST /devices/{id}/video rejects it), while
-- `gt06_video` does. Encoding this in the enum avoids a `has_camera` boolean
-- that could drift from the real protocol, same rationale as jt808/gt06.

-- ADD VALUE cannot be used in the SAME explicit transaction that adds it, but
-- as a standalone statement (autocommit via `psql -f`, see
-- apply_migrations.sh) it is available for the rest of this file. Same
-- pattern as 0015_driver_shift_events.sql.
ALTER TYPE device_protocol ADD VALUE 'gt06_video';

-- gt06_video shares its identifier with gt06 (same TCP server, same 15-digit
-- IMEI), so it joins the SAME CHECK branch instead of a third identical one.
ALTER TABLE devices DROP CONSTRAINT devices_protocol_identifier_match;
ALTER TABLE devices ADD CONSTRAINT devices_protocol_identifier_match CHECK (
    (protocol = 'jt808' AND jt808_terminal_id IS NOT NULL AND gt06_imei IS NULL)
    OR (protocol IN ('gt06', 'gt06_video') AND gt06_imei IS NOT NULL AND jt808_terminal_id IS NULL)
);

COMMENT ON COLUMN devices.protocol IS 'Hardware connection protocol: jt808 (camera/MDVR, JT1078), gt06 (GPS tracker without camera) or gt06_video (GT06 dashcam with RTMP video, e.g. JIMI JC261/JC400).';
