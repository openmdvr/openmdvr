-- Include ignition_changed_at/power_changed_at in the device_status payload.
-- Without them (0047), the status ICON updated live but the "X ago" label kept
-- reading the stale value from the last REST poll (up to 60s behind).
-- NEW.ignition_changed_at/power_changed_at are already correct here:
-- UpdateDeviceStatus (Go) sets them in the SAME UPDATE statement that fires
-- this trigger, before AFTER UPDATE is evaluated.
CREATE OR REPLACE FUNCTION notify_device_status_change() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.ignition_on IS DISTINCT FROM OLD.ignition_on THEN
        PERFORM insert_alarm(
            NEW.tenant_id, NEW.id, now(),
            CASE WHEN NEW.ignition_on THEN 'ignition_on' ELSE 'ignition_off' END,
            'info'::alarm_severity
        );
    END IF;
    IF NEW.power_connected IS DISTINCT FROM OLD.power_connected THEN
        PERFORM insert_alarm(
            NEW.tenant_id, NEW.id, now(),
            CASE WHEN NEW.power_connected THEN 'power_connected' ELSE 'power_cut' END,
            (CASE WHEN NEW.power_connected THEN 'info' ELSE 'warning' END)::alarm_severity
        );
    END IF;

    IF NEW.ignition_on IS DISTINCT FROM OLD.ignition_on
        OR NEW.power_connected IS DISTINCT FROM OLD.power_connected
    THEN
        PERFORM pg_notify(
            'device_status',
            json_build_object(
                'tenant_id', NEW.tenant_id,
                'device_id', NEW.id,
                'ignition_on', NEW.ignition_on,
                'power_connected', NEW.power_connected,
                'ignition_changed_at', NEW.ignition_changed_at,
                'power_changed_at', NEW.power_changed_at
            )::text
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
