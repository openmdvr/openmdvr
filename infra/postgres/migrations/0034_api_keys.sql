-- API keys for machine-to-machine (M2M) integrations, e.g. a read-only user
-- with an API credential consumed by another system. Central design decision:
-- an API key is NEVER a parallel auth mechanism disconnected from the role
-- model. It authenticates AS an existing user (with their real
-- tenant_id/role/device assignments) and only ADDS two restrictions on top:
--   1. can_write=false: the key cannot use any HTTP method beyond
--      GET/HEAD/OPTIONS, whatever the underlying user could do (enforced in
--      api/app/deps.py, a single point for ALL present and future endpoints).
--   2. allowed_device_ids: narrows FURTHER which devices this specific key can
--      see/touch, within what the user could already see. Implemented as a
--      session GUC (app.api_key_device_filter, same mechanism as
--      app.driver_id/app.user_id since 0015/0031) read INSIDE
--      app_can_view_device(), the function that ALREADY guards
--      devices/gps_positions_v/alarms_v/device_commands/video (see
--      0032_device_visibility_rls.sql). Those endpoints and their table RLS
--      policies inherit the restriction by transitivity, as 0032 documented for
--      per-user assignment. notifications_select/_update ARE redefined below
--      because that table is isolated by recipient_user_id and never went
--      through app_can_view_device().
--
-- Why the key is NEVER stored in plain text: key_hash is HMAC-SHA256 of the full
-- key with a server-side pepper (API_KEY_PEPPER, deliberately separate from
-- JWT_SECRET so if one leaks the other still protects its mechanism). Unlike a
-- human password, the key already has 256 bits of entropy; a slow hash such as
-- bcrypt adds nothing and would cost real time on every request of a
-- high-volume integration.
--
-- Why the permission columns (can_write, allowed_device_ids, expires_at) are
-- IMMUTABLE after creation (no UPDATE grant on them): changing the scope of an
-- existing key without changing its identity would break the audit guarantee
-- ("this key always meant this"). To change scope: revoke and issue a new one,
-- never edit in place. There is no DELETE either: a revoked key stays in history
-- forever (truly auditable), only disabled at auth time by
-- revoked_at/expires_at.

