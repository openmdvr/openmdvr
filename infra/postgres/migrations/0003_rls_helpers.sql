-- Helper functions that read the session context set by the API in each
-- transaction. Centralized here so that ALL RLS policies use exactly the same
-- security logic; a new table never reimplements (and potentially breaks) the
-- isolation condition.
--
-- Contract with the application layer (mandatory, also documented in
-- docs/architecture.md):
--   1. When validating a JWT, the API decides whether the user is "bypass"
--      (role super_admin or support) or "tenant-scoped" (any other role).
--   2. At the start of EVERY transaction, before any business query, the API
--      runs EXACTLY one of these two sequences:
--        - bypass:        SELECT set_config('app.bypass_rls', 'true', true);
--        - tenant-scoped: SELECT set_config('app.tenant_id', '<uuid>', true);
--                          SELECT set_config('app.bypass_rls', 'false', true);
--   3. The third argument of set_config MUST always be `true` (is_local): the
--      variable lives only within the current transaction and is cleared on
--      COMMIT/ROLLBACK. This is mandatory because pooled connections are reused
--      across requests from different tenants; SET (without LOCAL) or
--      is_local=false would leave a previous request's tenant_id "stuck" on the
--      next request that reuses the connection.
--   4. The tenant_id value is ALWAYS passed as a bound parameter (set_config is
--      a normal function, not a SET statement with text interpolation), never
--      concatenated into the SQL by hand.
--
-- If the API fails to set these variables, current_setting(..., true) returns
-- NULL and the policies below deny access (fail-closed); they never allow it
-- by default.

CREATE OR REPLACE FUNCTION app_current_tenant_id() RETURNS UUID
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION app_bypass_rls() RETURNS BOOLEAN
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT current_setting('app.bypass_rls', true) = 'true'
$$;

COMMENT ON FUNCTION app_current_tenant_id() IS
    'tenant_id of the current authenticated request. NULL if the API did not set it (fail-closed).';

COMMENT ON FUNCTION app_bypass_rls() IS
    'true only when the API, after validating the JWT, explicitly determined that '
    'the role is super_admin or support. Never inferred implicitly from a NULL '
    'tenant_id or any other condition.';

REVOKE ALL ON FUNCTION app_current_tenant_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION app_bypass_rls() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_current_tenant_id() TO app_user;
GRANT EXECUTE ON FUNCTION app_bypass_rls() TO app_user;

-- Generic trigger to maintain updated_at, used by several tables.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;
