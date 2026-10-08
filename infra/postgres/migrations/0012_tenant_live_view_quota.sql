-- MONTHLY live video quota per tenant (cumulative, consumed by real usage).
-- Distinct from max_live_view_seconds (0011), which is a PER-SESSION cap that
-- resets on every click. This one is a balance that goes down over the month.
--
-- Seconds, not minutes, for consistency with max_live_view_seconds; the UI
-- converts for display.
--
-- No "cycle start" column: the reset is always "from day 1 of the calendar
-- month in UTC", computed in the consumption query
-- (jt808-server/internal/db/tenants.go), not by a reset job. Per-tenant
-- billing cycles would be a change local to that query.
ALTER TABLE tenants
    ADD COLUMN live_view_monthly_quota_seconds INTEGER NOT NULL DEFAULT 18000
        CHECK (live_view_monthly_quota_seconds > 0);

COMMENT ON COLUMN tenants.live_view_monthly_quota_seconds IS
    'Cumulative live video seconds per calendar month (UTC) before the bridge rejects new sessions (hard block). Real consumption is read from usage_events_v (metadata->>duration_s of live_view events). Default 18000s = 5h/month, adjustable per tenant.';
