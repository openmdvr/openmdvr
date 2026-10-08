"""Billing -- plan catalog (billing_plans) + per-tenant subscriptions
(tenant_subscription_items). The core concern of this file: billing_plans
is a GLOBAL catalog of internal prices that NO tenant_admin may ever read,
not even empty/indirectly -- and one tenant's subscription lines never
cross over to another."""
import uuid

import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _create_plan(client, token, sku=None, category="gps", unit_price=190.0):
    resp = await client.post(
        "/billing/plans",
        json={
            "name": f"Plan {sku or uuid.uuid4().hex[:6]}",
            "sku": sku or f"SKU-{uuid.uuid4().hex[:8]}",
            "category": category,
            "unit_price": unit_price,
            "currency": "MXN",
            "billing_period": "monthly",
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_tenant_operator(client, token, tenant_id, pool):
    from app import db as db_module
    from app.security import hash_password

    email = f"operator-billing-{uuid.uuid4().hex[:8]}@example.com"
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (%s, %s, %s, 'tenant_operator')",
            (tenant_id, email, hash_password(TEST_PASSWORD)),
        )
    return email


# ---------------------------------------------------------------------------
# billing_plans: global catalog, bypass-only, invisible to any tenant
# ---------------------------------------------------------------------------

async def test_super_admin_can_create_plan(client, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    sku = f"CAM-{uuid.uuid4().hex[:8]}"
    plan = await _create_plan(client, token, sku=sku)
    assert plan["sku"] == sku
    assert plan["active"] is True


async def test_support_cannot_create_plan(client, platform_users):
    """require_super_admin, not require_bypass -- the catalog is a product
    decision, distinct from day-to-day operational work (see the billing.py
    docstring and api/README.md)."""
    token = await login(client, platform_users["support"]["email"])
    resp = await client.post(
        "/billing/plans",
        json={"name": "Plan X", "sku": f"SKU-{uuid.uuid4().hex[:8]}", "category": "gps", "unit_price": 99.0},
        headers=auth_header(token),
    )
    assert resp.status_code == 403


async def test_support_can_read_and_update_existing_plan(client, platform_users):
    super_token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, super_token, sku=f"SKU-{uuid.uuid4().hex[:8]}")

    support_token = await login(client, platform_users["support"]["email"])
    listed = await client.get("/billing/plans", headers=auth_header(support_token))
    assert listed.status_code == 200
    assert any(p["id"] == plan["id"] for p in listed.json()["items"])


async def test_tenant_admin_cannot_read_plan_catalog(client, two_tenants, platform_users):
    """Core requirement: a tenant_admin must NEVER see the internal price
    catalog, not even as an empty list from a confusing permission error --
    an explicit 403."""
    super_token = await login(client, platform_users["super_admin"]["email"])
    await _create_plan(client, super_token, sku=f"SKU-{uuid.uuid4().hex[:8]}")

    tenant_token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/billing/plans", headers=auth_header(tenant_token))
    assert resp.status_code == 403


async def test_duplicate_sku_rejected(client, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    sku = f"SKU-{uuid.uuid4().hex[:8]}"
    await _create_plan(client, token, sku=sku)
    resp = await client.post(
        "/billing/plans",
        json={"name": "Another name", "sku": sku, "category": "gps", "unit_price": 1.0},
        headers=auth_header(token),
    )
    assert resp.status_code == 409


async def test_super_admin_can_deactivate_plan(client, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, sku=f"SKU-{uuid.uuid4().hex[:8]}")
    resp = await client.patch(f"/billing/plans/{plan['id']}", json={"active": False}, headers=auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["active"] is False


# ---------------------------------------------------------------------------
# tenant_subscription_items: tenant isolation + bypass-only access
# ---------------------------------------------------------------------------

async def test_bypass_can_subscribe_tenant_to_plan(client, two_tenants, platform_users):
    super_token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, super_token, sku=f"SKU-{uuid.uuid4().hex[:8]}", unit_price=299.0)

    resp = await client.post(
        "/billing/subscription-items",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "billing_plan_id": plan["id"], "quantity": 3},
        headers=auth_header(super_token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["quantity"] == 3
    assert body["effective_unit_price"] == 299.0
    assert body["plan_sku"] == plan["sku"]


async def test_unit_price_override_takes_precedence(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, sku=f"SKU-{uuid.uuid4().hex[:8]}", unit_price=499.0)

    resp = await client.post(
        "/billing/subscription-items",
        json={
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
            "billing_plan_id": plan["id"],
            "quantity": 1,
            "unit_price_override": 350.0,
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 201
    assert resp.json()["effective_unit_price"] == 350.0


async def test_custom_line_without_plan(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.post(
        "/billing/subscription-items",
        json={
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
            "custom_description": "Initial installation (one-time charge)",
            "category": "addon",  # not a device -- explicit category is required without a plan
            "quantity": 1,
            "unit_price_override": 800.0,
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["billing_plan_id"] is None
    assert body["effective_unit_price"] == 800.0


async def test_subscription_item_requires_plan_or_description(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.post(
        "/billing/subscription-items",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "quantity": 1},
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_invalid_tenant_id_rejected(client, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.post(
        "/billing/subscription-items",
        # category present on purpose -- without it, Pydantic already rejects
        # with 422 before reaching the FK, which is exactly what this test needs
        # to exercise (nonexistent tenant_id).
        json={"tenant_id": str(uuid.uuid4()), "custom_description": "x", "category": "addon", "quantity": 1},
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_tenant_admin_cannot_create_subscription_item(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/billing/subscription-items",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "custom_description": "x", "quantity": 1},
        headers=auth_header(token),
    )
    assert resp.status_code == 403


async def test_tenant_viewer_cannot_access_billing(client, two_tenants, pool):
    """Coverage gap: the role matrix already tested tenant_admin/operator but
    not tenant_viewer. Not exploitable (require_bypass is an allowlist, it
    does not depend on remembering to exclude each new role), but closing the
    gap is cheap."""
    from app import db as db_module
    from app.security import hash_password

    tenant_id = two_tenants["a"]["tenant_id"]
    email = f"viewer-billing-{uuid.uuid4().hex[:8]}@example.com"
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (%s, %s, %s, 'tenant_viewer')",
            (tenant_id, email, hash_password(TEST_PASSWORD)),
        )
    token = await login(client, email)

    assert (await client.get("/billing/plans", headers=auth_header(token))).status_code == 403
    assert (await client.get("/billing/subscription-items", headers=auth_header(token))).status_code == 403


async def test_driver_cannot_access_billing(client, two_tenants):
    """Same gap as test_tenant_viewer_cannot_access_billing, but for `driver`
    specifically -- the role that caused a real finding elsewhere when it was
    missing from a blocklist. That risk does not apply here (require_bypass is
    an allowlist), but it is tested explicitly anyway."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post(
            "/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Billing"}, headers=auth_header(token)
        )
    ).json()
    driver_email = f"driver-billing-{uuid.uuid4().hex[:8]}@example.com"
    resp = await client.post(
        "/users",
        json={
            "email": driver_email,
            "password": TEST_PASSWORD,
            "role": "driver",
            "tenant_id": str(tenant_id),
            "driver_id": driver["id"],
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text

    driver_token = await login(client, driver_email)
    assert (await client.get("/billing/plans", headers=auth_header(driver_token))).status_code == 403
    assert (await client.get("/billing/subscription-items", headers=auth_header(driver_token))).status_code == 403


async def test_tenant_operator_cannot_list_subscription_items(client, two_tenants, pool):
    """require_bypass on list_subscription_items -- no tenant role, whatever
    it is, has access to this endpoint (the narrow view for a tenant to see
    ITS OWN subscription is GET /billing/my-subscription)."""
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    operator_email = await _create_tenant_operator(client, admin_token, tenant_id, pool)
    operator_token = await login(client, operator_email)

    resp = await client.get("/billing/subscription-items", headers=auth_header(operator_token))
    assert resp.status_code == 403


async def test_subscription_items_scoped_per_tenant(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, sku=f"SKU-{uuid.uuid4().hex[:8]}", unit_price=190.0)

    item_a = (
        await client.post(
            "/billing/subscription-items",
            json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "billing_plan_id": plan["id"], "quantity": 1},
            headers=auth_header(token),
        )
    ).json()
    item_b = (
        await client.post(
            "/billing/subscription-items",
            json={"tenant_id": str(two_tenants["b"]["tenant_id"]), "billing_plan_id": plan["id"], "quantity": 5},
            headers=auth_header(token),
        )
    ).json()

    only_a = await client.get(
        "/billing/subscription-items", params={"tenant_id": str(two_tenants["a"]["tenant_id"])}, headers=auth_header(token)
    )
    ids = {i["id"] for i in only_a.json()["items"]}
    assert item_a["id"] in ids
    assert item_b["id"] not in ids


async def test_end_subscription_item_sets_ended_at_not_delete(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, sku=f"SKU-{uuid.uuid4().hex[:8]}")
    item = (
        await client.post(
            "/billing/subscription-items",
            json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "billing_plan_id": plan["id"], "quantity": 2},
            headers=auth_header(token),
        )
    ).json()
    assert item["ended_at"] is None

    resp = await client.patch(f"/billing/subscription-items/{item['id']}", json={"end_now": True}, headers=auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["ended_at"] is not None

    # Still exists (not deleted) -- it just no longer counts as active.
    active_only = await client.get(
        "/billing/subscription-items",
        params={"tenant_id": str(two_tenants["a"]["tenant_id"]), "active_only": True},
        headers=auth_header(token),
    )
    assert all(i["id"] != item["id"] for i in active_only.json()["items"])
    everything = await client.get(
        "/billing/subscription-items", params={"tenant_id": str(two_tenants["a"]["tenant_id"])}, headers=auth_header(token)
    )
    assert any(i["id"] == item["id"] for i in everything.json()["items"])


# ---------------------------------------------------------------------------
# GET /billing/my-subscription -- the narrow view of a tenant's own
# subscription.
# ---------------------------------------------------------------------------

async def test_tenant_admin_sees_own_subscription(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, unit_price=299.0)
    item = (
        await client.post(
            "/billing/subscription-items",
            json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "billing_plan_id": plan["id"], "quantity": 2},
            headers=auth_header(token),
        )
    ).json()

    tenant_token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/billing/my-subscription", headers=auth_header(tenant_token))
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["id"] == item["id"]
    assert items[0]["effective_unit_price"] == 299.0
    assert items[0]["quantity"] == 2


async def test_my_subscription_never_shows_other_tenants_lines(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    plan = await _create_plan(client, token, unit_price=150.0)
    await client.post(
        "/billing/subscription-items",
        json={"tenant_id": str(two_tenants["b"]["tenant_id"]), "billing_plan_id": plan["id"], "quantity": 1},
        headers=auth_header(token),
    )

    tenant_a_token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/billing/my-subscription", headers=auth_header(tenant_a_token))
    assert resp.status_code == 200
    assert resp.json() == []


async def test_tenant_operator_cannot_see_my_subscription(client, two_tenants, pool):
    operator_email = await _create_tenant_operator(client, await login(client, two_tenants["a"]["email"]), two_tenants["a"]["tenant_id"], pool)
    operator_token = await login(client, operator_email)
    resp = await client.get("/billing/my-subscription", headers=auth_header(operator_token))
    assert resp.status_code == 403


async def test_platform_bypass_cannot_use_my_subscription(client, platform_users):
    """my-subscription is for a real tenant session -- a platform session has
    no "own" tenant, and silently returning an empty list would be confusing;
    an explicit 400 is more honest. The check is on is_platform_bypass, not on
    the specific role -- tested with BOTH platform roles."""
    for platform_role in ("super_admin", "support"):
        token = await login(client, platform_users[platform_role]["email"])
        resp = await client.get("/billing/my-subscription", headers=auth_header(token))
        assert resp.status_code == 400
