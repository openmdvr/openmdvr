"""Real-time GPS positions + ignition/power status: the Postgres listener
(LISTEN gps_positions -- pg_notify() in insert_gps_position(), migration 0018
-- AND LISTEN device_status -- trigger added in migration 0047, see its
comment for why a trigger instead of a single SECURITY DEFINER function) and
the in-memory fan-out to connected SSE clients (GET /positions/stream,
api/app/routers/positions.py). One listener/broadcaster/stream for both -- the
device_status payload carries `"type": "device_status"` so the frontend can
tell them apart without breaking the position payload (which never had that
field).

Design:
- ONE listener per API process, with its own reconnect backoff (never one
  LISTEN per connected client -- it would not remove the need to filter by
  tenant in code anyway, and it would multiply Postgres connections with every
  open browser tab).
- pg_notify() has NO permission model: the `gps_positions` channel delivers
  EVERY tenant's events to any session that LISTENs. All the real isolation of
  this feature lives in PositionBroadcaster.publish() below -- a barrier
  parallel to RLS but NOT covered by RLS. Treat it with the same rigor as any
  other tenant-isolation code.
- TicketStore exists because EventSource (the browser SSE API) cannot send
  the Authorization header -- a long-lived JWT in a query string is a real
  exposure risk via access logs/history. The ticket is opaque, single-use,
  short-lived, and lives in the memory of ONE process (accepted limitation:
  with more than one API replica, a ticket issued by one replica is unknown to
  another -- an isolated 401, self-corrected by the frontend's reconnect).

This module is device-protocol agnostic on purpose: it only knows the generic
`gps_positions` channel and the payload insert_gps_position() publishes
(tenant_id/device_id already resolved + position data), never JT808 or any
protocol detail. Any protocol server that resolves its own device_id gets the
full real-time push without this file changing -- see the comment in
infra/postgres/migrations/0018_gps_position_notify.sql and, in
api/tests/test_positions_stream.py, every test inserts positions by calling
insert_gps_position() directly via SQL, never through jt808-server, as
empirical proof of this decoupling."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass

import psycopg

from .config import Settings
from .db import build_conninfo
from .security import TokenClaims

logger = logging.getLogger(__name__)

# Fixed, identifiable listener name in pg_stat_activity -- lets production
# verify it is alive without new tooling, and lets a test kill it on purpose
# (pg_terminate_backend) to prove it reconnects on its own.
LISTENER_APPLICATION_NAME = "openmdvr_live_listener"

_TICKET_TTL_SECONDS = 30
_QUEUE_MAX_SIZE = 100
_RECONNECT_BACKOFF_INITIAL = 1.0
_RECONNECT_BACKOFF_MAX = 30.0


class PositionBroadcaster:
    """In-memory registry of connected SSE clients, per tenant, plus a
    separate group for bypass sessions (super_admin/support) that see every
    tenant -- same rule gps_positions_v applies with RLS, reimplemented by hand
    here because NOTIFY has no ACL.

    Per-device visibility (see
    infra/postgres/migrations/0032_device_visibility_rls.sql): RLS already
    filters `devices`/`alarms_v`/`gps_positions_v` through
    app_can_view_device(), but pg_notify() still has no ACL -- without this
    filter, a tenant_operator/tenant_viewer with UNASSIGNED devices would keep
    receiving their real-time position on this channel even though they could
    no longer see it in /positions/latest, the same kind of gap already closed
    for TENANT isolation. Each connection stores the set of device_ids it can
    see -- computed on connect with a `SELECT id FROM devices` on that
    session's RLS-scoped connection (see routers/positions.py), and
    RECOMPUTED periodically (same ~22s timer that revalidates active
    session/tenant, see _KEEPALIVE_SECONDS in positions.py) via
    update_allowed_devices() -- never per event, which would be too expensive.
    Uniform across bypass/tenant_admin/operator/viewer: RLS already decides
    that set correctly for each role, so this class needs no role knowledge.

    Without the periodic recompute, revoking a user's assignment in the MIDDLE
    of an open SSE connection had no effect until the client reconnected (the
    frontend only reconnects on error) -- an operator whose access to a device
    was removed kept seeing its real-time position indefinitely. Revocation is
    a security barrier, not just a data-freshness concern, so "fixed on
    reconnect" is not enough."""

    def __init__(self) -> None:
        self._by_tenant: dict[str, dict[asyncio.Queue, frozenset[str]]] = {}
        self._bypass: dict[asyncio.Queue, frozenset[str]] = {}

    def register(self, *, tenant_id: str | None, bypass: bool, allowed_device_ids: frozenset[str]) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX_SIZE)
        if bypass:
            self._bypass[queue] = allowed_device_ids
        elif tenant_id:
            self._by_tenant.setdefault(tenant_id, {})[queue] = allowed_device_ids
        return queue

    def update_allowed_devices(
        self, queue: asyncio.Queue, *, tenant_id: str | None, bypass: bool, allowed_device_ids: frozenset[str]
    ) -> None:
        """Replaces the visible-device set of an ALREADY registered connection
        -- called periodically (never per event) from the GET
        /positions/stream loop. Silent no-op if the queue was unregistered in
        the meantime (benign race with unregister())."""
        if bypass:
            if queue in self._bypass:
                self._bypass[queue] = allowed_device_ids
        elif tenant_id and tenant_id in self._by_tenant and queue in self._by_tenant[tenant_id]:
            self._by_tenant[tenant_id][queue] = allowed_device_ids

    def unregister(self, queue: asyncio.Queue, *, tenant_id: str | None, bypass: bool) -> None:
        if bypass:
            self._bypass.pop(queue, None)
        elif tenant_id and tenant_id in self._by_tenant:
            self._by_tenant[tenant_id].pop(queue, None)
            if not self._by_tenant[tenant_id]:
                del self._by_tenant[tenant_id]

    def publish(self, tenant_id: str | None, payload: dict) -> None:
        if not tenant_id:
            return
        # device_id is ALWAYS populated by insert_gps_position() (NOT NULL
        # column, see 0018_gps_position_notify.sql) -- if it were ever missing,
        # fail closed (deliver to nobody) instead of fail open.
        device_id = payload.get("device_id")
        targets: dict[asyncio.Queue, frozenset[str]] = dict(self._by_tenant.get(tenant_id, {}))
        targets.update(self._bypass)
        for queue, allowed_device_ids in targets.items():
            if device_id is None or device_id not in allowed_device_ids:
                continue
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # A slow/stuck client must never block others or accumulate
                # unbounded memory at their expense -- ITS event is dropped,
                # the whole fan-out does not fail. Same robustness rule as for
                # untrusted network traffic, applied here as hygiene even
                # though the client is an authenticated browser.
                logger.warning("live_positions: queue full for tenant %s, dropping an event", tenant_id)


