"""Device groups + user<->device assignment (see
infra/postgres/migrations/0031_device_groups_and_assignments.sql). These
tests cover the group CRUD and the assignment itself; visibility
restriction based on assignments is covered in test_device_visibility.py."""
import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _create_tenant_role(client, admin_token, tenant_id, role, suffix):
    resp = await client.post(
        "/users",
        json={
            "email": f"{role}-{suffix}@example.com",
            "password": TEST_PASSWORD,
            "role": role,
            "tenant_id": str(tenant_id),
        },
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_group_add_members_and_device_count(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    device_id = str(two_tenants["a"]["device_id"])

    created = await client.post("/device-groups", json={"tenant_id": tenant_id, "name": "North"}, headers=auth_header(token))
    assert created.status_code == 201, created.text
    group = created.json()
    assert group["device_count"] == 0

    put_resp = await client.put(
        f"/device-groups/{group['id']}/members", json={"device_ids": [device_id]}, headers=auth_header(token)
    )
    assert put_resp.status_code == 200
    assert put_resp.json() == [device_id]

    listed = await client.get("/device-groups", params={"tenant_id": tenant_id}, headers=auth_header(token))
    assert any(g["id"] == group["id"] and g["device_count"] == 1 for g in listed.json()["items"])


async def test_group_name_unique_per_tenant(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    first = await client.post("/device-groups", json={"tenant_id": tenant_id, "name": "Dup"}, headers=auth_header(token))
    assert first.status_code == 201
    second = await client.post("/device-groups", json={"tenant_id": tenant_id, "name": "Dup"}, headers=auth_header(token))
    assert second.status_code == 409


async def test_group_members_reject_device_from_other_tenant(client, two_tenants):
    token_a = await login(client, two_tenants["a"]["email"])
    tenant_a = str(two_tenants["a"]["tenant_id"])
    device_b = str(two_tenants["b"]["device_id"])

    group = (
        await client.post("/device-groups", json={"tenant_id": tenant_a, "name": "Crossed"}, headers=auth_header(token_a))
    ).json()
    resp = await client.put(
        f"/device-groups/{group['id']}/members", json={"device_ids": [device_b]}, headers=auth_header(token_a)
    )
    assert resp.status_code == 422


async def test_tenant_b_cannot_see_or_edit_tenant_a_group(client, two_tenants):
    token_a = await login(client, two_tenants["a"]["email"])
    token_b = await login(client, two_tenants["b"]["email"])
    tenant_a = str(two_tenants["a"]["tenant_id"])
    group = (await client.post("/device-groups", json={"tenant_id": tenant_a, "name": "Secret"}, headers=auth_header(token_a))).json()

    rename = await client.patch(f"/device-groups/{group['id']}", json={"name": "Stolen"}, headers=auth_header(token_b))
    assert rename.status_code == 404

    listed_b = await client.get("/device-groups", headers=auth_header(token_b))
    assert all(g["id"] != group["id"] for g in listed_b.json()["items"])


async def test_operator_cannot_manage_groups_or_assignments(client, two_tenants):
    """require_tenant_admin: a tenant_operator (or viewer) cannot write, only
    tenant_admin or platform -- RLS alone does not distinguish roles."""
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    operator = await _create_tenant_role(client, admin_token, tenant_id, "tenant_operator", "opgrp")
    operator_token = await login(client, operator["email"])

    resp = await client.post("/device-groups", json={"tenant_id": tenant_id, "name": "NotAllowed"}, headers=auth_header(operator_token))
    assert resp.status_code == 403

    resp2 = await client.put(
        f"/users/{operator['id']}/device-assignments",
        json={"device_ids": [], "device_group_ids": []},
        headers=auth_header(operator_token),
    )
    assert resp2.status_code == 403


async def test_replace_user_device_assignments_direct_and_group(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    device_id = str(two_tenants["a"]["device_id"])
    viewer = await _create_tenant_role(client, admin_token, tenant_id, "tenant_viewer", "vwassign")

    group = (await client.post("/device-groups", json={"tenant_id": tenant_id, "name": "G1"}, headers=auth_header(admin_token))).json()
    await client.put(f"/device-groups/{group['id']}/members", json={"device_ids": [device_id]}, headers=auth_header(admin_token))

    put_resp = await client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [device_id], "device_group_ids": [group["id"]]},
        headers=auth_header(admin_token),
    )
    assert put_resp.status_code == 200
    body = put_resp.json()
    assert body["device_ids"] == [device_id]
    assert body["device_group_ids"] == [group["id"]]

    get_resp = await client.get(f"/users/{viewer['id']}/device-assignments", headers=auth_header(admin_token))
    assert get_resp.json() == {"device_ids": [device_id], "device_group_ids": [group["id"]]}

    # Full replacement: a second call with empty lists must CLEAR the
    # previous assignment, not accumulate it.
    clear_resp = await client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [], "device_group_ids": []},
        headers=auth_header(admin_token),
    )
    assert clear_resp.json() == {"device_ids": [], "device_group_ids": []}


async def test_assignment_rejects_non_operator_viewer_role(client, two_tenants):
    """Assigning devices to a tenant_admin has no effect (they already see
    everything) -- it is rejected explicitly instead of silently accepted."""
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    other_admin = await _create_tenant_role(client, admin_token, tenant_id, "tenant_admin", "otheradmin")

    resp = await client.put(
        f"/users/{other_admin['id']}/device-assignments",
        json={"device_ids": [], "device_group_ids": []},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422


async def test_assignment_rejects_device_from_other_tenant(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    device_b = str(two_tenants["b"]["device_id"])
    viewer = await _create_tenant_role(client, admin_token, tenant_id, "tenant_viewer", "vwcross")

    resp = await client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [device_b], "device_group_ids": []},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422


async def test_notification_settings_default_and_partial_patch_does_not_clobber(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    viewer = await _create_tenant_role(client, admin_token, tenant_id, "tenant_viewer", "vwnotif")

    defaults = await client.get(f"/users/{viewer['id']}/notification-settings", headers=auth_header(admin_token))
    assert defaults.json() == {"in_app_enabled": True, "email_enabled": False}

    set_email = await client.patch(
        f"/users/{viewer['id']}/notification-settings", json={"email_enabled": True}, headers=auth_header(admin_token)
    )
    assert set_email.json() == {"in_app_enabled": True, "email_enabled": True}

    # Partial PATCH: omitting email_enabled must NOT reset it to false (see
    # the comment in users.py::update_user_notification_settings).
    set_in_app = await client.patch(
        f"/users/{viewer['id']}/notification-settings", json={"in_app_enabled": False}, headers=auth_header(admin_token)
    )
    assert set_in_app.json() == {"in_app_enabled": False, "email_enabled": True}
