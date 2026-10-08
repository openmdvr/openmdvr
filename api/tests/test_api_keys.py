"""API keys for M2M integrations (0034_api_keys.sql) -- they authenticate
AS an existing user, restricted by can_write (read-only vs read-write
umbrella) and optionally by allowed_device_ids. Focus: that enforcement in
deps.py::get_current_user (tag allowlist, method gate, device scoping) is
real and does not depend on each router remembering to repeat it, and that
management (create/list/revoke/audit) stays tenant-isolated like the rest
of the project."""
import datetime as dt

import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _create_api_key(client, admin_token, user_id, **overrides) -> dict:
    body = {"name": "test key", "can_write": False}
    body.update(overrides)
    resp = await client.post(f"/users/{user_id}/api-keys", json=body, headers=auth_header(admin_token))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_role(client, admin_token, tenant_id, role, suffix):
    resp = await client.post(
        "/users",
        json={"email": f"{role}-{suffix}@example.com", "password": TEST_PASSWORD, "role": role, "tenant_id": str(tenant_id)},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- Management (create/list/revoke/audit) ----------------------------------


async def test_create_api_key_returns_raw_key_once_and_never_again(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"])
    assert created["raw_key"].startswith("omd_")
    assert "key_hash" not in created
    assert "key_prefix" in created and created["key_prefix"] == created["raw_key"][: len(created["key_prefix"])]

    listing = (
        await client.get(f"/users/{two_tenants['a']['user_id']}/api-keys", headers=auth_header(admin_token))
    ).json()
    assert listing["total"] == 1
    # The full key NEVER shows up again in any listing.
    assert "raw_key" not in listing["items"][0]
    assert "key_hash" not in listing["items"][0]


async def test_create_api_key_rejects_platform_user(client, two_tenants, platform_users):
    admin_token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        f"/users/{platform_users['super_admin']['user_id']}/api-keys",
        json={"name": "should not be created"},
        headers=auth_header(admin_token),
    )
    # users_select RLS already hides a platform account from a tenant
    # session (404), before the tenant_id check is even reached.
    assert resp.status_code == 404


async def test_create_api_key_rejects_device_outside_tenant(client, two_tenants):
    admin_a = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        f"/users/{two_tenants['a']['user_id']}/api-keys",
        json={"name": "x", "allowed_device_ids": [str(two_tenants["b"]["device_id"])]},
        headers=auth_header(admin_a),
    )
    assert resp.status_code == 422


async def test_revoke_api_key_is_idempotent(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"])

    first = await client.post(
        f"/users/{two_tenants['a']['user_id']}/api-keys/{created['id']}/revoke", headers=auth_header(admin_token)
    )
    assert first.status_code == 200
    revoked_at_first = first.json()["revoked_at"]
    assert revoked_at_first is not None

    second = await client.post(
        f"/users/{two_tenants['a']['user_id']}/api-keys/{created['id']}/revoke", headers=auth_header(admin_token)
    )
    assert second.json()["revoked_at"] == revoked_at_first


async def test_revoke_still_works_after_a_scoped_device_is_deleted(client, two_tenants, superuser_conn):
    """Regression (F2): the tenant/devices validation trigger ran on INSERT
    *and* UPDATE -- if a device referenced in allowed_device_ids was deleted
    after the key was created, revoking it (the only real UPDATE the API
    does) failed with a raw 500, making the key UNREVOCABLE. The kill switch
    for a leaked credential must ALWAYS work, regardless of what happened to
    the devices that once scoped it.

    Deleted via superuser_conn (not app_user/bypass): alarms/gps_positions
    are hypertables with no direct GRANT for app_user, not even with bypass
    (see 0009_timeseries_access.sql)."""
    admin_token = await login(client, two_tenants["a"]["email"])
    device_id = two_tenants["a"]["device_id"]

    created = await _create_api_key(
        client, admin_token, two_tenants["a"]["user_id"], allowed_device_ids=[str(device_id)]
    )

    su_cur = superuser_conn.cursor()
    await su_cur.execute("DELETE FROM alarms WHERE device_id = %s", (device_id,))
    await su_cur.execute("DELETE FROM gps_positions WHERE device_id = %s", (device_id,))
    await su_cur.execute("DELETE FROM devices WHERE id = %s", (device_id,))

    resp = await client.post(
        f"/users/{two_tenants['a']['user_id']}/api-keys/{created['id']}/revoke", headers=auth_header(admin_token)
    )
    assert resp.status_code == 200
    assert resp.json()["revoked_at"] is not None


async def test_cannot_revoke_key_through_wrong_user_id(client, two_tenants):
    """Even when both users belong to the SAME tenant -- the explicit WHERE
    on user_id (not just tenant RLS) is the real barrier here."""
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    other_user = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "keyowner")
    created = await _create_api_key(client, admin_token, other_user["id"])

    resp = await client.post(
        f"/users/{two_tenants['a']['user_id']}/api-keys/{created['id']}/revoke", headers=auth_header(admin_token)
    )
    assert resp.status_code == 404


