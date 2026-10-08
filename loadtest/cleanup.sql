-- Removes the load-test tenant and its data. Run as the Postgres superuser
-- against a DEV/STAGING database only:
--
--   psql -f cleanup.sql
--
-- gps_positions, alarms and usage_events reference devices with
-- ON DELETE RESTRICT (history is never deleted by cascade), so their rows are
-- removed explicitly first; deleting the tenant then cascades to everything
-- else (devices, geofence state, data usage, ...).
\set ON_ERROR_STOP on
BEGIN;
CREATE TEMP TABLE loadtest_devices ON COMMIT DROP AS
  SELECT d.id FROM devices d JOIN tenants t ON t.id = d.tenant_id
  WHERE t.name = 'loadtest';
DELETE FROM gps_positions WHERE device_id IN (SELECT id FROM loadtest_devices);
DELETE FROM alarms        WHERE device_id IN (SELECT id FROM loadtest_devices);
DELETE FROM usage_events  WHERE device_id IN (SELECT id FROM loadtest_devices);
DELETE FROM tenants WHERE name = 'loadtest';
COMMIT;
