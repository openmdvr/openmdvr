"""GET /positions/latest exposes the latest GPS position of each device.
The case that matters most, as always in this project: a tenant must never
see the position of another tenant's device."""
import pytest

from conftest import login, auth_header

pytestmark = pytest.mark.asyncio


async def _insert_position(pool, tenant_id, device_id, lat, lon):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "SELECT insert_gps_position(%s, %s, now(), %s, %s, NULL, NULL, NULL, NULL)",
            (tenant_id, device_id, lat, lon),
        )


async def test_latest_position_excludes_other_tenant(client, two_tenants, pool):
    await _insert_position(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"], 19.4326, -99.1332)
    await _insert_position(pool, two_tenants["b"]["tenant_id"], two_tenants["b"]["device_id"], 25.6866, -100.3161)

    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/positions/latest", headers=auth_header(token))
    assert resp.status_code == 200
    device_ids = {p["device_id"] for p in resp.json()}
    assert str(two_tenants["a"]["device_id"]) in device_ids
    assert str(two_tenants["b"]["device_id"]) not in device_ids


async def test_latest_position_returns_most_recent_row(client, two_tenants, pool):
    device_id = two_tenants["a"]["device_id"]
    await _insert_position(pool, two_tenants["a"]["tenant_id"], device_id, 19.0, -99.0)
    await _insert_position(pool, two_tenants["a"]["tenant_id"], device_id, 19.5, -99.5)

    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/positions/latest", headers=auth_header(token))
    assert resp.status_code == 200
    [pos] = [p for p in resp.json() if p["device_id"] == str(device_id)]
    assert pos["lat"] == 19.5
    assert pos["lon"] == -99.5


async def test_device_position_history_excludes_other_tenant(client, two_tenants, pool):
    await _insert_position(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"], 19.0, -99.0)

    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get(f"/devices/{two_tenants['b']['device_id']}/positions", headers=auth_header(token))
    assert resp.status_code == 200
    assert resp.json() == []


async def test_device_position_history_returns_ordered_points(client, two_tenants, pool):
    device_id = two_tenants["a"]["device_id"]
    await _insert_position(pool, two_tenants["a"]["tenant_id"], device_id, 19.0, -99.0)
    await _insert_position(pool, two_tenants["a"]["tenant_id"], device_id, 19.5, -99.5)

    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get(f"/devices/{device_id}/positions", headers=auth_header(token))
    assert resp.status_code == 200
    points = resp.json()
    assert len(points) == 2
    # ASC by time: the first inserted must come first
    assert points[0]["lat"] == 19.0
    assert points[1]["lat"] == 19.5
