"""Optional `tenant_id` filter on GET /users, /vehicles, /drivers, /devices,
/routes -- the basis of the per-tenant "workspace": a platform session can
request ONLY one tenant's resources instead of the global list. It never
replaces RLS -- it only narrows further within what RLS already allows, so
these tests focus on "the filter really filters" and "it never exposes
something RLS would already block"."""
import pytest

from conftest import login, auth_header

pytestmark = pytest.mark.asyncio


async def test_users_tenant_id_filter_isolates(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.get("/users", params={"tenant_id": str(two_tenants["a"]["tenant_id"])}, headers=auth_header(token))
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) >= 1
    assert all(u["tenant_id"] == str(two_tenants["a"]["tenant_id"]) for u in items)
    assert not any(u["id"] == str(two_tenants["b"]["user_id"]) for u in items)


async def test_vehicles_tenant_id_filter_isolates(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_a = str(two_tenants["a"]["tenant_id"])
    tenant_b = str(two_tenants["b"]["tenant_id"])
    v_a = (await client.post("/vehicles", json={"tenant_id": tenant_a, "plate": "FIL-AAA"}, headers=auth_header(token))).json()
    v_b = (await client.post("/vehicles", json={"tenant_id": tenant_b, "plate": "FIL-BBB"}, headers=auth_header(token))).json()

    resp = await client.get("/vehicles", params={"tenant_id": tenant_a}, headers=auth_header(token))
    ids = {v["id"] for v in resp.json()["items"]}
    assert v_a["id"] in ids
    assert v_b["id"] not in ids


async def test_drivers_tenant_id_filter_isolates(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_a = str(two_tenants["a"]["tenant_id"])
    tenant_b = str(two_tenants["b"]["tenant_id"])
    d_a = (await client.post("/drivers", json={"tenant_id": tenant_a, "name": "Filter A"}, headers=auth_header(token))).json()
    d_b = (await client.post("/drivers", json={"tenant_id": tenant_b, "name": "Filter B"}, headers=auth_header(token))).json()

    resp = await client.get("/drivers", params={"tenant_id": tenant_a}, headers=auth_header(token))
    ids = {d["id"] for d in resp.json()["items"]}
    assert d_a["id"] in ids
    assert d_b["id"] not in ids


async def test_devices_tenant_id_filter_isolates(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.get("/devices", params={"tenant_id": str(two_tenants["a"]["tenant_id"])}, headers=auth_header(token))
    items = resp.json()["items"]
    assert len(items) >= 1
    assert all(d["tenant_id"] == str(two_tenants["a"]["tenant_id"]) for d in items)
    assert not any(d["id"] == str(two_tenants["b"]["device_id"]) for d in items)


async def test_routes_tenant_id_filter_isolates(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_a = str(two_tenants["a"]["tenant_id"])
    tenant_b = str(two_tenants["b"]["tenant_id"])
    r_a = (
        await client.post("/routes", json={"tenant_id": tenant_a, "name": "Route A", "date": "2026-01-01"}, headers=auth_header(token))
    ).json()
    r_b = (
        await client.post("/routes", json={"tenant_id": tenant_b, "name": "Route B", "date": "2026-01-01"}, headers=auth_header(token))
    ).json()

    resp = await client.get("/routes", params={"tenant_id": tenant_a}, headers=auth_header(token))
    ids = {r["id"] for r in resp.json()["items"]}
    assert r_a["id"] in ids
    assert r_b["id"] not in ids


async def test_tenant_admin_tenant_id_filter_cannot_widen_scope(client, two_tenants):
    """A tenant session passing ANOTHER tenant's tenant_id must not see
    anything from that tenant -- RLS remains the real boundary; the filter can
    only narrow, never widen."""
    token_a = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/devices", params={"tenant_id": str(two_tenants["b"]["tenant_id"])}, headers=auth_header(token_a))
    assert resp.status_code == 200
    assert resp.json()["items"] == []
