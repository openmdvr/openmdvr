-- Map tile provider. Provides resilience if a tile provider fails or hits its
-- quota, plus a manual super_admin override in case a provider fails without
-- the automatic detection noticing. The override is an escape hatch, NEVER the
-- expected path.
--
-- active_provider = 'auto' (default) tells the frontend to decide on its own
-- through its automatic tile-error failover (web/src/lib/mapProviders.ts);
-- this value never chooses the provider in 'auto' mode. Any other value FORCES
-- that provider for ALL sessions, skipping automatic detection. That is why the
-- API ENDPOINT (not this RLS policy; see the UPDATE comment below) restricts it
-- to super_admin, same as creating/editing billing_plans: the super_admin vs.
-- support distinction never lives in RLS in this project, only in the API's
-- require_super_admin.
CREATE TABLE platform_map_settings (
    -- Single row; same singleton pattern as platform_monitoring_settings.
    id              BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    active_provider TEXT NOT NULL DEFAULT 'auto'
        CHECK (active_provider IN ('auto', 'osm', 'esri', 'carto')),
    -- Audit of the manual override: who forced a specific provider and when
    -- (NULL in 'auto', the expected state).
    forced_by       UUID REFERENCES users(id) ON DELETE SET NULL,
    forced_at       TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER platform_map_settings_set_updated_at
    BEFORE UPDATE ON platform_map_settings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO platform_map_settings (id) VALUES (true);

COMMENT ON TABLE platform_map_settings IS 'Map tile provider. "auto" delegates to the frontend''s automatic tile-error failover; any other value is a manual super_admin override, an escape hatch that should never be needed in normal operation.';

ALTER TABLE platform_map_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_map_settings FORCE ROW LEVEL SECURITY;

-- SELECT: any authenticated session. ALL tenants must see the same forced
-- provider if there is one, same as platform_monitoring_settings (global
-- technical value, not sensitive).
CREATE POLICY platform_map_settings_select ON platform_map_settings
    FOR SELECT USING (true);
-- UPDATE: bypass at the RLS level (like billing_plans). The real restriction
-- to super_admin only (not support) lives in require_super_admin in
-- api/app/routers/platform.py, never here; RLS in this project only separates
-- platform from tenant.
CREATE POLICY platform_map_settings_update ON platform_map_settings
    FOR UPDATE USING (app_bypass_rls()) WITH CHECK (app_bypass_rls());
-- No INSERT/DELETE policy: the only row is seeded by this migration.

GRANT SELECT, UPDATE ON platform_map_settings TO app_user;
