"""Billing -- payments + automatic service suspension
(enforce_billing_suspension, 0022_billing_payments.sql). Core concerns: the
"is this invoice settled?" logic lives in ONE place
(api/app/payments.py::PaymentProvider), real-time reactivation happens on
payment (without waiting for the job), and the job is only the safety net
for overdue suspension and for reactivation when an invoice is voided
instead of paid. The real service-cut check on POST /devices/{id}/video
lives in test_video.py, not here."""
import uuid
from datetime import date, timedelta

import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _create_plan(client, token, unit_price=190.0):
    resp = await client.post(
        "/billing/plans",
        json={
            "name": f"Plan {uuid.uuid4().hex[:6]}",
            "sku": f"SKU-{uuid.uuid4().hex[:8]}",
            "category": "gps",
            "unit_price": unit_price,
            "currency": "MXN",
            "billing_period": "monthly",
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _subscribe(client, token, tenant_id, plan_id, quantity=1):
    resp = await client.post(
        "/billing/subscription-items",
        json={"tenant_id": str(tenant_id), "billing_plan_id": plan_id, "quantity": quantity},
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _run_invoice_job(pool):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("CALL generate_invoices(0, '{}'::jsonb)")


async def _run_suspension_job(pool):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("CALL enforce_billing_suspension(0, '{}'::jsonb)")


async def _get_single_invoice(client, token, tenant_id):
    resp = await client.get("/billing/invoices", params={"tenant_id": str(tenant_id)}, headers=auth_header(token))
    items = resp.json()["items"]
    assert len(items) == 1
    return items[0]


async def _backdate_invoice(pool, invoice_id, due_date):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("UPDATE invoices SET due_date = %s WHERE id = %s", (due_date, invoice_id))


async def _tenant_status(pool, tenant_id):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (await conn.execute("SELECT status FROM tenants WHERE id = %s", (tenant_id,))).fetchone()
        return row[0]


# ---------------------------------------------------------------------------
# Recording payments
# ---------------------------------------------------------------------------

async def test_full_payment_marks_invoice_paid(client, two_tenants, platform_users, pool):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, unit_price=300.0)
    tenant_id = two_tenants["a"]["tenant_id"]
    await _subscribe(client, token, tenant_id, plan["id"], quantity=1)
    await _run_invoice_job(pool)
    invoice = await _get_single_invoice(client, token, tenant_id)
    assert invoice["status"] == "issued"

    resp = await client.post(
        "/billing/payments",
        json={"invoice_id": invoice["id"], "amount": 300.0, "method": "cash"},
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["invoice_status"] == "paid"
    assert body["amount"] == 300.0

    refetched = await _get_single_invoice(client, token, tenant_id)
    assert refetched["status"] == "paid"


async def test_partial_payment_leaves_invoice_issued(client, two_tenants, platform_users, pool):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, unit_price=500.0)
    tenant_id = two_tenants["a"]["tenant_id"]
    await _subscribe(client, token, tenant_id, plan["id"], quantity=1)
    await _run_invoice_job(pool)
    invoice = await _get_single_invoice(client, token, tenant_id)

    resp = await client.post(
        "/billing/payments",
        json={"invoice_id": invoice["id"], "amount": 200.0, "method": "bank_transfer"},
        headers=auth_header(token),
    )
    assert resp.status_code == 201
    assert resp.json()["invoice_status"] == "issued"

    # A second payment that covers the remainder DOES mark it 'paid' --
    # partial payments are supported even though the UI records one at a time.
    resp2 = await client.post(
        "/billing/payments",
        json={"invoice_id": invoice["id"], "amount": 300.0, "method": "bank_transfer"},
        headers=auth_header(token),
    )
    assert resp2.json()["invoice_status"] == "paid"


async def test_cannot_pay_already_paid_or_void_invoice(client, two_tenants, platform_users, pool):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, unit_price=100.0)
    tenant_id = two_tenants["a"]["tenant_id"]
    await _subscribe(client, token, tenant_id, plan["id"], quantity=1)
    await _run_invoice_job(pool)
    invoice = await _get_single_invoice(client, token, tenant_id)

    await client.post(f"/billing/invoices/{invoice['id']}/void", headers=auth_header(token))

    resp = await client.post(
        "/billing/payments",
        json={"invoice_id": invoice["id"], "amount": 100.0, "method": "cash"},
        headers=auth_header(token),
    )
    assert resp.status_code == 409


async def test_payment_for_nonexistent_invoice_404(client, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.post(
        "/billing/payments",
        json={"invoice_id": str(uuid.uuid4()), "amount": 100.0, "method": "cash"},
        headers=auth_header(token),
    )
    assert resp.status_code == 404


async def test_tenant_admin_cannot_create_payment(client, two_tenants, platform_users, pool):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, unit_price=100.0)
    tenant_id = two_tenants["a"]["tenant_id"]
    await _subscribe(client, token, tenant_id, plan["id"], quantity=1)
    await _run_invoice_job(pool)
    invoice = await _get_single_invoice(client, token, tenant_id)

    tenant_token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/billing/payments",
        json={"invoice_id": invoice["id"], "amount": 100.0, "method": "cash"},
        headers=auth_header(tenant_token),
    )
    assert resp.status_code == 403


