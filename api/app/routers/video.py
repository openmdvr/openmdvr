"""Request live video from a device. This is the only place in the system
that verifies the user has permission over a device BEFORE talking to the
JT1078 bridge (jt808-server/internal/jt1078bridge) -- traffic between this API
and the bridge is trusted-internal with no auth of its own (docker network),
but PLAYBACK is not open: this API issues a single-use ticket bound to
tenant+device and ZLMediaKit validates it in its on_play hook before serving a
single byte (see handleVideoPlayAuth in the bridge). Never expose the bridge
or ZLMediaKit directly to the browser.

POST /devices/{id}/snapshot also lives here -- a cheap preview photo, same
permission and same "request real video" path as the endpoint above, but the
bridge cuts the stream within seconds instead of leaving it open -- see
jt808-server/internal/videobridge/snapshot.go. Before requesting a real
capture, it always checks the bridge's shared cache first
(POST /api/v1/snapshot-cache) -- if another session/tenant recently requested
a photo of this same device+channel, that one is served without turning the
camera on again (reloading the page or viewing the same camera from another
device must not spend real data twice)."""
from __future__ import annotations

import logging
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from psycopg import AsyncConnection

from ..config import Settings
from ..deps import get_db, get_settings_dep, require_non_driver
from ..schemas import LiveViewBalance, VideoRequest, VideoResponse
from ..security import TokenClaims

router = APIRouter(prefix="/devices", tags=["video"])
logger = logging.getLogger(__name__)


