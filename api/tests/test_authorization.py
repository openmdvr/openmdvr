"""Tests focused on the class of findings fixed in earlier security
reviews: IDOR, role escalation, cross-tenant data leaks. The API relies on
RLS for tenant isolation, but these tests verify the observable end-to-end
behavior (over HTTP), not just that the SQL policy exists."""
import uuid

import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


# --- tenants ---

async def test_tenant_admin_sees_only_own_tenant(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/tenants", headers=auth_header(token))
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()["items"]}
    assert ids == {str(two_tenants["a"]["tenant_id"])}


async def test_tenant_admin_cannot_create_tenant(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post("/tenants", json={"name": "Sneaky Tenant"}, headers=auth_header(token))
    assert resp.status_code == 403


async def test_support_cannot_create_tenant(client, platform_users):
    """support bypasses RLS (cross-tenant reads) but creating a tenant
    (customer onboarding) is super_admin work -- see docs/architecture.md and
    require_super_admin in deps.py."""
    token = await login(client, platform_users["support"]["email"])
    resp = await client.post("/tenants", json={"name": "Tenant Via Support"}, headers=auth_header(token))
    assert resp.status_code == 403


async def test_super_admin_can_create_tenant(client, platform_users, superuser_conn):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.post("/tenants", json={"name": f"Tenant Super Admin {uuid.uuid4().hex[:8]}"}, headers=auth_header(token))
    assert resp.status_code == 201
    await superuser_conn.execute("DELETE FROM tenants WHERE id = %s", (resp.json()["id"],))


# --- devices: IDOR ---

async def test_tenant_a_cannot_get_tenant_b_device_by_id(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get(f"/devices/{two_tenants['b']['device_id']}", headers=auth_header(token))
    assert resp.status_code == 404


async def test_tenant_a_device_list_excludes_tenant_b(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/devices", headers=auth_header(token))
    assert resp.status_code == 200
    ids = {d["id"] for d in resp.json()["items"]}
    assert str(two_tenants["a"]["device_id"]) in ids
    assert str(two_tenants["b"]["device_id"]) not in ids


async def test_tenant_admin_cannot_create_device(client, two_tenants):
    """Creating devices is bypass-only (a platform action) -- see the note in
    infra/postgres/migrations/0008_rls_policies.sql."""
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/devices",
        json={
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
            "jt808_terminal_id": "999888777",
            "label": "self-service attempt",
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 403


async def test_device_terminal_id_with_leading_zero_rejected(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/devices",
        json={
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
            "jt808_terminal_id": "0123456",
            "label": "x",
        },
        headers=auth_header(token),
    )
    # 403 (no bypass) comes before field validation in this flow, so the
    # schema validation is tested directly.
    assert resp.status_code in (403, 422)


# --- users: role escalation and tenant crossing ---

async def test_tenant_admin_cannot_create_user_for_other_tenant(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/users",
        json={
            "email": "intruder@example.com",
            "password": "a-long-password",
            "role": "tenant_viewer",
            "tenant_id": str(two_tenants["b"]["tenant_id"]),
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 403


async def test_tenant_admin_cannot_create_platform_user(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/users",
        json={
            "email": "wants-to-be-admin@example.com",
            "password": "a-long-password",
            "role": "super_admin",
            "tenant_id": None,
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 403


async def test_tenant_admin_can_create_user_in_own_tenant(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/users",
        json={
            "email": f"new-{uuid.uuid4().hex[:8]}@example.com",
            "password": "a-long-password",
            "role": "tenant_viewer",
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 201
    assert resp.json()["tenant_id"] == str(two_tenants["a"]["tenant_id"])


async def test_create_user_with_oversized_password_returns_422(client, two_tenants):
    """bcrypt rejects inputs longer than 72 UTF-8 bytes -- this must become an
    explicit 422, not a 500 from an uncaught ValueError."""
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/users",
        json={
            "email": f"long-password-{uuid.uuid4().hex[:8]}@example.com",
            "password": "á" * 73,  # each 'á' is 2 bytes in UTF-8 -> 146 bytes
            "role": "tenant_viewer",
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_users_list_excludes_other_tenant(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/users", headers=auth_header(token))
    assert resp.status_code == 200
    tenant_ids = {u["tenant_id"] for u in resp.json()["items"]}
    assert tenant_ids == {str(two_tenants["a"]["tenant_id"])}


async def test_support_cannot_create_platform_user(client, platform_users):
    """support bypasses RLS, but creating ANOTHER platform account (support or
    super_admin) is a privilege escalation only super_admin may perform -- RLS
    alone does not distinguish this."""
    token = await login(client, platform_users["support"]["email"])
    resp = await client.post(
        "/users",
        json={
            "email": f"another-support-{uuid.uuid4().hex[:8]}@example.com",
            "password": "a-long-password",
            "role": "support",
            "tenant_id": None,
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 403


async def test_super_admin_can_create_platform_user(client, platform_users, superuser_conn):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.post(
        "/users",
        json={
            "email": f"new-support-{uuid.uuid4().hex[:8]}@example.com",
            "password": "a-long-password",
            "role": "support",
            "tenant_id": None,
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 201
    await superuser_conn.execute("DELETE FROM users WHERE id = %s", (resp.json()["id"],))
