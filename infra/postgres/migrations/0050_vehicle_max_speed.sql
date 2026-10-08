-- Configurable maximum speed per unit + a real overspeed alarm. Same approach
-- as ignition/power (0046/0048): the hook point is insert_gps_position(), the
-- SAME SECURITY DEFINER function called by both jt808server (JT808) and
-- gt06server (GT06). A future protocol that resolves its own device_id and
-- calls this function inherits the max-speed check for free, without touching
-- this migration or any per-protocol code.
--
-- max_speed_kmh lives on `vehicles` (not `devices`): it is an operational limit
-- of THE VEHICLE (like distance and engine hours, which also resolve "the
-- device currently linked to this vehicle"), not an attribute of the tracking
-- hardware. Same as /vehicles/{id}/distance and /vehicles/{id}/engine-hours.
ALTER TABLE vehicles ADD COLUMN max_speed_kmh NUMERIC(5,1) CHECK (max_speed_kmh IS NULL OR max_speed_kmh > 0);

COMMENT ON COLUMN vehicles.max_speed_kmh IS
    'Speed limit configurable by the tenant_admin. NULL = no limit configured, the overspeed alarm never fires. See insert_gps_position() for the actual check.';

-- overspeed_active lives on `devices` instead of being derived from `alarms`:
-- we need to know whether the device IS ALREADY over the limit to fire the
-- alarm ONLY on the upward crossing (an edge, like ignition_on/power_connected,
-- not a state repeated on every position). Without it, every GPS position
-- above the limit (one every ~20-30s while it lasts) would generate its own
-- alarm/notification.
ALTER TABLE devices ADD COLUMN overspeed_active BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN devices.overspeed_active IS
    'true while the last known position exceeded vehicles.max_speed_kmh. Only used to detect the enter/exit EDGE in insert_gps_position(); never exposed directly by the API.';

-- insert_gps_position(): CREATE OR REPLACE keeps existing GRANT/REVOKE
-- (0009/0018). The speed check runs AFTER the existing INSERT+NOTIFY, in its own
-- block: a failure there (e.g. unexpected vehicles/devices rows) must NEVER roll
-- back the real position already stored. A fragile side feature must never
-- break the main path.
CREATE OR REPLACE FUNCTION insert_gps_position(
    p_tenant_id UUID,
    p_device_id UUID,
    p_time      TIMESTAMPTZ,
    p_lat       DOUBLE PRECISION,
    p_lon       DOUBLE PRECISION,
    p_speed_kmh REAL DEFAULT NULL,
    p_heading   REAL DEFAULT NULL,
    p_altitude  REAL DEFAULT NULL,
    p_raw       JSONB DEFAULT NULL
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_max_speed NUMERIC;
    v_active    BOOLEAN;
BEGIN
    IF NOT (app_bypass_rls() OR p_tenant_id = app_current_tenant_id()) THEN
        RAISE EXCEPTION 'tenant_id % not authorized for this session', p_tenant_id
            USING ERRCODE = '42501';
    END IF;
    INSERT INTO gps_positions (time, tenant_id, device_id, lat, lon, speed_kmh, heading, altitude, raw)
    VALUES (p_time, p_tenant_id, p_device_id, p_lat, p_lon, p_speed_kmh, p_heading, p_altitude, p_raw);

    PERFORM pg_notify(
        'gps_positions',
        json_build_object(
            'tenant_id', p_tenant_id,
            'device_id', p_device_id,
            'lat', p_lat,
            'lon', p_lon,
            'speed_kmh', p_speed_kmh,
            'heading', p_heading,
            'time', p_time
        )::text
    );

    IF p_speed_kmh IS NOT NULL THEN
        BEGIN
            SELECT v.max_speed_kmh, d.overspeed_active INTO v_max_speed, v_active
            FROM devices d LEFT JOIN vehicles v ON v.id = d.vehicle_id
            WHERE d.id = p_device_id;

            IF v_max_speed IS NOT NULL THEN
                IF p_speed_kmh > v_max_speed AND NOT COALESCE(v_active, false) THEN
                    UPDATE devices SET overspeed_active = true WHERE id = p_device_id;
                    PERFORM insert_alarm(
                        p_tenant_id, p_device_id, p_time, 'overspeed_limit', 'warning',
                        jsonb_build_object('speed_kmh', p_speed_kmh, 'max_speed_kmh', v_max_speed)
                    );
                ELSIF p_speed_kmh <= v_max_speed AND COALESCE(v_active, false) THEN
                    UPDATE devices SET overspeed_active = false WHERE id = p_device_id;
                END IF;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            -- Must never roll back the position/NOTIFY already committed above.
            RAISE WARNING 'insert_gps_position: max speed check failed for device %: %', p_device_id, SQLERRM;
        END;
    END IF;
END;
$$;