CREATE TABLE api_keys (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- The key acts AS this user, inheriting role/tenant_id/real device
    -- assignments. Rejected by trigger if the user has no tenant (platform
    -- accounts): API keys are a tenant FLEET feature, not platform administration.
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
    -- Prefix ALWAYS visible (so the admin can identify the key in a list without
    -- seeing the full secret again), a common convention for API tokens.
    key_prefix          TEXT NOT NULL UNIQUE,
    key_hash            TEXT NOT NULL UNIQUE,
    -- Read-only/read-write umbrella; see the header comment.
    can_write           BOOLEAN NOT NULL DEFAULT false,
    -- NULL = "everything the user can already see" (no extra narrowing).
    -- Non-NULL = only these devices, validated by trigger to belong to the tenant.
    allowed_device_ids  UUID[] NULL,
    -- ON DELETE SET NULL (not RESTRICT/CASCADE) on purpose: if the account that
    -- created/revoked a key is ever deleted, the key must NOT disappear nor block
    -- deleting that account; it only loses the "created by" audit metadata.
    created_by          UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    -- Mandatory on purpose: never a "forever" key by default; forces real
    -- periodic rotation.
    expires_at          TIMESTAMPTZ NOT NULL,
    revoked_at          TIMESTAMPTZ NULL,
    revoked_by          UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    last_used_at        TIMESTAMPTZ NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX api_keys_tenant_id_idx ON api_keys (tenant_id);
CREATE INDEX api_keys_user_id_idx ON api_keys (user_id);

COMMENT ON TABLE api_keys IS 'API credentials for M2M integrations. They authenticate AS user_id (same role/tenant/device assignments), never an independent auth mechanism. can_write/allowed_device_ids/expires_at are immutable after creation; to change scope, revoke and issue a new key.';
COMMENT ON COLUMN api_keys.key_hash IS 'HMAC-SHA256(full key, API_KEY_PEPPER). The plain-text key is NEVER persisted; it is shown only once at creation.';

-- ---------------------------------------------------------------------------
-- Tenant/device validation, same pattern as enforce_vehicle_tenant_match (0014)/
-- enforce_notification_settings_tenant_match (0031): BEFORE INSERT OR UPDATE,
-- defense in depth on top of the API's own validation.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION enforce_api_key_tenant_match() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    u_tenant UUID;
    bad_device_count INT;
BEGIN
    -- Security finding F11: revoked_at must be MONOTONIC. "A revoked key stays
    -- in history forever" previously only lived in the COALESCE(revoked_at,
    -- now()) of a single endpoint (users.py::revoke_api_key) and was never
    -- enforced: GRANT UPDATE(revoked_at) allowed un-revoking via a direct UPDATE.
    IF TG_OP = 'UPDATE' AND OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NULL THEN
        RAISE EXCEPTION 'a revoked API key cannot be un-revoked';
    END IF;

    -- Security finding F2: this trigger ran the SAME tenant/device validation on
    -- INSERT and UPDATE, but the only real UPDATE the API does is
    -- revoked_at/revoked_by/last_used_at (grantable columns, see GRANT below),
    -- never user_id/allowed_device_ids (immutable, no GRANT). If a device in
    -- allowed_device_ids was deleted AFTER creating the key, the revalidation on
    -- UPDATE failed with a raw 500, making the key IRREVOCABLE (breaking the
    -- only kill switch for a leaked credential). Now it only revalidates if those
    -- columns ACTUALLY changed (never, given the GRANT, but correct if they ever
    -- did).
    IF TG_OP = 'INSERT'
       OR NEW.user_id IS DISTINCT FROM OLD.user_id
       OR NEW.allowed_device_ids IS DISTINCT FROM OLD.allowed_device_ids
    THEN
        SELECT tenant_id INTO u_tenant FROM users WHERE id = NEW.user_id;
        IF u_tenant IS NULL THEN
            RAISE EXCEPTION 'user_id % does not belong to any tenant; API keys are a tenant feature, not a platform one', NEW.user_id;
        END IF;
        IF u_tenant <> NEW.tenant_id THEN
            RAISE EXCEPTION 'user_id % is not valid for tenant_id %', NEW.user_id, NEW.tenant_id;
        END IF;
        IF NEW.allowed_device_ids IS NOT NULL THEN
            SELECT count(*) INTO bad_device_count
            FROM unnest(NEW.allowed_device_ids) AS d(id)
            WHERE NOT EXISTS (SELECT 1 FROM devices WHERE devices.id = d.id AND devices.tenant_id = NEW.tenant_id);
            IF bad_device_count > 0 THEN
                RAISE EXCEPTION 'allowed_device_ids contains a device outside tenant %', NEW.tenant_id;
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER api_keys_enforce_tenant
    BEFORE INSERT OR UPDATE ON api_keys
    FOR EACH ROW EXECUTE FUNCTION enforce_api_key_tenant_match();

-- ---------------------------------------------------------------------------
-- RLS: managed by tenant_admin. Security finding F10: the original policy was
-- tenant-wide WITHOUT a role dimension (app_bypass_rls() OR tenant_id = ...), so
-- require_tenant_admin in the API was the ONLY real barrier ("single layer of
-- defense"). app_is_tenant_admin() (0031) adds the role dimension INSIDE RLS: a
-- future endpoint touching api_keys with require_non_driver/get_current_user
-- instead of require_tenant_admin would no longer expose key_hash/revocation to
-- a tenant_operator/tenant_viewer.
--
-- Column-scoped UPDATE: ONLY revoked_at/revoked_by (the revoking admin) and
-- last_used_at (the auth process, via a bypass connection, updates it on every
-- use). No other column accepts UPDATE, with bypass or with RLS: rotating means
-- creating a new row, never mutating an existing key's scope. revoked_at
-- monotonicity (F11) is enforced in the trigger above, not here (a policy cannot
-- compare against OLD directly).
-- ---------------------------------------------------------------------------
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys FORCE ROW LEVEL SECURITY;

CREATE POLICY api_keys_select ON api_keys
    FOR SELECT USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
CREATE POLICY api_keys_insert ON api_keys
    FOR INSERT WITH CHECK (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
CREATE POLICY api_keys_update ON api_keys
    FOR UPDATE
    USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()))
    WITH CHECK (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));

