-- A real notification on every ignition/power change. Extends the SAME
-- trigger from 0047 (AFTER UPDATE ON devices, which already fires ONLY on real
-- transitions via IS DISTINCT FROM: never on a PATCH of label/vehicle/etc, nor
-- on a heartbeat repeating the same state) to also call insert_alarm(), the
-- SAME SECURITY DEFINER function (0009/0033/0037) that already does the FULL
-- fan-out (in-app mailbox + pg_notify for the webhook dispatcher) for every
-- other alarm. No new notification code; the audited pipeline is reused.
--
-- alarm_type has NO gt06_ prefix on purpose: this trigger fires regardless of
-- which protocol wrote the row. Both JT808
-- (jt808server/handlers.go::handleLocation) and GT06
-- (gt06server/handlers.go::handleAlarm/handleHeartbeat) call
-- UpdateDeviceStatus, the single write path.
--
-- Severity: ignition and "power connected" are routine transitions (info); a
-- driver turning the vehicle on/off several times a day should not read as
-- urgent. "Power cut" stays at warning, same severity as gt06_power_cut (the
-- existing explicit signal for the same kind of event), most associated with
-- theft/tampering.
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
                'power_connected', NEW.power_connected
            )::text
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
