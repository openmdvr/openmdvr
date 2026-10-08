-- Configurable threshold for considering a device "online / seen now".
-- Previously a hard-coded 5-minute constant in the frontend, applied
-- inconsistently across views.
--
-- This is a network/technical parameter (how often a real device sends a
-- heartbeat), not a product/pricing decision. So, unlike
-- platform_billing_settings (bypass-only both ways), SELECT is allowed for ANY
-- authenticated session (a tenant session needs this value to render the
-- status of ITS OWN devices with the same criteria as the platform). Only
-- UPDATE is bypass-only.
CREATE TABLE platform_monitoring_settings (
    -- Single row; same singleton pattern as platform_billing_settings.
    id                                 BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    device_offline_threshold_seconds   INTEGER NOT NULL DEFAULT 300
        CHECK (device_offline_threshold_seconds > 0),
    updated_at                         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER platform_monitoring_settings_set_updated_at
    BEFORE UPDATE ON platform_monitoring_settings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO platform_monitoring_settings (id) VALUES (true);

COMMENT ON TABLE platform_monitoring_settings IS 'Non-billing platform operating settings; currently only the "device online / seen now" threshold. Unlike platform_billing_settings, readable by any authenticated session (global technical value, not sensitive).';

ALTER TABLE platform_monitoring_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_monitoring_settings FORCE ROW LEVEL SECURITY;

-- Any session with a valid JWT may read it (including tenant sessions and
-- drivers): a single integer of seconds is not sensitive, and EVERY session
-- needs it to render its own devices' status consistently.
CREATE POLICY platform_monitoring_settings_select ON platform_monitoring_settings
    FOR SELECT USING (true);
-- Editing is operational support/platform work, like adjusting a tenant's
-- max_live_view_seconds; it does not require super_admin.
CREATE POLICY platform_monitoring_settings_update ON platform_monitoring_settings
    FOR UPDATE USING (app_bypass_rls()) WITH CHECK (app_bypass_rls());
-- No INSERT/DELETE policy: the only row is seeded by this migration.

GRANT SELECT, UPDATE ON platform_monitoring_settings TO app_user;
