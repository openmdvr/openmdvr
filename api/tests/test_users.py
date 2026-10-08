"""PATCH /users/{id}/status -- deactivate/reactivate an account (soft
delete, never a real DELETE). users.status has a real effect
(deps.py::assert_session_active / api_key_auth.py check users.status on
EVERY request). See migration 0038_user_device_status_audit.sql."""
import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _create_role(client, admin_token, tenant_id, role, suffix):
    resp = await client.post(
        "/users",
        json={"email": f"{role}-{suffix}@example.com", "password": TEST_PASSWORD, "role": role, "tenant_id": str(tenant_id)},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_tenant_admin_can_disable_and_reenable_operator(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    operator = await _create_role(client, admin_token, two_tenants["a"]["tenant_id"], "tenant_operator", "statustest1")

    disabled = await client.patch(
        f"/users/{operator['id']}/status", json={"status": "disabled"}, headers=auth_header(admin_token)
    )
    assert disabled.status_code == 200, disabled.text
    body = disabled.json()
    assert body["status"] == "disabled"
    assert body["status_changed_by"] == str(two_tenants["a"]["user_id"])
    assert body["status_changed_at"] is not None

    reenabled = await client.patch(
        f"/users/{operator['id']}/status", json={"status": "active"}, headers=auth_header(admin_token)
    )
    assert reenabled.status_code == 200
    assert reenabled.json()["status"] == "active"


async def test_disabled_user_loses_access_on_next_request(client, two_tenants):
    """Immediate real effect: deps.py::assert_session_active checks
    users.status on every request -- no need to wait for the JWT to expire
    (up to 8h)."""
    admin_token = await login(client, two_tenants["a"]["email"])
    viewer = await _create_role(client, admin_token, two_tenants["a"]["tenant_id"], "tenant_viewer", "statustest2")
    viewer_token = await login(client, viewer["email"])

    ok = await client.get("/devices", headers=auth_header(viewer_token))
    assert ok.status_code == 200

    await client.patch(f"/users/{viewer['id']}/status", json={"status": "disabled"}, headers=auth_header(admin_token))

    blocked = await client.get("/devices", headers=auth_header(viewer_token))
    assert blocked.status_code == 401


async def test_cannot_change_own_status(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    resp = await client.patch(
        f"/users/{two_tenants['a']['user_id']}/status", json={"status": "disabled"}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 422


async def test_tenant_viewer_cannot_change_user_status(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    viewer = await _create_role(client, admin_token, two_tenants["a"]["tenant_id"], "tenant_viewer", "statustest3")
    other = await _create_role(client, admin_token, two_tenants["a"]["tenant_id"], "tenant_operator", "statustest4")
    viewer_token = await login(client, viewer["email"])

    resp = await client.patch(f"/users/{other['id']}/status", json={"status": "disabled"}, headers=auth_header(viewer_token))
    assert resp.status_code == 403


async def test_tenant_admin_cannot_change_status_of_user_in_another_tenant(client, two_tenants):
    admin_a_token = await login(client, two_tenants["a"]["email"])
    viewer_b = await _create_role(client, await login(client, two_tenants["b"]["email"]), two_tenants["b"]["tenant_id"], "tenant_viewer", "statustest5")

    resp = await client.patch(
        f"/users/{viewer_b['id']}/status", json={"status": "disabled"}, headers=auth_header(admin_a_token)
    )
    # RLS hides another tenant's row -- 404, not 403 (the project-wide
    # "neither confirm nor deny" rule).
    assert resp.status_code == 404


async def test_support_cannot_change_status_of_platform_account(client, platform_users):
    support_token = await login(client, platform_users["support"]["email"])
    resp = await client.patch(
        f"/users/{platform_users['super_admin']['user_id']}/status",
        json={"status": "disabled"},
        headers=auth_header(support_token),
    )
    assert resp.status_code == 403


async def test_super_admin_can_change_status_of_support(client, platform_users):
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.patch(
        f"/users/{platform_users['support']['user_id']}/status",
        json={"status": "disabled"},
        headers=auth_header(super_admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"
    # Reverted so as not to affect other tests reusing this fixture within
    # the same pytest session.
    await client.patch(
        f"/users/{platform_users['support']['user_id']}/status",
        json={"status": "active"},
        headers=auth_header(super_admin_token),
    )


async def test_invalid_status_value_rejected(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    operator = await _create_role(client, admin_token, two_tenants["a"]["tenant_id"], "tenant_operator", "statustest6")
    resp = await client.patch(
        f"/users/{operator['id']}/status", json={"status": "deleted"}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 422


async def test_nonexistent_user_returns_404(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    resp = await client.patch(
        "/users/00000000-0000-0000-0000-000000000000/status",
        json={"status": "disabled"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404
