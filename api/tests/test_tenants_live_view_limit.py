"""PATCH /tenants/{id} -- the per-tenant live video seconds limit
(tenants.max_live_view_seconds) that the JT1078 bridge enforces server
side. What matters most: who can change it (bypass, not any tenant_admin)
and that the default is correct."""
import pytest

from conftest import login, auth_header

pytestmark = pytest.mark.asyncio


async def test_new_tenant_has_default_live_view_limit(client, platform_users, superuser_conn):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.post("/tenants", json={"name": "Tenant Default Limit"}, headers=auth_header(token))
    assert resp.status_code == 201
    assert resp.json()["max_live_view_seconds"] == 60
    assert resp.json()["live_view_monthly_quota_seconds"] == 18000
    await superuser_conn.execute("DELETE FROM tenants WHERE id = %s", (resp.json()["id"],))


async def test_tenant_out_reflects_real_consumption_this_month(client, two_tenants, superuser_conn):
    """live_view_seconds_remaining must reflect the REAL consumption recorded
    in usage_events (duration_s in metadata), not stay fixed at the full
    quota -- the same calculation jt808-server uses for enforcement."""
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]
    await superuser_conn.execute(
        """INSERT INTO usage_events (time, tenant_id, device_id, event_type, bytes_transferred, metadata)
           VALUES (now(), %s, %s, 'live_view', 1000, %s)""",
        (tenant_id, device_id, '{"duration_s": 42}'),
    )

    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/tenants", headers=auth_header(token))
    assert resp.status_code == 200
    [tenant] = resp.json()["items"]  # RLS: a regular tenant only sees its own row
    assert tenant["live_view_seconds_remaining"] == tenant["live_view_monthly_quota_seconds"] - 42


async def test_support_can_update_live_view_quota_without_touching_session_limit(client, two_tenants, platform_users):
    """Partial PATCH: changing the monthly quota must not overwrite the
    existing per-session limit, and vice versa (two independent columns)."""
    token = await login(client, platform_users["support"]["email"])
    resp = await client.patch(
        f"/tenants/{two_tenants['a']['tenant_id']}",
        json={"live_view_monthly_quota_seconds": 3600},
        headers=auth_header(token),
    )
    assert resp.status_code == 200
    assert resp.json()["live_view_monthly_quota_seconds"] == 3600
    assert resp.json()["max_live_view_seconds"] == 60  # default, untouched


async def test_support_can_update_live_view_limit(client, two_tenants, platform_users):
    token = await login(client, platform_users["support"]["email"])
    resp = await client.patch(
        f"/tenants/{two_tenants['a']['tenant_id']}",
        json={"max_live_view_seconds": 300},
        headers=auth_header(token),
    )
    assert resp.status_code == 200
    assert resp.json()["max_live_view_seconds"] == 300