async def _lookup_camera_device(conn: AsyncConnection, device_id: uuid.UUID):
    """Shared by request_video and request_snapshot -- same ownership check
    (RLS via get_db) and same rejection of a pure gt06 device (no camera)."""
    row = await (
        await conn.execute(
            "SELECT d.jt808_terminal_id, d.gt06_imei, d.tenant_id, d.protocol FROM devices d WHERE d.id = %s AND d.status = 'active'",
            (device_id,),
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found or inactive")
    terminal_id, gt06_imei, tenant_id, protocol = row
    if protocol == "gt06":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "this device has no camera (GT06 protocol)")
    return terminal_id, gt06_imei, tenant_id, protocol


@router.post("/{device_id}/video", response_model=VideoResponse)
async def request_video(
    device_id: uuid.UUID,
    body: VideoRequest,
    conn: AsyncConnection = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    _: TokenClaims = Depends(require_non_driver),
) -> VideoResponse:
    # Step 1: does this device exist FOR THIS USER? The query runs on the
    # connection already RLS-scoped by get_db -- another tenant's device does
    # not show up, not even to confirm it exists elsewhere.
    #
    # The service cutoff for a suspended/cancelled tenant does NOT live here:
    # it is enforced by get_db (deps.py, assert_session_active) for EVERY
    # authenticated endpoint, with the same 402 code. GPS/alarm ingestion is
    # deliberately NEVER cut (see 0022_billing_payments.sql).
    terminal_id, gt06_imei, tenant_id, protocol = await _lookup_camera_device(conn, device_id)

    # Step 2: only with ownership confirmed, ask the internal bridge for video
    # (trusted network, never exposed to the browser). jt808 sends 0x9101 over
    # the already open JT1078 connection; gt06_video sends the "start video"
    # text command over the SAME authenticated GT06 TCP connection (same
    # mechanism as engine_stop/engine_resume, see device_commands.py) -- both
    # return the SAME response shape (code/url/expiresInSeconds/
    # liveViewSecondsRemaining), so the rest of this endpoint (error handling,
    # ticket, URL assembly) is protocol-agnostic. Error details (host,
    # connection reason, raw JSON) are logged server-side only, never returned
    # to the client -- the bridge is internal infrastructure and those details
    # are exactly what we do not want to leak by accident.
    # bridge_timeout: gt06_video needs far more headroom than jt808 -- sending
    # the command over GT06 and waiting for the real on_publish confirmation
    # can take tens of seconds. With a shorter client timeout, httpx aborted
    # BEFORE the bridge finished, cancelling its context: the device could
    # still accept and publish, but nobody was waiting for the confirmation
    # and the URL was never returned. 10.0 for jt808 (its own budget is a few
    # seconds, 0x9101 with a 5s ack).
    if protocol == "jt808":
        bridge_path, bridge_body, bridge_timeout = "/api/v1/9101", {"terminalId": terminal_id, "channel": body.channel}, 10.0
    else:
        # gt06_video: the JC261 has TWO independent cameras (front=channel 0,
        # cabin=channel 1, confirmed against real hardware) -- body.channel is
        # meaningful here ("RTMP,ON,INOUT#" starts both and each publishes its
        # own RTMP stream).
        # 38 s: above the bridge's budget (gt06VideoRequestBudget, 30 s, which
        # waits for a slow/busy device instead of failing) and below the
        # page's timeout (40 s).
        bridge_path, bridge_body, bridge_timeout = "/api/v1/gt06-video", {"imei": gt06_imei, "channel": body.channel}, 38.0
    try:
        async with httpx.AsyncClient(timeout=bridge_timeout) as client:
            resp = await client.post(
                f"{settings.jt1078_bridge_base_url}{bridge_path}",
                json=bridge_body,
            )
        payload = resp.json()
    except httpx.HTTPError:
        logger.exception("failed to reach jt1078bridge for device_id=%s", device_id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "could not reach the video service")
    except ValueError:
        logger.exception("non-JSON response from jt1078bridge for device_id=%s", device_id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "invalid response from the video service")

    if payload.get("code") != 0:
        # The bridge formats its `msg` for ITS OWN logs ("jt1078bridge:
        # terminal ... has no active JT808 session") -- it must never reach any
        # client as-is, regardless of role (same principle as the httpx
        # branches above: details only in the server log).
        logger.warning(
            "jt1078bridge responded code=%s msg=%s for device_id=%s",
            payload.get("code"),
            payload.get("msg"),
            device_id,
        )
        # Bridge code 404 = ErrDeviceNotConnected -- the common, expected case
        # ("the camera has no signal right now"), not an internal error; it
        # is translated into a business message. Code 402 =
        # ErrLiveViewQuotaExhausted -- the tenant's MONTHLY quota (distinct
        # from the per-session limit) is used up. Any other code is a real
        # bridge failure and becomes fully generic.
        if payload.get("code") == 404:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the camera does not have an active connection right now")
        if payload.get("code") == 402:
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "the monthly live video limit for this plan has been reached")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "could not start the video")

    # Step 3: issue the single-use playback ticket. The URL the bridge
    # returned is the base (http://<host>/rtp/<terminal>_<channel>.live.flv)
    # WITHOUT a token -- returned as-is, anyone with the terminal_id could
    # watch the video. Here the bridge is asked for a ticket bound to THIS
    # tenant+device+channel (RLS resolved the ownership above) and the final
    # URL is assembled with ?token=... -- ZLMediaKit validates it in its
    # on_play hook before serving a single byte, and a request without a valid
    # token gets 401 without turning the camera on (on_stream_not_found never
    # fires).
    # The ticket's deviceKey/channel must be EXACTLY what on_play will compare
    # against the real stream: for jt808 it is terminal_id+channel (stream
    # "<terminal>_<channel>" under the "rtp" app); for gt06_video it is
    # imei+channel (stream "<channel>/<imei>" -- the JC261 publishes an
    # INDEPENDENT RTMP stream per camera, front=0, cabin=1).
    ticket_device_key = terminal_id if protocol == "jt808" else gt06_imei
    ticket_channel = body.channel
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            ticket_resp = await client.post(
                f"{settings.jt1078_bridge_base_url}/api/v1/video-tickets",
                json={
                    "tenantId": str(tenant_id),
                    "terminalId": ticket_device_key,
                    "channel": ticket_channel,
                    # The bridge maps this to the real ZLMediaKit "app":
                    # jt808_terminal_id and gt06_imei are columns with
                    # INDEPENDENT unique constraints, nothing in the schema
                    # prevents them from matching numerically across two
                    # devices of different tenants -- without this field, a
                    # ticket minted for a jt808 device could authorize another
                    # tenant's gt06_video stream if their identifiers collided
                    # as digits.
                    "protocol": protocol,
                },
            )
        ticket_payload = ticket_resp.json()
    except httpx.HTTPError:
        logger.exception("failed to mint video ticket for device_id=%s", device_id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "could not prepare the video")
    except ValueError:
        logger.exception("non-JSON response minting ticket for device_id=%s", device_id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "invalid response from the video service")

    token = ticket_payload.get("token")
    if ticket_payload.get("code") != 0 or not token:
        logger.warning(
            "ticket minting responded code=%s for device_id=%s",
            ticket_payload.get("code"),
            device_id,
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "could not prepare the video")

    # Assemble the final URLs. The bridge's base FLV URL never has a query
    # string (it is the ZLM_PLAY_URL_FORMAT template with a single %s), so
    # '?' is always used. The WHEP URL ALWAYS has a query string
    # (?app=&stream=), so it is always '&' -- the same defensive computation
    # is used anyway, never assumed blindly.
    sep = "&" if "?" in payload["url"] else "?"
    authed_url = f"{payload['url']}{sep}token={token}"
    webrtc_sep = "&" if "?" in payload.get("webrtcUrl", "") else "?"
    authed_webrtc_url = f"{payload.get('webrtcUrl', '')}{webrtc_sep}token={token}"

    return VideoResponse(
        url=authed_url,
        webrtc_url=authed_webrtc_url,
        expires_in_seconds=payload.get("expiresInSeconds", 0),
        live_view_seconds_remaining=payload.get("liveViewSecondsRemaining", 0),
    )


