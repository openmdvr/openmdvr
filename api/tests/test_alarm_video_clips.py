"""POST /alarms/{id}/request-clip and GET /alarms/{id}/clip -- retrieval of
video clips tied to an alarm (GT06/JC261). This file focuses on the
protocol gate (only gt06_video supports this today), idempotency (never
fire the request twice), that jt808-server is called with the right
payload, that a failure of that call marks the request as failed instead
of leaving it stuck in 'requested', and tenant isolation -- same approach
as test_device_commands.py, including explicit coverage of the driver role."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _create_gt06_video_device(pool, tenant_id, status="active"):
    from app import db as db_module

    imei = str(uuid.uuid4().int)[:15].ljust(15, "0")
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute(
                """INSERT INTO devices (tenant_id, protocol, gt06_imei, label, status)
                   VALUES (%s, 'gt06_video', %s, %s, %s) RETURNING id""",
                (tenant_id, imei, f"JC261 {imei[-4:]}", status),
            )
        ).fetchone()
        return row[0], imei


async def _insert_alarm(pool, tenant_id, device_id, alarm_type="gt06_camera_event", severity="warning"):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute(
                "SELECT insert_alarm(%s, %s, now(), %s, %s)",
                (tenant_id, device_id, alarm_type, severity),
            )
        ).fetchone()
        return row[0]


async def _create_driver_login(client, admin_token, tenant_id):
    driver_resp = await client.post(
        "/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Clips"}, headers=auth_header(admin_token)
    )
    assert driver_resp.status_code == 201, driver_resp.text
    driver_id = driver_resp.json()["id"]

    email = f"driver-clip-{uuid.uuid4().hex[:8]}@example.com"
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


def _mock_bridge_response(code=0, msg="ok"):
    mock_resp = AsyncMock()
    mock_resp.json = lambda: {"code": code, "msg": msg}
    return AsyncMock(return_value=mock_resp)


async def test_request_clip_rejects_unsupported_protocol(client, two_tenants, pool):
    # two_tenants creates jt808 devices -- JT808 clip retrieval is designed
    # but not implemented yet, so a clean 400 is expected.
    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])
    token = await login(client, two_tenants["a"]["email"])

    resp = await client.post(f"/alarms/{alarm_id}/request-clip", headers=auth_header(token))
    assert resp.status_code == 400


async def test_request_clip_rejects_alarm_type_without_video(client, two_tenants, pool):
    # Ignition, engine cut, geofences... the device never stores video for
    # those alarms: clean 400 and the bridge is never called (otherwise the
    # button always ended in "the device did not upload the file").
    device_id, _ = await _create_gt06_video_device(pool, two_tenants["a"]["tenant_id"])
    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], device_id, alarm_type="ignition_off", severity="info")
    token = await login(client, two_tenants["a"]["email"])

    with patch("app.routers.alarms.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(f"/alarms/{alarm_id}/request-clip", headers=auth_header(token))
        mock_client_cls.assert_not_called()
    assert resp.status_code == 400
    assert "does not produce video" in resp.json()["detail"]


async def test_request_clip_success_calls_bridge_with_expected_payload(client, two_tenants, pool):
    device_id, imei = await _create_gt06_video_device(pool, two_tenants["a"]["tenant_id"])
    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], device_id)
    token = await login(client, two_tenants["a"]["email"])

    with patch("app.routers.alarms.httpx.AsyncClient") as mock_client_cls:
        mock_post = _mock_bridge_response(code=0)
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(f"/alarms/{alarm_id}/request-clip", headers=auth_header(token))

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["alarm_id"] == str(alarm_id)
    assert body["status"] == "requested"
    assert body["url"] is None

    mock_post.assert_awaited_once()
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["imei"] == imei
    assert kwargs["json"]["clipId"] == body["id"]


async def test_request_clip_bridge_connection_error_marks_failed(client, two_tenants, pool):
    import httpx

    device_id, _ = await _create_gt06_video_device(pool, two_tenants["a"]["tenant_id"])
    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], device_id)
    token = await login(client, two_tenants["a"]["email"])

    with patch("app.routers.alarms.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.ConnectError("connection refused")
        )
        resp = await client.post(f"/alarms/{alarm_id}/request-clip", headers=auth_header(token))

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error_detail"]


async def test_request_clip_bridge_nonzero_code_marks_failed(client, two_tenants, pool):
    device_id, _ = await _create_gt06_video_device(pool, two_tenants["a"]["tenant_id"])
    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], device_id)
    token = await login(client, two_tenants["a"]["email"])

    with patch("app.routers.alarms.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = _mock_bridge_response(
            code=1, msg="device busy"
        )
        resp = await client.post(f"/alarms/{alarm_id}/request-clip", headers=auth_header(token))

    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "failed"


async def test_request_clip_bridge_already_marked_failed_no_500(client, two_tenants, pool):
    """Regression: jt808-server (internal/alarmclip::handleRequestClip) marks
    the clip 'failed' itself BEFORE the HTTP response reaches this API when
    SendCommand fails immediately (e.g. device not connected).
    mark_alarm_clip_failed() refuses to reopen a terminal state
    (enforce_alarm_video_clip_status_transition) -- without the fix, that
    InsufficientPrivilege escaped uncaught as a raw 500 instead of the API
    simply recognizing that the other half of the system already did its job."""
    from app import db as db_module

    device_id, _ = await _create_gt06_video_device(pool, two_tenants["a"]["tenant_id"])
    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], device_id)
    token = await login(client, two_tenants["a"]["email"])

    async def fake_post(_url, json):
        # Reproduce exactly what jt808-server really does: mark the clip
        # failed BEFORE this response reaches the API.
        async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
            await conn.execute(
                "SELECT mark_alarm_clip_failed(%s, 'failed', %s)",
                (json["clipId"], "gt06: device not connected"),
            )
        mock_resp = AsyncMock()
        mock_resp.json = lambda: {"code": 500, "msg": "gt06: device not connected"}
        return mock_resp

    with patch("app.routers.alarms.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = fake_post
        resp = await client.post(f"/alarms/{alarm_id}/request-clip", headers=auth_header(token))

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error_detail"] == "gt06: device not connected"


async def test_request_clip_is_idempotent(client, two_tenants, pool):
    """A request already in progress ('requested'/'uploading') for the same
    alarm does not fire a second real command -- the bridge must be called
    only once, no matter how many times the operator clicks/refreshes."""
    device_id, _ = await _create_gt06_video_device(pool, two_tenants["a"]["tenant_id"])
    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], device_id)
    token = await login(client, two_tenants["a"]["email"])

    with patch("app.routers.alarms.httpx.AsyncClient") as mock_client_cls:
        mock_post = _mock_bridge_response(code=0)
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post

        first = await client.post(f"/alarms/{alarm_id}/request-clip", headers=auth_header(token))
        second = await client.post(f"/alarms/{alarm_id}/request-clip", headers=auth_header(token))

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    mock_post.assert_awaited_once()


async def test_request_clip_cross_tenant_alarm_returns_404(client, two_tenants, pool):
    device_id, _ = await _create_gt06_video_device(pool, two_tenants["b"]["tenant_id"])
    alarm_id = await _insert_alarm(pool, two_tenants["b"]["tenant_id"], device_id)
    token = await login(client, two_tenants["a"]["email"])

    with patch("app.routers.alarms.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(f"/alarms/{alarm_id}/request-clip", headers=auth_header(token))
        mock_client_cls.assert_not_called()

    assert resp.status_code == 404


async def test_request_clip_nonexistent_alarm_returns_404(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(f"/alarms/{uuid.uuid4()}/request-clip", headers=auth_header(token))
    assert resp.status_code == 404


async def test_driver_cannot_request_clip(client, two_tenants, pool):
    device_id, _ = await _create_gt06_video_device(pool, two_tenants["a"]["tenant_id"])
    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], device_id)
    admin_token = await login(client, two_tenants["a"]["email"])
    driver_email = await _create_driver_login(client, admin_token, two_tenants["a"]["tenant_id"])
    token = await login(client, driver_email)

    with patch("app.routers.alarms.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(f"/alarms/{alarm_id}/request-clip", headers=auth_header(token))
        mock_client_cls.assert_not_called()

    assert resp.status_code == 403


async def test_get_clip_returns_404_when_never_requested(client, two_tenants, pool):
    device_id, _ = await _create_gt06_video_device(pool, two_tenants["a"]["tenant_id"])
    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], device_id)
    token = await login(client, two_tenants["a"]["email"])

    resp = await client.get(f"/alarms/{alarm_id}/clip", headers=auth_header(token))
    assert resp.status_code == 404


async def test_get_clip_reflects_requested_status_after_request(client, two_tenants, pool):
    device_id, _ = await _create_gt06_video_device(pool, two_tenants["a"]["tenant_id"])
    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], device_id)
    token = await login(client, two_tenants["a"]["email"])

    with patch("app.routers.alarms.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = _mock_bridge_response(code=0)
        post_resp = await client.post(f"/alarms/{alarm_id}/request-clip", headers=auth_header(token))

    get_resp = await client.get(f"/alarms/{alarm_id}/clip", headers=auth_header(token))
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == post_resp.json()["id"]
    assert get_resp.json()["status"] == "requested"


async def test_get_clip_cross_tenant_returns_404(client, two_tenants, pool):
    device_id, _ = await _create_gt06_video_device(pool, two_tenants["b"]["tenant_id"])
    alarm_id = await _insert_alarm(pool, two_tenants["b"]["tenant_id"], device_id)
    with patch("app.routers.alarms.httpx.AsyncClient") as mock_client_cls:
        admin_b_token = await login(client, two_tenants["b"]["email"])
        mock_client_cls.return_value.__aenter__.return_value.post = _mock_bridge_response(code=0)
        await client.post(f"/alarms/{alarm_id}/request-clip", headers=auth_header(admin_b_token))

    token_a = await login(client, two_tenants["a"]["email"])
    resp = await client.get(f"/alarms/{alarm_id}/clip", headers=auth_header(token_a))
    assert resp.status_code == 404


async def test_get_clip_marks_stale_upload_as_failed(client, two_tenants, pool):
    """HVIDEO never confirms anything immediately (fire-and-forget, see
    gt06server.SendRawCommandFireAndForget), so a clip is optimistically left
    'uploading' with no guarantee the file will ever arrive -- without this
    "stuck" check, the user sees "uploading clip..." forever, with no way to
    retry (the frontend only offers "Retry" on a 'failed' state)."""
    from datetime import datetime, timedelta, timezone

    from app import db as db_module

    device_id, _ = await _create_gt06_video_device(pool, two_tenants["a"]["tenant_id"])
    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], device_id)
    token = await login(client, two_tenants["a"]["email"])

    with patch("app.routers.alarms.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = _mock_bridge_response(code=0)
        post_resp = await client.post(f"/alarms/{alarm_id}/request-clip", headers=auth_header(token))
    clip_id = post_resp.json()["id"]

    stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "UPDATE alarm_video_clips SET status = 'uploading', requested_at = %s WHERE id = %s",
            (stale_time, clip_id),
        )

    resp = await client.get(f"/alarms/{alarm_id}/clip", headers=auth_header(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error_detail"]

    # A second GET must not blow up trying to mark an already terminal
    # state again (mark_alarm_clip_failed refuses to reopen it) -- confirms
    # the staleness check is not repeated on an already resolved clip.
    resp2 = await client.get(f"/alarms/{alarm_id}/clip", headers=auth_header(token))
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "failed"


async def test_get_clip_recent_upload_not_marked_stale(client, two_tenants, pool):
    """Counterpart of the previous test: a recent request (inside the window)
    must never be marked failed just for being 'uploading'."""
    device_id, _ = await _create_gt06_video_device(pool, two_tenants["a"]["tenant_id"])
    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], device_id)
    token = await login(client, two_tenants["a"]["email"])

    with patch("app.routers.alarms.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = _mock_bridge_response(code=0)
        await client.post(f"/alarms/{alarm_id}/request-clip", headers=auth_header(token))

    resp = await client.get(f"/alarms/{alarm_id}/clip", headers=auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "requested"


async def test_driver_cannot_get_clip(client, two_tenants, pool):
    device_id, _ = await _create_gt06_video_device(pool, two_tenants["a"]["tenant_id"])
    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], device_id)
    admin_token = await login(client, two_tenants["a"]["email"])
    driver_email = await _create_driver_login(client, admin_token, two_tenants["a"]["tenant_id"])
    token = await login(client, driver_email)

    resp = await client.get(f"/alarms/{alarm_id}/clip", headers=auth_header(token))
    assert resp.status_code == 403


async def test_get_clip_404_for_unassigned_device(client, two_tenants, pool):
    """Security finding: alarm_video_clips_select (0040) only isolated by
    tenant_id and never incorporated app_can_view_device(), unlike
    devices_select/alarms_v/device_commands (see test_device_visibility.py).
    A tenant_viewer WITHOUT that device assigned, and an API key scoped to
    ANOTHER device, could still read the clip row (including a signed
    storage URL) of a camera that is not theirs. Same verification as
    test_device_commands_history_404_for_unassigned_device: 404 without the
    assignment, 200 with it -- never over-restrictive."""
    device_id, _ = await _create_gt06_video_device(pool, two_tenants["a"]["tenant_id"])
    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], device_id)
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]

    # The tenant_admin requests the clip -- a real audit row in 'requested'
    # state ('ready' is not needed: the new gate runs before looking at the
    # clip status).
    with patch("app.routers.alarms.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = _mock_bridge_response(code=0)
        req_resp = await client.post(f"/alarms/{alarm_id}/request-clip", headers=auth_header(admin_token))
    assert req_resp.status_code == 202, req_resp.text

    viewer_resp = await client.post(
        "/users",
        json={
            "email": f"viewer-noclip-{uuid.uuid4().hex[:8]}@example.com",
            "password": TEST_PASSWORD,
            "role": "tenant_viewer",
            "tenant_id": str(tenant_id),
        },
        headers=auth_header(admin_token),
    )
    assert viewer_resp.status_code == 201, viewer_resp.text
    viewer_id = viewer_resp.json()["id"]
    viewer_token = await login(client, viewer_resp.json()["email"])

    # Without the assignment: 404, even though the clip exists and belongs
    # to their own tenant.
    resp = await client.get(f"/alarms/{alarm_id}/clip", headers=auth_header(viewer_token))
    assert resp.status_code == 404

    # With the device assigned, they can see it -- confirms the fix is not
    # over-restrictive.
    assign_resp = await client.put(
        f"/users/{viewer_id}/device-assignments",
        json={"device_ids": [str(device_id)]},
        headers=auth_header(admin_token),
    )
    assert assign_resp.status_code == 200, assign_resp.text

    resp2 = await client.get(f"/alarms/{alarm_id}/clip", headers=auth_header(viewer_token))
    assert resp2.status_code == 200
