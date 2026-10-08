-- Outgoing webhooks for M2M integrations: the "push" counterpart of API keys
-- (0034, "pull": a third party queries us). Enabling the feature for a tenant
-- is a platform decision, same pattern as plan attributes
-- (max_live_view_seconds, gps_retention_days): tenants.webhooks_enabled,
-- editable ONLY via the bypass-only PATCH /tenants/{id} (tenants.py), never
-- self-service. A tenant_admin can only create/manage THEIR OWN
-- webhook_endpoints once it is true, enforced by a trigger (not only in the
-- API) for defense in depth.
--
-- Efficiency (only run when needed): the source event (currently an alarm)
-- is still emitted via the SAME pg_notify('notifications', ...) used by the
-- in-app mailbox (0033), with ZERO changes to insert_alarm()/alarms. A new
-- listener (api/app/webhooks.py) listens on the SAME Postgres channel (several
-- sessions can LISTEN to one channel independently) and runs ONE cheap indexed
-- query per event ("does this tenant have the feature enabled AND at least one
-- endpoint subscribed to this event type?"). If not (the normal case), it stops
-- there: no HTTP calls, no delivery rows. When there is something to deliver,
-- enqueuing (webhook_deliveries) is separate from the DELIVERY itself (outgoing
-- HTTP with retries); a separate worker (exponential backoff + auto-disable
-- after consecutive failures, see webhooks.py) is what actually spends
-- time/network, never the listener.
ALTER TABLE tenants ADD COLUMN webhooks_enabled BOOLEAN NOT NULL DEFAULT false;
COMMENT ON COLUMN tenants.webhooks_enabled IS 'Enables the webhooks feature for this tenant. A PLATFORM decision (bypass-only PATCH /tenants/{id}), never self-service. Revoking it does NOT delete existing webhook_endpoints, but the dispatcher stops enqueuing new deliveries for them (see webhooks.py).';

-- ---------------------------------------------------------------------------
-- webhook_endpoints: managed by tenant_admin (or the platform) once the tenant
-- has webhooks_enabled=true. `secret` IS persisted in plain text (unlike
-- api_keys.key_hash). A deliberate design difference: an API key is a
-- credential SOMEONE ELSE presents to us (we only need to VERIFY a hash); a
-- webhook secret is something WE use repeatedly to SIGN every future delivery,
-- so we need the real value, not a hash. Same UX as api_keys: shown in full
-- ONLY on create (or rotate), never again in a listing; losing it means
-- rotating, not recovering.
-- ---------------------------------------------------------------------------
CREATE TABLE webhook_endpoints (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    url                   TEXT NOT NULL CHECK (char_length(url) BETWEEN 1 AND 2000),
    -- Free text (same as notifications.event_type): more event types can be
    -- added without a new migration.
    event_types           TEXT[] NOT NULL CHECK (array_length(event_types, 1) > 0),
    secret                TEXT NOT NULL,
    enabled               BOOLEAN NOT NULL DEFAULT true,
    -- Circuit breaker: a dead endpoint must not be retried forever; see
    -- enforce_webhook_endpoint_failure_threshold below.
    consecutive_failures  INT NOT NULL DEFAULT 0,
    disabled_at           TIMESTAMPTZ NULL,
    disabled_reason       TEXT NULL,
    last_attempt_at       TIMESTAMPTZ NULL,
    last_success_at       TIMESTAMPTZ NULL,
    created_by            UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX webhook_endpoints_tenant_id_idx ON webhook_endpoints (tenant_id);
-- Used by the dispatcher on EVERY event, so it must be cheap (see the
-- efficiency note above). GIN on the array so "event_types @> ARRAY[...]" uses
-- an index instead of scanning rows.
CREATE INDEX webhook_endpoints_event_types_gin_idx ON webhook_endpoints USING GIN (event_types);

COMMENT ON TABLE webhook_endpoints IS 'A tenant''s HTTP endpoints subscribed to outgoing events. Requires tenants.webhooks_enabled=true (platform-approved) to create a row.';

CREATE TRIGGER webhook_endpoints_set_updated_at
    BEFORE UPDATE ON webhook_endpoints
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- Trigger: requires tenants.webhooks_enabled=true when CREATING an endpoint.
-- Defense in depth on top of the API check, same as enforce_api_key_tenant_match
-- (0034). INSERT only: if the platform revokes the feature AFTER endpoints
-- exist, they must not become impossible to edit/disable/delete; the dispatcher
-- (not this trigger) actually pauses delivery by checking
-- tenants.webhooks_enabled on every event.
-- ---------------------------------------------------------------------------
-- SECURITY DEFINER is required here, not cosmetic: without it, a tenant_admin of
-- ANOTHER tenant trying to create an endpoint for a foreign tenant_id would run
-- this SELECT under THEIR OWN RLS on `tenants` (which hides every row but
-- theirs). enabled_flag would be NULL and this trigger would raise "feature not
-- enabled" (422) instead of letting the real webhook_endpoints RLS policy
-- (foreign tenant_id) reject with the correct 403.
CREATE OR REPLACE FUNCTION enforce_webhook_endpoint_tenant_enabled() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    enabled_flag BOOLEAN;
BEGIN
    SELECT webhooks_enabled INTO enabled_flag FROM tenants WHERE id = NEW.tenant_id;
    IF enabled_flag IS NOT TRUE THEN
        RAISE EXCEPTION 'tenant % does not have the webhooks feature enabled', NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER webhook_endpoints_enforce_tenant_enabled
    BEFORE INSERT ON webhook_endpoints
    FOR EACH ROW EXECUTE FUNCTION enforce_webhook_endpoint_tenant_enabled();

-- ---------------------------------------------------------------------------
-- Trigger: circuit breaker. Auto-disables an endpoint after too many
-- consecutive failures. Runs on every UPDATE of consecutive_failures (the
-- delivery worker increments/resets it, see webhooks.py), so the worker does
-- not have to remember this threshold in Python; it lives once, here,
-- regardless of what future code touches this column.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION enforce_webhook_endpoint_failure_threshold() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NEW.consecutive_failures >= 10 AND NEW.enabled THEN
        NEW.enabled := false;
        NEW.disabled_at := now();
        NEW.disabled_reason := 'too many consecutive delivery failures (10+)';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER webhook_endpoints_enforce_failure_threshold
    BEFORE UPDATE OF consecutive_failures ON webhook_endpoints
    FOR EACH ROW EXECUTE FUNCTION enforce_webhook_endpoint_failure_threshold();

-- ---------------------------------------------------------------------------
-- RLS: same pattern as api_keys (0034), with app_is_tenant_admin() inside the
-- policy from the start, not only require_tenant_admin in the API.
-- ---------------------------------------------------------------------------
ALTER TABLE webhook_endpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_endpoints FORCE ROW LEVEL SECURITY;

CREATE POLICY webhook_endpoints_select ON webhook_endpoints
    FOR SELECT USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
CREATE POLICY webhook_endpoints_insert ON webhook_endpoints
    FOR INSERT WITH CHECK (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
CREATE POLICY webhook_endpoints_update ON webhook_endpoints
    FOR UPDATE
    USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()))
    WITH CHECK (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
CREATE POLICY webhook_endpoints_delete ON webhook_endpoints
    FOR DELETE USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));