async def test_cannot_manage_keys_for_another_tenants_user(client, two_tenants):
    admin_a = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        f"/users/{two_tenants['b']['user_id']}/api-keys", json={"name": "x"}, headers=auth_header(admin_a)
    )
    assert resp.status_code == 404


async def test_support_cannot_create_api_key_for_another_tenants_admin(client, two_tenants, platform_users):
    """Regression (F1): `support` (RLS bypass, but not super_admin) could
    issue a read-write key for ANY tenant's tenant_admin -- a credential that
    survived intact even if the support account itself was later disabled.
    Issuing an API key is issuing a new credential, same rule as "support
    cannot create users" (create_user)."""
    support_token = await login(client, platform_users["support"]["email"], TEST_PASSWORD)
    resp = await client.post(
        f"/users/{two_tenants['a']['user_id']}/api-keys",
        json={"name": "should not be able to create this", "can_write": True},
        headers=auth_header(support_token),
    )
    assert resp.status_code == 403


async def test_super_admin_can_still_create_api_key(client, two_tenants, platform_users):
    """super_admin still can -- only support is excluded."""
    super_admin_token = await login(client, platform_users["super_admin"]["email"], TEST_PASSWORD)
    resp = await client.post(
        f"/users/{two_tenants['a']['user_id']}/api-keys",
        json={"name": "created by platform", "can_write": False},
        headers=auth_header(super_admin_token),
    )
    assert resp.status_code == 201
    assert resp.json()["created_by"] == str(platform_users["super_admin"]["user_id"])


async def test_support_can_still_revoke_and_list_keys(client, two_tenants, platform_users):
    """Revoking only REDUCES access -- it stays on require_tenant_admin
    (legitimate support work: revoking a leaked key on the customer's behalf)."""
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"])
    support_token = await login(client, platform_users["support"]["email"], TEST_PASSWORD)

    listing = await client.get(f"/users/{two_tenants['a']['user_id']}/api-keys", headers=auth_header(support_token))
    assert listing.status_code == 200

    revoke = await client.post(
        f"/users/{two_tenants['a']['user_id']}/api-keys/{created['id']}/revoke", headers=auth_header(support_token)
    )
    assert revoke.status_code == 200
    assert revoke.json()["revoked_at"] is not None


async def test_only_tenant_admin_can_manage_keys(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "notadmin")
    viewer_token = await login(client, viewer["email"])
    resp = await client.post(
        f"/users/{two_tenants['a']['user_id']}/api-keys", json={"name": "x"}, headers=auth_header(viewer_token)
    )
    assert resp.status_code == 403


# --- Real API key authentication --------------------------------------------


