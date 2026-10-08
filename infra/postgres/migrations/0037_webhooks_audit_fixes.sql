-- Fixes from the webhooks security review (0035/0036): 3 findings that live in
-- the database.
--
-- MEDIUM-1: insert_alarm() (0033) only emitted pg_notify('notifications', ...)
-- when the in-app mailbox fan-out produced >=1 recipient (recipient_count > 0).
-- The webhook dispatcher (api/app/webhooks.py) consumes THAT SAME channel, so an
-- alarm whose tenant users had all muted notifications (in_app_enabled=false,
-- email_enabled=false) never produced a NOTIFY and was therefore NEVER delivered
-- by webhook either, with no error or trace. In a fleet safety product, a
-- critical alarm silently not being pushed because of someone else's UI
-- preference is worse than a loud failure. Fix: NOTIFY is now ALWAYS emitted
-- once the alarm is inserted, regardless of in-app recipients; the payload is
-- fixed-size, so the cost is negligible.
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
    -- The 'Alarm: ' title prefix is a stored data format parsed by the
    -- frontend (web/src/lib/alarmLabels.ts); do not change it here alone.
    BEGIN
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
    -- never roll back rows written in block 1. ALWAYS emitted (previously only
    -- when recipient_count > 0, see header): the in-app mailbox and outgoing
    -- webhooks are two INDEPENDENT consumers of the same event, and neither may
    -- switch the other off.
    BEGIN
        PERFORM pg_notify(
            'notifications',
            json_build_object('tenant_id', p_tenant_id, 'alarm_id', new_id)::text
        );
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'insert_alarm: pg_notify failed for alarm %: %', new_id, SQLERRM;
    END;

    RETURN new_id;
END;
$$;

REVOKE ALL ON FUNCTION insert_alarm(UUID, UUID, TIMESTAMPTZ, TEXT, alarm_severity, JSONB, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION insert_alarm(UUID, UUID, TIMESTAMPTZ, TEXT, alarm_severity, JSONB, TEXT) TO app_user;

-- ---------------------------------------------------------------------------
-- LOW-2: 0035's GRANT UPDATE on webhook_endpoints necessarily includes columns
-- that only the delivery WORKER should touch (consecutive_failures/disabled_at/
-- disabled_reason/last_attempt_at/last_success_at), because the worker runs as
-- the SAME app_user role as a tenant_admin (only the bypass GUC differs); RLS
-- alone cannot distinguish "who" beyond bypass/no-bypass. A tenant_admin
-- session (no bypass) could set consecutive_failures=0/enabled=true/
-- disabled_at=NULL via direct SQL. Not exploitable over HTTP, but the same
-- pattern already closed with a trigger for api_keys.revoked_at (0034).
--
-- A non-bypass session may RESET these columns to their healthy defaults
-- (consecutive_failures=0, disabled_at/disabled_reason=NULL), needed for the
-- legitimate "re-enable manually" flow of PATCH /webhook-endpoints/{id}, but
-- can never FORGE an arbitrary value (a fake consecutive_failures, an invented
-- disabled_reason) nor touch last_attempt_at/last_success_at at all.
CREATE OR REPLACE FUNCTION enforce_webhook_endpoint_worker_columns() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NOT app_bypass_rls() THEN
        IF NEW.last_attempt_at IS DISTINCT FROM OLD.last_attempt_at
           OR NEW.last_success_at IS DISTINCT FROM OLD.last_success_at
        THEN
            RAISE EXCEPTION 'only the webhook delivery worker may modify last_attempt_at/last_success_at'
                USING ERRCODE = '42501';
        END IF;
        IF NEW.consecutive_failures IS DISTINCT FROM OLD.consecutive_failures AND NEW.consecutive_failures <> 0 THEN
            RAISE EXCEPTION 'a non-bypass session can only reset consecutive_failures to 0'
                USING ERRCODE = '42501';
        END IF;
        IF (NEW.disabled_at IS DISTINCT FROM OLD.disabled_at AND NEW.disabled_at IS NOT NULL)
           OR (NEW.disabled_reason IS DISTINCT FROM OLD.disabled_reason AND NEW.disabled_reason IS NOT NULL)
        THEN
            RAISE EXCEPTION 'a non-bypass session can only clear disabled_at/disabled_reason, never set them'
                USING ERRCODE = '42501';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER webhook_endpoints_enforce_worker_columns
    BEFORE UPDATE ON webhook_endpoints
    FOR EACH ROW EXECUTE FUNCTION enforce_webhook_endpoint_worker_columns();

-- ---------------------------------------------------------------------------
-- LOW-4 (hygiene): the other 0035 functions already REVOKE ALL FROM PUBLIC
-- explicitly; this one was missed. Not exploitable (Postgres refuses to invoke
-- a trigger function directly, and app_user has no CREATE privilege on schema
-- public to reuse it in its own trigger), but keeps the hygiene consistent.
REVOKE ALL ON FUNCTION enforce_webhook_endpoint_tenant_enabled() FROM PUBLIC;
