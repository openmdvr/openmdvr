"""Driver CRUD and tenant isolation -- the driver<->vehicle assignment is
tested in test_vehicles.py (it lives at /vehicles/{id}/assign-driver)."""
import pytest

from conftest import login, auth_header

pytestmark = pytest.mark.asyncio


async def test_tenant_admin_can_create_and_list_own_driver(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/drivers",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "name": "John Doe", "license_number": "LIC-1"},
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "John Doe"
    assert body["current_vehicle_id"] is None

    list_resp = await client.get("/drivers", headers=auth_header(token))
    assert any(d["id"] == body["id"] for d in list_resp.json()["items"])


async def test_tenant_b_cannot_see_tenant_a_driver(client, two_tenants):
    token_a = await login(client, two_tenants["a"]["email"])
    token_b = await login(client, two_tenants["b"]["email"])
    created = await client.post(
        "/drivers", json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "name": "Secret"}, headers=auth_header(token_a)
    )
    driver_id = created.json()["id"]

    resp = await client.get(f"/drivers/{driver_id}", headers=auth_header(token_b))
    assert resp.status_code == 404


async def test_tenant_admin_cannot_create_driver_for_other_tenant(client, two_tenants):
    token_a = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/drivers",
        json={"tenant_id": str(two_tenants["b"]["tenant_id"]), "name": "Crossed"},
        headers=auth_header(token_a),
    )
    assert resp.status_code == 403


async def test_search_driver_by_name(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    await client.post("/drivers", json={"tenant_id": tenant_id, "name": "Unique Searchable Driver"}, headers=auth_header(token))

    resp = await client.get("/drivers", params={"search": "Unique Searchable"}, headers=auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


async def test_update_driver_partial(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    created = (
        await client.post("/drivers", json={"tenant_id": tenant_id, "name": "Original", "phone": "555-0001"}, headers=auth_header(token))
    ).json()

    resp = await client.patch(f"/drivers/{created['id']}", json={"phone": "555-9999"}, headers=auth_header(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["phone"] == "555-9999"
    assert body["name"] == "Original"  # untouched


# ---------------------------------------------------------------------------
# drivers_tenant_license_number_unique (0025): prevents duplicate drivers.
# The NAME is never the key (two real drivers can share a name) -- the real
# key is the license number.
# ---------------------------------------------------------------------------

async def test_duplicate_name_is_allowed(client, two_tenants):
    """The name must NOT be unique."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    await client.post("/drivers", json={"tenant_id": tenant_id, "name": "Jane Smith"}, headers=auth_header(token))
    resp = await client.post("/drivers", json={"tenant_id": tenant_id, "name": "Jane Smith"}, headers=auth_header(token))
    assert resp.status_code == 201


async def test_duplicate_license_number_rejected(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    await client.post(
        "/drivers", json={"tenant_id": tenant_id, "name": "One", "license_number": "ABC-123"}, headers=auth_header(token)
    )
    resp = await client.post(
        "/drivers", json={"tenant_id": tenant_id, "name": "Other", "license_number": "ABC-123"}, headers=auth_header(token)
    )
    assert resp.status_code == 409


async def test_duplicate_license_number_case_and_whitespace_insensitive(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    await client.post(
        "/drivers", json={"tenant_id": tenant_id, "name": "One", "license_number": "abc-123"}, headers=auth_header(token)
    )
    resp = await client.post(
        "/drivers", json={"tenant_id": tenant_id, "name": "Other", "license_number": " ABC-123 "}, headers=auth_header(token)
    )
    assert resp.status_code == 409


async def test_same_license_number_allowed_across_different_tenants(client, two_tenants):
    token_a = await login(client, two_tenants["a"]["email"])
    token_b = await login(client, two_tenants["b"]["email"])
    resp_a = await client.post(
        "/drivers",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "name": "One", "license_number": "SHARED-1"},
        headers=auth_header(token_a),
    )
    resp_b = await client.post(
        "/drivers",
        json={"tenant_id": str(two_tenants["b"]["tenant_id"]), "name": "Other", "license_number": "SHARED-1"},
        headers=auth_header(token_b),
    )
    assert resp_a.status_code == 201
    assert resp_b.status_code == 201


async def test_two_drivers_without_license_number_allowed(client, two_tenants):
    """The index is partial (WHERE license_number IS NOT NULL) -- two drivers
    without a license number captured yet must not collide."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    resp1 = await client.post("/drivers", json={"tenant_id": tenant_id, "name": "No License 1"}, headers=auth_header(token))
    resp2 = await client.post("/drivers", json={"tenant_id": tenant_id, "name": "No License 2"}, headers=auth_header(token))
    assert resp1.status_code == 201
    assert resp2.status_code == 201


async def test_update_driver_license_number_conflict_rejected(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    await client.post(
        "/drivers", json={"tenant_id": tenant_id, "name": "One", "license_number": "XYZ-1"}, headers=auth_header(token)
    )
    other = (
        await client.post("/drivers", json={"tenant_id": tenant_id, "name": "Other"}, headers=auth_header(token))
    ).json()

    resp = await client.patch(f"/drivers/{other['id']}", json={"license_number": "XYZ-1"}, headers=auth_header(token))
    assert resp.status_code == 409