async def test_authenticate_with_raw_key_scopes_to_underlying_user(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"])

    resp = await client.get("/devices", headers=auth_header(created["raw_key"]))
    assert resp.status_code == 200
    labels = {d["id"] for d in resp.json()["items"]}
    assert str(two_tenants["a"]["device_id"]) in labels
    # Never sees the OTHER tenant's devices -- the key inherits the same
    # RLS isolation as the underlying user.
    assert str(two_tenants["b"]["device_id"]) not in labels


async def test_garbage_key_is_rejected(client):
    resp = await client.get("/devices", headers=auth_header("omd_this-does-not-exist-in-the-db"))
    assert resp.status_code == 401


async def test_expired_key_is_rejected(client, two_tenants, pool):
    from app import db as db_module
    from app.config import get_settings
    from app.security import generate_api_key, hash_api_key

    full_key, prefix = generate_api_key()
    key_hash = hash_api_key(get_settings(), full_key)
    expired_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            """INSERT INTO api_keys (tenant_id, user_id, name, key_prefix, key_hash, created_by, expires_at)
               VALUES (%s, %s, 'expired', %s, %s, %s, %s)""",
            (
                two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"], prefix, key_hash,
                two_tenants["a"]["user_id"], expired_at,
            ),
        )
    resp = await client.get("/devices", headers=auth_header(full_key))
    assert resp.status_code == 401


