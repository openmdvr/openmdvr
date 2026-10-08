"""Route assignment. Focus on the same RLS dimension as driver_shift_events:
a driver must only see THEIR OWN assigned route, never another driver's in
the same tenant -- and, unlike devices/positions/alarms/etc., a driver
here CAN read (GET /routes), with RLS scoping it to their own rows.
Creating/editing routes remains tenant_admin-only."""
import uuid

import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _create_driver_login(client, token, tenant_id, driver_id):
    email = f"driver-route-{uuid.uuid4().hex[:8]}@example.com"
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


async def test_tenant_admin_can_create_route(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    resp = await client.post(
        "/routes",
        json={"tenant_id": str(tenant_id), "name": "Downtown Route", "date": "2026-03-10"},
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "planned"


async def test_driver_cannot_create_route(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Route"}, headers=auth_header(token))
    ).json()
    email = await _create_driver_login(client, token, tenant_id, driver["id"])
    driver_token = await login(client, email)

    resp = await client.post(
        "/routes", json={"tenant_id": str(tenant_id), "name": "Attempt", "date": "2026-03-10"}, headers=auth_header(driver_token)
    )
    assert resp.status_code == 403


async def test_route_rejects_driver_from_other_tenant(client, two_tenants):
    token_a = await login(client, two_tenants["a"]["email"])
    token_b = await login(client, two_tenants["b"]["email"])
    driver_b = (
        await client.post("/drivers", json={"tenant_id": str(two_tenants["b"]["tenant_id"]), "name": "Driver B"}, headers=auth_header(token_b))
    ).json()

    resp = await client.post(
        "/routes",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "name": "Crossed", "date": "2026-03-10", "driver_id": driver_b["id"]},
        headers=auth_header(token_a),
    )
    assert resp.status_code == 422


async def test_driver_sees_only_own_route(client, two_tenants):
    """The central case: two drivers of the same tenant, each with their own
    route for the day -- neither may see the other's."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]

    driver1 = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Route One"}, headers=auth_header(token))
    ).json()
    driver2 = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Route Two"}, headers=auth_header(token))
    ).json()
    route1 = (
        await client.post(
            "/routes",
            json={"tenant_id": str(tenant_id), "name": "Route One", "date": "2026-03-10", "driver_id": driver1["id"]},
            headers=auth_header(token),
        )
    ).json()
    await client.post(
        "/routes",
        json={"tenant_id": str(tenant_id), "name": "Route Two", "date": "2026-03-10", "driver_id": driver2["id"]},
        headers=auth_header(token),
    )

    email1 = await _create_driver_login(client, token, tenant_id, driver1["id"])
    token1 = await login(client, email1)

    resp = await client.get("/routes", headers=auth_header(token1))
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == route1["id"]


async def test_tenant_admin_sees_all_routes(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Visible Route"}, headers=auth_header(token))
    ).json()
    await client.post(
        "/routes",
        json={"tenant_id": str(tenant_id), "name": "Route Visible", "date": "2026-03-10", "driver_id": driver["id"]},
        headers=auth_header(token),
    )
    resp = await client.get("/routes", headers=auth_header(token))
    assert resp.status_code == 200
    assert any(r["driver_id"] == driver["id"] for r in resp.json()["items"])


async def test_tenant_b_cannot_see_tenant_a_routes(client, two_tenants):
    token_a = await login(client, two_tenants["a"]["email"])
    token_b = await login(client, two_tenants["b"]["email"])
    route = (
        await client.post(
            "/routes", json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "name": "Route A", "date": "2026-03-10"},
            headers=auth_header(token_a),
        )
    ).json()

    resp = await client.get("/routes", headers=auth_header(token_b))
    assert resp.status_code == 200
    assert all(r["id"] != route["id"] for r in resp.json()["items"])


async def test_update_route_assigns_driver_and_vehicle(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    route = (
        await client.post("/routes", json={"tenant_id": str(tenant_id), "name": "Editable Route", "date": "2026-03-10"}, headers=auth_header(token))
    ).json()
    vehicle = (
        await client.post("/vehicles", json={"tenant_id": str(tenant_id), "plate": "ROUTE-1"}, headers=auth_header(token))
    ).json()

    resp = await client.patch(
        f"/routes/{route['id']}", json={"vehicle_id": vehicle["id"], "status": "in_progress"}, headers=auth_header(token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["vehicle_id"] == vehicle["id"]
    assert body["status"] == "in_progress"


async def test_create_route_malformed_date_returns_422(client, two_tenants):
    """Security finding: `date` was an unvalidated `str` in RouteCreate and
    reached the INSERT raw -- a value not parseable as a date failed with a
    500 instead of the clean 422 vehicles.py/drivers.py give for the same
    kind of parameter."""
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/routes",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "name": "Malformed Route", "date": "zzz"},
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_update_route_malformed_date_returns_422(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    route = (
        await client.post(
            "/routes", json={"tenant_id": str(tenant_id), "name": "Route To Edit", "date": "2026-03-10"},
            headers=auth_header(token),
        )
    ).json()
    resp = await client.patch(f"/routes/{route['id']}", json={"date": "zzz"}, headers=auth_header(token))
    assert resp.status_code == 422


async def test_list_routes_malformed_date_query_returns_422(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/routes", params={"from": "zzz"}, headers=auth_header(token))
    assert resp.status_code == 422
    resp = await client.get("/routes", params={"to": "zzz"}, headers=auth_header(token))
    assert resp.status_code == 422


async def test_create_route_cross_tenant_returns_403_not_500(client, two_tenants):
    """Security finding: RLS already correctly blocked (fail-closed, no
    cross-tenant write) a tenant_id from ANOTHER tenant, but routes.py did not
    catch InsufficientPrivilege -- unlike vehicles.py/drivers.py/devices.py --
    so the Postgres exception propagated as a raw 500 instead of a clean 403."""
    token_b = await login(client, two_tenants["b"]["email"])
    resp = await client.post(
        "/routes",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "name": "Crossed", "date": "2026-03-10"},
        headers=auth_header(token_b),
    )
    assert resp.status_code == 403


async def test_driver_cannot_update_route(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Not Editing"}, headers=auth_header(token))
    ).json()
    route = (
        await client.post(
            "/routes", json={"tenant_id": str(tenant_id), "name": "Protected Route", "date": "2026-03-10", "driver_id": driver["id"]},
            headers=auth_header(token),
        )
    ).json()
    email = await _create_driver_login(client, token, tenant_id, driver["id"])
    driver_token = await login(client, email)

    resp = await client.patch(f"/routes/{route['id']}", json={"status": "completed"}, headers=auth_header(driver_token))
    assert resp.status_code == 403
