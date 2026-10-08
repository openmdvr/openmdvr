"""The video endpoint is where the API verifies, BEFORE talking to the
internal video bridge (which deliberately has no authentication of its own
between the API and the bridge -- see jt808-server/internal/videobridge),
that the user has permission over the requested device. The IDOR test here
is the most important one in this module.

On the happy path this endpoint makes TWO calls to the bridge: POST
/api/v1/9101 (request the video) and, only if that succeeded, POST
/api/v1/video-tickets (issue the single-use playback ticket ZLMediaKit
validates in its on_play hook). The URL returned to the client ALWAYS
carries ?token=... -- a URL without a token is rejected by ZLMediaKit with
401 without waking the camera."""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from conftest import login, auth_header

pytestmark = pytest.mark.asyncio


async def test_cannot_request_video_for_other_tenant_device(client, two_tenants):
    """It must not even call the JT1078 bridge -- the 404 happens on the
    RLS-filtered devices query, before any outgoing HTTP call."""
    token = await login(client, two_tenants["a"]["email"])
    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(
            f"/devices/{two_tenants['b']['device_id']}/video",
            json={"channel": 1},
            headers=auth_header(token),
        )
        assert resp.status_code == 404
        mock_client_cls.assert_not_called()


async def test_request_video_for_own_device_calls_bridge_and_returns_url(client, two_tenants):
    """Full happy path: 9101 (request the video) + video-tickets (issue the
    ticket). The URL returned to the client carries the ?token=... already
    assembled -- the frontend uses it as-is, without building anything."""
    token = await login(client, two_tenants["a"]["email"])

    mock_9101 = AsyncMock()
    mock_9101.json = lambda: {
        "code": 0,
        "url": "http://zlm/rtp/x.live.flv",
        "webrtcUrl": "http://zlm/index/api/whep?app=rtp&stream=x",
        "expiresInSeconds": 60,
        "liveViewSecondsRemaining": 17940,
    }
    mock_ticket = AsyncMock()
    mock_ticket.json = lambda: {"code": 0, "token": "abc123token"}
    mock_post = AsyncMock(side_effect=[mock_9101, mock_ticket])

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/video",
            json={"channel": 1},
            headers=auth_header(token),
        )

    assert resp.status_code == 200
    assert resp.json()["url"] == "http://zlm/rtp/x.live.flv?token=abc123token"
    assert resp.json()["webrtc_url"] == "http://zlm/index/api/whep?app=rtp&stream=x&token=abc123token"
    assert resp.json()["expires_in_seconds"] == 60
    assert resp.json()["live_view_seconds_remaining"] == 17940
    assert mock_post.call_count == 2
    # First call: request the video from the bridge.
    assert mock_post.call_args_list[0].kwargs["json"]["channel"] == 1
    # Second call: mint the ticket with the device's tenant_id (from the RLS
    # query, not the JWT -- this is where the ticket is bound to the
    # authorized device) and the SAME terminal_id/channel as the 9101.
    ticket_body = mock_post.call_args_list[1].kwargs["json"]
    assert ticket_body["tenantId"] == str(two_tenants["a"]["tenant_id"])
    assert ticket_body["channel"] == 1
    assert ticket_body["terminalId"]


async def test_request_video_ticket_mint_failure_returns_502_without_url(client, two_tenants):
    """If the 9101 succeeded but minting the ticket fails, the raw URL without
    a token is NOT returned (it would be useless anyway -- ZLMediaKit would
    reject it with 401 in on_play -- but returning it would expose the
    guessable stream_id with no barrier). A clean 502 with no leak."""
    token = await login(client, two_tenants["a"]["email"])

    mock_9101 = AsyncMock()
    mock_9101.json = lambda: {
        "code": 0,
        "url": "http://zlm/rtp/x.live.flv",
        "expiresInSeconds": 60,
    }
    mock_post = AsyncMock(side_effect=[mock_9101, httpx.ConnectError("[Errno 111] Connection refused to 172.28.0.9:8082")])

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/video",
            json={"channel": 1},
            headers=auth_header(token),
        )

    assert resp.status_code == 502
    assert "url" not in resp.json()
    assert "172.28.0.9" not in resp.text


