"""Driving alarms (collision, overspeed, fatigue, etc.) detected by the
protocol servers (e.g. the JT808 0x0200 alarm bits, see
jt808-server/internal/jt808server/location.go).

Retrieving video clips tied to an alarm: requested ON DEMAND, mirroring the
data-saving rule of the whole project. Same pattern as device_commands.py: the
audit row goes in its own short transaction BEFORE talking to the internal Go
server (never in the same transaction as the rest of the request), and
jt808-server never returns the file synchronously -- the device uploads it
separately (POST /upload/{imei} on the jt808-server side) seconds/minutes
later, so this API only TRIGGERS the request and exposes its status via
polling (GET .../clip)."""
from __future__ import annotations

import logging
import uuid
from typing import Literal
from datetime import datetime, timedelta, timezone

import httpx
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from psycopg import AsyncConnection
from psycopg.errors import InsufficientPrivilege

from .. import db as db_module
from ..config import Settings
from ..deps import get_db, get_settings_dep, require_non_driver
from ..schemas import AlarmOut, AlarmVideoClipOut
from ..security import TokenClaims
from ..storage import StorageNotConfigured, generate_signed_url

router = APIRouter(prefix="/alarms", tags=["alarms"])
logger = logging.getLogger(__name__)


# Alarm types for which the device does store video on its SD card (JC261/
# JC400 camera events reported via 0x95, including the panic button). Single
# source of truth on the backend; the frontend mirrors the same list in
# web/src/components/AlarmClipPlayer.tsx.
CLIP_CAPABLE_ALARM_TYPES = frozenset({"gt06_camera_event"})


