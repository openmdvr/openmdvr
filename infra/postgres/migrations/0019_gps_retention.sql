-- Retention for the time-series hypertables. Decided PER TABLE, not as one
-- generic window:
--
--   - gps_positions: PER-TENANT retention (configurable, sellable per plan),
--     via a custom job. TimescaleDB's native policy does not fit here because it
--     drops whole chunks, which mix rows from ALL tenants by time range.
--   - alarms: fixed GLOBAL retention via the native policy (much lower volume,
--     no business case today for per-tenant retention).
--   - usage_events: NEVER deleted (it is the billing ledger).
--   - driver_shift_events/driver_shift_alerts: intentionally unchanged. Not
--     hypertables, low volume, and labor-compliance implications (driving
--     hours) where "delete early" is riskier than "keep extra".
--
-- NO COMPRESSION (tried and rejected, not forgotten): TimescaleDB 2.17 rejects
-- `ALTER TABLE ... SET (timescaledb.compress)` with "compression cannot be used
-- on table with row security" on ANY table with RLS enabled. All three
-- hypertables (gps_positions/alarms/usage_events) have FORCE ROW LEVEL
-- SECURITY, the real tenant isolation mechanism (see
-- 0009_timeseries_access.sql). Disabling RLS to compress would reopen exactly
-- that vulnerability; not a trade-off worth making to save disk. Retention
-- (which does work with RLS) already bounds gps_positions and alarms;
-- usage_events stays uncompressed but its volume is orders of magnitude lower
-- (one event per video session/shift, not a periodic ping).

-- ---------------------------------------------------------------------------
-- 1. tenants.gps_retention_days: same pattern as max_live_view_seconds/
--    live_view_monthly_quota_seconds (0011/0012): a plan attribute, editable
--    only via PATCH /tenants/{id} (bypass-only), never tenant_admin self-service.
-- ---------------------------------------------------------------------------
ALTER TABLE tenants
    ADD COLUMN gps_retention_days INTEGER NOT NULL DEFAULT 90
        CHECK (gps_retention_days > 0);

COMMENT ON COLUMN tenants.gps_retention_days IS
    'Days this tenant''s GPS positions are kept before enforce_gps_position_retention() deletes them. Plan/billing attribute (bypass-only), not self-service; see PATCH /tenants/{id}.';

-- ---------------------------------------------------------------------------
-- 2. Per-tenant retention job for gps_positions, registered on the background
--    job scheduler TimescaleDB already runs for its own native policies. No
--    new infrastructure (no external cron or container).
-- ---------------------------------------------------------------------------
-- SECURITY DEFINER, like insert_gps_position()/delete_gps_positions_before()
-- (0009_timeseries_access.sql): app_user has NO direct privilege on the
-- gps_positions hypertable (by design; see that migration), so without this the
-- DELETE below fails with "permission denied for table gps_positions" whenever
-- something other than the migration owner runs it (the TimescaleDB scheduler
-- runs the job as the registering role, but a direct CALL from an app_user
-- connection hits this error). An explicit search_path closes the same
-- temp-table shadowing vector already closed in 0009.
CREATE OR REPLACE PROCEDURE enforce_gps_position_retention(job_id INT, config JSONB)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    t RECORD;
BEGIN
    FOR t IN SELECT id, gps_retention_days FROM tenants LOOP
        DELETE FROM gps_positions
        WHERE tenant_id = t.id
          AND "time" < now() - (t.gps_retention_days || ' days')::interval;
    END LOOP;
END;
$$;

COMMENT ON PROCEDURE enforce_gps_position_retention(INT, JSONB) IS
    'Daily job (add_job below): deletes GPS positions older than EACH tenant''s window (tenants.gps_retention_days). Not a hot path; one DELETE per tenant once a day is acceptable.';

REVOKE ALL ON PROCEDURE enforce_gps_position_retention(INT, JSONB) FROM PUBLIC;
GRANT EXECUTE ON PROCEDURE enforce_gps_position_retention(INT, JSONB) TO app_user;

SELECT add_job('enforce_gps_position_retention', '1 day');

-- ---------------------------------------------------------------------------
-- 3. Fixed global retention for alarms: TimescaleDB native policy (drops whole
--    chunks, more efficient than a hand-written job; correct here because the
--    window is the same for every tenant). Unlike compression,
--    drop_chunks/retention DOES work with RLS enabled; it only removes whole
--    chunks already older than the threshold.
-- ---------------------------------------------------------------------------
SELECT add_retention_policy('alarms', INTERVAL '365 days');
