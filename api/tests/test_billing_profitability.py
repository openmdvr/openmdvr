"""Billing -- estimated cost and margin (platform_billing_settings,
GET /billing/profitability). Core concerns: cost assumptions NEVER reach a
tenant_admin (not even by accident), estimated cost is computed correctly
from active devices + real usage_events bytes, revenue is NORMALIZED to a
monthly equivalent according to the tenant's billing_period (so the margin
compares like with like), and margin_pct is None (not a division error)
when the tenant has no contracted revenue."""
import uuid

import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio

_GIB = 1073741824  # 1 GiB in bytes -- the unit cost_usd_per_gb uses


async def _create_plan(client, token, unit_price):
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


async def _profitability_for(client, token, tenant_id):
    resp = await client.get("/billing/profitability", params={"tenant_id": str(tenant_id)}, headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    [row] = resp.json()["items"]
    return row


# ---------------------------------------------------------------------------
# platform_billing_settings
# ---------------------------------------------------------------------------

async def test_default_settings_seeded_by_migration(client, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.get("/billing/settings", headers=auth_header(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["cost_usd_per_device_month"] == 1.0
    assert body["cost_usd_per_gb"] == 0.02
    assert body["exchange_rate_mxn_per_usd"] == 18.0


async def test_support_can_read_but_not_update_settings(client, platform_users):
    token = await login(client, platform_users["support"]["email"])
    assert (await client.get("/billing/settings", headers=auth_header(token))).status_code == 200
    resp = await client.patch("/billing/settings", json={"exchange_rate_mxn_per_usd": 20.0}, headers=auth_header(token))
    assert resp.status_code == 403


async def test_super_admin_can_update_settings(client, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.patch("/billing/settings", json={"exchange_rate_mxn_per_usd": 19.5}, headers=auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["exchange_rate_mxn_per_usd"] == 19.5
    # Restore -- this table is a global row shared between tests, unlike
    # everything else in this module, which cleans up after itself.
    await client.patch("/billing/settings", json={"exchange_rate_mxn_per_usd": 18.0}, headers=auth_header(token))


async def test_settings_value_out_of_column_range_rejected_with_422(client, platform_users):
    """Security finding: these columns are NUMERIC(10,4) (real ceiling
    ~999,999.9999), but the schema initially reused _MAX_UNIT_PRICE
    (calibrated for NUMERIC(12,2), almost four orders of magnitude larger) --
    a value like 1,000,000 passed Pydantic and broke the UPDATE with a raw
    500. Fixed with its own bound (_MAX_COST_SETTING) + catching
    NumericValueOutOfRange as defense in depth."""
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.patch(
        "/billing/settings", json={"cost_usd_per_device_month": 1_000_000}, headers=auth_header(token)
    )
    assert resp.status_code == 422


async def test_tenant_admin_cannot_read_settings(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/billing/settings", headers=auth_header(token))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /billing/profitability
# ---------------------------------------------------------------------------

async def test_tenant_admin_cannot_read_profitability(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/billing/profitability", headers=auth_header(token))
    assert resp.status_code == 403


async def test_tenant_operator_cannot_read_profitability(client, two_tenants, pool):
    from app import db as db_module
    from app.security import hash_password

    tenant_id = two_tenants["a"]["tenant_id"]
    email = f"operator-profit-{uuid.uuid4().hex[:8]}@example.com"
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (%s, %s, %s, 'tenant_operator')",
            (tenant_id, email, hash_password(TEST_PASSWORD)),
        )
    token = await login(client, email)
    resp = await client.get("/billing/profitability", headers=auth_header(token))
    assert resp.status_code == 403


async def test_profitability_computes_cost_and_margin_correctly(client, two_tenants, platform_users, superuser_conn):
    """Exact numbers with the default assumptions (1 USD/device, 0.02 USD/GB,
    exchange rate 18.0): two_tenants already has 1 active device per tenant.
    1 GiB of traffic this month + a 100 MXN/month plan:
      estimated_cost_usd = 1*1.00 + 1*0.02 = 1.02
      estimated_cost_mxn = 1.02 * 18.00 = 18.36
      monthly_revenue_mxn = 100.00
      margin_mxn = 81.64, margin_pct = 81.64
    """
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]

    await superuser_conn.execute(
        """INSERT INTO usage_events (time, tenant_id, device_id, event_type, bytes_transferred, metadata)
           VALUES (now(), %s, %s, 'playback', %s, '{}')""",
        (tenant_id, device_id, _GIB),
    )
    plan = await _create_plan(client, token, unit_price=100.0)
    await _subscribe(client, token, tenant_id, plan["id"], quantity=1)

    row = await _profitability_for(client, token, tenant_id)
    assert row["active_devices"] == 1
    assert row["bytes_this_month"] == _GIB
    assert row["estimated_cost_usd"] == pytest.approx(1.02)
    assert row["estimated_cost_mxn"] == pytest.approx(18.36)
    assert row["monthly_revenue_mxn"] == pytest.approx(100.0)
    assert row["margin_mxn"] == pytest.approx(81.64)
    assert row["margin_pct"] == pytest.approx(81.64)


async def test_profitability_normalizes_annual_billing_to_monthly_equivalent(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    await client.patch(f"/tenants/{tenant_id}", json={"billing_period": "annual"}, headers=auth_header(token))
    plan = await _create_plan(client, token, unit_price=1200.0)
    await _subscribe(client, token, tenant_id, plan["id"], quantity=1)

    row = await _profitability_for(client, token, tenant_id)
    assert row["monthly_revenue_mxn"] == pytest.approx(100.0)  # 1200 / 12


async def test_profitability_normalizes_semiannual_billing_to_monthly_equivalent(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    await client.patch(f"/tenants/{tenant_id}", json={"billing_period": "semiannual"}, headers=auth_header(token))
    plan = await _create_plan(client, token, unit_price=600.0)
    await _subscribe(client, token, tenant_id, plan["id"], quantity=1)

    row = await _profitability_for(client, token, tenant_id)
    assert row["monthly_revenue_mxn"] == pytest.approx(100.0)  # 600 / 6


async def test_profitability_margin_pct_none_without_revenue(client, two_tenants, platform_users):
    """Without any active subscription line: monthly_revenue_mxn = 0 --
    margin_pct must be None (division avoided), not an error or -inf."""
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]

    row = await _profitability_for(client, token, tenant_id)
    assert row["monthly_revenue_mxn"] == 0.0
    assert row["margin_pct"] is None


async def test_profitability_zero_devices_and_usage_is_zero_cost(client, two_tenants, platform_users, superuser_conn):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    # two_tenants already has an active device -- deactivate it to test the
    # zero-devices case cleanly.
    await superuser_conn.execute("UPDATE devices SET status = 'inactive' WHERE tenant_id = %s", (tenant_id,))

    row = await _profitability_for(client, token, tenant_id)
    assert row["active_devices"] == 0
    assert row["bytes_this_month"] == 0
    assert row["estimated_cost_usd"] == 0.0
    assert row["estimated_cost_mxn"] == 0.0


async def test_profitability_filter_by_tenant_id_excludes_other_tenants(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.get(
        "/billing/profitability", params={"tenant_id": str(two_tenants["a"]["tenant_id"])}, headers=auth_header(token)
    )
    ids = {r["tenant_id"] for r in resp.json()["items"]}
    assert ids == {str(two_tenants["a"]["tenant_id"])}


async def test_profitability_orders_worst_margin_first(client, two_tenants, platform_users):
    """Without a tenant_id filter, sorted ascending by margin_pct -- the
    tenant that costs the business the most (relative to what it pays)
    appears first."""
    token = await login(client, platform_users["super_admin"]["email"])
    plan_cheap_margin = await _create_plan(client, token, unit_price=10.0)  # low revenue, same cost per device
    plan_good_margin = await _create_plan(client, token, unit_price=500.0)
    await _subscribe(client, token, two_tenants["a"]["tenant_id"], plan_cheap_margin["id"], quantity=1)
    await _subscribe(client, token, two_tenants["b"]["tenant_id"], plan_good_margin["id"], quantity=1)

    resp = await client.get("/billing/profitability", headers=auth_header(token))
    items = resp.json()["items"]
    tenant_a_str = str(two_tenants["a"]["tenant_id"])
    tenant_b_str = str(two_tenants["b"]["tenant_id"])
    idx_a = next(i for i, r in enumerate(items) if r["tenant_id"] == tenant_a_str)
    idx_b = next(i for i, r in enumerate(items) if r["tenant_id"] == tenant_b_str)
    assert idx_a < idx_b  # worst margin (low revenue) appears first
