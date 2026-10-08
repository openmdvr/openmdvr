-- Per-category device quota/billing (GT06 support). Previously
-- `_assert_device_quota_not_exceeded` summed ALL active
-- tenant_subscription_items lines regardless of category (see
-- api/app/routers/devices.py). billing_plans.category ('gps'|'camera'|'addon')
-- already existed since 0020_billing_catalog.sql but was never propagated
-- beyond the catalog.

-- A subscription line with billing_plan_id inherits the plan's category (via
-- join, not duplicated). A custom line (no plan) has nothing to inherit from,
-- so it needs its own explicit column -- same pattern as custom_description,
-- which is required when there is no plan.
ALTER TABLE tenant_subscription_items
    ADD COLUMN category billing_plan_category;

-- Backfill EXISTING custom lines: before GT06 support, every device was a
-- JT808 camera, so 'camera' is the only correct interpretation of history.
UPDATE tenant_subscription_items
SET category = 'camera'
WHERE billing_plan_id IS NULL AND category IS NULL;

ALTER TABLE tenant_subscription_items
    ADD CONSTRAINT tenant_subscription_items_category_present
    CHECK (billing_plan_id IS NOT NULL OR category IS NOT NULL);

COMMENT ON COLUMN tenant_subscription_items.category IS 'Category of this line when it has NO billing_plan_id (custom line). With a plan, the real category is billing_plans.category via join and this value is ignored.';

-- Second estimated cost rate: a GT06 tracker (no camera, no video) has a much
-- lower real bandwidth/storage cost than a JT808 camera. A single per-device
-- rate inflates the estimated cost of a GT06 fleet and distorts the margin in
-- GET /billing/profitability. Same type/bound as the existing column
-- (NUMERIC(10,4)); do not copy billing_plans' NUMERIC(12,2) bound.
ALTER TABLE platform_billing_settings
    ADD COLUMN cost_usd_per_gps_device_month NUMERIC(10, 4) NOT NULL DEFAULT 0.10
        CHECK (cost_usd_per_gps_device_month >= 0);

COMMENT ON COLUMN platform_billing_settings.cost_usd_per_device_month IS 'Estimated USD/month cost per jt808 device (camera, with video). See cost_usd_per_gps_device_month for gt06 (GPS-only) devices.';
COMMENT ON COLUMN platform_billing_settings.cost_usd_per_gps_device_month IS 'Estimated USD/month cost per gt06 device (GPS-only, no video). Deliberately separate from, and lower than, cost_usd_per_device_month (camera).';