@router.get("", response_model=list[AlarmOut])
async def list_alarms(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
    unacknowledged_only: bool = Query(False),
    device_id: uuid.UUID | None = Query(None),
    # Minimum severity (ordered enum info < warning < critical). Exists for the
    # map's "unit with alarm" marker: without it, informational alarms
    # (ignition, geofence entry) filled up the `limit` and a real CRITICAL
    # alarm could fall off the list.
    min_severity: Literal["info", "warning", "critical"] | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> list[AlarmOut]:
    # Like positions.py: no manual tenant_id filter, alarms_v (security_barrier,
    # 0009_timeseries_access.sql) already applies it -- the device_id filter is
    # only a convenience (per-device preview/history in the dashboard), never
    # the real isolation barrier.
    where_extra = "AND a.acknowledged_at IS NULL" if unacknowledged_only else ""
    params: list[object] = []
    if device_id is not None:
        where_extra += " AND a.device_id = %s"
        params.append(device_id)
    if min_severity is not None:
        where_extra += " AND a.severity >= %s::alarm_severity"
        params.append(min_severity)
    params.append(limit)
    rows = await (
        await conn.execute(
            f"""SELECT a.id, a.device_id, d.label, a.alarm_type, a.severity, a.time, a.acknowledged_at,
                       a.video_evidence_key IS NOT NULL AS has_video_clip
                FROM alarms_v a
                JOIN devices d ON d.id = a.device_id
                WHERE true {where_extra}
                ORDER BY a.time DESC
                LIMIT %s""",
            params,
        )
    ).fetchall()
    return [
        AlarmOut(
            id=r[0],
            device_id=r[1],
            device_label=r[2],
            alarm_type=r[3],
            severity=r[4],
            time=r[5].isoformat(),
            acknowledged_at=r[6].isoformat() if r[6] else None,
            has_video_clip=r[7],
        )
        for r in rows
    ]


@router.post("/{alarm_id}/acknowledge", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def acknowledge_alarm(
    alarm_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    user: TokenClaims = Depends(require_non_driver),
) -> None:
    # acknowledge_alarm() (SECURITY DEFINER, 0009_timeseries_access.sql)
    # already rejects -- with the same generic error as "does not exist" -- an
    # alarm from another tenant, so no prior SELECT is needed for the 404: the
    # Postgres exception is translated directly.
    try:
        await conn.execute(
            "SELECT acknowledge_alarm(%s, %s)",
            (alarm_id, user.user_id),
        )
    except InsufficientPrivilege:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alarm not found")


async def _mark_clip_failed(pool, clip_id: uuid.UUID, detail: str) -> None:
    # jt808-server (internal/alarmclip::handleRequestClip) ALREADY marks the
    # request failed itself when the GT06 command fails immediately
    # (SendCommand error) -- so this same request can arrive here already in a
    # terminal state (failed/ready/unsupported) set by the Go side.
    # mark_alarm_clip_failed() refuses to reopen a terminal state
    # (enforce_alarm_video_clip_status_transition) with InsufficientPrivilege
    # -- a real race (an unconnected device triggers exactly this). It is not
    # our error, it is the other half of the system already doing its job --
    # ignored instead of letting a raw 500 escape.
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as audit_conn:
        try:
            await audit_conn.execute(
                "SELECT mark_alarm_clip_failed(%s, 'failed', %s)",
                (clip_id, detail),
            )
        except InsufficientPrivilege:
            logger.info("clip %s was already in a terminal state (marked by jt808-server) -- ignored", clip_id)


# CLIP_STALE_AFTER: the device never confirms the request immediately (see
# gt06server.SendRawCommandFireAndForget), so a clip moves to 'uploading'
# OPTIMISTICALLY as soon as the command is sent, with no real guarantee the
# device will upload the file. Without a timeout, a request the device simply
# ignores stays "uploading clip..." forever -- the user has no way to know
# nothing will happen, and no retry button (the frontend only offers "Retry"
# on a 'failed' state). Generous on purpose: vendor documentation already
# describes waits of tens of seconds just to LIST files (FILELIST); uploading
# a real one can take considerably longer.
CLIP_STALE_AFTER = timedelta(minutes=5)


def _sign_clip_key(settings: Settings, clip_id: uuid.UUID, storage_key: str, label: str) -> str | None:
    try:
        return generate_signed_url(settings, storage_key)
    except StorageNotConfigured:
        logger.error("clip %s (%s) ready but storage is not configured in this deployment", clip_id, label)
    except ClientError:
        # The object no longer exists -- most likely the bucket lifecycle rule
        # (retention) removed it. Not a 500: it is a real, expected state, and
        # the frontend should show "this clip is no longer available".
        logger.info("clip %s (%s): object %s no longer exists in storage (retention)", clip_id, label, storage_key)
    return None


async def _clip_out(conn: AsyncConnection, settings: Settings, pool, clip_id: uuid.UUID) -> AlarmVideoClipOut:
    row = await (
        await conn.execute(
            """SELECT id, alarm_id, status, requested_at, completed_at, error_detail, storage_key,
                      storage_key_secondary
               FROM alarm_video_clips WHERE id = %s""",
            (clip_id,),
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "clip request not found")
    clip_id_, alarm_id, clip_status, requested_at, completed_at, error_detail, storage_key, storage_key_secondary = row

    if clip_status in ("requested", "uploading") and datetime.now(timezone.utc) - requested_at > CLIP_STALE_AFTER:
        stale_detail = "the device did not upload the file in time"
        await _mark_clip_failed(pool, clip_id_, stale_detail)
        clip_status, error_detail = "failed", stale_detail

    url: str | None = None
    if clip_status == "ready" and storage_key:
        url = _sign_clip_key(settings, clip_id_, storage_key, "front")
    # storage_key_secondary (cabin) is independent of `status` -- see
    # migration 0044: it can arrive before or after the front clip is already
    # 'ready', best-effort, so it is signed as soon as it exists, without
    # waiting for any particular state.
    url_secondary: str | None = None
    if storage_key_secondary:
        url_secondary = _sign_clip_key(settings, clip_id_, storage_key_secondary, "cabin")
    return AlarmVideoClipOut(
        id=clip_id_,
        alarm_id=alarm_id,
        status=clip_status,
        requested_at=requested_at.isoformat(),
        completed_at=completed_at.isoformat() if completed_at else None,
        error_detail=error_detail,
        url=url,
        url_secondary=url_secondary,
    )


@router.post("/{alarm_id}/request-clip", response_model=AlarmVideoClipOut, status_code=status.HTTP_202_ACCEPTED)
async def request_alarm_clip(
    alarm_id: uuid.UUID,
    request: Request,
    conn: AsyncConnection = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user: TokenClaims = Depends(require_non_driver),
) -> AlarmVideoClipOut:
    # Step 1: does this alarm exist FOR THIS USER? alarms_v (RLS) already
    # resolves ownership -- same pattern as video.py/device_commands.py.
    row = await (
        await conn.execute(
            """SELECT a.tenant_id, a.device_id, a.time, d.protocol, d.gt06_imei, a.alarm_type
               FROM alarms_v a JOIN devices d ON d.id = a.device_id
               WHERE a.id = %s""",
            (alarm_id,),
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alarm not found")
    tenant_id, device_id, alarm_time, protocol, gt06_imei, alarm_type = row
    if protocol != "gt06_video":
        # Only GT06/JC261 clip retrieval is implemented today (JT808 is
        # designed but not implemented) -- clean 400, same as the protocol
        # gate in video.py.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "this device does not support clip retrieval yet")
    if alarm_type not in CLIP_CAPABLE_ALARM_TYPES:
        # The device only records SD video for its camera events (collision,
        # panic/SOS...), reported via 0x95. Ignition, engine cut, geofences or
        # speed never have a file: requesting one always ended in "the device
        # did not upload the file in time".
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "this alarm type does not produce video on the device")

    pool = request.app.state.pool

    # Idempotency: a request already in progress for this alarm does not
    # trigger a new one (avoids spending the device's data twice if the
    # operator double-clicks or refreshes while waiting).
    existing = await (
        await conn.execute(
            """SELECT id FROM alarm_video_clips
               WHERE alarm_id = %s AND status IN ('requested', 'uploading')
               ORDER BY requested_at DESC LIMIT 1""",
            (alarm_id,),
        )
    ).fetchone()
    if existing is not None:
        return await _clip_out(conn, settings, pool, existing[0])

    # Step 2: audit row in its OWN short transaction, BEFORE talking to the Go
    # server -- same rule as device_commands.py (never in the same transaction
    # as the rest of the request).
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as audit_conn:
        clip_row = await (
            await audit_conn.execute(
                """INSERT INTO alarm_video_clips (tenant_id, alarm_id, alarm_time, device_id, protocol, requested_by)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (tenant_id, alarm_id, alarm_time, device_id, protocol, user.user_id),
            )
        ).fetchone()
        clip_id = clip_row[0]

    # Step 3: fire the real request at jt808-server (trusted internal network,
    # same host:port as video.py/device_commands.py) -- async, NEVER waits for
    # the file: the device uploads it separately later, correlated by IMEI
    # (see internal/alarmclip on the Go side). A failure here marks the request
    # failed immediately instead of leaving it stuck in 'requested'.
    # timeout=10.0: jt808-server no longer waits for any synchronous device
    # reply for this command (see gt06server.SendRawCommandFireAndForget: the
    # device never answers on the normal command channel), so waiting here
    # would only reintroduce mismatched timeouts between this client and the
    # Go-side budget. Sending is just a write to an already open socket, so
    # jt808-server replies almost immediately -- this timeout only covers real
    # internal network/connectivity problems.
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.jt1078_bridge_base_url}/api/v1/gt06-alarm-clip",
                json={"clipId": str(clip_id), "imei": gt06_imei, "alarmTime": alarm_time.isoformat()},
            )
        payload = resp.json()
    except (httpx.HTTPError, ValueError):
        logger.exception("failed to reach jt808-server to request clip alarm_id=%s", alarm_id)
        await _mark_clip_failed(pool, clip_id, "could not reach the video service")
    else:
        if payload.get("code") != 0:
            logger.warning(
                "jt808-server responded code=%s msg=%s for clip_id=%s",
                payload.get("code"),
                payload.get("msg"),
                clip_id,
            )
            await _mark_clip_failed(pool, clip_id, "the device could not process the request")

    return await _clip_out(conn, settings, pool, clip_id)


@router.get("/{alarm_id}/clip", response_model=AlarmVideoClipOut)
async def get_alarm_clip(
    alarm_id: uuid.UUID,
    request: Request,
    conn: AsyncConnection = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    _: TokenClaims = Depends(require_non_driver),
) -> AlarmVideoClipOut:
    # alarm_video_clips_select (0040_alarm_video_clips.sql) only isolates by
    # tenant_id -- unlike devices_select/alarms_v (0032), it NEVER incorporated
    # app_can_view_device(). Without the check below, a tenant_viewer with NO
    # device assignment, and an API key scoped explicitly to ANOTHER device,
    # could both obtain the row (and, with storage configured, a real signed
    # URL) of a camera clip that is not theirs. Same check as
    # device_commands.py: SELECT id FROM devices on get_db's RLS-scoped
    # connection (that table DOES have app_can_view_device() in its policy)
    # before trusting the clip row.
    row = await (
        await conn.execute(
            """SELECT id, device_id FROM alarm_video_clips WHERE alarm_id = %s ORDER BY requested_at DESC LIMIT 1""",
            (alarm_id,),
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no clip has been requested for this alarm yet")
    clip_id, device_id = row
    device_row = await (await conn.execute("SELECT id FROM devices WHERE id = %s", (device_id,))).fetchone()
    if device_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no clip has been requested for this alarm yet")
    return await _clip_out(conn, settings, request.app.state.pool, clip_id)