GRANT SELECT, INSERT ON api_keys TO app_user;
GRANT UPDATE (revoked_at, revoked_by, last_used_at) ON api_keys TO app_user;

-- ---------------------------------------------------------------------------
-- api_key_usage_log: real usage audit. Written ONLY from a BYPASS connection of
-- the auth process itself (api/app/api_key_auth.py). "Bypass" is a GUC
-- (app.bypass_rls), not a different Postgres role: the connection is still
-- app_user, so the INSERT grant is unavoidable, but the WITH CHECK policy ties
-- it to app_bypass_rls(). No normal tenant session (JWT or API key) can insert
-- here even with the GRANT; RLS blocks it. No HTTP endpoint ever exposes a
-- direct INSERT to this table.
-- ---------------------------------------------------------------------------
CREATE TABLE api_key_usage_log (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    api_key_id   UUID NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    tenant_id    UUID NOT NULL,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    method       TEXT NOT NULL,
    path         TEXT NOT NULL,
    status_code  INT NOT NULL,
    ip_address   INET NULL
);

CREATE INDEX api_key_usage_log_key_idx ON api_key_usage_log (api_key_id, occurred_at DESC);
CREATE INDEX api_key_usage_log_tenant_idx ON api_key_usage_log (tenant_id, occurred_at DESC);

COMMENT ON TABLE api_key_usage_log IS 'Audit of every request authenticated by API key: who (api_key_id), when, which endpoint, with what result. Written only by the API process via a bypass connection, never by app_user in a normal session.';

ALTER TABLE api_key_usage_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_key_usage_log FORCE ROW LEVEL SECURITY;

CREATE POLICY api_key_usage_log_select ON api_key_usage_log
    FOR SELECT USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY api_key_usage_log_insert ON api_key_usage_log
    FOR INSERT WITH CHECK (app_bypass_rls());

GRANT SELECT, INSERT ON api_key_usage_log TO app_user;

-- ---------------------------------------------------------------------------
-- app_api_key_device_filter(): same mechanism as app_current_user_id()/
-- app_current_driver_id() (session GUC set by tenant_scoped_connection on EVERY
-- transaction, see api/app/db.py). NULL when the session is not an API key with
-- allowed_device_ids, or when the key has no such restriction; in both cases no
-- effect (fail-open with respect to THIS condition only, because the other
-- conditions of app_can_view_device()/notifications_select already decided
-- fail-closed on their own).
--
-- Deliberate encoding: distinguishing "not narrowed" from "narrowed to NO
-- device" with a single string is ambiguous if both use '' (string_to_array('',
-- ',') does NOT yield an empty array but a one-element array with an empty
-- string, and NULL/'' got conflated). '__unset__' (a sentinel that is never a
-- valid UUID) = no API key or no allowed_device_ids -> real NULL (no
-- restriction). '' (exact empty string) = deliberate allowed_device_ids=[] ->
-- ARRAY[]::uuid[] (not NULL); ANY() against an empty array is ALWAYS false and
-- correctly denies every device. Any other value: comma-separated UUIDs.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_api_key_device_filter() RETURNS UUID[]
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT CASE current_setting('app.api_key_device_filter', true)
        WHEN '__unset__' THEN NULL
        WHEN '' THEN ARRAY[]::uuid[]
        ELSE string_to_array(current_setting('app.api_key_device_filter', true), ',')::uuid[]
    END