@dataclass(frozen=True)
class _Ticket:
    claims: TokenClaims
    expires_at: float


class TicketStore:
    """Opaque single-use tokens for the GET /positions/stream handshake (see
    module docstring). Atomic pop: the asyncio loop is single-threaded and
    there is no `await` between the expiry check and the `pop`, so there is no
    real race between two concurrent consumptions of the same ticket."""

    def __init__(self, ttl_seconds: float = _TICKET_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._tickets: dict[str, _Ticket] = {}

    def mint(self, claims: TokenClaims) -> str:
        self._evict_expired()
        token = secrets.token_urlsafe(32)
        self._tickets[token] = _Ticket(claims=claims, expires_at=time.monotonic() + self._ttl)
        return token

    def consume(self, token: str) -> TokenClaims | None:
        ticket = self._tickets.pop(token, None)
        if ticket is None:
            return None
        if time.monotonic() > ticket.expires_at:
            return None
        return ticket.claims

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [t for t, ticket in self._tickets.items() if now > ticket.expires_at]
        for t in expired:
            del self._tickets[t]


@dataclass
class LivePositionsState:
    """Single container stored in app.state.live_positions (see main.py
    lifespan) -- groups what GET /positions/stream and POST
    /positions/stream/ticket need, without cluttering app.state with loose
    attributes."""
    broadcaster: PositionBroadcaster
    ticket_store: TicketStore
    # Updated by run_listener on connect/disconnect -- exposed on GET /health
    # (main.py). Cheap, and it removes the blind spot where a silently dead
    # broadcast listener goes unnoticed in production.
    listener_connected: bool = False


async def run_listener(settings: Settings, state: "LivePositionsState", stop_event: asyncio.Event) -> None:
    """Background task (started in the FastAPI lifespan, see main.py): keeps
    ONE LISTEN connection to Postgres, never from the pool (the pool is
    per-request via tenant_scoped_connection; this connection must live
    indefinitely). Reconnects with exponential backoff on ANY exception --
    otherwise a network blip or a Postgres restart kills the push for ALL
    tenants until the API process is restarted."""
    conninfo = build_conninfo(settings)
    backoff = _RECONNECT_BACKOFF_INITIAL
    while not stop_event.is_set():
        try:
            async with await psycopg.AsyncConnection.connect(
                conninfo,
                autocommit=True,
                application_name=LISTENER_APPLICATION_NAME,
            ) as conn:
                await conn.execute("LISTEN gps_positions")
                # device_status (migration 0047): real-time ignition/power,
                # same listener/fan-out as gps_positions -- never a second
                # LISTEN connection. Tagged with "type" so the frontend can
                # tell both events apart on the same SSE stream without
                # breaking the existing position payload (which never carried
                # "type").
                await conn.execute("LISTEN device_status")
                backoff = _RECONNECT_BACKOFF_INITIAL
                state.listener_connected = True
                async for notify in conn.notifies():
                    if stop_event.is_set():
                        break
                    # Anything that goes wrong processing ONE notification
                    # (invalid JSON, a payload that is not an object -- e.g.
                    # `pg_notify('gps_positions', '42')` is valid JSON but an
                    # int has no .get() -- or any other unexpected data) must
                    # drop ONLY that notification, never the whole LISTEN
                    # connection. Without this broad try/except, a single
                    # malformed notification kills the listener (via the
                    # except Exception below) and cuts the push for ALL tenants
                    # during the reconnect backoff.
                    try:
                        payload = json.loads(notify.payload)
                        if not isinstance(payload, dict):
                            raise ValueError("NOTIFY payload is not a JSON object")
                        if notify.channel == "device_status":
                            payload["type"] = "device_status"
                        state.broadcaster.publish(payload.get("tenant_id"), payload)
                    except Exception:
                        logger.warning(
                            "live_positions: notification dropped, could not be processed", exc_info=True
                        )
                        continue
        except asyncio.CancelledError:
            raise
        except Exception:
            state.listener_connected = False
            logger.exception(
                "live_positions: listener lost its connection, retrying in %.0fs", backoff
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)
