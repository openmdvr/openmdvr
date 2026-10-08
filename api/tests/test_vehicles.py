"""Vehicle CRUD + driver assignment, and tenant isolation
(vehicles/drivers/driver_vehicle_assignments are tenant_admin
self-service, unlike devices -- see migration 0014)."""
import pytest

from conftest import login, auth_header

pytestmark = pytest.mark.asyncio


async def test_tenant_admin_can_create_and_list_own_vehicle(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/vehicles",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "plate": "AAA-111", "make": "Ford", "model": "Transit"},
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["plate"] == "AAA-111"
    assert body["current_driver_id"] is None

    list_resp = await client.get("/vehicles", headers=auth_header(token))
    assert list_resp.status_code == 200
    assert any(v["id"] == body["id"] for v in list_resp.json()["items"])


async def test_duplicate_plate_same_tenant_rejected(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    first = await client.post("/vehicles", json={"tenant_id": tenant_id, "plate": "DUP-001"}, headers=auth_header(token))
    assert first.status_code == 201
    second = await client.post("/vehicles", json={"tenant_id": tenant_id, "plate": "DUP-001"}, headers=auth_header(token))
    assert second.status_code == 409


async def test_same_plate_different_tenants_allowed(client, two_tenants):
    """Plate uniqueness is PER TENANT (partial unique index in migration
    0014), not global -- two different fleets can share a plate without a
    real conflict."""
    token_a = await login(client, two_tenants["a"]["email"])
    token_b = await login(client, two_tenants["b"]["email"])
    resp_a = await client.post(
        "/vehicles", json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "plate": "SAME-1"}, headers=auth_header(token_a)
    )
    resp_b = await client.post(
        "/vehicles", json={"tenant_id": str(two_tenants["b"]["tenant_id"]), "plate": "SAME-1"}, headers=auth_header(token_b)
    )
    assert resp_a.status_code == 201
    assert resp_b.status_code == 201


async def test_tenant_b_cannot_see_tenant_a_vehicle(client, two_tenants):
    """IDOR: RLS must hide (404, not 403 -- same rule as devices) another
    tenant's vehicle, even when its real id is known."""
    token_a = await login(client, two_tenants["a"]["email"])
    token_b = await login(client, two_tenants["b"]["email"])
    created = await client.post(
        "/vehicles", json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "plate": "SECRET-1"}, headers=auth_header(token_a)
    )
    vehicle_id = created.json()["id"]

    resp = await client.get(f"/vehicles/{vehicle_id}", headers=auth_header(token_b))
    assert resp.status_code == 404

    list_resp = await client.get("/vehicles", headers=auth_header(token_b))
    assert all(v["id"] != vehicle_id for v in list_resp.json()["items"])


async def test_tenant_admin_cannot_create_vehicle_for_other_tenant(client, two_tenants):
    """RLS (WITH CHECK tenant_id = app_current_tenant_id()) must reject an
    INSERT with a foreign tenant_id even when the body requests it explicitly."""
    token_a = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/vehicles",
        json={"tenant_id": str(two_tenants["b"]["tenant_id"]), "plate": "CROSSED-1"},
        headers=auth_header(token_a),
    )
    assert resp.status_code == 403


async def test_assign_and_reassign_driver_closes_previous_assignment(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])

    v1 = (await client.post("/vehicles", json={"tenant_id": tenant_id, "plate": "V-1"}, headers=auth_header(token))).json()
    v2 = (await client.post("/vehicles", json={"tenant_id": tenant_id, "plate": "V-2"}, headers=auth_header(token))).json()
    d1 = (await client.post("/drivers", json={"tenant_id": tenant_id, "name": "Driver One"}, headers=auth_header(token))).json()

    r1 = await client.post(f"/vehicles/{v1['id']}/assign-driver", json={"driver_id": d1["id"]}, headers=auth_header(token))
    assert r1.status_code == 200
    assert r1.json()["current_driver_id"] == d1["id"]

    # Reassigning the same driver to v2 must close their active assignment on v1.
    r2 = await client.post(f"/vehicles/{v2['id']}/assign-driver", json={"driver_id": d1["id"]}, headers=auth_header(token))
    assert r2.status_code == 200
    assert r2.json()["current_driver_id"] == d1["id"]

    v1_after = await client.get(f"/vehicles/{v1['id']}", headers=auth_header(token))
    assert v1_after.json()["current_driver_id"] is None

    driver_after = await client.get(f"/drivers/{d1['id']}", headers=auth_header(token))
    assert driver_after.json()["current_vehicle_id"] == v2["id"]


async def test_unassign_driver(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    v = (await client.post("/vehicles", json={"tenant_id": tenant_id, "plate": "V-UNASSIGN"}, headers=auth_header(token))).json()
    d = (await client.post("/drivers", json={"tenant_id": tenant_id, "name": "Driver X"}, headers=auth_header(token))).json()
    await client.post(f"/vehicles/{v['id']}/assign-driver", json={"driver_id": d["id"]}, headers=auth_header(token))

    resp = await client.post(f"/vehicles/{v['id']}/unassign-driver", headers=auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["current_driver_id"] is None
