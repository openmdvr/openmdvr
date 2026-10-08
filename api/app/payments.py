"""Payment provider abstraction -- today the only real implementation is
manual (support records a cash/transfer payment that already arrived outside
the system: bank, cash desk). A real Stripe/Mercado Pago integration will be
ANOTHER class implementing the same `PaymentProvider` (called from a webhook
instead of a support POST), without changing how the rest of the system marks
an invoice paid or reactivates a tenant -- business code never calls a vendor
SDK directly.

The router (`billing.py`) never inserts into `payments` / updates `invoices` /
`tenants` directly -- it always goes through `DEFAULT_PROVIDER.record_payment()`,
so that single place is the source of truth for "what happens when a payment
is recorded?"."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from psycopg import AsyncConnection


@dataclass(frozen=True)
class PaymentResult:
    payment_id: uuid.UUID
    # Invoice status AFTER applying this payment -- a convenience for the
    # caller (avoids a second SELECT), never a source of truth separate from
    # the real `invoices` column.
    invoice_status: str


class PaymentProvider(Protocol):
    async def record_payment(
        self,
        conn: AsyncConnection,
        *,
        invoice_id: uuid.UUID,
        tenant_id: uuid.UUID,
        amount: float,
        method: str,
        recorded_by: uuid.UUID | None,
        reference_note: str | None,
    ) -> PaymentResult: ...


class ManualPaymentProvider:
    """The only real provider today. The caller passes `tenant_id` (already
    read from the invoice) instead of this class resolving it, so it does not
    depend on RLS being in any particular state at call time."""

    async def record_payment(
        self,
        conn: AsyncConnection,
        *,
        invoice_id: uuid.UUID,
        tenant_id: uuid.UUID,
        amount: float,
        method: str,
        recorded_by: uuid.UUID | None,
        reference_note: str | None,
    ) -> PaymentResult:
        payment_id = (
            await (
                await conn.execute(
                    """INSERT INTO payments (invoice_id, tenant_id, amount, method, recorded_by, reference_note)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (invoice_id, tenant_id, amount, method, recorded_by, reference_note),
                )
            ).fetchone()
        )[0]

        # Does the sum of ALL payments for this invoice cover its total?
        # Supports partial payments even though the UI records one at a time
        # -- the model allows several.
        row = await (
            await conn.execute(
                """SELECT i.total, i.status, COALESCE(SUM(p.amount), 0)
                   FROM invoices i
                   LEFT JOIN payments p ON p.invoice_id = i.id
                   WHERE i.id = %s
                   GROUP BY i.id""",
                (invoice_id,),
            )
        ).fetchone()
        invoice_total, invoice_status, paid_so_far = row

        if invoice_status not in ("paid", "void") and paid_so_far >= invoice_total:
            invoice_status = "paid"
            await conn.execute("UPDATE invoices SET status = 'paid' WHERE id = %s", (invoice_id,))
            # Real-time reactivation (the enforce_billing_suspension job is
            # only the safety net, see 0022_billing_payments.sql): a suspended
            # tenant whose last outstanding invoice was just settled goes back
            # to 'active' immediately, without waiting for the next job run.
            await conn.execute(
                """UPDATE tenants t SET status = 'active'
                   WHERE t.id = %s AND t.status = 'suspended'
                     AND NOT EXISTS (
                         SELECT 1 FROM invoices i2
                         WHERE i2.tenant_id = t.id AND i2.status IN ('issued', 'overdue') AND i2.due_date < CURRENT_DATE
                     )""",
                (tenant_id,),
            )

        return PaymentResult(payment_id=payment_id, invoice_status=invoice_status)


DEFAULT_PROVIDER: PaymentProvider = ManualPaymentProvider()