async def test_tenant_admin_can_read_own_payments(client, two_tenants, platform_users, pool):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, unit_price=150.0)
    tenant_id = two_tenants["a"]["tenant_id"]
    await _subscribe(client, token, tenant_id, plan["id"], quantity=1)
    await _run_invoice_job(pool)
    invoice = await _get_single_invoice(client, token, tenant_id)
    await client.post(
        "/billing/payments", json={"invoice_id": invoice["id"], "amount": 150.0, "method": "cash"}, headers=auth_header(token)
    )

    tenant_token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/billing/payments", headers=auth_header(tenant_token))
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


async def test_tenant_operator_cannot_read_payments(client, two_tenants, pool):
    from app import db as db_module
    from app.security import hash_password

    tenant_id = two_tenants["a"]["tenant_id"]
    email = f"operator-payments-{uuid.uuid4().hex[:8]}@example.com"
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (%s, %s, %s, 'tenant_operator')",
            (tenant_id, email, hash_password(TEST_PASSWORD)),
        )
    token = await login(client, email)
    resp = await client.get("/billing/payments", headers=auth_header(token))
    assert resp.status_code == 403


async def test_payments_isolated_per_tenant(client, two_tenants, platform_users, pool):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, unit_price=200.0)
    await _subscribe(client, token, two_tenants["a"]["tenant_id"], plan["id"], quantity=1)
    await _subscribe(client, token, two_tenants["b"]["tenant_id"], plan["id"], quantity=1)
    await _run_invoice_job(pool)
    invoice_a = await _get_single_invoice(client, token, two_tenants["a"]["tenant_id"])
    invoice_b = await _get_single_invoice(client, token, two_tenants["b"]["tenant_id"])
    await client.post("/billing/payments", json={"invoice_id": invoice_a["id"], "amount": 200.0, "method": "cash"}, headers=auth_header(token))
    await client.post("/billing/payments", json={"invoice_id": invoice_b["id"], "amount": 200.0, "method": "cash"}, headers=auth_header(token))

    tenant_a_token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/billing/payments", headers=auth_header(tenant_a_token))
    ids = {p["invoice_id"] for p in resp.json()["items"]}
    assert invoice_a["id"] in ids
    assert invoice_b["id"] not in ids


# ---------------------------------------------------------------------------
# Real-time reactivation on payment
# ---------------------------------------------------------------------------

async def test_paying_off_only_overdue_invoice_reactivates_tenant_immediately(client, two_tenants, platform_users, pool):
    """The PRIMARY reactivation path -- does not depend on waiting for the next
    daily run of enforce_billing_suspension."""
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, unit_price=400.0)
    tenant_id = two_tenants["a"]["tenant_id"]
    await _subscribe(client, token, tenant_id, plan["id"], quantity=1)
    await _run_invoice_job(pool)
    invoice = await _get_single_invoice(client, token, tenant_id)

    await _backdate_invoice(pool, invoice["id"], date.today() - timedelta(days=10))
    await _run_suspension_job(pool)
    assert await _tenant_status(pool, tenant_id) == "suspended"

    resp = await client.post(
        "/billing/payments", json={"invoice_id": invoice["id"], "amount": 400.0, "method": "cash"}, headers=auth_header(token)
    )
    assert resp.status_code == 201
    assert await _tenant_status(pool, tenant_id) == "active"


