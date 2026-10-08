-- Wires app_can_view_device() (defined in 0031_device_groups_and_assignments.sql,
-- inert until this migration) into the three real telemetry read surfaces:
-- devices, alarms_v, gps_positions_v. From here on, a
-- tenant_operator/tenant_viewer ONLY sees devices assigned to them (directly
-- or via a group); tenant_admin and platform sessions still see everything.
--
-- By transitivity, without touching those files: GET /devices (table RLS),
-- GET /positions/latest (via gps_positions_v), GET /alarms (via alarms_v), and
-- POST /devices/{id}/video (api/app/routers/video.py, whose query goes through
-- get_db's RLS-scoped connection) inherit this restriction automatically. The
-- only place that does NOT go through RLS (pg_notify() has no ACL, see
-- api/app/live_positions.py) is closed separately: PositionBroadcaster filters
-- by device_id.
--
-- The 0031 backfill already assigned each EXISTING tenant_operator/
-- tenant_viewer to every device they could already see, so this migration
-- should not change observable behavior of existing accounts; it only starts
-- ENFORCING the limit going forward.

-- ---------------------------------------------------------------------------
-- devices: add the device condition inside the tenant-scoped term, without
-- touching the bypass term.
-- ---------------------------------------------------------------------------
DROP POLICY devices_select ON devices;
CREATE POLICY devices_select ON devices
    FOR SELECT
    USING (
        app_bypass_rls()
        OR (tenant_id = app_current_tenant_id() AND app_can_view_device(id))
    );

-- devices INSERT/UPDATE/DELETE stay exactly the same (INSERT bypass-only since
-- 0008_rls_policies.sql; UPDATE/DELETE tenant-wide without the assignment
-- filter: a tenant_admin still edits any device of their tenant. Per-user
-- assignment is a READ/notification rule, not inventory administration).

-- ---------------------------------------------------------------------------
-- gps_positions_v / alarms_v: same condition, security_barrier intact.
-- CREATE OR REPLACE VIEW is safe here: no column changes, only the WHERE.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW gps_positions_v WITH (security_barrier = true) AS
    SELECT * FROM gps_positions
    WHERE app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_can_view_device(device_id));

CREATE OR REPLACE VIEW alarms_v WITH (security_barrier = true) AS
    SELECT * FROM alarms
    WHERE app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_can_view_device(device_id));

-- usage_events_v intentionally unchanged: it is the billing ledger, queried
-- only by bypass-only/tenant_admin endpoints (see billing.py), never by a
-- tenant_operator/tenant_viewer.

-- ---------------------------------------------------------------------------
-- acknowledge_alarm(): security finding F5. The original function
-- (0009_timeseries_access.sql) only validated the tenant, never whether the
-- session can see the alarm's DEVICE. A tenant_operator/tenant_viewer whose
-- access to a device was revoked no longer sees it in GET /alarms, but if they
-- kept the UUID of an old alarm of that device (e.g. stored in the browser)
-- they could keep acknowledging it indefinitely: reads were narrowed by
-- alarms_v, writes were not. Redefined here (not in 0009) because
-- app_can_view_device() only exists since 0031.
CREATE OR REPLACE FUNCTION acknowledge_alarm(
    p_alarm_id UUID,
    p_acknowledged_by UUID
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    alarm_tenant UUID;
    alarm_device UUID;
BEGIN
    SELECT tenant_id, device_id INTO alarm_tenant, alarm_device FROM alarms WHERE id = p_alarm_id;
    -- Generic, identical message regardless of the cause (missing, other
    -- tenant, or device not visible): it neither confirms nor denies the
    -- existence of a foreign id, same as enforce_device_tenant_match (0007).
    IF NOT FOUND OR NOT (app_bypass_rls() OR (alarm_tenant = app_current_tenant_id() AND app_can_view_device(alarm_device))) THEN
        RAISE EXCEPTION 'alarm % not valid for this session', p_alarm_id USING ERRCODE = '42501';
    END IF;
    UPDATE alarms SET acknowledged_at = now(), acknowledged_by = p_acknowledged_by
    WHERE id = p_alarm_id;
END;
$$;
