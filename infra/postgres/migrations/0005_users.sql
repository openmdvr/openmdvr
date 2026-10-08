CREATE TYPE user_role AS ENUM (
    'super_admin', 'support',
    'tenant_admin', 'tenant_operator', 'tenant_viewer'
);

CREATE TYPE user_status AS ENUM ('active', 'disabled');

CREATE TABLE users (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID REFERENCES tenants(id) ON DELETE CASCADE,
    email               CITEXT NOT NULL,
    password_hash       TEXT NOT NULL,
    role                user_role NOT NULL,
    -- EXPLICIT platform-level RLS bypass flag. Never inferred implicitly from
    -- tenant_id IS NULL in application code: it is its own column precisely
    -- so the intent is written down and validated by a CHECK constraint.
    is_platform_bypass  BOOLEAN NOT NULL DEFAULT false,
    status              user_status NOT NULL DEFAULT 'active',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Business invariant: a platform user (super_admin/support) ALWAYS has
    -- tenant_id NULL and the bypass flag true; a tenant user ALWAYS has
    -- tenant_id NOT NULL and the flag false. No intermediate state.
    CONSTRAINT users_tenant_role_consistency CHECK (
        (tenant_id IS NULL AND role IN ('super_admin', 'support') AND is_platform_bypass = true)
        OR
        (tenant_id IS NOT NULL AND role IN ('tenant_admin', 'tenant_operator', 'tenant_viewer') AND is_platform_bypass = false)
    )
);

-- Email uniqueness: per tenant for customer users, global for platform users.
-- A plain UNIQUE(tenant_id, email) is NOT enough because Postgres treats each
-- NULL as distinct, so two partial indexes are needed.
CREATE UNIQUE INDEX users_tenant_email_unique ON users (tenant_id, email) WHERE tenant_id IS NOT NULL;
CREATE UNIQUE INDEX users_platform_email_unique ON users (email) WHERE tenant_id IS NULL;

CREATE INDEX users_tenant_id_idx ON users (tenant_id);

CREATE TRIGGER users_set_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE users IS 'Platform users (tenant_id NULL) and tenant users.';
COMMENT ON COLUMN users.is_platform_bypass IS
    'true only for super_admin/support. This column is never used on its own as '
    'the runtime RLS bypass mechanism: the API decides bypass per session from '
    'the role validated in the JWT (see app_bypass_rls() in 0003_rls_helpers.sql). '
    'This column is the data-integrity guarantee in the table, not the '
    'authorization mechanism itself.';