async def test_revoked_key_is_rejected(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"])
    await client.post(
        f"/users/{two_tenants['a']['user_id']}/api-keys/{created['id']}/revoke", headers=auth_header(admin_token)
    )
    resp = await client.get("/devices", headers=auth_header(created["raw_key"]))
    assert resp.status_code == 401


async def test_last_used_at_updates_on_real_use(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"])
    assert created["last_used_at"] is None

    await client.get("/devices", headers=auth_header(created["raw_key"]))

    listing = (
        await client.get(f"/users/{two_tenants['a']['user_id']}/api-keys", headers=auth_header(admin_token))
    ).json()
    assert listing["items"][0]["last_used_at"] is not None


async def test_usage_log_records_requests_including_rejected_ones(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"])

    await client.get("/devices", headers=auth_header(created["raw_key"]))
    # An attempt REJECTED by the gate itself (tag not allowed) must also
    # be audited -- "auditable" includes rejections.
    await client.get("/tenants", headers=auth_header(created["raw_key"]))

    usage = (
        await client.get(
            f"/users/{two_tenants['a']['user_id']}/api-keys/{created['id']}/usage", headers=auth_header(admin_token)
        )
    ).json()
    assert usage["total"] == 2
    statuses = {item["status_code"] for item in usage["items"]}
    assert 200 in statuses
    assert 403 in statuses


# --- The read-only gate (can_write) -----------------------------------------


async def test_read_only_key_blocked_from_writing(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"], can_write=False)

    resp = await client.post(
        "/vehicles", json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "plate": "ABC-123"},
        headers=auth_header(created["raw_key"]),
    )
    assert resp.status_code == 403


async def test_read_write_key_can_write(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"], can_write=True)

    resp = await client.post(
        "/vehicles", json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "plate": "ABC-123"},
        headers=auth_header(created["raw_key"]),
    )
    assert resp.status_code == 201


async def test_read_only_key_can_still_read(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"], can_write=False)
    resp = await client.get("/vehicles", headers=auth_header(created["raw_key"]))
    assert resp.status_code == 200


# --- The tag allowlist -- ALWAYS excluded, regardless of can_write ----------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/users"),
        ("GET", "/tenants"),
        ("GET", "/billing/plans"),
        ("GET", "/platform/monitoring-settings"),
    ],
)
async def test_key_cannot_reach_excluded_tags_even_read(client, two_tenants, method, path):
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"], can_write=True)
    resp = await client.request(method, path, headers=auth_header(created["raw_key"]))
    assert resp.status_code == 403


async def test_key_cannot_send_engine_command_even_with_write(client, two_tenants):
    """device-commands (engine cut/resume) is ALWAYS excluded -- a real
    physical danger, not something that should depend on the admin
    remembering not to set can_write=true on a key."""
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"], can_write=True)
    resp = await client.post(
        f"/devices/{two_tenants['a']['device_id']}/commands",
        json={"command_type": "engine_stop"},
        headers=auth_header(created["raw_key"]),
    )
    assert resp.status_code == 403


async def test_key_cannot_mint_stream_ticket(client, two_tenants):
    """The real-time push mechanism (SSE) is meant for the interactive
    dashboard -- an API key must use REST polling, even with can_write=true
    (this is not about writing; the whole mechanism is excluded)."""
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"], can_write=True)
    resp = await client.post("/positions/stream/ticket", headers=auth_header(created["raw_key"]))
    assert resp.status_code == 403
    resp2 = await client.post("/notifications/stream/ticket", headers=auth_header(created["raw_key"]))
    assert resp2.status_code == 403


# --- allowed_device_ids -- extra scoping within what the user already sees


async def test_allowed_device_ids_narrows_visibility(client, two_tenants, pool):
    from app import db as db_module

    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_a = two_tenants["a"]["device_id"]

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        other_device = (
            await (
                await conn.execute(
                    """INSERT INTO devices (tenant_id, jt808_terminal_id, label, status)
                       VALUES (%s, %s, 'second device', 'active') RETURNING id""",
                    (tenant_id, "18800000001"),
                )
            ).fetchone()
        )[0]

    created = await _create_api_key(
        client, admin_token, two_tenants["a"]["user_id"], allowed_device_ids=[str(device_a)]
    )
    resp = await client.get("/devices", headers=auth_header(created["raw_key"]))
    ids = {d["id"] for d in resp.json()["items"]}
    assert ids == {str(device_a)}
    assert str(other_device) not in ids


async def test_allowed_device_ids_empty_list_sees_no_devices(client, two_tenants):
    """[] (explicit empty list) is DIFFERENT from omitting the field -- a key
    deliberately without access to any device, not "unscoped"."""
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"], allowed_device_ids=[])
    assert created["allowed_device_ids"] == []

    resp = await client.get("/devices", headers=auth_header(created["raw_key"]))
    assert resp.json()["items"] == []


async def test_allowed_device_ids_narrows_alarms_too(client, two_tenants, pool):
    """app_can_view_device() is the real barrier of alarms_v (0032) -- the
    API key filter enters there, without alarms.py having to know anything
    about API keys."""
    from app import db as db_module

    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_a = two_tenants["a"]["device_id"]

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        other_device = (
            await (
                await conn.execute(
                    """INSERT INTO devices (tenant_id, jt808_terminal_id, label, status)
                       VALUES (%s, %s, 'second device', 'active') RETURNING id""",
                    (tenant_id, "18800000002"),
                )
            ).fetchone()
        )[0]
        await conn.execute(
            "SELECT insert_alarm(%s, %s, now(), 'over_speed', 'warning')", (tenant_id, device_a)
        )
        await conn.execute(
            "SELECT insert_alarm(%s, %s, now(), 'over_speed', 'warning')", (tenant_id, other_device)
        )

    created = await _create_api_key(
        client, admin_token, two_tenants["a"]["user_id"], allowed_device_ids=[str(device_a)]
    )
    resp = await client.get("/alarms", headers=auth_header(created["raw_key"]))
    assert resp.status_code == 200
    device_ids_seen = {a["device_id"] for a in resp.json()}
    assert device_ids_seen == {str(device_a)}


# --- The key inherits the underlying user's role ----------------------------


async def test_key_of_tenant_viewer_cannot_reach_tenant_admin_only_endpoint(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "viewerkey")
    created = await _create_api_key(client, admin_token, viewer["id"], can_write=True)

    # POST /vehicles requires require_tenant_admin -- a tenant_viewer (with
    # or without an API key) must never be able to, regardless of can_write.
    resp = await client.post(
        "/vehicles", json={"tenant_id": str(tenant_id), "plate": "XYZ-999"}, headers=auth_header(created["raw_key"])
    )
    assert resp.status_code == 403


# --- Rate limiting -----------------------------------------------------------


async def test_rate_limit_blocks_after_threshold(client, two_tenants):
    from app.main import app
    from app.rate_limit import FixedWindowRateLimiter

    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"])

    original = app.state.api_key_rate_limiter
    app.state.api_key_rate_limiter = FixedWindowRateLimiter(max_requests=2, window_seconds=60.0)
    try:
        r1 = await client.get("/devices", headers=auth_header(created["raw_key"]))
        r2 = await client.get("/devices", headers=auth_header(created["raw_key"]))
        r3 = await client.get("/devices", headers=auth_header(created["raw_key"]))
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r3.status_code == 429
    finally:
        app.state.api_key_rate_limiter = original


# --- Regression: NUL byte in free text (found by active fuzzing, see -------
# api/README.md) ---------------------------------------------------------------


async def test_nul_byte_in_name_returns_clean_422_not_500(client, two_tenants):
    """A \x00 in any free-text field passes Pydantic validation (it is a valid
    string) but Postgres rejects NUL in text columns -- before the fix
    (main.py::data_error_handler) this was a raw 500."""
    admin_token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        f"/users/{two_tenants['a']['user_id']}/api-keys",
        json={"name": "key\x00with nul byte", "can_write": False},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422
    assert resp.json() == {"detail": "invalid input"}


async def test_nul_byte_in_search_param_returns_clean_422_not_500(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/devices", params={"search": "\x00nullbyte"}, headers=auth_header(admin_token))
    assert resp.status_code == 422
    assert resp.json() == {"detail": "invalid input"}


# --- Regression: 429 is audited too (F4) ------------------------------------


async def test_rate_limited_429_is_still_audited(client, two_tenants):
    """Before the fix, request.state.auth_claims was set AFTER the rate limit
    check -- a 429 never reached audit_api_key_usage (main.py), so the most
    interesting security signal (key abuse) was invisible in the audit log."""
    from app.main import app
    from app.rate_limit import FixedWindowRateLimiter

    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_api_key(client, admin_token, two_tenants["a"]["user_id"])

    original = app.state.api_key_rate_limiter
    app.state.api_key_rate_limiter = FixedWindowRateLimiter(max_requests=1, window_seconds=60.0)
    try:
        await client.get("/devices", headers=auth_header(created["raw_key"]))
        blocked = await client.get("/devices", headers=auth_header(created["raw_key"]))
        assert blocked.status_code == 429
    finally:
        app.state.api_key_rate_limiter = original

    usage = (
        await client.get(
            f"/users/{two_tenants['a']['user_id']}/api-keys/{created['id']}/usage", headers=auth_header(admin_token)
        )
    ).json()
    statuses = [item["status_code"] for item in usage["items"]]
    assert 429 in statuses


async def test_invalid_key_attempts_are_rate_limited_by_ip(client):
    """Regression (F3): before the fix, a NONEXISTENT key never hit any rate
    limit (the original limiter was keyed by api_key_id, which only exists for
    a real key) -- a brute-force campaign against the omd_* space had no brake."""
    from app.main import app
    from app.rate_limit import FixedWindowRateLimiter

    original = app.state.api_key_fail_limiter
    app.state.api_key_fail_limiter = FixedWindowRateLimiter(max_requests=2, window_seconds=60.0)
    try:
        r1 = await client.get("/devices", headers=auth_header("omd_does-not-exist-one"))
        r2 = await client.get("/devices", headers=auth_header("omd_does-not-exist-two"))
        r3 = await client.get("/devices", headers=auth_header("omd_does-not-exist-three"))
        assert r1.status_code == 401
        assert r2.status_code == 401
        assert r3.status_code == 429
    finally:
        app.state.api_key_fail_limiter = original
