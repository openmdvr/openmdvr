"""Real session revocation (security finding): get_current_user only
verified the JWT SIGNATURE, never whether the account or the tenant were
still active -- disabling/deleting a user, or suspending/cancelling a
tenant, had no real effect until the JWT expired on its own (up to 8h).
Before the fix, a CANCELLED tenant kept receiving its real-time GPS
position stream.

Also covers claim shape validation in a JWT signed with the real key but
with a non-UUID tenant_id or an unknown role -- without it, the value
slipped through to a ::uuid cast in RLS and failed with a 500 instead of a
clean 401."""
import datetime as dt
import uuid

import jwt
import pytest

from app.config import get_settings
from app.security import JWT_ALGORITHM
from conftest import auth_header, login

pytestmark = pytest.mark.asyncio


def _forge_token(**overrides) -> str:
    settings = get_settings()
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": str(uuid.uuid4()),
        "tenant_id": None,
        "role": "super_admin",
        "bypass": True,
        "driver_id": None,
        "iat": now,
        "exp": now + dt.timedelta(minutes=5),
    }
    payload.update(overrides)
    return jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGORITHM)


async def test_disabled_user_loses_access_immediately(client, two_tenants, superuser_conn):
    token = await login(client, two_tenants["a"]["email"])
    assert (await client.get("/devices", headers=auth_header(token))).status_code == 200

    await superuser_conn.execute("UPDATE users SET status = 'disabled' WHERE id = %s", (two_tenants["a"]["user_id"],))

    resp = await client.get("/devices", headers=auth_header(token))
    assert resp.status_code == 401


async def test_deleted_user_loses_access_immediately(client, two_tenants, superuser_conn):
    token = await login(client, two_tenants["a"]["email"])
    await superuser_conn.execute("DELETE FROM users WHERE id = %s", (two_tenants["a"]["user_id"],))

    resp = await client.get("/devices", headers=auth_header(token))
    assert resp.status_code == 401

    # Re-create the row so the normal two_tenants teardown (which deletes by
    # tenant_id) is not affected by this already-missing user.
    from app.security import hash_password
    from conftest import TEST_PASSWORD

    await superuser_conn.execute(
        "INSERT INTO users (id, tenant_id, email, password_hash, role) VALUES (%s, %s, %s, %s, 'tenant_admin')",
        (
            two_tenants["a"]["user_id"],
            two_tenants["a"]["tenant_id"],
            two_tenants["a"]["email"],
            hash_password(TEST_PASSWORD),
        ),
    )


async def test_suspended_tenant_returns_402_not_401(client, two_tenants, superuser_conn):
    """402 (not 401): the user is still who they claim to be -- it is the
    TENANT that does not have active service. A real distinction, not a
    cosmetic one: the frontend can show a billing message instead of sending
    the user to a re-login that would not help."""
    token = await login(client, two_tenants["a"]["email"])
    await superuser_conn.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", (two_tenants["a"]["tenant_id"],))

    resp = await client.get("/devices", headers=auth_header(token))
    assert resp.status_code == 402


async def test_cancelled_tenant_returns_402(client, two_tenants, superuser_conn):
    token = await login(client, two_tenants["a"]["email"])
    await superuser_conn.execute("UPDATE tenants SET status = 'cancelled' WHERE id = %s", (two_tenants["a"]["tenant_id"],))

    resp = await client.get("/devices", headers=auth_header(token))
    assert resp.status_code == 402


async def test_reactivated_tenant_restores_access(client, two_tenants, superuser_conn):
    token = await login(client, two_tenants["a"]["email"])
    await superuser_conn.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", (two_tenants["a"]["tenant_id"],))
    assert (await client.get("/devices", headers=auth_header(token))).status_code == 402

    await superuser_conn.execute("UPDATE tenants SET status = 'active' WHERE id = %s", (two_tenants["a"]["tenant_id"],))
    resp = await client.get("/devices", headers=auth_header(token))
    assert resp.status_code == 200


async def test_platform_session_unaffected_by_tenant_status(client, platform_users, superuser_conn):
    """A platform session (tenant_id NULL in the JWT) must not be affected by
    the status of ANY tenant -- the tenant-active check must be treated as
    true when the session belongs to no tenant, not evaluated against NULL
    and accidentally fail closed."""
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.get("/tenants", headers=auth_header(token))
    assert resp.status_code == 200


async def test_disabled_platform_user_loses_access(client, platform_users, superuser_conn):
    token = await login(client, platform_users["super_admin"]["email"])
    await superuser_conn.execute("UPDATE users SET status = 'disabled' WHERE id = %s", (platform_users["super_admin"]["user_id"],))

    resp = await client.get("/tenants", headers=auth_header(token))
    assert resp.status_code == 401


async def test_ticket_mint_blocked_for_disabled_user(client, two_tenants, superuser_conn):
    """POST /positions/stream/ticket does not go through get_db -- it needs
    its own check (without it, an already disabled user could keep minting
    fresh tickets indefinitely)."""
    token = await login(client, two_tenants["a"]["email"])
    await superuser_conn.execute("UPDATE users SET status = 'disabled' WHERE id = %s", (two_tenants["a"]["user_id"],))

    resp = await client.post("/positions/stream/ticket", headers=auth_header(token))
    assert resp.status_code == 401


async def test_ticket_mint_blocked_for_suspended_tenant(client, two_tenants, superuser_conn):
    token = await login(client, two_tenants["a"]["email"])
    await superuser_conn.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", (two_tenants["a"]["tenant_id"],))

    resp = await client.post("/positions/stream/ticket", headers=auth_header(token))
    assert resp.status_code == 402


async def test_forged_token_with_non_uuid_tenant_id_rejected(client):
    """Signed with the REAL key (settings.jwt_secret) but with a tenant_id
    that is not a UUID -- without shape validation, this slipped through to a
    ::uuid cast in RLS and failed with a 500 instead of a 401."""
    token = _forge_token(tenant_id="not-a-uuid", role="tenant_admin", bypass=False)
    resp = await client.get("/tenants", headers=auth_header(token))
    assert resp.status_code == 401


async def test_forged_token_with_sql_payload_tenant_id_rejected(client):
    token = _forge_token(tenant_id="'; DROP TABLE users; --", role="tenant_admin", bypass=False)
    resp = await client.get("/tenants", headers=auth_header(token))
    assert resp.status_code == 401


async def test_forged_token_with_unknown_role_rejected(client):
    token = _forge_token(role="root")
    resp = await client.get("/tenants", headers=auth_header(token))
    assert resp.status_code == 401


async def test_forged_token_with_valid_uuid_tenant_id_reaches_normal_401_or_402(client):
    """Control: a tenant_id with a VALID format (even if it does not exist)
    must not be rejected for its shape -- it must reach get_db, which rejects
    it anyway because the user_id does not exist either (401), but for a
    different reason than shape validation. Confirms the shape fix is not
    over-restrictive."""
    token = _forge_token(tenant_id=str(uuid.uuid4()), role="tenant_admin", bypass=False)
    resp = await client.get("/tenants", headers=auth_header(token))
    assert resp.status_code == 401
