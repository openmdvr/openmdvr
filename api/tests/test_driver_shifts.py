"""Driver login (driver role) + shift events -- this introduces a new
auth/RLS dimension. Explicit focus on the same class of findings as
earlier security reviews: IDOR, role escalation, and here also a new
dimension -- a driver must NEVER see ANOTHER driver's shift event, not even
within the same tenant."""
import uuid

import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _create_driver_login(client, token, tenant_id, driver_id, email=None):
    email = email or f"driver-{uuid.uuid4().hex[:8]}@example.com"
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
    return resp.json(), email


# --- creating the driver account ---


async def test_tenant_admin_can_create_driver_login(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Login"}, headers=auth_header(token))
    ).json()

    user, _ = await _create_driver_login(client, token, tenant_id, driver["id"])
    assert user["role"] == "driver"
    assert user["driver_id"] == driver["id"]


async def test_driver_role_without_driver_id_rejected(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/users",
        json={
            "email": f"no-driver-{uuid.uuid4().hex[:8]}@example.com",
            "password": TEST_PASSWORD,
            "role": "driver",
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_non_driver_role_with_driver_id_rejected(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Spare"}, headers=auth_header(token))
    ).json()
    resp = await client.post(
        "/users",
        json={
            "email": f"viewer-with-driver-{uuid.uuid4().hex[:8]}@example.com",
            "password": TEST_PASSWORD,
            "role": "tenant_viewer",
            "tenant_id": str(tenant_id),
            "driver_id": driver["id"],
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_cannot_create_driver_login_for_other_tenant_driver(client, two_tenants):
    """The CHECK/trigger from migration 0015 (enforce_driver_tenant_match,
    reused from 0014) must reject a driver_id from ANOTHER tenant, not just a
    foreign tenant_id -- two distinct crossing vectors."""
    token_a = await login(client, two_tenants["a"]["email"])
    token_b = await login(client, two_tenants["b"]["email"])
    driver_b = (
        await client.post(
            "/drivers", json={"tenant_id": str(two_tenants["b"]["tenant_id"]), "name": "Driver B"}, headers=auth_header(token_b)
        )
    ).json()

    resp = await client.post(
        "/users",
        json={
            "email": f"crossed-{uuid.uuid4().hex[:8]}@example.com",
            "password": TEST_PASSWORD,
            "role": "driver",
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
            "driver_id": driver_b["id"],
        },
        headers=auth_header(token_a),
    )
    assert resp.status_code == 422


async def test_driver_cannot_have_two_login_accounts(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Double"}, headers=auth_header(token))
    ).json()
    await _create_driver_login(client, token, tenant_id, driver["id"])

    resp = await client.post(
        "/users",
        json={
            "email": f"second-account-{uuid.uuid4().hex[:8]}@example.com",
            "password": TEST_PASSWORD,
            "role": "driver",
            "tenant_id": str(tenant_id),
            "driver_id": driver["id"],
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 409


# --- role escalation: a driver inherits no elevated privilege ---


async def test_driver_cannot_create_vehicles(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Without Privileges"}, headers=auth_header(token))
    ).json()
    _, email = await _create_driver_login(client, token, tenant_id, driver["id"])

    driver_token = await login(client, email)
    resp = await client.post(
        "/vehicles", json={"tenant_id": str(tenant_id), "plate": "NOPE-1"}, headers=auth_header(driver_token)
    )
    assert resp.status_code == 403


async def test_non_driver_cannot_clock_shift_event(client, two_tenants):
    """require_driver must reject any role other than driver, including
    tenant_admin -- clock-in/out is driver self-service only."""
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post("/shifts/clock", json={"event_type": "clock_in"}, headers=auth_header(token))
    assert resp.status_code == 403


@pytest.fixture
async def driver_token(client, two_tenants):
    """A real driver with no other privilege -- used by all the "a driver
    cannot see/do X" tests below. Security finding: get_current_user ALONE
    (without explicitly excluding the driver role) left a driver with exactly
    the same access as tenant_viewer to devices/positions/alarms/vehicles/
    drivers/users/video -- contradicting the intent (a driver may only clock
    their own shift). Fixed with require_non_driver (deps.py), tested here
    endpoint by endpoint."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Restricted"}, headers=auth_header(token))
    ).json()
    _, email = await _create_driver_login(client, token, tenant_id, driver["id"])
    return await login(client, email)


async def test_driver_cannot_list_devices(client, driver_token):
    resp = await client.get("/devices", headers=auth_header(driver_token))
    assert resp.status_code == 403


async def test_driver_cannot_get_device(client, driver_token, two_tenants):
    resp = await client.get(f"/devices/{two_tenants['a']['device_id']}", headers=auth_header(driver_token))
    assert resp.status_code == 403


async def test_driver_cannot_see_latest_positions(client, driver_token):
    resp = await client.get("/positions/latest", headers=auth_header(driver_token))
    assert resp.status_code == 403


async def test_driver_cannot_list_alarms(client, driver_token):
    resp = await client.get("/alarms", headers=auth_header(driver_token))
    assert resp.status_code == 403


async def test_driver_cannot_acknowledge_alarm(client, driver_token):
    resp = await client.post(f"/alarms/{uuid.uuid4()}/acknowledge", headers=auth_header(driver_token))
    assert resp.status_code == 403


async def test_driver_cannot_request_video(client, driver_token, two_tenants):
    resp = await client.post(f"/devices/{two_tenants['a']['device_id']}/video", headers=auth_header(driver_token))
    assert resp.status_code == 403


async def test_driver_cannot_list_vehicles(client, driver_token):
    resp = await client.get("/vehicles", headers=auth_header(driver_token))
    assert resp.status_code == 403


async def test_driver_cannot_list_drivers(client, driver_token):
    resp = await client.get("/drivers", headers=auth_header(driver_token))
    assert resp.status_code == 403


async def test_driver_cannot_list_users(client, driver_token):
    resp = await client.get("/users", headers=auth_header(driver_token))
    assert resp.status_code == 403


async def test_driver_cannot_list_tenants(client, driver_token):
    resp = await client.get("/tenants", headers=auth_header(driver_token))
    assert resp.status_code == 403


async def test_driver_cannot_create_user(client, driver_token, two_tenants):
    resp = await client.post(
        "/users",
        json={
            "email": f"other-{uuid.uuid4().hex[:8]}@example.com",
            "password": TEST_PASSWORD,
            "role": "tenant_viewer",
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
        },
        headers=auth_header(driver_token),
    )
    assert resp.status_code == 403


async def test_driver_can_still_use_own_shift_endpoints(client, driver_token):
    """Confirms require_non_driver did not slip into /shifts by mistake -- a
    driver can still clock and see their own shift."""
    clock_resp = await client.post("/shifts/clock", json={"event_type": "clock_in"}, headers=auth_header(driver_token))
    assert clock_resp.status_code == 201
    list_resp = await client.get("/shifts", headers=auth_header(driver_token))
    assert list_resp.status_code == 200
    assert len(list_resp.json()["items"]) == 1


# --- the core: a driver only sees/inserts THEIR OWN events ---


async def test_driver_can_clock_in_and_see_own_event(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver One"}, headers=auth_header(token))
    ).json()
    _, email = await _create_driver_login(client, token, tenant_id, driver["id"])
    driver_token = await login(client, email)

    resp = await client.post(
        "/shifts/clock", json={"event_type": "clock_in", "lat": 19.43, "lon": -99.13}, headers=auth_header(driver_token)
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["driver_id"] == driver["id"]
    assert body["event_type"] == "clock_in"
    assert body["source"] == "driver_app"

    own_events = await client.get("/shifts", headers=auth_header(driver_token))
    assert own_events.status_code == 200
    assert len(own_events.json()["items"]) == 1
    assert own_events.json()["items"][0]["driver_id"] == driver["id"]


async def test_driver_cannot_see_another_drivers_events(client, two_tenants):
    """The central case: two drivers of the SAME tenant, one must NOT see the
    other's events -- the new RLS dimension (app_current_driver_id()) must
    isolate per driver, not just per tenant."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]

    driver1 = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver One"}, headers=auth_header(token))
    ).json()
    driver2 = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Two"}, headers=auth_header(token))
    ).json()
    _, email1 = await _create_driver_login(client, token, tenant_id, driver1["id"])
    _, email2 = await _create_driver_login(client, token, tenant_id, driver2["id"])

    token1 = await login(client, email1)
    token2 = await login(client, email2)

    await client.post("/shifts/clock", json={"event_type": "clock_in"}, headers=auth_header(token1))
    await client.post("/shifts/clock", json={"event_type": "clock_in"}, headers=auth_header(token2))

    events1 = (await client.get("/shifts", headers=auth_header(token1))).json()["items"]
    events2 = (await client.get("/shifts", headers=auth_header(token2))).json()["items"]

    assert len(events1) == 1
    assert events1[0]["driver_id"] == driver1["id"]
    assert len(events2) == 1
    assert events2[0]["driver_id"] == driver2["id"]

    # Not even when explicitly requesting the other driver_id as a filter --
    # RLS must drop the row from the result before the query's manual WHERE
    # matters.
    filtered = await client.get("/shifts", params={"driver_id": driver2["id"]}, headers=auth_header(token1))
    assert filtered.json()["items"] == []


async def test_tenant_admin_sees_all_drivers_shift_events(client, two_tenants):
    """tenant_admin (non-driver session) must see ALL the tenant's events, not
    just one driver's -- app_current_driver_id() is NULL for the current session, so
    the policy falls back to the usual rule (the whole tenant)."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Visible"}, headers=auth_header(token))
    ).json()
    _, email = await _create_driver_login(client, token, tenant_id, driver["id"])
    driver_token = await login(client, email)
    await client.post("/shifts/clock", json={"event_type": "clock_in"}, headers=auth_header(driver_token))

    resp = await client.get("/shifts", headers=auth_header(token))
    assert resp.status_code == 200
    driver_ids = {e["driver_id"] for e in resp.json()["items"]}
    assert driver["id"] in driver_ids


async def test_tenant_b_admin_cannot_see_tenant_a_shift_events(client, two_tenants):
    """The usual tenant isolation, not broken by the new driver dimension."""
    token_a = await login(client, two_tenants["a"]["email"])
    token_b = await login(client, two_tenants["b"]["email"])
    tenant_a = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_a), "name": "Driver Tenant A"}, headers=auth_header(token_a))
    ).json()
    _, email = await _create_driver_login(client, token_a, tenant_a, driver["id"])
    driver_token = await login(client, email)
    await client.post("/shifts/clock", json={"event_type": "clock_in"}, headers=auth_header(driver_token))

    resp = await client.get("/shifts", headers=auth_header(token_b))
    assert resp.status_code == 200
    assert all(e["driver_id"] != driver["id"] for e in resp.json()["items"])


async def test_driver_events_are_append_only(client, two_tenants):
    """There is no PATCH/DELETE endpoint for driver_shift_events -- the table
    is append-only even for the driver (same as usage_events). This confirms
    no alternative HTTP method allows it, not just that the one we tried does
    not exist."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Append Only"}, headers=auth_header(token))
    ).json()
    _, email = await _create_driver_login(client, token, tenant_id, driver["id"])
    driver_token = await login(client, email)
    event = (
        await client.post("/shifts/clock", json={"event_type": "clock_in"}, headers=auth_header(driver_token))
    ).json()

    patch_resp = await client.patch(f"/shifts/{event['id']}", json={"event_type": "clock_out"}, headers=auth_header(driver_token))
    assert patch_resp.status_code in (404, 405)
    delete_resp = await client.delete(f"/shifts/{event['id']}", headers=auth_header(driver_token))
    assert delete_resp.status_code in (404, 405)
