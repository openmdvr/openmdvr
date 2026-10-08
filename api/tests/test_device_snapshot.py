"""POST /devices/{id}/snapshot -- cheap preview snapshot shown before
"View live". Same permission and same first step (starting the real
signaling) as POST /devices/{id}/video (see test_video.py, which covers
that part in depth) -- this file focuses on the endpoint's own second step
(capturing and translating the bridge response), the dedicated rate limit,
and that neither bridge failure mode (start vs. capture) leaks internal
detail."""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.rate_limit import FixedWindowRateLimiter
from conftest import auth_header, login

pytestmark = pytest.mark.asyncio


def _mock_bridge_ok():
    m = AsyncMock()
    m.json = lambda: {"code": 0, "url": "http://zlm/rtp/x.live.flv", "expiresInSeconds": 60}
    return m


def _mock_snapshot_jpeg(body: bytes = b"\xff\xd8\xff fake jpeg bytes"):
    m = AsyncMock()
    m.headers = {"content-type": "image/jpeg"}
    m.content = body
    return m


def _mock_snapshot_error(code: int, msg: str):
    m = AsyncMock()
    m.headers = {"content-type": "application/json"}
    m.json = lambda: {"code": code, "msg": msg}
    return m


def _mock_native_unsupported():
    """Response of POST /api/v1/snapshot-native when the protocol has no
    native photo (code 501) -- the API continues with the generic path."""
    m = AsyncMock()
    m.headers = {"content-type": "application/json"}
    m.json = lambda: {"code": 501, "msg": "no native photo for this protocol"}
    return m


def _mock_snapshot_cache_miss():
    """Response of Step 0 (POST /api/v1/snapshot-cache) when there is no recent
    photo -- the SAME "generic miss" shape as the rest of the bridge (code != 0,
    never an HTTP error status), see snapshot.go::handleSnapshotCache."""
    m = AsyncMock()
    m.headers = {"content-type": "application/json"}
    m.json = lambda: {"code": 404, "msg": "no recent photo in cache"}
    return m


async def test_cannot_request_snapshot_for_other_tenant_device(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(
            f"/devices/{two_tenants['b']['device_id']}/snapshot",
            json={"channel": 1},
            headers=auth_header(token),
        )
        assert resp.status_code == 404
        mock_client_cls.assert_not_called()


async def test_request_snapshot_happy_path_returns_jpeg_bytes(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    jpeg_bytes = b"\xff\xd8\xff real jpeg bytes here"
    mock_post = AsyncMock(side_effect=[_mock_snapshot_cache_miss(), _mock_native_unsupported(), _mock_bridge_ok(), _mock_snapshot_jpeg(jpeg_bytes)])

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/snapshot",
            json={"channel": 1},
            headers=auth_header(token),
        )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == jpeg_bytes
    assert mock_post.call_count == 4
    # First call: Step 0, ask the shared cache -- a miss here, so the normal
    # path continues.
    assert mock_post.call_args_list[0].args[0].endswith("/api/v1/snapshot-cache")
    # Second call: start the real signaling, the SAME path as live video.
    assert mock_post.call_args_list[2].args[0].endswith("/api/v1/9101")
    # Third call: the bridge capture endpoint, with the device's real
    # tenant_id (from the RLS query) and the same requested channel.
    snap_call = mock_post.call_args_list[3]
    assert snap_call.args[0].endswith("/api/v1/snapshot")
    assert snap_call.kwargs["json"]["protocol"] == "jt808"
    assert snap_call.kwargs["json"]["tenantId"] == str(two_tenants["a"]["tenant_id"])
    assert snap_call.kwargs["json"]["channel"] == 1


async def test_request_snapshot_cache_hit_returns_bytes_without_starting_or_capturing(client, two_tenants):
    """Reloading the page or viewing the same camera from another session must
    not wake the device again -- a Step 0 hit must short-circuit the flow
    right there, WITHOUT ever calling the start endpoint (/api/v1/9101 or
    /api/v1/gt06-video) or the real capture."""
    token = await login(client, two_tenants["a"]["email"])
    jpeg_bytes = b"\xff\xd8\xff cached jpeg bytes"
    mock_post = AsyncMock(return_value=_mock_snapshot_jpeg(jpeg_bytes))

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/snapshot",
            json={"channel": 1},
            headers=auth_header(token),
        )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == jpeg_bytes
    assert mock_post.call_count == 1
    assert mock_post.call_args_list[0].args[0].endswith("/api/v1/snapshot-cache")


