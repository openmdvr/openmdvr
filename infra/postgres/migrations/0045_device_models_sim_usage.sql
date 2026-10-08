-- Device model catalog + SIM number + real data usage per line + plan
-- cost/cap.

-- Model catalog: data organization only; it does NOT change behavior yet
-- (which GT06 command to use, how many cameras, etc. is still fixed in code,
-- see alarmclip.go/gt06server). Wiring the model to real logic waits until a
-- second model with genuinely different needs exists; building it earlier
-- would mean guessing rules. Global catalog (no tenant_id), same pattern as
-- billing_plans.
CREATE TABLE device_models (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL,
    protocol   device_protocol NOT NULL,
    notes      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT device_models_name_unique UNIQUE (name)
);

COMMENT ON TABLE device_models IS 'Global hardware model catalog (JC261, CY06-2G, etc.). Organization/reporting only for now.';

ALTER TABLE device_models ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_models FORCE ROW LEVEL SECURITY;

-- SELECT open to any authenticated session (like platform_monitoring_settings):
-- the model name is resolved in the device listing for ANY tenant role and is
-- not sensitive. INSERT/UPDATE/DELETE bypass-only; the API additionally decides
-- whether super_admin specifically is required (same as billing_plans: that
-- distinction never lives in RLS in this project).
CREATE POLICY device_models_select ON device_models FOR SELECT USING (true);
CREATE POLICY device_models_write ON device_models FOR ALL USING (app_bypass_rls()) WITH CHECK (app_bypass_rls());

GRANT SELECT, INSERT, UPDATE, DELETE ON device_models TO app_user;

-- Seeded with models that are supported and tested.
INSERT INTO device_models (name, protocol, notes) VALUES
    ('CY06-2G', 'gt06', 'GPS-only tracker (GT06 protocol).'),
    ('JC261', 'gt06_video', 'Jimi IoT dashcam, 2 cameras (front + cabin).');

ALTER TABLE devices
    ADD COLUMN device_model_id UUID REFERENCES device_models(id) ON DELETE SET NULL,
    ADD COLUMN sim_number TEXT,
    ADD COLUMN sim_carrier TEXT,
    -- Contracted cost and data cap per line, to compare REAL consumption
    -- (device_data_usage_monthly, below) against the contract and detect
    -- overuse before the carrier bills an overage.
    ADD COLUMN sim_plan_cost_mxn_month NUMERIC(10,2) CHECK (sim_plan_cost_mxn_month IS NULL OR sim_plan_cost_mxn_month >= 0),
    ADD COLUMN sim_plan_data_cap_mb INTEGER CHECK (sim_plan_data_cap_mb IS NULL OR sim_plan_data_cap_mb >= 0);

COMMENT ON COLUMN devices.device_model_id IS 'Hardware model (see device_models). Nullable; devices created before this migration have no model assigned.';
COMMENT ON COLUMN devices.sim_number IS 'Cellular SIM line number of the device. Visible to any session that can already see the device (tenant_admin included), unlike the usage/cost below.';
COMMENT ON COLUMN devices.sim_carrier IS 'SIM carrier, free text; useful to know which APN applies.';
COMMENT ON COLUMN devices.sim_plan_cost_mxn_month IS 'Monthly cost paid for this line. Platform only (see GET /billing/sim-usage).';
COMMENT ON COLUMN devices.sim_plan_data_cap_mb IS 'Contracted monthly data cap. Platform only; used to flag overuse in the report.';

-- REAL data usage per device. usage_events measures bytes WE SERVE to the
-- browser (live video via ZLMediaKit), never bytes the DEVICE consumes on its
-- own cellular line (heartbeats, positions, alarms, video commands, clip
-- uploads); these are different things. jt808server and gt06server wrap each
-- TCP connection with a real byte counter (internal/datausage.CountingConn)
-- and call record_device_data_usage when the connection closes (and
-- periodically if it stays open). REAL bytes, never estimates, same as
-- usage_events/on_flow_report.
--
-- Direct monthly rollup (never a hypertable): the report only needs per
-- device/month sums + a yearly average, not per-connection detail. This also
-- avoids retention/compression concerns and the RLS + hypertable + direct
-- GRANT pitfall (see 0009_timeseries_access.sql). One row per (device, month),
-- incremented on each flush.
CREATE TABLE device_data_usage_monthly (
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    device_id  UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    year_month DATE NOT NULL,
    bytes_rx   BIGINT NOT NULL DEFAULT 0 CHECK (bytes_rx >= 0),
    bytes_tx   BIGINT NOT NULL DEFAULT 0 CHECK (bytes_tx >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, year_month)
);

COMMENT ON TABLE device_data_usage_monthly IS 'Monthly rollup of real rx/tx bytes per TCP connection of each device (cellular SIM line usage). year_month = first day of the month (date_trunc).';

CREATE INDEX device_data_usage_monthly_tenant_idx ON device_data_usage_monthly (tenant_id, year_month);

ALTER TABLE device_data_usage_monthly ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_data_usage_monthly FORCE ROW LEVEL SECURITY;

-- Platform only: not even tenant_admin sees this (a tenant only sees the SIM
-- number).
CREATE POLICY device_data_usage_monthly_select ON device_data_usage_monthly FOR SELECT USING (app_bypass_rls());

GRANT SELECT ON device_data_usage_monthly TO app_user;

-- record_device_data_usage: the only write path, called from jt808-server (Go,
-- bypass session) when each connection closes / periodically while it is open.
-- SECURITY DEFINER because app_user has no direct INSERT/UPDATE grant (same
-- pattern as insert_alarm/insert_usage_event). Best effort: if the device was
-- deleted between connection open and this flush, there is nobody to attribute
-- the bytes to; not an error.
CREATE OR REPLACE FUNCTION record_device_data_usage(
    p_device_id UUID,
    p_bytes_rx BIGINT,
    p_bytes_tx BIGINT
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_tenant_id UUID;
BEGIN
    IF NOT app_bypass_rls() THEN
        RAISE EXCEPTION 'recording data usage requires a platform session' USING ERRCODE = '42501';
    END IF;
    IF p_bytes_rx = 0 AND p_bytes_tx = 0 THEN
        RETURN;
    END IF;
    SELECT tenant_id INTO v_tenant_id FROM devices WHERE id = p_device_id;
    IF v_tenant_id IS NULL THEN
        RETURN;
    END IF;
    INSERT INTO device_data_usage_monthly (tenant_id, device_id, year_month, bytes_rx, bytes_tx)
    VALUES (v_tenant_id, p_device_id, date_trunc('month', now())::date, p_bytes_rx, p_bytes_tx)
    ON CONFLICT (device_id, year_month) DO UPDATE
    SET bytes_rx = device_data_usage_monthly.bytes_rx + EXCLUDED.bytes_rx,
        bytes_tx = device_data_usage_monthly.bytes_tx + EXCLUDED.bytes_tx,
        updated_at = now();
END;
$$;

REVOKE ALL ON FUNCTION record_device_data_usage(UUID, BIGINT, BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_device_data_usage(UUID, BIGINT, BIGINT) TO app_user;
