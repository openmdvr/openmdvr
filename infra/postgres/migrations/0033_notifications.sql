-- In-app notification mailbox + fan-out. The fan-out is computed at EVENT time
-- (never cached) and lives INSIDE insert_alarm(), the same SECURITY DEFINER
-- function used by both jt808server and gt06server, so the pipeline covers both
-- device types without touching any Go code.
--
-- notifications is a normal table (not a hypertable) for now. If volume grows,
-- the same retention pattern as alarms/gps_positions (0019_gps_retention.sql)
-- can be applied; premature without real volume data.

CREATE TYPE notification_email_status AS ENUM ('not_applicable', 'pending', 'sent', 'failed');

CREATE TABLE notifications (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    recipient_user_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- Free text on purpose (currently only 'device_alarm'): future emitters
    -- (driver_shift_alert/device_offline/billing_overdue) can join the SAME
    -- pipeline (mailbox + SSE + email) without a new migration, same as
    -- alarms.alarm_type.
    event_type          TEXT NOT NULL,
    device_id           UUID REFERENCES devices(id) ON DELETE SET NULL,
    -- Soft reference to alarms (id, without "time"): a real FK to a hypertable
    -- with composite PK (id, time) would drag "time" into the FK, and alarms
    -- never grants direct privileges to app_user (see
    -- 0009_timeseries_access.sql). This column is purely informational,
    -- resolved with a best-effort JOIN in the API.
    alarm_id            UUID,
    alarm_time          TIMESTAMPTZ,
    title               TEXT NOT NULL,
    body                TEXT,
    severity            alarm_severity,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at             TIMESTAMPTZ,
    -- Denormalized copy (at event time) of the recipient's in_app preference.
    -- Security finding F5: the row is created even when ONLY email is enabled
    -- (the email worker needs it for its email_status queue), but
    -- GET /notifications and the SSE stream must honor this column so they never
    -- show/push something the user explicitly turned off for the platform.
    in_app_enabled      BOOLEAN NOT NULL DEFAULT true,
    -- Avoids a separate outbox table: the email worker does
    -- SELECT ... WHERE email_status = 'pending' FOR UPDATE SKIP LOCKED directly
    -- on this row.
    email_status        notification_email_status NOT NULL DEFAULT 'not_applicable'
);

CREATE INDEX notifications_recipient_created_idx ON notifications (recipient_user_id, created_at DESC);
CREATE INDEX notifications_recipient_unread_idx ON notifications (recipient_user_id) WHERE read_at IS NULL;
CREATE INDEX notifications_email_pending_idx ON notifications (email_status) WHERE email_status = 'pending';
CREATE INDEX notifications_tenant_id_idx ON notifications (tenant_id);
-- Used by the Python listener to resolve recipients after a lightweight
-- pg_notify (see below, finding F1): one SELECT by alarm_id right after
-- receiving the notification.
CREATE INDEX notifications_alarm_id_idx ON notifications (alarm_id);

COMMENT ON TABLE notifications IS 'Per-user in-app mailbox: one row per (alarm, recipient). Written ONLY by insert_alarm() (SECURITY DEFINER); app_user never has direct INSERT.';

-- ---------------------------------------------------------------------------
-- RLS: the first policy in this schema that filters by individual USER rather
-- than tenant (recipient_user_id, not tenant_id). SELECT and UPDATE are granted
-- to app_user; INSERT has NO grant: the only real write is the fan-out inside
-- insert_alarm(), which runs as the function owner (RLS-exempt), never as
-- app_user.
--
-- Column-scoped GRANT UPDATE on read_at ONLY (security finding F3): with a
-- whole-table GRANT, any session could rewrite tenant_id/email_status/title/
-- severity of its OWN row via direct SQL. Not reachable over HTTP (the router
-- only touches read_at), but security must not depend on the API never changing
-- that discipline. email_status is updated by the email worker with a bypass
-- session, which still sees the whole table via app_bypass_rls() in RLS, but
-- GRANT UPDATE (read_at) does NOT let bypass touch email_status either: the
-- email worker must use its own SECURITY DEFINER function, never a full-column
-- app_user UPDATE.
-- ---------------------------------------------------------------------------
ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications FORCE ROW LEVEL SECURITY;

CREATE POLICY notifications_select ON notifications
    FOR SELECT USING (app_bypass_rls() OR recipient_user_id = app_current_user_id());

