CREATE TYPE device_status AS ENUM ('active', 'inactive', 'maintenance');

CREATE TABLE devices (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- JT808 terminal identifier (typically the SIM number). Globally unique
    -- because the JT808 server routes by this identifier before it knows which
    -- tenant the device belongs to.
    --
    -- NO leading zeros: the BCD decoder used by the JT808 server
    -- (jt808-server/internal/db/devices.go) always strips them when reading the
    -- terminal number reported by the hardware, so a value stored here WITH a
    -- leading zero would never match and the device could never connect. The
    -- CHECK enforces this instead of leaving it as an easy-to-miss note.
    jt808_terminal_id   TEXT NOT NULL CHECK (jt808_terminal_id !~ '^0'),
    label               TEXT NOT NULL,
    vehicle_plate       TEXT,
    status              device_status NOT NULL DEFAULT 'active',
    last_seen_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT devices_jt808_terminal_id_unique UNIQUE (jt808_terminal_id)
);

CREATE INDEX devices_tenant_id_idx ON devices (tenant_id);

CREATE TRIGGER devices_set_updated_at
    BEFORE UPDATE ON devices
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE devices IS 'A camera/MDVR installed in a vehicle, owned by a tenant.';