async def test_partial_payment_does_not_reactivate_suspended_tenant(client, two_tenants, platform_users, pool):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, unit_price=400.0)
    tenant_id = two_tenants["a"]["tenant_id"]
    await _subscribe(client, token, tenant_id, plan["id"], quantity=1)
    await _run_invoice_job(pool)
    invoice = await _get_single_invoice(client, token, tenant_id)

    await _backdate_invoice(pool, invoice["id"], date.today() - timedelta(days=10))
    await _run_suspension_job(pool)
    assert await _tenant_status(pool, tenant_id) == "suspended"

    await client.post(
        "/billing/payments", json={"invoice_id": invoice["id"], "amount": 100.0, "method": "cash"}, headers=auth_header(token)
    )
    assert await _tenant_status(pool, tenant_id) == "suspended"


# ---------------------------------------------------------------------------
# enforce_billing_suspension: issued->overdue, suspension, safety-net reactivation
# ---------------------------------------------------------------------------

async def test_issued_invoice_past_due_date_becomes_overdue(client, two_tenants, platform_users, pool):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, unit_price=100.0)
    tenant_id = two_tenants["a"]["tenant_id"]
    await _subscribe(client, token, tenant_id, plan["id"], quantity=1)
    await _run_invoice_job(pool)
    invoice = await _get_single_invoice(client, token, tenant_id)

    await _backdate_invoice(pool, invoice["id"], date.today() - timedelta(days=1))
    await _run_suspension_job(pool)

    refetched = await _get_single_invoice(client, token, tenant_id)
    assert refetched["status"] == "overdue"


async def test_within_grace_period_tenant_not_yet_suspended(client, two_tenants, platform_users, pool):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, unit_price=100.0)
    tenant_id = two_tenants["a"]["tenant_id"]
    await _subscribe(client, token, tenant_id, plan["id"], quantity=1)
    await _run_invoice_job(pool)
    invoice = await _get_single_invoice(client, token, tenant_id)

    # 2 days overdue -- inside the 5-day grace period.
    await _backdate_invoice(pool, invoice["id"], date.today() - timedelta(days=2))
    await _run_suspension_job(pool)

    assert await _tenant_status(pool, tenant_id) == "active"


async def test_beyond_grace_period_tenant_suspended(client, two_tenants, platform_users, pool):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, unit_price=100.0)
    tenant_id = two_tenants["a"]["tenant_id"]
    await _subscribe(client, token, tenant_id, plan["id"], quantity=1)
    await _run_invoice_job(pool)
    invoice = await _get_single_invoice(client, token, tenant_id)

    await _backdate_invoice(pool, invoice["id"], date.today() - timedelta(days=10))
    await _run_suspension_job(pool)

    assert await _tenant_status(pool, tenant_id) == "suspended"


async def test_voiding_overdue_invoice_reactivates_tenant_via_job(client, two_tenants, platform_users, pool):
    """Job safety net: if support voids the invoice instead of collecting it,
    the tenant must be reactivated too (not only on payment)."""
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, unit_price=100.0)
    tenant_id = two_tenants["a"]["tenant_id"]
    await _subscribe(client, token, tenant_id, plan["id"], quantity=1)
    await _run_invoice_job(pool)
    invoice = await _get_single_invoice(client, token, tenant_id)

    await _backdate_invoice(pool, invoice["id"], date.today() - timedelta(days=10))
    await _run_suspension_job(pool)
    assert await _tenant_status(pool, tenant_id) == "suspended"

    await client.post(f"/billing/invoices/{invoice['id']}/void", headers=auth_header(token))
    await _run_suspension_job(pool)
    assert await _tenant_status(pool, tenant_id) == "active"


async def test_suspension_job_never_touches_cancelled_tenant(client, two_tenants, platform_users, pool):
    from app import db as db_module

    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, unit_price=100.0)
    tenant_id = two_tenants["a"]["tenant_id"]
    await _subscribe(client, token, tenant_id, plan["id"], quantity=1)
    await _run_invoice_job(pool)
    invoice = await _get_single_invoice(client, token, tenant_id)
    await _backdate_invoice(pool, invoice["id"], date.today() - timedelta(days=10))

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("UPDATE tenants SET status = 'cancelled' WHERE id = %s", (tenant_id,))

    await _run_suspension_job(pool)
    assert await _tenant_status(pool, tenant_id) == "cancelled"