async def test_request_snapshot_cache_peek_failure_falls_through_to_real_capture(client, two_tenants):
    """The cache is an optimization, never a hard dependency -- if querying it
    fails (bridge down, timeout), the photo is still requested through the
    normal path instead of failing the whole request."""
    token = await login(client, two_tenants["a"]["email"])
    jpeg_bytes = b"\xff\xd8\xff real jpeg bytes here"
    mock_post = AsyncMock(
        side_effect=[httpx.ConnectError("connection refused"), _mock_native_unsupported(), _mock_bridge_ok(), _mock_snapshot_jpeg(jpeg_bytes)]
    )

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/snapshot",
            json={"channel": 1},
            headers=auth_header(token),
        )

    assert resp.status_code == 200, resp.text
    assert resp.content == jpeg_bytes
    assert mock_post.call_count == 4


async def test_request_snapshot_for_gt06_pure_device_returns_400(client, two_tenants, platform_users):
    admin_token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    quota = await client.post(
        "/billing/subscription-items",
        json={"tenant_id": tenant_id, "custom_description": "GPS", "category": "gps", "quantity": 1},
        headers=auth_header(admin_token),
    )
    assert quota.status_code == 201, quota.text
    device = await client.post(
        "/devices",
        json={"tenant_id": tenant_id, "protocol": "gt06", "gt06_imei": "111111111111112", "label": "GPS only"},
        headers=auth_header(admin_token),
    )
    assert device.status_code == 201, device.text

    token = await login(client, two_tenants["a"]["email"])
    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(
            f"/devices/{device.json()['id']}/snapshot",
            json={"channel": 1},
            headers=auth_header(token),
        )
        assert resp.status_code == 400
        mock_client_cls.assert_not_called()


async def test_request_snapshot_for_gt06_video_device_uses_correct_bridge_paths(client, two_tenants, pool):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute(
                """INSERT INTO devices (tenant_id, protocol, gt06_imei, label, status)
                   VALUES (%s, 'gt06_video', '359000000000001', 'jc261', 'active') RETURNING id""",
                (two_tenants["a"]["tenant_id"],),
            )
        ).fetchone()
        device_id = row[0]

    try:
        token = await login(client, two_tenants["a"]["email"])
        mock_post = AsyncMock(side_effect=[_mock_snapshot_cache_miss(), _mock_native_unsupported(), _mock_bridge_ok(), _mock_snapshot_jpeg()])

        with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value.post = mock_post
            resp = await client.post(
                f"/devices/{device_id}/snapshot",
                json={"channel": 0},
                headers=auth_header(token),
            )

        assert resp.status_code == 200, resp.text
        cache_call = mock_post.call_args_list[0]
        assert cache_call.args[0].endswith("/api/v1/snapshot-cache")
        assert cache_call.kwargs["json"] == {"protocol": "gt06_video", "deviceKey": "359000000000001", "channel": 0}
        first_call = mock_post.call_args_list[2]
        assert first_call.args[0].endswith("/api/v1/gt06-video")
        assert first_call.kwargs["json"] == {"imei": "359000000000001", "channel": 0, "purpose": "snapshot"}
        snap_call = mock_post.call_args_list[3]
        assert snap_call.kwargs["json"]["deviceKey"] == "359000000000001"
        assert snap_call.kwargs["json"]["protocol"] == "gt06_video"
    finally:
        async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
            await conn.execute("DELETE FROM devices WHERE id = %s", (device_id,))


async def test_request_snapshot_start_device_offline_returns_503_no_capture_call(client, two_tenants):
    """code=404 from the bridge in STEP 1 (start) = device offline -- it must
    never reach the capture step."""
    token = await login(client, two_tenants["a"]["email"])
    mock_response = AsyncMock()
    mock_response.json = lambda: {"code": 404, "msg": "jt1078bridge: terminal x has no active JT808 session"}
    mock_post = AsyncMock(side_effect=[_mock_snapshot_cache_miss(), _mock_native_unsupported(), mock_response])

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/snapshot",
            json={"channel": 1},
            headers=auth_header(token),
        )
    assert resp.status_code == 503
    assert "jt1078bridge" not in resp.text
    assert mock_post.call_count == 3


async def test_request_snapshot_start_quota_exhausted_returns_402(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    mock_response = AsyncMock()
    mock_response.json = lambda: {"code": 402, "msg": "jt1078bridge: monthly live video quota exhausted"}
    mock_post = AsyncMock(side_effect=[_mock_snapshot_cache_miss(), _mock_native_unsupported(), mock_response])

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/snapshot",
            json={"channel": 1},
            headers=auth_header(token),
        )
    assert resp.status_code == 402
    assert "jt1078bridge" not in resp.text


