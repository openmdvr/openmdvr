"""POST /users/{id}/reset-password -- administrative password reset. Same
permission rule as PATCH /users/{id}/status (see test_users.py), plus one
extra restriction: a tenant_admin can reset passwords for their own team,
but NEVER for another tenant_admin -- that is reserved to the platform."""
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


async def test_tenant_admin_can_reset_operator_password_and_new_password_works(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    operator = await _create_role(client, admin_token, two_tenants["a"]["tenant_id"], "tenant_operator", "pwreset1")

    resp = await client.post(
        f"/users/{operator['id']}/reset-password",
        json={"new_password": "a-real-new-password"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == operator["id"]

    # The old password (TEST_PASSWORD, used at creation) no longer works.
    old_login = await client.post("/auth/login", json={"email": operator["email"], "password": TEST_PASSWORD})
    assert old_login.status_code == 401

    # The new one does.
    new_login = await client.post(
        "/auth/login", json={"email": operator["email"], "password": "a-real-new-password"}
    )
    assert new_login.status_code == 200


async def test_tenant_viewer_cannot_reset_any_password(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    viewer = await _create_role(client, admin_token, two_tenants["a"]["tenant_id"], "tenant_viewer", "pwreset2")
    other = await _create_role(client, admin_token, two_tenants["a"]["tenant_id"], "tenant_operator", "pwreset3")
    viewer_token = await login(client, viewer["email"])

    resp = await client.post(
        f"/users/{other['id']}/reset-password", json={"new_password": "another-real-password"}, headers=auth_header(viewer_token)
    )
    assert resp.status_code == 403


async def test_tenant_admin_cannot_reset_password_of_user_in_another_tenant(client, two_tenants):
    admin_a_token = await login(client, two_tenants["a"]["email"])
    admin_b_token = await login(client, two_tenants["b"]["email"])
    viewer_b = await _create_role(client, admin_b_token, two_tenants["b"]["tenant_id"], "tenant_viewer", "pwreset4")

    resp = await client.post(
        f"/users/{viewer_b['id']}/reset-password",
        json={"new_password": "another-real-password"},
        headers=auth_header(admin_a_token),
    )
    # RLS hides another tenant's row -- 404, same rule as status.
    assert resp.status_code == 404


async def test_tenant_admin_cannot_reset_another_tenant_admins_password(client, two_tenants, pool):
    """Reserved to the platform."""
    from app import db as db_module
    from app.security import hash_password

    admin_a_token = await login(client, two_tenants["a"]["email"])
    # A second tenant_admin of the SAME tenant (co-admin) -- the generic
    # _create_role helper works for this (role="tenant_admin" is a valid
    # TENANT_ROLE).
    resp = await client.post(
        "/users",
        json={
            "email": "coadmin-pwreset@example.com",
            "password": TEST_PASSWORD,
            "role": "tenant_admin",
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
        },
        headers=auth_header(admin_a_token),
    )
    assert resp.status_code == 201, resp.text
    coadmin = resp.json()

    reset_resp = await client.post(
        f"/users/{coadmin['id']}/reset-password",
        json={"new_password": "another-real-password"},
        headers=auth_header(admin_a_token),
    )
    assert reset_resp.status_code == 403


async def test_support_can_reset_tenant_admins_password(client, two_tenants, platform_users):
    """Accepted trade-off (see the comment in users.py): support CAN do this,
    unlike issuing a new API key for a tenant_admin, because the affected user
    can detect the change (their login stops working) and it is not a silent
    credential like an API key."""
    support_token = await login(client, platform_users["support"]["email"])
    resp = await client.post(
        f"/users/{two_tenants['a']['user_id']}/reset-password",
        json={"new_password": "another-real-password"},
        headers=auth_header(support_token),
    )
    assert resp.status_code == 200, resp.text


async def test_support_cannot_reset_platform_account_password(client, platform_users):
    support_token = await login(client, platform_users["support"]["email"])
    resp = await client.post(
        f"/users/{platform_users['super_admin']['user_id']}/reset-password",
        json={"new_password": "another-real-password"},
        headers=auth_header(support_token),
    )
    assert resp.status_code == 403


async def test_super_admin_can_reset_support_password(client, platform_users):
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.post(
        f"/users/{platform_users['support']['user_id']}/reset-password",
        json={"new_password": "another-real-password"},
        headers=auth_header(super_admin_token),
    )
    assert resp.status_code == 200, resp.text


async def test_password_too_short_rejected(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    operator = await _create_role(client, admin_token, two_tenants["a"]["tenant_id"], "tenant_operator", "pwreset5")
    resp = await client.post(
        f"/users/{operator['id']}/reset-password", json={"new_password": "short"}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 422


async def test_nonexistent_user_returns_404(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/users/00000000-0000-0000-0000-000000000000/reset-password",
        json={"new_password": "another-real-password"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 404


async def test_driver_cannot_reset_any_password(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    driver_resp = await client.post(
        "/drivers", json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "name": "Driver PwReset"}, headers=auth_header(admin_token)
    )
    assert driver_resp.status_code == 201, driver_resp.text
    driver_id = driver_resp.json()["id"]
    driver_user_resp = await client.post(
        "/users",
        json={
            "email": "driver-pwreset@example.com",
            "password": TEST_PASSWORD,
            "role": "driver",
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
            "driver_id": str(driver_id),
        },
        headers=auth_header(admin_token),
    )
    assert driver_user_resp.status_code == 201, driver_user_resp.text
    driver_token = await login(client, "driver-pwreset@example.com")

    other = await _create_role(client, admin_token, two_tenants["a"]["tenant_id"], "tenant_viewer", "pwreset6")
    resp = await client.post(
        f"/users/{other['id']}/reset-password", json={"new_password": "another-real-password"}, headers=auth_header(driver_token)
    )
    assert resp.status_code == 403