async def test_request_video_for_gt06_device_returns_400(client, two_tenants, platform_users):
    """A GPS-only GT06 tracker has no camera -- a clean 400, without calling
    the JT1078 bridge (which has nothing to offer for a terminal_id that
    never existed; jt808_terminal_id is NULL for this device)."""
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
        json={"tenant_id": tenant_id, "protocol": "gt06", "gt06_imei": "111111111111111", "label": "GPS only"},
        headers=auth_header(admin_token),
    )
    assert device.status_code == 201, device.text

    token = await login(client, two_tenants["a"]["email"])
    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(
            f"/devices/{device.json()['id']}/video",
            json={"channel": 1},
            headers=auth_header(token),
        )
        assert resp.status_code == 400
        mock_client_cls.assert_not_called()


async def test_request_video_for_gt06_video_device_calls_gt06_video_endpoint(client, two_tenants, pool):
    """JC261/JC400 (gt06_video): unlike jt808 (0x9101 over the JT1078
    connection) and unlike plain gt06 (rejected with 400 above), this one DOES
    have a camera -- the correct bridge_path is /api/v1/gt06-video with
    imei+channel in the body. The channel is REAL (the JC261 has two
    independent cameras, front=0/cabin=1, each with its own RTMP stream) --
    the ticket is minted with the IMEI as terminalId and the SAME requested
    channel, not a fixed 0."""
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute(
                """INSERT INTO devices (tenant_id, protocol, gt06_imei, label, status)
                   VALUES (%s, 'gt06_video', '490154203237518', 'jc261', 'active') RETURNING id""",
                (two_tenants["a"]["tenant_id"],),
            )
        ).fetchone()
        device_id = row[0]

    token = await login(client, two_tenants["a"]["email"])

    mock_gt06_video = AsyncMock()
    mock_gt06_video.json = lambda: {
        "code": 0,
        "url": "http://zlm/live/xyz.live.flv",
        "webrtcUrl": "http://zlm/index/api/whep?app=live&stream=xyz",
        "expiresInSeconds": 60,
        "liveViewSecondsRemaining": 100,
    }
    mock_ticket = AsyncMock()
    mock_ticket.json = lambda: {"code": 0, "token": "gt06token"}
    mock_post = AsyncMock(side_effect=[mock_gt06_video, mock_ticket])

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{device_id}/video",
            json={"channel": 1},
            headers=auth_header(token),
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["url"] == "http://zlm/live/xyz.live.flv?token=gt06token"
    assert resp.json()["webrtc_url"] == "http://zlm/index/api/whep?app=live&stream=xyz&token=gt06token"

    first_call = mock_post.call_args_list[0]
    assert first_call.args[0].endswith("/api/v1/gt06-video")
    assert first_call.kwargs["json"] == {"imei": "490154203237518", "channel": 1}

    ticket_body = mock_post.call_args_list[1].kwargs["json"]
    assert ticket_body["terminalId"] == "490154203237518"
    assert ticket_body["channel"] == 1

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("DELETE FROM devices WHERE id = %s", (device_id,))


async def test_request_video_for_nonexistent_device_returns_404(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/devices/00000000-0000-0000-0000-000000000000/video",
        json={"channel": 1},
        headers=auth_header(token),
    )
    assert resp.status_code == 404


async def test_request_video_bridge_unreachable_does_not_leak_exception_detail(client, two_tenants):
    """The 502 must not leak the raw text of the httpx exception (host, port,
    connection reason) -- only a generic message to the client."""
    token = await login(client, two_tenants["a"]["email"])
    mock_post = AsyncMock(side_effect=httpx.ConnectError("[Errno 111] Connection refused to 172.28.0.9:8082"))

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/video",
            json={"channel": 1},
            headers=auth_header(token),
        )

    assert resp.status_code == 502
    assert "172.28.0.9" not in resp.text
    assert "8082" not in resp.text


