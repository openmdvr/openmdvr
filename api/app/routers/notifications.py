"""In-app notification mailbox -- GET /notifications (paginated, unread
first), mark as read, and the real-time SSE stream. Structural copy of
positions.py for the stream: same single-use ticket mechanism (EventSource
cannot send the Authorization header) and the same periodic session
revalidation."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from psycopg import AsyncConnection

from ..deps import assert_session_active, get_db, is_session_active, require_non_driver
from ..notifications import NotificationBroadcaster
from ..schemas import NotificationListOut, NotificationOut, NotificationStreamTicket
from ..security import TokenClaims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])

_KEEPALIVE_SECONDS = 22
_DISCONNECT_POLL_SECONDS = 1.0

_SELECT_COLUMNS = """id, event_type, device_id, alarm_id, title, body, severity, created_at, read_at"""


def _notification_out(row) -> NotificationOut:
    return NotificationOut(
        id=row[0], event_type=row[1], device_id=row[2], alarm_id=row[3], title=row[4], body=row[5],
        severity=row[6], created_at=row[7].isoformat(), read_at=row[8].isoformat() if row[8] else None,
    )


@router.get("", response_model=NotificationListOut)
async def list_notifications(
    conn: AsyncConnection = Depends(get_db),
    user: TokenClaims = Depends(require_non_driver),
    unread_only: bool = Query(False),
    device_id: uuid.UUID | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> NotificationListOut:
    # EXPLICIT filter on recipient_user_id, not just RLS: notifications_select
    # includes `app_bypass_rls() OR ...`, so a platform session
    # (super_admin/support) would otherwise see the mailbox of EVERY user of
    # EVERY tenant through this endpoint -- "my notifications" must mean
    # exactly that for any role, RLS bypass included. Never rely on a single
    # layer for an isolation barrier.
    # in_app_enabled: if the admin turned off the in-app channel for this
    # user, the row still exists (the email worker needs it for its
    # email_status queue) but must not appear in their in-app mailbox.
    where_parts = ["recipient_user_id = %s", "in_app_enabled"]
    params: list[object] = [user.user_id]
    if unread_only:
        where_parts.append("read_at IS NULL")
    # device_id: convenience filter (per-device preview/history in the device
    # detail panel) -- recipient_user_id above is the real barrier, this never
    # widens what this session can see, only narrows it.
    if device_id is not None:
        where_parts.append("device_id = %s")
        params.append(device_id)
    where = "WHERE " + " AND ".join(where_parts)

    total_row = await (await conn.execute(f"SELECT count(*) FROM notifications {where}", params)).fetchone()
    total = total_row[0] if total_row else 0
    unread_row = await (
        await conn.execute(
            "SELECT count(*) FROM notifications WHERE recipient_user_id = %s AND in_app_enabled AND read_at IS NULL",
            (user.user_id,),
        )
    ).fetchone()
    unread_count = unread_row[0] if unread_row else 0

    rows = await (
        await conn.execute(
            f"""SELECT {_SELECT_COLUMNS} FROM notifications {where}
                ORDER BY read_at IS NULL DESC, created_at DESC
                LIMIT %s OFFSET %s""",
            [*params, limit, offset],
        )
    ).fetchall()
    return NotificationListOut(
        items=[_notification_out(r) for r in rows], total=total, unread_count=unread_count, limit=limit, offset=offset
    )


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_notification_read(
    notification_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    user: TokenClaims = Depends(require_non_driver),
) -> NotificationOut:
    # RLS (notifications_update) already prevents touching another user's
    # mailbox, but the WHERE requires it EXPLICITLY too: notifications_update
    # includes `app_bypass_rls() OR ...`, so without this filter a platform
    # session could mark ANY user's notification as read with just
    # `WHERE id = %s` -- tampering with someone else's alert channel with no
    # record of who did it. 0 rows affected if the id is not ours (generic
    # 404, neither confirm nor deny). COALESCE(read_at, now()): idempotent, a
    # second POST does not overwrite the real first-read timestamp.
    row = await (
        await conn.execute(
            f"""UPDATE notifications SET read_at = COALESCE(read_at, now())
                WHERE id = %s AND recipient_user_id = %s
                RETURNING {_SELECT_COLUMNS}""",
            (notification_id, user.user_id),
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found")

    # One button does both: reading an alarm notification also acknowledges
    # the underlying alarm (a tenant-wide action that affects every other
    # recipient's badge) -- the notifications page is the single surface for
    # alarms. acknowledge_alarm() already requires this session to be able to
    # SEE the alarm's device (0032), which is almost always true for a real
    # recipient, but "almost always" is not "always" -- e.g. an assignment
    # change between when the notification was generated and when it is read.
    # Wrapped in its own SAVEPOINT: if acknowledging fails, the notification
    # still ends up marked read (never the other way around -- a secondary
    # problem must never block the primary action).
    alarm_id = row[3]
    if alarm_id is not None:
        try:
            async with conn.transaction():
                await conn.execute("SELECT acknowledge_alarm(%s, %s)", (alarm_id, user.user_id))
        except Exception:
            logger.exception(
                "mark_notification_read: could not acknowledge alarm %s (notification %s) -- read state unaffected",
                alarm_id, notification_id,
            )
    return _notification_out(row)


@router.post("/stream/ticket", response_model=NotificationStreamTicket)
async def create_notification_stream_ticket(
    request: Request,
    user: TokenClaims = Depends(require_non_driver),
) -> NotificationStreamTicket:
    # Same reason as create_position_stream_ticket (positions.py): this
    # endpoint does not go through get_db, so it revalidates the session by
    # hand -- otherwise an already disabled user could keep minting fresh
    # tickets indefinitely.
    await assert_session_active(request.app.state.pool, user)
    ticket_store = request.app.state.notifications.ticket_store
    return NotificationStreamTicket(ticket=ticket_store.mint(user), expires_in=30)


@router.get("/stream")
async def stream_notifications(request: Request, ticket: str = Query(...)) -> StreamingResponse:
    ticket_store = request.app.state.notifications.ticket_store
    claims = ticket_store.consume(ticket)
    if claims is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid, expired, or already used ticket")
    # Extra defense, not the real guarantee -- same as stream_positions
    # (positions.py): never rely on a single layer for an isolation barrier.
    if claims.role == "driver":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this action is not available for driver accounts")

    broadcaster: NotificationBroadcaster = request.app.state.notifications.broadcaster
    queue = broadcaster.register(
        user_id=claims.user_id,
        # `is not None`, not a truthiness check -- an empty tuple is a real
        # value distinct from None (an API key deliberately scoped to no
        # device must not be treated as "unrestricted").
        allowed_device_ids=frozenset(claims.allowed_device_ids) if claims.allowed_device_ids is not None else None,
    )

    async def event_generator():
        last_sent = time.monotonic()
        last_active_check = time.monotonic()
        pool = request.app.state.pool
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=_DISCONNECT_POLL_SECONDS)
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if now - last_active_check >= _KEEPALIVE_SECONDS:
                        last_active_check = now
                        if not await is_session_active(pool, claims):
                            break
                    if now - last_sent >= _KEEPALIVE_SECONDS:
                        yield ": keepalive\n\n"
                        last_sent = now
                    continue
                yield f"data: {json.dumps(payload)}\n\n"
                last_sent = time.monotonic()
        finally:
            broadcaster.unregister(queue, user_id=claims.user_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
