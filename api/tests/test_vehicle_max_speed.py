"""Configurable per-unit maximum speed + a real overspeed alarm (migration
0050). The check lives in insert_gps_position() (SECURITY DEFINER), the
SAME single entry point jt808server and gt06server already call -- these
tests drive that function directly via SQL (same approach as
test_gps_retention.py/test_device_ignition_power.py), without needing a
real protocol simulator to confirm the protocol-agnostic logic."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import login, auth_header

pytestmark = pytest.mark.asyncio


async def _insert_position(pool, tenant_id, device_id, speed_kmh, when: datetime, lat=19.0, lon=-99.0):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "SELECT insert_gps_position(%s, %s, %s, %s, %s, %s::real, NULL, NULL, NULL)",
            (tenant_id, device_id, when, lat, lon, speed_kmh),
        )


async def _link_vehicle_to_device(pool, device_id, vehicle_id):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("UPDATE devices SET vehicle_id = %s WHERE id = %s", (vehicle_id, device_id))


async def _alarm_count(superuser_conn, device_id, alarm_type="overspeed_limit"):
    cur = superuser_conn.cursor()
    await cur.execute("SELECT count(*) FROM alarms WHERE device_id = %s AND alarm_type = %s", (device_id, alarm_type))
    (count,) = await cur.fetchone()
    return count


async def test_create_and_update_vehicle_max_speed(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])

    resp = await client.post(
        "/vehicles", json={"tenant_id": tenant_id, "plate": "SPD-1", "max_speed_kmh": 90}, headers=auth_header(token)
    )
    assert resp.status_code == 201, resp.text
    vehicle_id = resp.json()["id"]
    assert resp.json()["max_speed_kmh"] == 90.0

    patch = await client.patch(f"/vehicles/{vehicle_id}", json={"max_speed_kmh": 110}, headers=auth_header(token))
    assert patch.status_code == 200
    assert patch.json()["max_speed_kmh"] == 110.0


async def test_vehicle_without_max_speed_defaults_to_none(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/vehicles", json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "plate": "SPD-2"}, headers=auth_header(token)
    )
    assert resp.status_code == 201
    assert resp.json()["max_speed_kmh"] is None


async def test_invalid_max_speed_rejected(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    resp = await client.post(
        "/vehicles", json={"tenant_id": tenant_id, "plate": "SPD-3", "max_speed_kmh": 0}, headers=auth_header(token)
    )
    assert resp.status_code == 422
    resp2 = await client.post(
        "/vehicles", json={"tenant_id": tenant_id, "plate": "SPD-4", "max_speed_kmh": 9999}, headers=auth_header(token)
    )
    assert resp2.status_code == 422


async def test_overspeed_crossing_creates_single_alarm_not_per_sample(client, two_tenants, pool, superuser_conn):
    """The rising edge fires ONE alarm -- repeated positions above the limit
    (the normal case, one every ~20-30s while the overspeed lasts) must NEVER
    produce one alarm each, same rule as ignition/power."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]
    vehicle = (
        await client.post(
            "/vehicles", json={"tenant_id": str(tenant_id), "plate": "SPD-5", "max_speed_kmh": 80},
            headers=auth_header(token),
        )
    ).json()
    await _link_vehicle_to_device(pool, device_id, vehicle["id"])

    base = datetime(2026, 3, 10, 8, 0, tzinfo=timezone.utc)
    # Three consecutive positions above the limit (80) -- must produce ONE
    # alarm, not three.
    await _insert_position(pool, tenant_id, device_id, 95.0, base)
    await _insert_position(pool, tenant_id, device_id, 100.0, base + timedelta(seconds=20))
    await _insert_position(pool, tenant_id, device_id, 92.0, base + timedelta(seconds=40))

    assert await _alarm_count(superuser_conn, device_id) == 1


async def test_overspeed_clears_and_refires_on_new_crossing(client, two_tenants, pool, superuser_conn):
    """Dropping below the limit and exceeding it again is a NEW real
    transition -- it must produce a SECOND alarm, not get stuck in "already
    warned once, never again"."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]
    vehicle = (
        await client.post(
            "/vehicles", json={"tenant_id": str(tenant_id), "plate": "SPD-6", "max_speed_kmh": 80},
            headers=auth_header(token),
        )
    ).json()
    await _link_vehicle_to_device(pool, device_id, vehicle["id"])

    base = datetime(2026, 3, 10, 9, 0, tzinfo=timezone.utc)
    await _insert_position(pool, tenant_id, device_id, 95.0, base)  # edge 1 -> alarm
    await _insert_position(pool, tenant_id, device_id, 60.0, base + timedelta(seconds=20))  # drops below the limit
    await _insert_position(pool, tenant_id, device_id, 90.0, base + timedelta(seconds=40))  # edge 2 -> alarm

    assert await _alarm_count(superuser_conn, device_id) == 2


async def test_no_alarm_without_max_speed_configured(client, two_tenants, pool, superuser_conn):
    """With no limit configured (max_speed_kmh NULL, the real default of any
    new vehicle), no speed -- however high -- may fire the alarm."""
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]
    base = datetime(2026, 3, 10, 10, 0, tzinfo=timezone.utc)
    await _insert_position(pool, tenant_id, device_id, 180.0, base)

    assert await _alarm_count(superuser_conn, device_id) == 0
