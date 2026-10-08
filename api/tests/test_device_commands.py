"""POST/GET /devices/{id}/commands -- remote commands to devices (GT06
engine cut/resume). This file focuses on the permission matrix (only
tenant_admin/bypass may FIRE a command -- a deliberate product decision
given the real risk of cutting fuel to a vehicle), tenant isolation, the
protocol gate (JT808 does not support commands today), and that the
lifecycle of the audit row (device_commands) reflects the REAL result
returned by the Go server, never just "it was sent"."""
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


async def _create_operator(client, admin_token, tenant_id, role):
    email = f"{role}-cmd-{uuid.uuid4().hex[:8]}@example.com"
    resp = await client.post(
        "/users",
        json={"email": email, "password": TEST_PASSWORD, "role": role, "tenant_id": str(tenant_id)},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201, resp.text
    return email


def _mock_command_response(code=0, reply="DYD=Success!", msg="success"):
    mock_resp = AsyncMock()
    mock_resp.json = lambda: {"code": code, "msg": msg, "reply": reply}
    return AsyncMock(return_value=mock_resp)


async def test_tenant_viewer_cannot_send_command(client, two_tenants, pool):
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    viewer_email = await _create_operator(client, admin_token, two_tenants["a"]["tenant_id"], "tenant_viewer")
    token = await login(client, viewer_email)

    with patch("app.routers.device_commands.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(
            f"/devices/{device_id}/commands", json={"command_type": "engine_stop"}, headers=auth_header(token)
        )
        assert resp.status_code == 403
        mock_client_cls.assert_not_called()


async def test_tenant_operator_cannot_send_command(client, two_tenants, pool):
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    operator_email = await _create_operator(client, admin_token, two_tenants["a"]["tenant_id"], "tenant_operator")
    token = await login(client, operator_email)

    resp = await client.post(
        f"/devices/{device_id}/commands", json={"command_type": "engine_stop"}, headers=auth_header(token)
    )
    assert resp.status_code == 403


async def test_tenant_admin_can_send_command(client, two_tenants, pool):
    """Happy path: authorized tenant_admin, the Go server reports real
    success, and the audit row ends in 'success' with the real device_reply
    (not text made up by the API)."""
    device_id, imei = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, two_tenants["a"]["email"])

    with patch("app.routers.device_commands.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = _mock_command_response()
        resp = await client.post(
            f"/devices/{device_id}/commands", json={"command_type": "engine_stop"}, headers=auth_header(token)
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    assert body["device_reply"] == "DYD=Success!"
    assert body["command_type"] == "engine_stop"

    # History reflects the same -- not two different sources of truth.
    hist = await client.get(f"/devices/{device_id}/commands", headers=auth_header(token))
    assert hist.status_code == 200
    assert hist.json()["total"] == 1
    assert hist.json()["items"][0]["status"] == "success"


async def test_device_offline_maps_to_device_offline_status(client, two_tenants, pool):
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, two_tenants["a"]["email"])

    with patch("app.routers.device_commands.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = _mock_command_response(
            code=404, reply="", msg="device not connected"
        )
        resp = await client.post(
            f"/devices/{device_id}/commands", json={"command_type": "engine_stop"}, headers=auth_header(token)
        )

    assert resp.status_code == 200  # the API answers 200 with the real state in the body, not an HTTP error
    body = resp.json()
    assert body["status"] == "device_offline"


async def test_bridge_unreachable_maps_to_failed_status_never_leaks_detail(client, two_tenants, pool):
    import httpx

    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, two_tenants["a"]["email"])

    with patch("app.routers.device_commands.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.ConnectError("connection refused")
        )
        resp = await client.post(
            f"/devices/{device_id}/commands", json={"command_type": "engine_stop"}, headers=auth_header(token)
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert "connection refused" not in resp.text  # internal detail never reaches the client


async def test_cannot_send_command_for_other_tenant_device(client, two_tenants, pool):
    device_id, _ = await _create_gt06_device(pool, two_tenants["b"]["tenant_id"])
    token = await login(client, two_tenants["a"]["email"])

    with patch("app.routers.device_commands.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(
            f"/devices/{device_id}/commands", json={"command_type": "engine_stop"}, headers=auth_header(token)
        )
        assert resp.status_code == 404
        mock_client_cls.assert_not_called()


async def test_cannot_list_commands_for_other_tenant_device(client, two_tenants, pool):
    device_id, _ = await _create_gt06_device(pool, two_tenants["b"]["tenant_id"])
    token = await login(client, two_tenants["a"]["email"])

    resp = await client.get(f"/devices/{device_id}/commands", headers=auth_header(token))
    # This endpoint validates that the device exists FOR the current session before
    # listing (the same explicit check as video.py/send_device_command:
    # SELECT id FROM devices WHERE id=%s under RLS), so another tenant's
    # device yields a clean 404 instead of a silent empty list -- the same
    # pattern used everywhere else in the app (device not found, never
    # "found but with zero results").
    assert resp.status_code == 404


async def test_jt808_device_rejects_command_with_400(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    with patch("app.routers.device_commands.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/commands",
            json={"command_type": "engine_stop"},
            headers=auth_header(token),
        )
        assert resp.status_code == 400
        mock_client_cls.assert_not_called()


async def test_invalid_command_type_rejected_with_422(client, two_tenants, pool):
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, two_tenants["a"]["email"])

    resp = await client.post(
        f"/devices/{device_id}/commands", json={"command_type": "drain_tank"}, headers=auth_header(token)
    )
    assert resp.status_code == 422


async def test_platform_bypass_can_send_command_for_any_tenant(client, two_tenants, platform_users, pool, superuser_conn):
    """Regression: using app_current_tenant_id() in the INSERT broke for a
    platform session (no tenant of its own, GUC is NULL) -- it must use the
    device's REAL tenant_id. Also covers F4: a command issued by a platform
    account must remain visible in the affected TENANT's history (before
    that fix, an INNER JOIN against users hid the whole row because the
    users_select RLS policy refuses to read a platform account from a tenant
    session)."""
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, platform_users["support"]["email"])

    try:
        with patch("app.routers.device_commands.httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value.post = _mock_command_response()
            resp = await client.post(
                f"/devices/{device_id}/commands", json={"command_type": "engine_resume"}, headers=auth_header(token)
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "success"

        # F4: the affected TENANT's own session (not the platform session that
        # issued it) must still see this row in its history.
        tenant_token = await login(client, two_tenants["a"]["email"])
        hist = await client.get(f"/devices/{device_id}/commands", headers=auth_header(tenant_token))
        assert hist.status_code == 200
        entries = hist.json()["items"]
        assert len(entries) == 1
        assert entries[0]["status"] == "success"
        # The support account that issued it is not readable through RLS from
        # this tenant session (tenant_id NULL) -- the LEFT JOIN keeps the row
        # anyway, with an explicit fallback instead of a real email this
        # session should not be able to resolve.
        assert entries[0]["requested_by_email"] == "platform"
    finally:
        # device_commands.requested_by is ON DELETE RESTRICT on purpose (same
        # as alarms.acknowledged_by -- a real audit record must not disappear
        # just because the requesting account is deleted) and the table has NO
        # DELETE policy (not even bypass can delete via app_user, on purpose --
        # it is an audit record). two_tenants cleans up its own devices via
        # CASCADE (tenant->device->device_commands), but platform_users ONLY
        # deletes the user -- without this explicit cleanup (as superuser, same
        # pattern as two_tenants' own teardown), its teardown would collide with
        # this row.
        su_cur = superuser_conn.cursor()
        await su_cur.execute("DELETE FROM device_commands WHERE device_id = %s", (device_id,))


async def _create_driver_login(client, admin_token, tenant_id):
    driver_resp = await client.post(
        "/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Commands"}, headers=auth_header(admin_token)
    )
    assert driver_resp.status_code == 201, driver_resp.text
    driver_id = driver_resp.json()["id"]

    email = f"driver-cmd-{uuid.uuid4().hex[:8]}@example.com"
    resp = await client.post(
        "/users",
        json={
            "email": email,
            "password": TEST_PASSWORD,
            "role": "driver",
            "tenant_id": str(tenant_id),
            "driver_id": str(driver_id),
        },
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201, resp.text
    return email


async def test_driver_cannot_send_command(client, two_tenants, pool):
    """F8: explicit coverage of the driver role against
    /devices/{id}/commands -- the same risk pattern found before (drivers
    inheriting fleet access through an endpoint without require_non_driver),
    verified here too even though this endpoint uses require_tenant_admin
    (stricter, but never explicitly tested with this role)."""
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    driver_email = await _create_driver_login(client, admin_token, two_tenants["a"]["tenant_id"])
    token = await login(client, driver_email)

    with patch("app.routers.device_commands.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(
            f"/devices/{device_id}/commands", json={"command_type": "engine_stop"}, headers=auth_header(token)
        )
        assert resp.status_code == 403
        mock_client_cls.assert_not_called()


async def test_driver_cannot_list_commands(client, two_tenants, pool):
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    driver_email = await _create_driver_login(client, admin_token, two_tenants["a"]["tenant_id"])
    token = await login(client, driver_email)

    resp = await client.get(f"/devices/{device_id}/commands", headers=auth_header(token))
    assert resp.status_code == 403


async def test_success_code_with_rejection_reply_recorded_as_failed(client, two_tenants, pool):
    """F3: code=0 (transport OK, a real 0x15 arrived) does not mean the device
    accepted the command -- its own speed/GPS-fix guardrail (section 6.4 of
    the protocol spec) can reject it and still answer with a real 0x15.
    Before this fix, this reply was stored as 'success' just because code==0."""
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, two_tenants["a"]["email"])

    with patch("app.routers.device_commands.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = _mock_command_response(
            code=0, reply="DYD=Speed Limit or Zero GPS Signal!"
        )
        resp = await client.post(
            f"/devices/{device_id}/commands", json={"command_type": "engine_stop"}, headers=auth_header(token)
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["device_reply"] == "DYD=Speed Limit or Zero GPS Signal!"


async def test_device_reply_with_control_bytes_is_sanitized_never_500s(client, two_tenants, pool):
    """F1: device_reply is sent by the DEVICE (untrusted input) -- a raw NUL
    used to break the final UPDATE with psycopg.errors.DataError inside the
    SAME transaction as the 'pending' INSERT, rolling back that audit record
    too. Now it is sanitized before any write and each write lives in its
    own short transaction."""
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, two_tenants["a"]["email"])

    with patch("app.routers.device_commands.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = _mock_command_response(
            code=0, reply="DYD=Success!\x00\x01\x02"
        )
        resp = await client.post(
            f"/devices/{device_id}/commands", json={"command_type": "engine_stop"}, headers=auth_header(token)
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    assert "\x00" not in body["device_reply"]
    assert body["device_reply"] == "DYD=Success!"

    # The audit record is durable regardless of the result -- confirmed by
    # reading the history back, not just the response.
    hist = await client.get(f"/devices/{device_id}/commands", headers=auth_header(token))
    assert hist.status_code == 200
    assert hist.json()["total"] == 1
    assert hist.json()["items"][0]["device_reply"] == "DYD=Success!"


async def _seed_command_at(pool, tenant_id, device_id, user_id, requested_at):
    """Inserts an already resolved device_commands row with an EXPLICIT
    requested_at -- testing pagination/date filtering requires controlling
    the timestamp, which the real flow (POST /devices/{id}/commands, which
    uses now()) does not allow. Bypass + direct INSERT, same as
    _create_gt06_device."""
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            """INSERT INTO device_commands
                   (tenant_id, device_id, command_type, requested_by, requested_at, status, completed_at)
               VALUES (%s, %s, 'engine_stop', %s, %s, 'success', %s)""",
            (tenant_id, device_id, user_id, requested_at, requested_at),
        )


async def test_pagination_limit_and_offset(client, two_tenants, pool, superuser_conn):
    """F8: the history was not paginated -- with enough commands, limit/offset
    must trim the page and total must reflect the real count, not the size of
    the current page."""
    import datetime

    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    admin_id = two_tenants["a"]["user_id"]

    try:
        base = datetime.datetime.now(datetime.timezone.utc)
        for i in range(5):
            await _seed_command_at(
                pool, two_tenants["a"]["tenant_id"], device_id, admin_id, base - datetime.timedelta(minutes=i)
            )

        page1 = await client.get(
            f"/devices/{device_id}/commands", params={"limit": 2, "offset": 0}, headers=auth_header(admin_token)
        )
        assert page1.status_code == 200
        body1 = page1.json()
        assert body1["total"] == 5
        assert len(body1["items"]) == 2
        assert body1["limit"] == 2
        assert body1["offset"] == 0

        page2 = await client.get(
            f"/devices/{device_id}/commands", params={"limit": 2, "offset": 2}, headers=auth_header(admin_token)
        )
        body2 = page2.json()
        assert body2["total"] == 5
        assert len(body2["items"]) == 2
        # Different pages, no repeated rows -- ORDER BY requested_at DESC is
        # deterministic across pages.
        assert {i["id"] for i in body1["items"]}.isdisjoint({i["id"] for i in body2["items"]})
    finally:
        su_cur = superuser_conn.cursor()
        await su_cur.execute("DELETE FROM device_commands WHERE device_id = %s", (device_id,))


async def test_date_range_filter(client, two_tenants, pool, superuser_conn):
    """date_from/date_to are instants (datetime, not date) -- the frontend
    resolves start/end of day in the browser's LOCAL timezone before sending
    them (Postgres runs in UTC; a "raw" date interpreted as UTC on the server
    can shift the day the user perceives by several hours for a UTC-8
    deployment). This test sends already-resolved instants, as the frontend
    does."""
    import datetime

    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    admin_id = two_tenants["a"]["user_id"]

    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        await _seed_command_at(pool, two_tenants["a"]["tenant_id"], device_id, admin_id, now)
        await _seed_command_at(pool, two_tenants["a"]["tenant_id"], device_id, admin_id, now - datetime.timedelta(days=10))

        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = now.replace(hour=23, minute=59, second=59, microsecond=999000)
        resp = await client.get(
            f"/devices/{device_id}/commands",
            params={"date_from": day_start.isoformat(), "date_to": day_end.isoformat()},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1  # only "today's"; the one from 10 days ago is excluded

        resp_all = await client.get(f"/devices/{device_id}/commands", headers=auth_header(admin_token))
        assert resp_all.json()["total"] == 2  # without a filter, both appear
    finally:
        su_cur = superuser_conn.cursor()
        await su_cur.execute("DELETE FROM device_commands WHERE device_id = %s", (device_id,))


async def test_invalid_date_format_rejected_with_422(client, two_tenants, pool):
    device_id, _ = await _create_gt06_device(pool, two_tenants["a"]["tenant_id"])
    token = await login(client, two_tenants["a"]["email"])

    resp = await client.get(
        f"/devices/{device_id}/commands", params={"date_from": "not-a-date"}, headers=auth_header(token)
    )
    assert resp.status_code == 422