async def test_request_video_bridge_device_offline_does_not_leak_bridge_detail(client, two_tenants):
    """The bridge formats its `msg` for ITS OWN logs ("jt1078bridge: terminal
    ... has no active JT808 session") -- it must not reach any client as-is,
    regardless of role. code=404 is translated into a useful business
    message, but without internal jargon."""
    token = await login(client, two_tenants["a"]["email"])

    mock_response = AsyncMock()
    mock_response.json = lambda: {
        "code": 404,
        "msg": "jt1078bridge: terminal 13800000099 has no active JT808 session",
    }
    mock_post = AsyncMock(return_value=mock_response)

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/video",
            json={"channel": 1},
            headers=auth_header(token),
        )
    assert resp.status_code == 503
    assert "jt1078bridge" not in resp.text
    assert "JT808" not in resp.text


async def test_request_video_bridge_quota_exhausted_returns_clean_402(client, two_tenants):
    """code=402 from the bridge = ErrLiveViewQuotaExhausted (the tenant's
    MONTHLY quota is exhausted, distinct from the per-session limit) --
    translated into a clean business message, without internal jargon."""
    token = await login(client, two_tenants["a"]["email"])

    mock_response = AsyncMock()
    mock_response.json = lambda: {
        "code": 402,
        "msg": "jt1078bridge: terminal 13800000099: monthly live video quota exhausted",
    }
    mock_post = AsyncMock(return_value=mock_response)

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/video",
            json={"channel": 1},
            headers=auth_header(token),
        )
    assert resp.status_code == 402
    assert "jt1078bridge" not in resp.text
    assert "JT808" not in resp.text


async def test_request_video_for_suspended_tenant_returns_402(client, two_tenants, pool):
    """Service cut (billing, 0022_billing_payments.sql): login already blocked
    a suspended tenant, but a JWT issued BEFORE the suspension (valid for up
    to 8h) could keep requesting live video with nothing stopping it. It must
    not even call the JT1078 bridge."""
    from app import db as db_module

    token = await login(client, two_tenants["a"]["email"])  # log in while the tenant is still active

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", (two_tenants["a"]["tenant_id"],))

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/video",
            json={"channel": 1},
            headers=auth_header(token),
        )
        assert resp.status_code == 402
        mock_client_cls.assert_not_called()


async def test_request_video_for_cancelled_tenant_returns_402(client, two_tenants, pool):
    """Security finding: the original check only compared against
    tenant_status == 'suspended', letting a 'cancelled' tenant (the third,
    more severe enum value, see 0004_tenants.sql) through unblocked -- fixed
    to != 'active', the same rule auth.py uses to block login."""
    from app import db as db_module

    token = await login(client, two_tenants["a"]["email"])

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("UPDATE tenants SET status = 'cancelled' WHERE id = %s", (two_tenants["a"]["tenant_id"],))

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/video",
            json={"channel": 1},
            headers=auth_header(token),
        )
        assert resp.status_code == 402
        mock_client_cls.assert_not_called()


async def test_request_video_bridge_internal_error_returns_generic_message(client, two_tenants):
    """Any bridge error code OTHER than 404 (device offline) is a real bridge
    failure -- it becomes fully generic, without forwarding its internal
    `msg`."""
    token = await login(client, two_tenants["a"]["email"])

    mock_response = AsyncMock()
    mock_response.json = lambda: {
        "code": 500,
        "msg": "jt1078bridge: opening RTP receiver in ZLMediaKit: dial tcp 172.28.0.5:8083: connection refused",
    }
    mock_post = AsyncMock(return_value=mock_response)

    with patch("app.routers.video.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value.post = mock_post
        resp = await client.post(
            f"/devices/{two_tenants['a']['device_id']}/video",
            json={"channel": 1},
            headers=auth_header(token),
        )
    assert resp.status_code == 503
    assert "jt1078bridge" not in resp.text
    assert "172.28.0.5" not in resp.text
