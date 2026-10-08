"""Real-time in-app notifications: the Postgres listener (LISTEN
notifications, fed by the pg_notify() that insert_alarm() emits, migration
0033) and the in-memory fan-out to connected SSE clients
(GET /notifications/stream, api/app/routers/notifications.py).

Structural copy of live_positions.py -- same shape, same reason: pg_notify()
has no ACL, so real isolation (here per RECIPIENT USER instead of per tenant)
lives in NotificationBroadcaster.publish(), never in RLS alone. TicketStore is
a class SHARED with live_positions.py (same implementation, imported from
there) but this instance is separate -- a ticket minted for /positions/stream
must not work for /notifications/stream and vice versa, even though both
tickets have the same shape (TokenClaims).

NOTIFY payload design -- deliberately LIGHTWEIGHT (only tenant_id + alarm_id,
never the recipient list). pg_notify() has a HARD 8000-byte payload limit; an
earlier insert_alarm() put the full recipient_user_ids array there, and from
~205 recipients on (a tenant_admin is an automatic recipient of EVERYTHING in
their tenant, so a tenant only has to grow in users) NOTIFY failed with
"payload string too long" -- and since that PERFORM lived in the SAME
BEGIN/EXCEPTION block as the row INSERTs, the error ALSO rolled back the rows
already written: total, silent loss of the whole fan-out for that tenant. The
lightweight payload never grows with the number of recipients, and it also
closes a second issue: the old version exposed to EVERY connected client the
UUIDs of ALL other recipients of that alarm. This listener now resolves
recipients with its OWN SELECT on notifications WHERE alarm_id = $1 (by the
time NOTIFY arrives, the transaction that inserted those rows has committed)
and publishes to each queue ONLY its own row, never the full list."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import psycopg
from psycopg_pool import AsyncConnectionPool

from . import db as db_module
from .config import Settings
from .db import build_conninfo
from .live_positions import TicketStore

logger = logging.getLogger(__name__)

LISTENER_APPLICATION_NAME = "openmdvr_notifications_listener"

_QUEUE_MAX_SIZE = 100
_RECONNECT_BACKOFF_INITIAL = 1.0
_RECONNECT_BACKOFF_MAX = 30.0

_RECIPIENT_ROWS_SQL = """
    SELECT id, recipient_user_id, event_type, device_id, alarm_id, title, body, severity, created_at
    FROM notifications
    WHERE alarm_id = %s AND in_app_enabled
"""


class NotificationBroadcaster:
    """In-memory registry of connected SSE clients, keyed by
    recipient_user_id -- never by tenant (unlike PositionBroadcaster): a
    notification is a personal mailbox, not something a whole platform
    session should receive in bulk (a platform session is never a recipient
    of any tenant's alarms via app_device_recipients, see 0031)."""

    def __init__(self) -> None:
        self._by_user: dict[str, dict[asyncio.Queue, frozenset[str] | None]] = {}

    def register(self, *, user_id: str, allowed_device_ids: frozenset[str] | None = None) -> asyncio.Queue:
        """allowed_device_ids -- EXTRA scoping for an API key with
        allowed_device_ids configured (0034_api_keys.sql), same as
        PositionBroadcaster: RLS (notifications_select) already applies it on
        GET /notifications, but pg_notify() has no ACL, so the live push needs
        its own in-memory filter or a stream ticket scoped to one device would
        still push events for ANOTHER device outside that scope. None = no
        extra scoping (all of this user's notifications, the normal JWT
        case)."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX_SIZE)
        self._by_user.setdefault(user_id, {})[queue] = allowed_device_ids
        return queue

    def unregister(self, queue: asyncio.Queue, *, user_id: str) -> None:
        if user_id in self._by_user:
            self._by_user[user_id].pop(queue, None)
            if not self._by_user[user_id]:
                del self._by_user[user_id]

    def publish(self, user_id: str, payload: dict) -> None:
        """Delivers ONLY to THAT user_id's queues -- never receives (or
        forwards) other recipients' data (see module docstring). Within that,
        honors each individual queue's per-device scoping (see register())."""
        for queue, allowed_device_ids in dict(self._by_user.get(user_id, {})).items():
            if allowed_device_ids is not None and payload.get("device_id") not in allowed_device_ids:
                continue
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Same as PositionBroadcaster: a slow client never blocks
                # others or accumulates unbounded memory at their expense --
                # ITS event is dropped.
                logger.warning("notifications: queue full for user %s, dropping an event", user_id)


@dataclass
class NotificationsState:
    """Single container stored in app.state.notifications (see main.py
    lifespan) -- same pattern as LivePositionsState."""
    broadcaster: NotificationBroadcaster
    ticket_store: TicketStore
    listener_connected: bool = False


async def run_notification_listener(
    settings: Settings, pool: AsyncConnectionPool, state: "NotificationsState", stop_event: asyncio.Event
) -> None:
    """Keeps ONE LISTEN connection to Postgres (channel 'notifications'), with
    the same reconnect backoff and the same tolerance for malformed payloads
    as run_listener() in live_positions.py. Unlike that listener (whose NOTIFY
    payload carries everything needed), this one DOES need `pool` -- the
    lightweight payload (tenant_id+alarm_id, see module docstring) requires
    resolving recipients with a SELECT on `notifications` per event."""
    conninfo = build_conninfo(settings)
    backoff = _RECONNECT_BACKOFF_INITIAL
    while not stop_event.is_set():
        try:
            async with await psycopg.AsyncConnection.connect(
                conninfo,
                autocommit=True,
                application_name=LISTENER_APPLICATION_NAME,
            ) as conn:
                await conn.execute("LISTEN notifications")
                backoff = _RECONNECT_BACKOFF_INITIAL
                state.listener_connected = True
                async for notify in conn.notifies():
                    if stop_event.is_set():
                        break
                    try:
                        payload = json.loads(notify.payload)
                        if not isinstance(payload, dict):
                            raise ValueError("NOTIFY payload is not a JSON object")
                        alarm_id = payload.get("alarm_id")
                        if not alarm_id:
                            raise ValueError("NOTIFY payload has no alarm_id")
                        await _publish_recipients_for_alarm(pool, state, alarm_id)
                    except Exception:
                        logger.warning(
                            "notifications: notification dropped, could not be processed", exc_info=True
                        )
                        continue
        except asyncio.CancelledError:
            raise
        except Exception:
            state.listener_connected = False
            logger.exception(
                "notifications: listener lost its connection, retrying in %.0fs", backoff
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)


async def _publish_recipients_for_alarm(pool: AsyncConnectionPool, state: "NotificationsState", alarm_id: str) -> None:
    """Resolves the real `notifications` rows for this alarm (already
    committed -- NOTIFY is delivered after the COMMIT of the transaction that
    wrote them) and publishes each one ONLY to its own recipient's queue.
    Bypass connection: this listener represents no particular user/tenant and
    needs to see anyone's rows."""
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        rows = await (await conn.execute(_RECIPIENT_ROWS_SQL, (alarm_id,))).fetchall()
    for row in rows:
        recipient_user_id = str(row[1])
        payload = {
            "id": str(row[0]),
            "recipient_user_id": recipient_user_id,
            "event_type": row[2],
            "device_id": str(row[3]) if row[3] else None,
            "alarm_id": str(row[4]) if row[4] else None,
            "title": row[5],
            "body": row[6],
            "severity": row[7],
            "created_at": row[8].isoformat(),
        }
        state.broadcaster.publish(recipient_user_id, payload)