$$;

COMMENT ON FUNCTION app_api_key_device_filter() IS
    'device_id list this API key is narrowed to (on top of what the user could already see): NULL = no extra restriction, ARRAY[]::uuid[] = narrowed to NO device (deny-all), non-empty list = only those devices.';

REVOKE ALL ON FUNCTION app_api_key_device_filter() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_api_key_device_filter() TO app_user;

-- ---------------------------------------------------------------------------
-- app_can_view_device() is redefined (0032 is not edited) adding the condition
-- above as a final AND. By transitivity, without touching routers/table RLS:
-- devices, gps_positions_v, alarms_v, device_commands.py, video.py, and the
-- allowed_device_ids computation of the positions SSE stream (positions.py,
-- which runs SELECT id FROM devices under the RLS-scoped connection) inherit
-- this restriction for ANY API key with allowed_device_ids.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_can_view_device(target_device_id UUID) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT (
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
    )
    AND (app_api_key_device_filter() IS NULL OR target_device_id = ANY(app_api_key_device_filter()))
$$;

REVOKE ALL ON FUNCTION app_can_view_device(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_can_view_device(UUID) TO app_user;

-- ---------------------------------------------------------------------------
-- notifications_select/_update: the only alarm/event surface that does NOT go
-- through app_can_view_device() (isolated by recipient_user_id, see 0033), so it
-- needs its own explicit device condition. device_id IS NULL (a future event
-- without a device) is EXCLUDED when a filter is active, fail-closed: a key
-- narrowed to specific devices must not see device-less events, even if the
-- underlying user would.
-- ---------------------------------------------------------------------------
DROP POLICY notifications_select ON notifications;
CREATE POLICY notifications_select ON notifications
    FOR SELECT USING (
        (app_bypass_rls() OR recipient_user_id = app_current_user_id())
        AND (
            app_api_key_device_filter() IS NULL
            OR (device_id IS NOT NULL AND device_id = ANY(app_api_key_device_filter()))
        )
    );

DROP POLICY notifications_update ON notifications;
CREATE POLICY notifications_update ON notifications
    FOR UPDATE
    USING (
        (app_bypass_rls() OR recipient_user_id = app_current_user_id())
        AND (
            app_api_key_device_filter() IS NULL
            OR (device_id IS NOT NULL AND device_id = ANY(app_api_key_device_filter()))
        )
    )
    WITH CHECK (app_bypass_rls() OR recipient_user_id = app_current_user_id());

-- ---------------------------------------------------------------------------
-- api_key_usage_log retention (security finding F6): a normal table (not a
-- hypertable) written on EVERY API-key-authenticated request, with no retention
-- policy, unlike gps_positions (0019, per tenant) and alarms (fixed 365 days).
-- At the default limit of 120 req/min per key, a single active integration writes
-- ~63M rows/year. Fixed GLOBAL window (not per tenant, same as alarms: it is an
-- operational/security log, not a plan feature); 90 days is enough to
-- investigate a recent incident. Same add_job() mechanism as
-- enforce_gps_position_retention/generate_invoices/enforce_billing_suspension;
-- the TimescaleDB job scheduler works the same for a non-hypertable.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE enforce_api_key_usage_log_retention(job_id INT, config JSONB)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    DELETE FROM api_key_usage_log WHERE occurred_at < now() - interval '90 days';
END;
$$;

COMMENT ON PROCEDURE enforce_api_key_usage_log_retention(INT, JSONB) IS
    'Daily job (add_job below): deletes API key audit rows older than 90 days. Fixed global window, not per tenant (operational log, not a plan feature).';

REVOKE ALL ON PROCEDURE enforce_api_key_usage_log_retention(INT, JSONB) FROM PUBLIC;
GRANT EXECUTE ON PROCEDURE enforce_api_key_usage_log_retention(INT, JSONB) TO app_user;

SELECT add_job('enforce_api_key_usage_log_retention', '1 day');