GRANT SELECT, INSERT, DELETE ON webhook_endpoints TO app_user;
-- All columns except immutable identity/audit ones (id/tenant_id/created_by/
-- created_at). tenant_admin edits url/event_types/enabled/secret (on rotate);
-- the delivery worker (bypass connection, same app_user role) updates
-- consecutive_failures/disabled_*/last_attempt_at/last_success_at. RLS decides
-- WHO may touch the row; this only decides WHICH columns UPDATE accepts at all.
GRANT UPDATE (url, event_types, secret, enabled, consecutive_failures,
              disabled_at, disabled_reason, last_attempt_at, last_success_at, updated_at)
    ON webhook_endpoints TO app_user;

-- ---------------------------------------------------------------------------
-- webhook_deliveries: audit + retry queue. Written by the dispatcher (INSERT,
-- when enqueuing) and the delivery worker (UPDATE, when resolving each
-- attempt), both via a bypass connection (same as api_key_usage_log, 0034). No
-- HTTP endpoint writes this directly, so no extra role dimension is needed on
-- INSERT/UPDATE (unlike api_keys, where an HTTP endpoint writes with the
-- caller's identity).
-- ---------------------------------------------------------------------------
CREATE TABLE webhook_deliveries (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    webhook_endpoint_id   UUID NOT NULL REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
    tenant_id             UUID NOT NULL,
    event_type            TEXT NOT NULL,
    payload               JSONB NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending', 'success', 'failed', 'exhausted')),
    attempt_count         INT NOT NULL DEFAULT 0,
    next_attempt_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    response_status_code  INT NULL,
    last_error            TEXT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at          TIMESTAMPTZ NULL
);

-- Queried by the worker on EVERY cycle. Partial (WHERE status='pending') so the
-- index stays small however much 'success'/'exhausted' history accumulates.
CREATE INDEX webhook_deliveries_pending_idx ON webhook_deliveries (next_attempt_at) WHERE status = 'pending';
CREATE INDEX webhook_deliveries_endpoint_idx ON webhook_deliveries (webhook_endpoint_id, created_at DESC);
CREATE INDEX webhook_deliveries_tenant_idx ON webhook_deliveries (tenant_id, created_at DESC);

COMMENT ON TABLE webhook_deliveries IS 'Audit + retry queue for webhook deliveries. Written only by the API process (dispatcher + worker) via a bypass connection, never directly by an HTTP endpoint.';

ALTER TABLE webhook_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_deliveries FORCE ROW LEVEL SECURITY;

CREATE POLICY webhook_deliveries_select ON webhook_deliveries
    FOR SELECT USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
CREATE POLICY webhook_deliveries_insert ON webhook_deliveries
    FOR INSERT WITH CHECK (app_bypass_rls());
CREATE POLICY webhook_deliveries_update ON webhook_deliveries
    FOR UPDATE USING (app_bypass_rls()) WITH CHECK (app_bypass_rls());

GRANT SELECT ON webhook_deliveries TO app_user;
GRANT INSERT, UPDATE ON webhook_deliveries TO app_user;

-- ---------------------------------------------------------------------------
-- Retention: same reasoning as api_key_usage_log (0034); this table is written
-- on every delivered event and infrastructure cost must stay minimal. Fixed
-- global 90-day window, same add_job() mechanism used elsewhere.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE enforce_webhook_delivery_retention(job_id INT, config JSONB)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    DELETE FROM webhook_deliveries WHERE created_at < now() - interval '90 days';
END;
$$;

COMMENT ON PROCEDURE enforce_webhook_delivery_retention(INT, JSONB) IS
    'Daily job: deletes webhook deliveries older than 90 days. Fixed global window, same as api_key_usage_log.';

REVOKE ALL ON PROCEDURE enforce_webhook_delivery_retention(INT, JSONB) FROM PUBLIC;
GRANT EXECUTE ON PROCEDURE enforce_webhook_delivery_retention(INT, JSONB) TO app_user;

SELECT add_job('enforce_webhook_delivery_retention', '1 day');