-- WITH CHECK repeats the USING condition: without it, a session could (in
-- theory) rewrite recipient_user_id of its own row to ANOTHER user in the same
-- UPDATE.
CREATE POLICY notifications_update ON notifications
    FOR UPDATE
    USING (app_bypass_rls() OR recipient_user_id = app_current_user_id())
    WITH CHECK (app_bypass_rls() OR recipient_user_id = app_current_user_id());

GRANT SELECT ON notifications TO app_user;
GRANT UPDATE (read_at) ON notifications TO app_user;

-- ---------------------------------------------------------------------------
-- Fan-out: insert_alarm() is REDEFINED here (0009 is not edited), adding after
-- the real alarm INSERT a BEGIN...EXCEPTION WHEN OTHERS block (SAVEPOINT): a bug
-- in the notification fan-out must NEVER prevent a real dashcam/GPS alarm from
-- being stored.
--
-- Recipients: app_device_recipients(device_id) (redefined below, finding F6),
-- which already binds tenant_admin to the device's tenant. Filtered through
-- user_notification_settings (LEFT JOIN + COALESCE to the true/false defaults
-- if the user never configured anything, same as
-- GET /users/{id}/notification-settings). A recipient with BOTH channels off
-- gets no row, avoiding a mailbox full of rows nobody will see.
--
-- Security finding F1, the most serious one here: pg_notify() has a HARD
-- 8000-byte payload limit. The original version put the full array of
-- recipient_user_ids in the payload and lived in the SAME BEGIN/EXCEPTION block
-- as the INSERT. Beyond ~205 recipients (a tenant_admin automatically receives
-- EVERYTHING in its tenant, so a tenant only needs to grow in users), pg_notify
-- failed with "payload string too long" and the EXCEPTION ALSO rolled back the
-- notifications rows already inserted: a TOTAL and SILENT loss of the fan-out
-- for that tenant, with no metric to surface it. Fixed by separating the fan-out
-- (INSERT, its own BEGIN/EXCEPTION) from the NOTIFY (fixed lightweight payload:
-- only tenant_id + alarm_id, NEVER the recipient list; this also closes F4,
-- which exposed other users' IDs to any connected client) in a SEPARATE
-- BEGIN/EXCEPTION: if notifying fails, the written rows survive. The Python
-- listener (api/app/notifications.py) resolves recipients with its own SELECT
-- on notifications WHERE alarm_id = $1 after receiving the NOTIFY.
--
-- The 'Alarm: ' title prefix is a stored data format parsed by the frontend
-- (web/src/lib/alarmLabels.ts); keep it in sync if changed.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION insert_alarm(
    p_tenant_id          UUID,
    p_device_id          UUID,
    p_time               TIMESTAMPTZ,
    p_alarm_type         TEXT,
    p_severity           alarm_severity DEFAULT 'warning',
    p_details            JSONB DEFAULT NULL,
    p_video_evidence_key TEXT DEFAULT NULL
) RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    new_id UUID;
    recipient_count INT;
BEGIN
    IF NOT (app_bypass_rls() OR p_tenant_id = app_current_tenant_id()) THEN
        RAISE EXCEPTION 'tenant_id % not authorized for this session', p_tenant_id
            USING ERRCODE = '42501';
    END IF;
    INSERT INTO alarms (time, tenant_id, device_id, alarm_type, severity, details, video_evidence_key)
    VALUES (p_time, p_tenant_id, p_device_id, p_alarm_type, p_severity, p_details, p_video_evidence_key)
    RETURNING id INTO new_id;

    -- Block 1: the actual fan-out (INSERT). Its own BEGIN/EXCEPTION: it must
    -- never prevent the alarm from being stored.
    BEGIN
        -- A WITH containing a data-modifying CTE (INSERT ... RETURNING) is only
        -- allowed as a top-level statement ("WITH clause containing a
        -- data-modifying statement must be at the top level" when nested in a
        -- scalar subquery), hence the top-level SELECT ... INTO, never PERFORM.
        WITH recipients AS (
            SELECT r.user_id,
                   COALESCE(s.in_app_enabled, true) AS in_app_enabled,
                   COALESCE(s.email_enabled, false) AS email_enabled
            FROM app_device_recipients(p_device_id) AS r(user_id)
            LEFT JOIN user_notification_settings s ON s.user_id = r.user_id
        ), inserted AS (
            INSERT INTO notifications (
                tenant_id, recipient_user_id, event_type, device_id, alarm_id, alarm_time,
                title, severity, in_app_enabled, email_status
            )
            SELECT p_tenant_id, user_id, 'device_alarm', p_device_id, new_id, p_time,
                   'Alarm: ' || p_alarm_type, p_severity, in_app_enabled,
                   -- Explicit cast: a CASE of untyped text literals resolves to
                   -- `unknown`/text, and INSERT...SELECT (unlike
                   -- INSERT...VALUES) does not cast it to the column's enum
                   -- type by itself.
                   (CASE WHEN email_enabled THEN 'pending' ELSE 'not_applicable' END)::notification_email_status
            FROM recipients
            WHERE in_app_enabled OR email_enabled
            RETURNING recipient_user_id
        )
        SELECT count(*) INTO recipient_count FROM inserted;
    EXCEPTION WHEN OTHERS THEN
        recipient_count := 0;
        RAISE WARNING 'insert_alarm: notification fan-out failed for alarm % (device %): %', new_id, p_device_id, SQLERRM;
    END;

    -- Block 2: NOTIFY, SEPARATE from the INSERT above, so a failure here can
    -- never roll back rows written in block 1. Deliberately small, FIXED-size
    -- payload (never grows with the number of recipients); see the comment
    -- above this function.
    IF recipient_count > 0 THEN
        BEGIN
            PERFORM pg_notify(
                'notifications',
                json_build_object('tenant_id', p_tenant_id, 'alarm_id', new_id)::text
            );
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'insert_alarm: pg_notify failed for alarm %: %', new_id, SQLERRM;
        END;
    END IF;

    RETURN new_id;
END;
$$;

REVOKE ALL ON FUNCTION insert_alarm(UUID, UUID, TIMESTAMPTZ, TEXT, alarm_severity, JSONB, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION insert_alarm(UUID, UUID, TIMESTAMPTZ, TEXT, alarm_severity, JSONB, TEXT) TO app_user;

-- ---------------------------------------------------------------------------
-- Security finding F6: app_device_recipients()/app_can_view_device()
-- (0031_device_groups_and_assignments.sql) never explicitly excluded the driver
-- role from the direct/group assignment branches. 0031 claimed a driver "never
-- has rows" there, but no data layer guaranteed it (neither the tenant trigger
-- nor those tables' RLS), only `_ASSIGNABLE_ROLES` in the API. Confirmed
-- exploitable: a driver inserted by hand into user_device_group_assignments DID
-- receive a notifications row. Both functions are redefined here (not in 0031)
-- adding `AND u.role <> 'driver'` to the direct/group branches. The
-- tenant_admin branch does not need it (a driver never has that role, per the
-- users_tenant_role_consistency CHECK of 0005/0015).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_device_recipients(target_device_id UUID) RETURNS SETOF UUID
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT u.id
    FROM users u
    JOIN devices d ON d.tenant_id = u.tenant_id
    WHERE d.id = target_device_id
      AND u.status = 'active'
      AND u.role = 'tenant_admin'
    UNION
    SELECT a.user_id
    FROM user_device_assignments a
    JOIN users u ON u.id = a.user_id
    WHERE a.device_id = target_device_id AND u.status = 'active' AND u.role <> 'driver'
    UNION
    SELECT uga.user_id
    FROM user_device_group_assignments uga
    JOIN device_group_members dgm ON dgm.device_group_id = uga.device_group_id
    JOIN users u ON u.id = uga.user_id
    WHERE dgm.device_id = target_device_id AND u.status = 'active' AND u.role <> 'driver'
$$;

REVOKE ALL ON FUNCTION app_device_recipients(UUID) FROM PUBLIC;

CREATE OR REPLACE FUNCTION app_can_view_device(target_device_id UUID) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT
        app_bypass_rls()
        OR EXISTS (
            SELECT 1 FROM users u
            JOIN devices d ON d.id = target_device_id AND d.tenant_id = u.tenant_id
            WHERE u.id = app_current_user_id() AND u.role = 'tenant_admin' AND u.status = 'active'
        )
        OR EXISTS (
            SELECT 1 FROM user_device_assignments a
            JOIN users u ON u.id = a.user_id
            WHERE a.user_id = app_current_user_id() AND a.device_id = target_device_id AND u.role <> 'driver'
        )
        OR EXISTS (
            SELECT 1 FROM user_device_group_assignments uga
            JOIN device_group_members dgm ON dgm.device_group_id = uga.device_group_id
            JOIN users u ON u.id = uga.user_id
            WHERE uga.user_id = app_current_user_id() AND dgm.device_id = target_device_id AND u.role <> 'driver'
        )
$$;

REVOKE ALL ON FUNCTION app_can_view_device(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_can_view_device(UUID) TO app_user;
