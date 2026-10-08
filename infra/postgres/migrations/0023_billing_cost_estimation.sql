-- Billing: estimated cost and margin per tenant. See 0020/0021/0022_billing_*.sql.
--
-- Design decision: there is no measured per-tenant "real cost" (hosting and
-- object storage bills are not broken down by tenant), so building an exact
-- costing system would fake precision we cannot measure. Instead,
-- `platform_billing_settings` (one editable row) holds cost assumptions, and a
-- tenant's estimated cost = active devices x cost per device + real bytes from
-- `usage_events` x cost per GB. It is exposed as an "estimate" in the API/UI,
-- never as an exact figure.
--
-- `exchange_rate_mxn_per_usd`: infrastructure costs are paid in USD while the
-- customer price may be in another currency (MXN here); without an exchange
-- rate no comparable margin can be computed. It is a MANUAL value edited by a
-- super_admin, not a live exchange-rate integration.
CREATE TABLE platform_billing_settings (
    -- Single row: boolean PRIMARY KEY that can only be true, so only ONE row
    -- can ever exist (singleton pattern used across this schema).
    id                          BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    cost_usd_per_device_month  NUMERIC(10, 4) NOT NULL DEFAULT 1.00 CHECK (cost_usd_per_device_month >= 0),
    cost_usd_per_gb            NUMERIC(10, 4) NOT NULL DEFAULT 0.02 CHECK (cost_usd_per_gb >= 0),
    exchange_rate_mxn_per_usd  NUMERIC(10, 4) NOT NULL DEFAULT 18.00 CHECK (exchange_rate_mxn_per_usd > 0),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER platform_billing_settings_set_updated_at
    BEFORE UPDATE ON platform_billing_settings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO platform_billing_settings (id) VALUES (true);

COMMENT ON TABLE platform_billing_settings IS 'Cost assumptions (single row) for the estimated per-tenant profitability report. Real infrastructure cost is not broken down by tenant; this is a configurable ESTIMATE, never exact accounting.';

ALTER TABLE platform_billing_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_billing_settings FORCE ROW LEVEL SECURITY;

-- Bypass-only, no exceptions: neither the estimated cost nor its inputs may
-- ever reach a tenant session, same as billing_plans (internal prices).
CREATE POLICY platform_billing_settings_select ON platform_billing_settings FOR SELECT USING (app_bypass_rls());
CREATE POLICY platform_billing_settings_update ON platform_billing_settings FOR UPDATE USING (app_bypass_rls()) WITH CHECK (app_bypass_rls());
-- No INSERT/DELETE policy: the only row is seeded by this migration; the app
-- never creates or deletes rows here.

GRANT SELECT, UPDATE ON platform_billing_settings TO app_user;
