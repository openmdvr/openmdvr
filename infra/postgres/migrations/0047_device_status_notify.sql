-- Real-time ignition/power status (same "push, not polling" approach as GPS,
-- 0018_gps_position_notify.sql, and in-app notifications, 0033/0034).
--
-- Unlike insert_gps_position(), writes to ignition_on/power_connected do NOT
-- go through a single SECURITY DEFINER function: jt808-server issues a direct
-- UPDATE (UpdateDeviceStatus, internal/db/devices.go) with its own
-- COALESCE/IS DISTINCT FROM logic. A TRIGGER is the right choke point: it fires
-- regardless of which code writes the row, and ONLY when
-- ignition_on/power_connected ACTUALLY change. Never on a PATCH of
-- label/vehicle_id/model/SIM that does not touch these columns, and never on
-- every last_seen_at (that would fire on EVERY heartbeat of EVERY device, more
-- volume than gps_positions, for "notify on change" semantics).
--
-- SAME ACL-less channel model as gps_positions: real isolation between
-- tenants/assigned devices lives in api/app/live_positions.py::
-- PositionBroadcaster (already filters by tenant_id + allowed_device_ids),
-- reused as-is. This channel joins the SAME listener/fan-out, not a parallel one.
CREATE FUNCTION notify_device_status_change() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.ignition_on IS DISTINCT FROM OLD.ignition_on
        OR NEW.power_connected IS DISTINCT FROM OLD.power_connected
    THEN
        PERFORM pg_notify(
            'device_status',
            json_build_object(
                'tenant_id', NEW.tenant_id,
                'device_id', NEW.id,
                'ignition_on', NEW.ignition_on,
                'power_connected', NEW.power_connected
            )::text
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER devices_notify_status_change
    AFTER UPDATE ON devices
    FOR EACH ROW EXECUTE FUNCTION notify_device_status_change();
