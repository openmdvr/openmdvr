"""POST/GET /devices/{id}/config-commands -- GT06 configuration commands
(full catalog in app/gt06_config_commands.py). This file focuses on the
permission matrix -- asymmetric: SENDING (POST) is super_admin ONLY (not
even support or tenant_admin, unlike device_commands.py), VIEWING the
history (GET) stays with super_admin/support -- the protocol gate, and
that an invalid body returns a clean 422 instead of reaching the Go
server."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _create_gt06_device(pool, tenant_id, status="active"):
    from app import db as db_module

    imei = str(uuid.uuid4().int)[:15].ljust(15, "0")
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute(
                """INSERT INTO devices (tenant_id, protocol, gt06_imei, label, status)
                   VALUES (%s, 'gt06', %s, %s, %s) RETURNING id""",
                (tenant_id, imei, f"GT06 {imei[-4:]}", status),
            )
        ).fetchone()
        return row[0], imei


def _mock_bridge_response(code=0, reply="SERVER OK"):
    mock_resp = AsyncMock()
    mock_resp.json = lambda: {"code": code, "msg": "ok", "reply": reply}
    return AsyncMock(return_value=mock_resp)


async def test_tenant_admin_cannot_send_config_command(client, two_tenants, pool):
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, two_tenants["a"]["email"])  # the two_tenants admin is a tenant_admin

    with patch("app.routers.device_config_commands.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(
            f"/devices/{device_id}/config-commands",
            json={"command_key": "timezone", "params": {"offset": "-07:00"}},
            headers=auth_header(token),
        )
        mock_client_cls.assert_not_called()

    assert resp.status_code == 403


async def test_tenant_admin_cannot_list_config_commands(client, two_tenants, pool):
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get(f"/devices/{device_id}/config-commands", headers=auth_header(token))
    assert resp.status_code == 403


async def test_super_admin_can_send_config_command(client, two_tenants, platform_users, pool, superuser_conn):
    device_id, imei = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, platform_users["super_admin"]["email"])

    try:
        with patch("app.routers.device_config_commands.httpx.AsyncClient") as mock_client_cls:
            mock_post = _mock_bridge_response(code=0, reply="TIMEZONE OK")
            mock_client_cls.return_value.__aenter__.return_value.post = mock_post
            resp = await client.post(
                f"/devices/{device_id}/config-commands",
                json={"command_key": "timezone", "params": {"offset": "-07:00"}},
                headers=auth_header(token),
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "success"
        assert body["raw_text"] == "TIMEZONE,-07:00#"
        assert body["device_reply"] == "TIMEZONE OK"

        _, kwargs = mock_post.call_args
        assert kwargs["json"]["deviceKey"] == imei
        assert kwargs["json"]["text"] == "TIMEZONE,-07:00#"
    finally:
        # device_config_commands.requested_by is ON DELETE RESTRICT (same as
        # device_commands) -- without this explicit cleanup, the platform_users
        # teardown (which deletes the user) would collide with this row.
        su_cur = superuser_conn.cursor()
        await su_cur.execute("DELETE FROM device_config_commands WHERE device_id = %s", (device_id,))


async def test_support_cannot_send_config_command(client, platform_users, two_tenants, pool):
    # Sending is restricted to super_admin because the catalog includes
    # high-risk commands (firmware, redirecting live video, disabling
    # crash/fatigue sensors): "support" can NOT send -- the same split used
    # for "create an API key" (require_tenant_admin_or_super_admin) vs.
    # "revoke/list it".
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, platform_users["support"]["email"])

    with patch("app.routers.device_config_commands.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(
            f"/devices/{device_id}/config-commands",
            json={"command_key": "corekitsw", "params": {}},
            headers=auth_header(token),
        )
        mock_client_cls.assert_not_called()

    assert resp.status_code == 403


async def test_support_can_list_config_commands_history(client, platform_users, two_tenants, pool, superuser_conn):
    # VIEWING the history stays on require_bypass (super_admin or support) --
    # auditing what was already sent is legitimate support work, distinct
    # from firing a new command.
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, platform_users["super_admin"]["email"])

    try:
        with patch("app.routers.device_config_commands.httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value.post = _mock_bridge_response(code=0)
            await client.post(
                f"/devices/{device_id}/config-commands",
                json={"command_key": "corekitsw", "params": {}},
                headers=auth_header(admin_token),
            )

        support_token = await login(client, platform_users["support"]["email"])
        resp = await client.get(f"/devices/{device_id}/config-commands", headers=auth_header(support_token))
        assert resp.status_code == 200
        assert resp.json()["total"] == 1
    finally:
        su_cur = superuser_conn.cursor()
        await su_cur.execute("DELETE FROM device_config_commands WHERE device_id = %s", (device_id,))


async def test_invalid_params_returns_422_never_calls_bridge(client, platform_users, two_tenants, pool):
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, platform_users["super_admin"]["email"])

    with patch("app.routers.device_config_commands.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(
            f"/devices/{device_id}/config-commands",
            json={"command_key": "timezone", "params": {"offset": "not-a-valid-offset"}},
            headers=auth_header(token),
        )
        mock_client_cls.assert_not_called()

    assert resp.status_code == 422


async def test_invalid_host_via_custom_validator_returns_422_not_500(client, platform_users, two_tenants, pool):
    """Regression: a custom field_validator that rejects with ValueError (e.g.
    _validate_host in ServerParams) makes pydantic.ValidationError.errors()
    include the RAW ValueError object in "ctx" -- not JSON serializable, so
    the expected 422 escaped as a real 500 ("TypeError: Object of type
    ValueError is not JSON serializable"). Different from a malformed
    "offset" (a plain Pydantic `pattern` constraint, which never had this
    problem) -- hence this separate test."""
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, platform_users["super_admin"]["email"])

    with patch("app.routers.device_config_commands.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(
            f"/devices/{device_id}/config-commands",
            json={"command_key": "server", "params": {"host": "bad host!"}},
            headers=auth_header(token),
        )
        mock_client_cls.assert_not_called()

    assert resp.status_code == 422, resp.text


async def test_jt808_device_rejects_config_command(client, platform_users, two_tenants, pool):
    # two_tenants creates jt808 devices -- this channel is 100% GT06.
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.post(
        f"/devices/{two_tenants['a']['device_id']}/config-commands",
        json={"command_key": "corekitsw", "params": {}},
        headers=auth_header(token),
    )
    assert resp.status_code == 400


async def test_bridge_error_marks_failed(client, platform_users, two_tenants, pool, superuser_conn):
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, platform_users["super_admin"]["email"])

    try:
        with patch("app.routers.device_config_commands.httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value.post = _mock_bridge_response(code=404)
            resp = await client.post(
                f"/devices/{device_id}/config-commands",
                json={"command_key": "corekitsw", "params": {}},
                headers=auth_header(token),
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "device_offline"
    finally:
        su_cur = superuser_conn.cursor()
        await su_cur.execute("DELETE FROM device_config_commands WHERE device_id = %s", (device_id,))


async def test_get_config_commands_history(client, platform_users, two_tenants, pool, superuser_conn):
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, platform_users["super_admin"]["email"])

    try:
        with patch("app.routers.device_config_commands.httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value.post = _mock_bridge_response(code=0)
            await client.post(
                f"/devices/{device_id}/config-commands",
                json={"command_key": "corekitsw", "params": {}},
                headers=auth_header(token),
            )

        resp = await client.get(f"/devices/{device_id}/config-commands", headers=auth_header(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["command_key"] == "corekitsw"
    finally:
        su_cur = superuser_conn.cursor()
        await su_cur.execute("DELETE FROM device_config_commands WHERE device_id = %s", (device_id,))


async def test_wakeup_query_without_reply_is_sent_not_timeout(client, platform_users, two_tenants, pool, superuser_conn):
    """WAKEUP_QUERY has no reply by design (per the vendor documentation): a
    504 from the bridge ("did not respond") is recorded as sent, not as a
    failure. Any other command still records a timeout."""
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, platform_users["super_admin"]["email"])
    try:
        with patch("app.routers.device_config_commands.httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value.post = _mock_bridge_response(code=504, reply=None)
            resp = await client.post(
                f"/devices/{device_id}/config-commands",
                json={"command_key": "wakeup_query", "params": {}},
                headers=auth_header(token),
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "success"
            assert resp.json()["raw_text"] == "WAKEUP_QUERY#"

            other = await client.post(
                f"/devices/{device_id}/config-commands",
                json={"command_key": "reboot", "params": {}},
                headers=auth_header(token),
            )
            assert other.json()["status"] == "timeout"
    finally:
        su_cur = superuser_conn.cursor()
        await su_cur.execute("DELETE FROM device_config_commands WHERE device_id = %s", (device_id,))
