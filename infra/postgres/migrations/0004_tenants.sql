CREATE TYPE tenant_status AS ENUM ('active', 'suspended', 'cancelled');

CREATE TABLE tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL CHECK (btrim(name) <> ''),
    status      tenant_status NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER tenants_set_updated_at
    BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE tenants IS 'A platform customer (owner of a fleet).';