@router.post("/{device_id}/snapshot")
async def request_snapshot(
    device_id: uuid.UUID,
    body: VideoRequest,
    request: Request,
    conn: AsyncConnection = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user: TokenClaims = Depends(require_non_driver),
) -> Response:
    """Cheap preview photo -- shown by default wherever the app displays a
    camera, instead of forcing a full live view (much more expensive in
    data). Same permission/ownership check as request_video (above) -- no new
    privilege surface. Returns the JPEG bytes DIRECTLY (never persisted in
    storage/Postgres: the photo is ephemeral by design and replaced on the
    client's next refresh)."""
    # Dedicated per-device rate limit -- defense in depth against a frontend
    # bug or deliberate abuse hammering photo requests at a real device (same
    # pattern as route_history_rate_limiter, see main.py). Independent of the
    # MONTHLY video quota (which is still honored below, inherited from the
    # same path live video uses).
    limiter = getattr(request.app.state, "snapshot_rate_limiter", None)
    if limiter is not None and not limiter.allow(str(device_id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many photos requested for this unit, please wait a moment")

    terminal_id, gt06_imei, tenant_id, protocol = await _lookup_camera_device(conn, device_id)
    snapshot_device_key = terminal_id if protocol == "jt808" else gt06_imei

    # Step 0: first ask whether there is ALREADY a recent photo in the
    # bridge's shared cache (see jt808-server/internal/videobridge/
    # snapshot_cache.go) -- reloading the page, or viewing the same device
    # from another tab/session/tenant, must not turn the camera on again if
    # another session did so recently. This call NEVER touches the device --
    # on a miss, the normal path (Step 1 + Step 2) below continues as usual.
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            cache_resp = await client.post(
                f"{settings.jt1078_bridge_base_url}/api/v1/snapshot-cache",
                json={"protocol": protocol, "deviceKey": snapshot_device_key, "channel": body.channel},
            )
        if cache_resp.headers.get("content-type", "").startswith("image/jpeg"):
            return Response(content=cache_resp.content, media_type="image/jpeg")
    except httpx.HTTPError:
        # Never block the photo on this -- a cache lookup failure simply falls
        # through to the normal path (real capture): the cache is an
        # optimization, not a hard dependency.
        logger.warning("failed to query snapshot-cache for device_id=%s, falling back to a real capture", device_id)

    # Step 0.5: the device's NATIVE photo, if its protocol has one (today the
    # JC261/JC400: "Picture,out#"/"Picture,in#" -- the camera takes and
    # uploads the photo itself, a few KB, without opening the video stream or
    # touching ZLMediaKit). The bridge decides whether it applies (code 501 =
    # no native photo for that protocol) -- this API knows no models. The
    # native photo spends no live-video quota; it is recorded as 'download'
    # in usage_events.
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            native_resp = await client.post(
                f"{settings.jt1078_bridge_base_url}/api/v1/snapshot-native",
                json={"tenantId": str(tenant_id), "protocol": protocol, "deviceKey": snapshot_device_key, "channel": body.channel},
            )
        if native_resp.headers.get("content-type", "").startswith("image/jpeg"):
            return Response(content=native_resp.content, media_type="image/jpeg")
        native_code = native_resp.json().get("code")
    except (httpx.HTTPError, ValueError):
        logger.warning("failed to reach the native photo endpoint for device_id=%s", device_id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "could not request the photo")
    if native_code != 501:
        # The device DOES have a native photo but did not deliver it in time.
        # No fallback to video: turning video on for a photo spent data, left
        # the other camera streaming and occupied the device's single command
        # slot, so a subsequent "View live" failed as "busy". If the photo
        # arrives late, the bridge stores it in the cache and the page's next
        # attempt gets it instantly.
        logger.warning("native photo not delivered (code=%s) for device_id=%s", native_code, device_id)
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "the camera has not delivered the photo yet")

    # Step 1: start the real signaling -- EXACTLY the same path as requesting
    # live video (request_video above, same bridge endpoint, same per-protocol
    # timeout, same error translation) -- never a new command. The real
    # difference is on the bridge side: POST /api/v1/snapshot (below) cuts the
    # stream within seconds instead of leaving it open like a normal live
    # session.
    if protocol == "jt808":
        bridge_path, bridge_body, bridge_timeout = "/api/v1/9101", {"terminalId": terminal_id, "channel": body.channel}, 10.0
    else:
        # purpose=snapshot: the bridge marks this start as "photo only" (Auto
        # entry) -- it never consumes a real viewer's clock, and the immediate
        # cut after the capture is skipped if someone claimed the stream to
        # watch it live (otherwise the photo would cut the same camera's live
        # video, see videobridge/snapshot.go).
        bridge_path, bridge_body, bridge_timeout = (
            "/api/v1/gt06-video",
            {"imei": gt06_imei, "channel": body.channel, "purpose": "snapshot"},
            30.0,
        )
    try:
        async with httpx.AsyncClient(timeout=bridge_timeout) as client:
            resp = await client.post(f"{settings.jt1078_bridge_base_url}{bridge_path}", json=bridge_body)
        payload = resp.json()
    except httpx.HTTPError:
        logger.exception("failed to reach jt1078bridge (snapshot, start) for device_id=%s", device_id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "could not reach the video service")
    except ValueError:
        logger.exception("non-JSON response from jt1078bridge (snapshot, start) for device_id=%s", device_id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "invalid response from the video service")

    if payload.get("code") != 0:
        logger.warning(
            "jt1078bridge (snapshot, start) responded code=%s msg=%s for device_id=%s",
            payload.get("code"), payload.get("msg"), device_id,
        )
        if payload.get("code") == 404:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the camera does not have an active connection right now")
        if payload.get("code") == 402:
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "the monthly live video limit for this plan has been reached")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "could not start the capture")

    # Step 2: capture ONE frame and cut immediately -- all the real
    # orchestration (wait for on_publish, mint its own internal ticket, ask
    # ZLMediaKit for the frame, cut) lives in the bridge (see
    # jt808-server/internal/videobridge/snapshot.go); this API only triggers
    # it and translates the response. snapshot_device_key was computed above
    # (Step 0) and reused as-is.
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            snap_resp = await client.post(
                f"{settings.jt1078_bridge_base_url}/api/v1/snapshot",
                json={
                    "tenantId": str(tenant_id),
                    "protocol": protocol,
                    "deviceKey": snapshot_device_key,
                    "channel": body.channel,
                },
            )
    except httpx.HTTPError:
        logger.exception("failed to reach jt1078bridge (snapshot, capture) for device_id=%s", device_id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "could not reach the video service")

    # The bridge responds WITH the JPEG bytes directly (Content-Type
    # image/jpeg) if the capture succeeded, or a JSON {code,msg} if not --
    # never both, so the response's actual Content-Type is the only signal
    # needed.
    content_type = snap_resp.headers.get("content-type", "")
    if content_type.startswith("image/jpeg"):
        return Response(content=snap_resp.content, media_type="image/jpeg")

    try:
        snap_payload = snap_resp.json()
    except ValueError:
        logger.exception("non-JSON response from jt1078bridge (snapshot, capture) for device_id=%s", device_id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "invalid response from the video service")

    logger.warning(
        "jt1078bridge (snapshot, capture) responded code=%s msg=%s for device_id=%s",
        snap_payload.get("code"), snap_payload.get("msg"), device_id,
    )
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "could not capture the image")


@router.get("/{device_id}/live-view-balance", response_model=LiveViewBalance)
async def live_view_balance(
    device_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    _: TokenClaims = Depends(require_non_driver),
) -> LiveViewBalance:
    """REAL live-video balance of the tenant owning this unit and how many
    cameras it has open right now (with two cameras open the balance drops
    twice as fast). The number comes from the bridge's central meter
    (jt808-server/internal/videobridge/meter.go), which knows the open
    sessions of ALL the tenant's users. Same permission as requesting video:
    the unit must be visible through RLS."""
    _, _, tenant_id, _ = await _lookup_camera_device(conn, device_id)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{settings.jt1078_bridge_base_url}/api/v1/live-balance",
                json={"tenantId": str(tenant_id)},
            )
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("live video balance: the bridge did not respond: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "could not query the video balance")
    if payload.get("code") != 0:
        logger.warning("live video balance: bridge response %s", payload)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "could not query the video balance")
    return LiveViewBalance(
        tenant_id=tenant_id,
        remaining_seconds=int(payload.get("remainingSeconds", 0)),
        active_sessions=int(payload.get("activeSessions", 0)),
    )