async def test_tenant_admin_cannot_update_live_view_limit(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.patch(
        f"/tenants/{two_tenants['a']['tenant_id']}",
        json={"max_live_view_seconds": 3600},
        headers=auth_header(token),
    )
    assert resp.status_code == 403


async def test_new_tenant_defaults_to_monthly_billing_period(client, platform_users, superuser_conn):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.post("/tenants", json={"name": "Tenant Default Period"}, headers=auth_header(token))
    assert resp.status_code == 201
    assert resp.json()["billing_period"] == "monthly"
    await superuser_conn.execute("DELETE FROM tenants WHERE id = %s", (resp.json()["id"],))


async def test_bypass_can_update_billing_period(client, two_tenants, platform_users):
    """tenants.billing_period must be editable through PATCH /tenants/{id}
    (semiannual/annual cycles), not stuck at 'monthly' forever."""
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.patch(
        f"/tenants/{two_tenants['a']['tenant_id']}", json={"billing_period": "annual"}, headers=auth_header(token)
    )
    assert resp.status_code == 200
    assert resp.json()["billing_period"] == "annual"


async def test_tenant_admin_cannot_update_billing_period(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.patch(
        f"/tenants/{two_tenants['a']['tenant_id']}", json={"billing_period": "annual"}, headers=auth_header(token)
    )
    assert resp.status_code == 403


async def test_update_live_view_limit_out_of_range_rejected(client, two_tenants, platform_users):
    token = await login(client, platform_users["support"]["email"])
    resp = await client.patch(
        f"/tenants/{two_tenants['a']['tenant_id']}",
        json={"max_live_view_seconds": 0},
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_new_tenant_has_zero_device_quota(client, platform_users, superuser_conn):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.post("/tenants", json={"name": "Tenant Without Quota"}, headers=auth_header(token))
    assert resp.status_code == 201
    # device_quota is split into two independent quotas by category
    # (camera=jt808, gps=gt06) -- see TenantOut.
    assert resp.json()["camera_device_quota"] == 0
    assert resp.json()["gps_device_quota"] == 0
    await superuser_conn.execute("DELETE FROM tenants WHERE id = %s", (resp.json()["id"],))


async def test_device_quota_reflects_active_subscription_sum(client, two_tenants, platform_users):
    """camera_device_quota/gps_device_quota are the SUM of quantity over the
    active lines OF EACH CATEGORY -- the same calculation as
    devices.py::_assert_device_quota_not_exceeded, exposed so the UI can show
    it before the 409 on device creation comes as a surprise."""
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    plan = await client.post(
        "/billing/plans",
        json={"name": "Quota Plan", "sku": f"SKU-{tenant_id[:8]}", "category": "camera", "unit_price": 100, "billing_period": "monthly"},
        headers=auth_header(token),
    )
    plan_id = plan.json()["id"]
    await client.post(
        "/billing/subscription-items",
        json={"tenant_id": tenant_id, "billing_plan_id": plan_id, "quantity": 5},
        headers=auth_header(token),
    )
    # A separate GPS line, to confirm the two categories are counted
    # separately and not merged into one number.
    gps_plan = await client.post(
        "/billing/plans",
        json={"name": "Plan GPS", "sku": f"SKU-GPS-{tenant_id[:8]}", "category": "gps", "unit_price": 20, "billing_period": "monthly"},
        headers=auth_header(token),
    )
    await client.post(
        "/billing/subscription-items",
        json={"tenant_id": tenant_id, "billing_plan_id": gps_plan.json()["id"], "quantity": 2},
        headers=auth_header(token),
    )

    resp = await client.get("/tenants", params={"search": "tenant-a"}, headers=auth_header(token))
    matching = [t for t in resp.json()["items"] if t["id"] == tenant_id]
    assert len(matching) == 1
    assert matching[0]["camera_device_quota"] == 5
    assert matching[0]["gps_device_quota"] == 2


# --- webhooks_enabled: approved by super_admin only -----------------------
# Specifically super_admin, not any platform bypass (same rule as API key
# finding F1: granting a new capability/credential is more sensitive than
# an operational quota tweak, and support must not be able to do it alone).


async def test_support_cannot_enable_webhooks_for_a_tenant(client, two_tenants, platform_users):
    token = await login(client, platform_users["support"]["email"])
    resp = await client.patch(
        f"/tenants/{two_tenants['a']['tenant_id']}", json={"webhooks_enabled": True}, headers=auth_header(token)
    )
    assert resp.status_code == 403


async def test_super_admin_can_enable_webhooks_for_a_tenant(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.patch(
        f"/tenants/{two_tenants['a']['tenant_id']}", json={"webhooks_enabled": True}, headers=auth_header(token)
    )
    assert resp.status_code == 200
    assert resp.json()["webhooks_enabled"] is True


async def test_tenant_admin_cannot_enable_webhooks_for_own_tenant(client, two_tenants):
    """PATCH /tenants/{id} is fully bypass-only (require_bypass) -- a
    tenant_admin never even reaches the super_admin check; it is rejected
    earlier."""
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.patch(
        f"/tenants/{two_tenants['a']['tenant_id']}", json={"webhooks_enabled": True}, headers=auth_header(token)
    )
    assert resp.status_code == 403


async def test_support_can_still_update_other_tenant_fields(client, two_tenants, platform_users):
    """The restriction is ONLY on webhooks_enabled -- support keeps its normal
    operational work on the rest of this endpoint's fields."""
    token = await login(client, platform_users["support"]["email"])
    resp = await client.patch(
        f"/tenants/{two_tenants['a']['tenant_id']}", json={"gps_retention_days": 30}, headers=auth_header(token)
    )
    assert resp.status_code == 200
    assert resp.json()["gps_retention_days"] == 30
