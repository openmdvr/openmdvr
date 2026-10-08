-- Real-time GPS positions ("push, not polling"): adds a PERFORM pg_notify() to
-- insert_gps_position(), the same SECURITY DEFINER function from
-- 0009_timeseries_access.sql, with its authorization checks untouched.
-- CREATE OR REPLACE keeps existing GRANT/REVOKE on the function, so they are
-- not repeated here.
--
-- IMPORTANT: NOTIFY has no permission model. Any session with app_user
-- credentials that runs LISTEN gps_positions receives events for ALL tenants;
-- Postgres does not filter the channel by RLS or anything else. Real tenant
-- isolation for this feature lives entirely in the API
-- (api/app/live_positions.py: PositionBroadcaster), NEVER in this NOTIFY.
--
-- The payload deliberately omits the device label (avoids an extra JOIN on
-- the hottest path in the system, one per incoming position from any device);
-- the frontend already has the label by device_id from its roster.
--
-- NOTIFY is only delivered if this transaction COMMITs (native Postgres
-- guarantee), so a rolled-back INSERT can never fire a notification.
--
-- This function is protocol-agnostic: it only takes already-resolved
-- tenant_id/device_id (UUIDs) plus position data. Any protocol server that
-- resolves its own device_id (JT808 terminal ID, GT06 IMEI, etc.) gets the full
-- real-time push by calling it, without touching this migration,
-- live_positions.py or the frontend.

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
END;
$$;
