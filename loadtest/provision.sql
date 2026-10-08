-- Provisions a throwaway tenant with N GT06 devices for the load test.
-- Run as the Postgres superuser against a DEV/STAGING database only:
--
--   psql -v count=5000 -v imei_base=990000000000000 -f provision.sql
--
-- Clean up afterwards with cleanup.sql (cascades to positions/alarms).

\set ON_ERROR_STOP on

INSERT INTO tenants (name) VALUES ('loadtest') ON CONFLICT DO NOTHING;

INSERT INTO devices (tenant_id, label, protocol, gt06_imei, status)
SELECT t.id,
       'loadtest-' || g,
       'gt06',
       lpad((:imei_base::numeric + g)::text, 15, '0'),
       'active'
FROM tenants t, generate_series(0, :count - 1) AS g
WHERE t.name = 'loadtest'
ON CONFLICT DO NOTHING;

SELECT count(*) AS provisioned_devices
FROM devices d JOIN tenants t ON t.id = d.tenant_id
WHERE t.name = 'loadtest';
