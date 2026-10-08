"""GET /vehicles/{id}/engine-hours -- driving hours / engine on while
stopped (idle) / engine off, crossing ignition_on (alarms, migration 0048)
with GPS speed (gps_positions). "Stopped" threshold: < 5 km/h."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import login, auth_header

pytestmark = pytest.mark.asyncio


async def _insert_position_at(pool, tenant_id, device_id, lat, lon, speed_kmh, when: datetime):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "SELECT insert_gps_position(%s, %s, %s, %s, %s, %s::real, NULL, NULL, NULL)",
            (tenant_id, device_id, when, lat, lon, speed_kmh),
        )


async def _insert_ignition_alarm(pool, tenant_id, device_id, alarm_type, when: datetime):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "SELECT insert_alarm(%s, %s, %s, %s, 'info')",
            (tenant_id, device_id, when, alarm_type),
        )


async def _link_vehicle_to_device(pool, device_id, vehicle_id):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("UPDATE devices SET vehicle_id = %s WHERE id = %s", (vehicle_id, device_id))


async def test_engine_hours_no_vehicle_device_returns_empty(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    vehicle = (
        await client.post("/vehicles", json={"tenant_id": tenant_id, "plate": "ENG-1"}, headers=auth_header(token))
    ).json()

    resp = await client.get(
        f"/vehicles/{vehicle['id']}/engine-hours",
        params={"from": "2026-03-10", "to": "2026-03-10"},
        headers=auth_header(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] is None
    assert body["days"] == []
    assert body["total_driving_hours"] == 0.0


async def test_engine_hours_splits_driving_and_idle_by_speed(client, two_tenants, pool):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]
    vehicle = (
        await client.post(
            "/vehicles", json={"tenant_id": str(tenant_id), "plate": "ENG-2"}, headers=auth_header(token)
        )
    ).json()
    await _link_vehicle_to_device(pool, device_id, vehicle["id"])

    on = datetime(2026, 3, 10, 8, 0, tzinfo=timezone.utc)
    off = datetime(2026, 3, 10, 10, 0, tzinfo=timezone.utc)
    await _insert_ignition_alarm(pool, tenant_id, device_id, "ignition_on", on)
    # 08:00-09:00 at 40km/h (driving, >= 5), 09:00-09:59 at 0km/h (stopped)
    # -- the third point is deliberately BEFORE `off` (10:00): the interval
    # [on, off) excludes its right end, a point exactly at the ignition-off
    # instant must not be counted on either side.
    await _insert_position_at(pool, tenant_id, device_id, 19.0, -99.0, 40.0, on)
    await _insert_position_at(pool, tenant_id, device_id, 19.1, -99.1, 0.0, on + timedelta(hours=1))
    await _insert_position_at(pool, tenant_id, device_id, 19.1, -99.1, 0.0, on + timedelta(hours=1, minutes=59))
    await _insert_ignition_alarm(pool, tenant_id, device_id, "ignition_off", off)

    resp = await client.get(
        f"/vehicles/{vehicle['id']}/engine-hours",
        params={"from": "2026-03-10", "to": "2026-03-10"},
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["device_id"] == str(device_id)
    assert len(body["days"]) == 1
    day = body["days"][0]
    assert day["driving_hours"] == pytest.approx(1.0, abs=0.01)
    assert day["idle_hours"] == pytest.approx(59 / 60, abs=0.01)
    # 24h in the day - 2h on (ignition_off at 10:00) = 22h off
    assert day["engine_off_hours"] == pytest.approx(22.0, abs=0.01)
    assert body["total_driving_hours"] == pytest.approx(1.0, abs=0.01)
    assert body["total_idle_hours"] == pytest.approx(59 / 60, abs=0.01)


async def test_engine_hours_interval_spans_midnight_splits_by_day(client, two_tenants, pool):
    """An ignition-on shift that crosses midnight must split
    engine_off_hours correctly across BOTH days (never negative, never
    counting the same minute twice)."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]
    vehicle = (
        await client.post(
            "/vehicles", json={"tenant_id": str(tenant_id), "plate": "ENG-3"}, headers=auth_header(token)
        )
    ).json()
    await _link_vehicle_to_device(pool, device_id, vehicle["id"])

    on = datetime(2026, 3, 10, 22, 0, tzinfo=timezone.utc)
    off = datetime(2026, 3, 11, 2, 0, tzinfo=timezone.utc)
    await _insert_ignition_alarm(pool, tenant_id, device_id, "ignition_on", on)
    await _insert_ignition_alarm(pool, tenant_id, device_id, "ignition_off", off)

    resp = await client.get(
        f"/vehicles/{vehicle['id']}/engine-hours",
        params={"from": "2026-03-10", "to": "2026-03-11"},
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_date = {d["date"]: d for d in body["days"]}
    assert set(by_date) == {"2026-03-10", "2026-03-11"}
    # Day 1: on 22:00-24:00 (2h) -> off 22h
    assert by_date["2026-03-10"]["engine_off_hours"] == pytest.approx(22.0, abs=0.01)
    # Day 2: on 00:00-02:00 (2h) -> off 22h
    assert by_date["2026-03-11"]["engine_off_hours"] == pytest.approx(22.0, abs=0.01)


async def test_engine_hours_window_too_large_rejected(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    vehicle = (
        await client.post("/vehicles", json={"tenant_id": tenant_id, "plate": "ENG-4"}, headers=auth_header(token))
    ).json()

    resp = await client.get(
        f"/vehicles/{vehicle['id']}/engine-hours",
        params={"from": "2026-01-01", "to": "2026-03-01"},
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_engine_hours_excludes_other_tenant_vehicle(client, two_tenants):
    token_a = await login(client, two_tenants["a"]["email"])
    token_b = await login(client, two_tenants["b"]["email"])
    vehicle = (
        await client.post(
            "/vehicles",
            json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "plate": "ENG-5"},
            headers=auth_header(token_a),
        )
    ).json()

    resp = await client.get(
        f"/vehicles/{vehicle['id']}/engine-hours",
        params={"from": "2026-01-01", "to": "2026-01-02"},
        headers=auth_header(token_b),
    )
    assert resp.status_code == 404


async def test_engine_hours_driver_forbidden(client, two_tenants, pool):
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    vehicle = (
        await client.post("/vehicles", json={"tenant_id": tenant_id, "plate": "ENG-6"}, headers=auth_header(admin_token))
    ).json()

    driver_resp = await client.post(
        "/drivers", json={"tenant_id": tenant_id, "name": "Driver EngineHours"}, headers=auth_header(admin_token)
    )
    driver_id = driver_resp.json()["id"]
    email = "driver-enghours@example.com"
    from conftest import TEST_PASSWORD

    await client.post(
        "/users",
        json={
            "email": email, "password": TEST_PASSWORD, "role": "driver",
            "tenant_id": tenant_id, "driver_id": str(driver_id),
        },
        headers=auth_header(admin_token),
    )
    driver_token = await login(client, email)

    resp = await client.get(
        f"/vehicles/{vehicle['id']}/engine-hours",
        params={"from": "2026-01-01", "to": "2026-01-02"},
        headers=auth_header(driver_token),
    )
    assert resp.status_code == 403
