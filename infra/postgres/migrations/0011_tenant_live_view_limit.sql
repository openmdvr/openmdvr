-- Maximum seconds a tenant can watch a device live per stream session. This is
-- a real bandwidth cost control, not a UI hint: the JT1078 bridge
-- (jt808-server/internal/jt1078bridge) enforces it by cutting the stream
-- server-side when it expires, regardless of what the client does.
-- Configurable per tenant so higher plans can get a higher limit
-- (PATCH /tenants/{id}, super_admin/support only).
ALTER TABLE tenants
    ADD COLUMN max_live_view_seconds INTEGER NOT NULL DEFAULT 60
        CHECK (max_live_view_seconds > 0);

COMMENT ON COLUMN tenants.max_live_view_seconds IS
    'Maximum seconds of a live video session before the JT1078 bridge cuts it server-side. Adjustable per tenant.';
