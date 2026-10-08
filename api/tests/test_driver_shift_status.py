"""GET /drivers/shift-status -- backs the "On shift now" card in
Operations.tsx: one row per driver in the tenant with their latest event,
including drivers who never clocked anything."""
import uuid
from datetime import datetime, timezone

import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _create_driver_login(client, token, tenant_id, driver_id):
    email = f"driver-status-{uuid.uuid4().hex[:8]}@example.com"
    resp = await client.post(
        "/users",
        json={
            "email": email,
            "password": TEST_PASSWORD,
            "role": "driver",
            "tenant_id": str(tenant_id),
            "driver_id": str(driver_id),
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    return email


async def test_shift_status_includes_driver_with_no_events(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Without Events"}, headers=auth_header(token))
    ).json()

    resp = await client.get("/drivers/shift-status", headers=auth_header(token))
    assert resp.status_code == 200
    row = next(r for r in resp.json() if r["driver_id"] == driver["id"])
    assert row["last_event_type"] is None
    assert row["last_event_at"] is None


async def test_shift_status_shows_latest_event(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver On Shift"}, headers=auth_header(token))
    ).json()
    email = await _create_driver_login(client, token, tenant_id, driver["id"])
    driver_token = await login(client, email)
    await client.post("/shifts/clock", json={"event_type": "clock_in"}, headers=auth_header(driver_token))

    resp = await client.get("/drivers/shift-status", headers=auth_header(token))
    row = next(r for r in resp.json() if r["driver_id"] == driver["id"])
    assert row["last_event_type"] == "clock_in"
    assert row["last_event_at"] is not None


async def test_driver_cannot_list_shift_status(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Blocked"}, headers=auth_header(token))
    ).json()
    email = await _create_driver_login(client, token, tenant_id, driver["id"])
    driver_token = await login(client, email)

    resp = await client.get("/drivers/shift-status", headers=auth_header(driver_token))
    assert resp.status_code == 403


async def test_tenant_b_does_not_see_tenant_a_drivers_in_shift_status(client, two_tenants):
    token_a = await login(client, two_tenants["a"]["email"])
    token_b = await login(client, two_tenants["b"]["email"])
    tenant_a = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_a), "name": "Driver A Status"}, headers=auth_header(token_a))
    ).json()

    resp = await client.get("/drivers/shift-status", headers=auth_header(token_b))
    assert resp.status_code == 200
    assert all(r["driver_id"] != driver["id"] for r in resp.json())
