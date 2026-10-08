import jwt as pyjwt
import pytest

from app.config import get_settings
from conftest import TEST_PASSWORD, login

pytestmark = pytest.mark.asyncio


async def test_login_success_returns_valid_jwt(client, two_tenants):
    resp = await client.post(
        "/auth/login",
        json={"email": two_tenants["a"]["email"], "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "tenant_admin"
    assert body["tenant_id"] == str(two_tenants["a"]["tenant_id"])

    settings = get_settings()
    claims = pyjwt.decode(body["access_token"], settings.jwt_secret, algorithms=["HS256"])
    assert claims["sub"] == str(two_tenants["a"]["user_id"])
    assert claims["tenant_id"] == str(two_tenants["a"]["tenant_id"])
    assert claims["bypass"] is False


async def test_login_wrong_password_rejected(client, two_tenants):
    resp = await client.post(
        "/auth/login",
        json={"email": two_tenants["a"]["email"], "password": "not-the-right-password"},
    )
    assert resp.status_code == 401


async def test_login_unknown_email_rejected_same_as_wrong_password(client, two_tenants):
    resp_unknown = await client.post(
        "/auth/login", json={"email": "does-not-exist@example.com", "password": "x"}
    )
    resp_wrong_pw = await client.post(
        "/auth/login", json={"email": two_tenants["a"]["email"], "password": "x"}
    )
    # Same status and same error body: it must not be possible to enumerate
    # registered emails by comparing responses.
    assert resp_unknown.status_code == resp_wrong_pw.status_code == 401
    assert resp_unknown.json() == resp_wrong_pw.json()


async def test_login_rejected_for_suspended_tenant(client, two_tenants, superuser_conn):
    """An 'active' user of a 'suspended' tenant must not be able to obtain a
    new JWT -- RLS only isolates by tenant_id and does not read the tenant
    status, so this check lives in /auth/login."""
    await superuser_conn.execute(
        "UPDATE tenants SET status = 'suspended' WHERE id = %s", (two_tenants["a"]["tenant_id"],)
    )
    try:
        resp = await client.post(
            "/auth/login", json={"email": two_tenants["a"]["email"], "password": TEST_PASSWORD}
        )
        assert resp.status_code == 401
    finally:
        await superuser_conn.execute(
            "UPDATE tenants SET status = 'active' WHERE id = %s", (two_tenants["a"]["tenant_id"],)
        )


async def test_protected_endpoint_without_token_rejected(client):
    resp = await client.get("/tenants")
    assert resp.status_code == 401


async def test_protected_endpoint_with_garbage_token_rejected(client):
    resp = await client.get("/tenants", headers={"Authorization": "Bearer not-a-real-jwt"})
    assert resp.status_code == 401


async def test_protected_endpoint_with_token_signed_by_someone_else_rejected(client, two_tenants):
    forged = pyjwt.encode(
        {"sub": str(two_tenants["a"]["user_id"]), "role": "super_admin", "bypass": True},
        "a-key-that-is-not-the-server-key",
        algorithm="HS256",
    )
    resp = await client.get("/tenants", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401
