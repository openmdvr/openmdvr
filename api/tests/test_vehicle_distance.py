"""GET /vehicles/{id}/distance -- kilometers traveled per day, summing the
haversine distance between consecutive GPS points of the device currently
installed in the vehicle."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import login, auth_header

pytestmark = pytest.mark.asyncio


async def _insert_position_at(pool, tenant_id, device_id, lat, lon, when: datetime):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "SELECT insert_gps_position(%s, %s, %s, %s, %s, NULL, NULL, NULL, NULL)",
            (tenant_id, device_id, when, lat, lon),
        )


async def _link_vehicle_to_device(pool, device_id, vehicle_id):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("UPDATE devices SET vehicle_id = %s WHERE id = %s", (vehicle_id, device_id))


async def test_distance_report_no_vehicle_device_returns_empty(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    vehicle = (
        await client.post("/vehicles", json={"tenant_id": tenant_id, "plate": "DIST-1"}, headers=auth_header(token))
    ).json()

    resp = await client.get(
        f"/vehicles/{vehicle['id']}/distance",
        params={"from": "2026-01-01", "to": "2026-01-02"},
        headers=auth_header(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] is None
    assert body["days"] == []
    assert body["total_distance_km"] == 0.0


async def test_distance_report_sums_consecutive_points(client, two_tenants, pool):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]
    vehicle = (
        await client.post(
            "/vehicles", json={"tenant_id": str(tenant_id), "plate": "DIST-2"}, headers=auth_header(token)
        )
    ).json()
    await _link_vehicle_to_device(pool, device_id, vehicle["id"])

    day = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    # Mexico City -> Puebla, ~100km as the crow flies -- not exact (the real
    # road is not a straight line), but the purpose of this test is to
    # confirm the SUM across consecutive points, not an exact reference value
    # from a real map.
    await _insert_position_at(pool, tenant_id, device_id, 19.4326, -99.1332, day)
    await _insert_position_at(pool, tenant_id, device_id, 19.0414, -98.2063, day + timedelta(hours=1))

    resp = await client.get(
        f"/vehicles/{vehicle['id']}/distance",
        params={"from": "2026-03-10", "to": "2026-03-10"},
        headers=auth_header(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] == str(device_id)
    assert len(body["days"]) == 1
    assert body["days"][0]["position_count"] == 2
    # ~90-100km straight line Mexico City-Puebla -- a generous range, not an
    # exact value, so the test is not coupled to the haversine formula digit
    # by digit.
    assert 80 < body["days"][0]["distance_km"] < 110
    assert body["total_distance_km"] == body["days"][0]["distance_km"]


async def test_distance_report_splits_by_day(client, two_tenants, pool):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]
    vehicle = (
        await client.post(
            "/vehicles", json={"tenant_id": str(tenant_id), "plate": "DIST-3"}, headers=auth_header(token)
        )
    ).json()
    await _link_vehicle_to_device(pool, device_id, vehicle["id"])

    day1 = datetime(2026, 3, 10, 10, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 3, 11, 10, 0, tzinfo=timezone.utc)
    await _insert_position_at(pool, tenant_id, device_id, 19.0, -99.0, day1)
    await _insert_position_at(pool, tenant_id, device_id, 19.1, -99.1, day1 + timedelta(minutes=30))
    await _insert_position_at(pool, tenant_id, device_id, 20.0, -100.0, day2)

    resp = await client.get(
        f"/vehicles/{vehicle['id']}/distance",
        params={"from": "2026-03-10", "to": "2026-03-11"},
        headers=auth_header(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    dates = [d["date"] for d in body["days"]]
    assert dates == ["2026-03-10", "2026-03-11"]
    # The first point of day 2 contributes no distance to day 1 (the jump
    # between days IS counted, attributed to the destination point's day).
    assert body["days"][1]["position_count"] == 1


async def test_distance_report_window_too_large_rejected(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    vehicle = (
        await client.post("/vehicles", json={"tenant_id": tenant_id, "plate": "DIST-4"}, headers=auth_header(token))
    ).json()

    resp = await client.get(
        f"/vehicles/{vehicle['id']}/distance",
        params={"from": "2026-01-01", "to": "2026-03-01"},
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_distance_report_excludes_other_tenant_vehicle(client, two_tenants):
    token_a = await login(client, two_tenants["a"]["email"])
    token_b = await login(client, two_tenants["b"]["email"])
    vehicle = (
        await client.post(
            "/vehicles",
            json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "plate": "DIST-5"},
            headers=auth_header(token_a),
        )
    ).json()

    resp = await client.get(
        f"/vehicles/{vehicle['id']}/distance",
        params={"from": "2026-01-01", "to": "2026-01-02"},
        headers=auth_header(token_b),
    )
    assert resp.status_code == 404
