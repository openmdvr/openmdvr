-- Billing: payments + automatic service suspension. See 0020/0021_billing_*.sql
-- for catalog/subscriptions and promotions/invoices.
--
-- Design decisions:
--
-- 1. `payments` is a SEPARATE record from `invoices`, never a simple status
--    field: a payment has its own amount/method/date/recorder, and an invoice
--    can receive several partial payments. The "is this invoice settled?"
--    logic lives in Python (api/app/payments.py::PaymentProvider), not here.
--    That abstraction lets online payment providers be integrated later
--    WITHOUT touching the schema.
-- 2. `method` already includes 'stripe'/'mercado_pago' even though no code
--    uses them yet: an extensible enum is cheap to leave ready; the real
--    integration (webhooks, credentials, SDK) is future work.
-- 3. Service suspension reuses `tenants.status` (since 0004_tenants.sql,
--    already blocks LOGIN). The point is that it ALSO blocks live video
--    (POST /devices/{id}/video) while a session with an unexpired JWT is alive.
--    GPS/alarm ingestion is NEVER cut on purpose (cheap to store, avoids data
--    gaps if the customer pays and is reactivated); no jt808-server changes.
-- 4. Fixed grace period (5 days after due_date) before suspending; not
--    configurable per tenant yet. If needed, that is a new `tenants` column,
--    not a change to this logic.

CREATE TYPE payment_method AS ENUM ('cash', 'bank_transfer', 'stripe', 'mercado_pago', 'other');

CREATE TABLE payments (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id           UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    -- Denormalized (same as invoice_line_items.tenant_id) so RLS does not
    -- depend on a JOIN against invoices to isolate.
    tenant_id            UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    amount               NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    method               payment_method NOT NULL,
    received_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- SET NULL, not CASCADE or RESTRICT: the payment record (for
    -- audit/accounting) must survive deletion of the recorder's account.
    recorded_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    reference_note       TEXT,
    -- Nullable on purpose: NULL for the only real method today (manual,
    -- cash/transfer). An online provider integration would fill both via its
    -- webhook to allow reconciliation.
    external_provider    TEXT,
    external_payment_id  TEXT
);

CREATE INDEX payments_invoice_id_idx ON payments (invoice_id);
CREATE INDEX payments_tenant_id_idx ON payments (tenant_id);

COMMENT ON TABLE payments IS 'Payments recorded against an invoice. Currently always manual (api/app/payments.py::ManualPaymentProvider); online providers can be added later as ANOTHER implementation of the same Protocol without changing this schema.';

ALTER TABLE payments ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments FORCE ROW LEVEL SECURITY;

-- SELECT: a tenant sees payments of ITS OWN invoices ("My billing", like
-- invoices). Writes are bypass-only: recording a manual payment is support
-- work; a future online integration would also run with bypass (called from
-- the backend, never from a tenant session).
CREATE POLICY payments_select ON payments
    FOR SELECT USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY payments_insert ON payments FOR INSERT WITH CHECK (app_bypass_rls());
CREATE POLICY payments_update ON payments FOR UPDATE USING (app_bypass_rls()) WITH CHECK (app_bypass_rls());
CREATE POLICY payments_delete ON payments FOR DELETE USING (app_bypass_rls());

GRANT SELECT, INSERT, UPDATE, DELETE ON payments TO app_user;

-- ---------------------------------------------------------------------------
-- enforce_billing_suspension: daily job (same add_job() mechanism as
-- enforce_gps_position_retention/generate_invoices).
--
-- Three passes:
--   1. issued -> overdue once due_date has passed (pure state transition, no
--      effect on tenants.status yet; that is step 2).
--   2. Suspends an ACTIVE tenant with an overdue invoice beyond the grace
--      period (5 days).
--   3. Reactivates a suspended tenant that no longer has ANY pending overdue
--      invoice. This is a safety net: the MAIN reactivation path is
--      ManualPaymentProvider.record_payment in real time (see
--      api/app/payments.py). Covers support voiding an invoice instead of
--      collecting it.
--
-- SECURITY DEFINER like the others: app_user does have direct GRANTs on
-- invoices/tenants (not hypertables), but SECURITY DEFINER lets the
-- TimescaleDB scheduler run it regardless of the invoking role.
CREATE OR REPLACE PROCEDURE enforce_billing_suspension(job_id INT, config JSONB)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    UPDATE invoices SET status = 'overdue' WHERE status = 'issued' AND due_date < CURRENT_DATE;

    UPDATE tenants t SET status = 'suspended'
    WHERE t.status = 'active'
      AND EXISTS (
          SELECT 1 FROM invoices i
          WHERE i.tenant_id = t.id AND i.status = 'overdue' AND i.due_date < CURRENT_DATE - INTERVAL '5 days'
      );

    UPDATE tenants t SET status = 'active'
    WHERE t.status = 'suspended'
      AND NOT EXISTS (
          SELECT 1 FROM invoices i
          WHERE i.tenant_id = t.id AND i.status IN ('issued', 'overdue') AND i.due_date < CURRENT_DATE
      );
END;
$$;

COMMENT ON PROCEDURE enforce_billing_suspension(INT, JSONB) IS
    'Daily job: issued->overdue on expiry, suspends an active tenant with an overdue invoice beyond the grace period (5 days), reactivates a suspended one with no pending overdue invoice (safety net; the main path is a real payment via ManualPaymentProvider).';

REVOKE ALL ON PROCEDURE enforce_billing_suspension(INT, JSONB) FROM PUBLIC;
GRANT EXECUTE ON PROCEDURE enforce_billing_suspension(INT, JSONB) TO app_user;

SELECT add_job('enforce_billing_suspension', '1 day');
