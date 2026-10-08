"""Device model catalog (device_models) + model/SIM fields on devices.
Focus: permission matrix (GET bypass-only, POST require_super_admin), that
device_model_name is resolved in DeviceOut, and that
sim_plan_cost_mxn_month/sim_plan_data_cap_mb NEVER appear in the response
(they are exclusive to the usage report, see test_billing_sim_usage.py)."""
import uuid

import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def test_list_device_models_rejects_tenant_admin(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/devices/models", headers=auth_header(token))
    assert resp.status_code == 403


async def test_list_device_models_allows_support(client, platform_users):
    token = await login(client, platform_users["support"]["email"])
    resp = await client.get("/devices/models", headers=auth_header(token))
    assert resp.status_code == 200
    # Seeded by migration 0045 -- confirms the catalog has at least the
    # seeded models, not an empty list.
    names = {m["name"] for m in resp.json()}
    assert "JC261" in names
    assert "CY06-2G" in names


async def test_create_device_model_rejects_support(client, platform_users):
    token = await login(client, platform_users["support"]["email"])
    resp = await client.post(
        "/devices/models", json={"name": f"model-{uuid.uuid4().hex[:8]}", "protocol": "gt06"}, headers=auth_header(token)
    )
    assert resp.status_code == 403


async def test_create_device_model_allows_super_admin(client, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    name = f"model-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/devices/models", json={"name": name, "protocol": "gt06_video"}, headers=auth_header(token))
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == name


async def test_create_device_model_duplicate_name_conflicts(client, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    name = f"model-{uuid.uuid4().hex[:8]}"
    first = await client.post("/devices/models", json={"name": name, "protocol": "gt06"}, headers=auth_header(token))
    assert first.status_code == 201
    second = await client.post("/devices/models", json={"name": name, "protocol": "gt06"}, headers=auth_header(token))
    assert second.status_code == 409


async def test_device_model_and_sim_persist_on_create(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    models = (await client.get("/devices/models", headers=auth_header(token))).json()
    jc261 = next(m for m in models if m["name"] == "JC261")

    # Explicit quota -- two_tenants has no subscription (see test_devices.py).
    resp = await client.post(
        "/billing/subscription-items",
        json={
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
            "custom_description": "test quota",
            "category": "camera",
            "quantity": 5,
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text

    imei = str(uuid.uuid4().int)[:15].ljust(15, "0")
    resp = await client.post(
        "/devices",
        json={
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
            "protocol": "gt06_video",
            "gt06_imei": imei,
            "label": "device with model/sim",
            "device_model_id": jc261["id"],
            "sim_number": "5215512345678",
            "sim_carrier": "ExampleCarrier",
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["device_model_id"] == jc261["id"]
    assert body["device_model_name"] == "JC261"
    assert body["sim_number"] == "5215512345678"
    assert body["sim_carrier"] == "ExampleCarrier"
    # Never in the response, not even for platform -- see
    # test_billing_sim_usage.py for where they ARE read back.
    assert "sim_plan_cost_mxn_month" not in body
    assert "sim_plan_data_cap_mb" not in body


async def test_tenant_admin_sees_sim_number_via_get(client, two_tenants, platform_users):
    """A tenant admin only sees the SIM number: sim_number/sim_carrier DO
    reach a normal tenant_admin session (no bypass needed to read them),
    unlike usage/cost."""
    admin_token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.patch(
        f"/devices/{two_tenants['a']['device_id']}",
        json={"sim_number": "5219988877766", "sim_carrier": "AT&T"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text

    tenant_token = await login(client, two_tenants["a"]["email"])
    resp = await client.get(f"/devices/{two_tenants['a']['device_id']}", headers=auth_header(tenant_token))
    assert resp.status_code == 200
    assert resp.json()["sim_number"] == "5219988877766"
    assert resp.json()["sim_carrier"] == "AT&T"


async def test_update_device_sim_plan_cost_never_leaks_in_response(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.patch(
        f"/devices/{two_tenants['a']['device_id']}",
        json={"sim_plan_cost_mxn_month": 199.99, "sim_plan_data_cap_mb": 2048},
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    assert "sim_plan_cost_mxn_month" not in resp.json()
    assert "sim_plan_data_cap_mb" not in resp.json()