async def test_request_snapshot_capture_step_failure_returns_503_no_leak(client, two_tenants):
    """STEP 1 (start) succeeded, but the bridge could not capture a real frame
    (e.g. it fell back to the placeholder image, see snapshot.go) -- a generic
    503, never the bridge's internal detail."""
    token = await login(client, two_tenants["a"]["email"])
    mock_post = AsyncMock(
        side_effect=[
            _mock_snapshot_cache_miss(),
            _mock_native_unsupported(),
            _mock_bridge_ok(),
            _mock_snapshot_error(502, "could not capture a real image"),
        ]
    )

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/snapshot",
            json={"channel": 1},
            headers=auth_header(token),
        )
    assert resp.status_code == 503
    assert "jt1078bridge" not in resp.text


async def test_request_snapshot_bridge_unreachable_does_not_leak_exception_detail(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    mock_post = AsyncMock(side_effect=httpx.ConnectError("[Errno 111] Connection refused to 172.28.0.9:8082"))

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/snapshot",
            json={"channel": 1},
            headers=auth_header(token),
        )
    assert resp.status_code == 502
    assert "172.28.0.9" not in resp.text


async def test_request_snapshot_for_nonexistent_device_returns_404(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/devices/00000000-0000-0000-0000-000000000000/snapshot",
        json={"channel": 1},
        headers=auth_header(token),
    )
    assert resp.status_code == 404


async def test_request_snapshot_for_suspended_tenant_returns_402(client, two_tenants, pool):
    from app import db as db_module

    token = await login(client, two_tenants["a"]["email"])
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", (two_tenants["a"]["tenant_id"],))

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/snapshot",
            json={"channel": 1},
            headers=auth_header(token),
        )
        assert resp.status_code == 402
        mock_client_cls.assert_not_called()


async def test_request_snapshot_rate_limited_returns_429_never_500(client, two_tenants):
    from app.main import app as fastapi_app

    token = await login(client, two_tenants["a"]["email"])
    original_limiter = fastapi_app.state.snapshot_rate_limiter
    fastapi_app.state.snapshot_rate_limiter = FixedWindowRateLimiter(max_requests=1, window_seconds=60.0)
    try:
        mock_post = AsyncMock(side_effect=[_mock_snapshot_cache_miss(), _mock_native_unsupported(), _mock_bridge_ok(), _mock_snapshot_jpeg()])
        with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value.post = mock_post
            first = await client.post(
                f"/devices/{two_tenants['a']['device_id']}/snapshot",
                json={"channel": 1},
                headers=auth_header(token),
            )
            assert first.status_code == 200, first.text

            second = await client.post(
                f"/devices/{two_tenants['a']['device_id']}/snapshot",
                json={"channel": 1},
                headers=auth_header(token),
            )
        assert second.status_code == 429
    finally:
        fastapi_app.state.snapshot_rate_limiter = original_limiter


async def test_native_snapshot_returned_without_starting_video(client, two_tenants):
    """If the bridge delivers the native photo (JC261: Picture,out#), the API
    returns it as-is and NEVER starts the video stream."""
    token = await login(client, two_tenants["a"]["email"])
    native_jpeg = b"\xff\xd8\xff native photo"
    mock_post = AsyncMock(side_effect=[_mock_snapshot_cache_miss(), _mock_snapshot_jpeg(native_jpeg)])
    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/snapshot",
            json={"channel": 0},
            headers=auth_header(token),
        )
    assert resp.status_code == 200, resp.text
    assert resp.content == native_jpeg
    assert mock_post.call_count == 2
    assert mock_post.call_args_list[1].args[0].endswith("/api/v1/snapshot-native")
    assert mock_post.call_args_list[1].kwargs["json"]["channel"] == 0


async def test_native_snapshot_failure_does_not_start_video(client, two_tenants):
    """The camera supports native photos but did not upload in time (code
    504): video is NOT started as a fallback (it wasted data, left the other
    camera streaming and occupied the command slot). A 504 is returned and, if
    the photo arrives late, the bridge caches it for the next attempt."""
    token = await login(client, two_tenants["a"]["email"])
    mock_post = AsyncMock(side_effect=[_mock_snapshot_cache_miss(), _mock_snapshot_error(504, "the camera did not deliver the photo")])
    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/snapshot",
            json={"channel": 1},
            headers=auth_header(token),
        )
    assert resp.status_code == 504, resp.text
    assert mock_post.call_count == 2  # cache + native photo; never gt06-video/9101
