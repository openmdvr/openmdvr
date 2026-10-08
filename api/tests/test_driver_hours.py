"""GET /drivers/{id}/hours -- hours worked per day, pairing
clock_in/clock_out and subtracting the meal break in between."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _insert_event_at(pool, tenant_id, driver_id, event_type, when):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "INSERT INTO driver_shift_events (tenant_id, driver_id, event_type, occurred_at) VALUES (%s, %s, %s, %s)",
            (tenant_id, driver_id, event_type, when),
        )


async def test_hours_report_no_events_returns_empty(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Without Hours"}, headers=auth_header(token))
    ).json()

    resp = await client.get(
        f"/drivers/{driver['id']}/hours", params={"from": "2026-01-01", "to": "2026-01-02"}, headers=auth_header(token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"] == []
    assert body["total_hours"] == 0.0


async def test_hours_report_subtracts_meal_break(client, two_tenants, pool):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver With Shift"}, headers=auth_header(token))
    ).json()

    day = datetime(2026, 3, 10, 8, 0, tzinfo=timezone.utc)
    await _insert_event_at(pool, tenant_id, driver["id"], "clock_in", day)
    await _insert_event_at(pool, tenant_id, driver["id"], "meal_start", day + timedelta(hours=4))
    await _insert_event_at(pool, tenant_id, driver["id"], "meal_end", day + timedelta(hours=5))  # 1h meal break
    await _insert_event_at(pool, tenant_id, driver["id"], "clock_out", day + timedelta(hours=9))  # 9h shift - 1h meal = 8h

    resp = await client.get(
        f"/drivers/{driver['id']}/hours", params={"from": "2026-03-10", "to": "2026-03-10"}, headers=auth_header(token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["days"]) == 1
    assert body["days"][0]["hours_worked"] == 8.0
    assert body["days"][0]["completed_shifts"] == 1
    assert body["total_hours"] == 8.0


async def test_hours_report_ignores_unclosed_shift(client, two_tenants, pool):
    """A shift that is still open (no clock_out) does not count -- a
    deliberate v1 simplification, see the comment in drivers.py."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Open Shift"}, headers=auth_header(token))
    ).json()
    await _insert_event_at(pool, tenant_id, driver["id"], "clock_in", datetime(2026, 3, 10, 8, 0, tzinfo=timezone.utc))

    resp = await client.get(
        f"/drivers/{driver['id']}/hours", params={"from": "2026-03-10", "to": "2026-03-10"}, headers=auth_header(token)
    )
    assert resp.status_code == 200
    assert resp.json()["total_hours"] == 0.0


async def test_hours_report_window_too_large_rejected(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Window"}, headers=auth_header(token))
    ).json()
    resp = await client.get(
        f"/drivers/{driver['id']}/hours", params={"from": "2026-01-01", "to": "2026-03-01"}, headers=auth_header(token)
    )
    assert resp.status_code == 422


async def test_driver_can_see_own_hours_but_not_another_drivers(client, two_tenants, pool):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver1 = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Hours One"}, headers=auth_header(token))
    ).json()
    driver2 = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Hours Two"}, headers=auth_header(token))
    ).json()
    day = datetime(2026, 3, 10, 8, 0, tzinfo=timezone.utc)
    await _insert_event_at(pool, tenant_id, driver1["id"], "clock_in", day)
    await _insert_event_at(pool, tenant_id, driver1["id"], "clock_out", day + timedelta(hours=8))
    await _insert_event_at(pool, tenant_id, driver2["id"], "clock_in", day)
    await _insert_event_at(pool, tenant_id, driver2["id"], "clock_out", day + timedelta(hours=5))

    email1 = f"driver-hours-{driver1['id']}@example.com"
    await client.post(
        "/users",
        json={"email": email1, "password": TEST_PASSWORD, "role": "driver", "tenant_id": str(tenant_id), "driver_id": driver1["id"]},
        headers=auth_header(token),
    )
    driver1_token = await login(client, email1)

    own = await client.get(
        f"/drivers/{driver1['id']}/hours", params={"from": "2026-03-10", "to": "2026-03-10"}, headers=auth_header(driver1_token)
    )
    assert own.json()["total_hours"] == 8.0

    # Explicitly requesting ANOTHER driver's driver_id from the same tenant
    # in the URL -- RLS must return empty, not driver2's real 5h.
    other = await client.get(
        f"/drivers/{driver2['id']}/hours", params={"from": "2026-03-10", "to": "2026-03-10"}, headers=auth_header(driver1_token)
    )
    assert other.status_code == 200
    assert other.json()["total_hours"] == 0.0
