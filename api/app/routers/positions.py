"""Last known GPS position of each device -- for the dashboard map.

Root-level route (`/positions/latest`, not `/devices/positions/latest`) on
purpose: mounting it under the same prefix as devices.py would risk FastAPI
matching it as `/devices/{device_id}` depending on the `include_router` order
in main.py -- simpler to avoid the clash than to depend on a correct order."""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from psycopg import AsyncConnection

from .. import db as db_module
from ..deps import assert_session_active, get_db, is_session_active, require_non_driver
from ..live_positions import PositionBroadcaster, TicketStore
from ..schemas import DevicePosition, PositionStreamTicket
from ..security import TokenClaims

router = APIRouter(prefix="/positions", tags=["positions"])

# Maximum interval between keepalive comments when there are no real events --
# shorter than any reasonable idle timeout of an intermediate proxy (typically
# 30-60s). The only real requirement on a reverse proxy is that it does not
# buffer this response and serves it over plain HTTP/1.1 without upgrade --
# SSE is a normal long-lived HTTP response.
_KEEPALIVE_SECONDS = 22
# How often to re-check whether the client disconnected. Deliberately much
# shorter than _KEEPALIVE_SECONDS: if the disconnect check lived on the same
# timeout as the keepalive, a client closing the tab would stay registered
# (with its queue accumulating memory) for up to 22s -- this brings it down to
# ~1s, a real resource limit, not just cosmetic.
_DISCONNECT_POLL_SECONDS = 1.0


@router.get("/latest", response_model=list[DevicePosition])
async def latest_positions(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
) -> list[DevicePosition]:
    # As in tenants.py/devices.py: no manual tenant_id filter -- devices (RLS
    # with app_can_view_device) and gps_positions_v (security_barrier) already
    # apply it.
    #
    # Performance: a DISTINCT ON over the WHOLE gps_positions_v would read and
    # sort ALL of the tenant's positions within retention (90 days by default:
    # ~2,880 pings/day/unit) just to keep one row per unit, on every map load
    # and every 90s reconciliation of every open browser. Instead: one LATERAL
    # per VISIBLE unit with ORDER BY time DESC LIMIT 1 over the
    # (device_id, time DESC) index from 0052 -- one index read per unit,
    # regardless of how much history exists.
    rows = await (
        await conn.execute(
            """SELECT d.id, d.label, p.lat, p.lon, p.speed_kmh, p.heading, p.time
               FROM devices d
               CROSS JOIN LATERAL (
                   SELECT gp.lat, gp.lon, gp.speed_kmh, gp.heading, gp.time
                   FROM gps_positions_v gp
                   WHERE gp.device_id = d.id
                   ORDER BY gp.time DESC
                   LIMIT 1
               ) p
               ORDER BY d.id"""
        )
    ).fetchall()
    return [
        DevicePosition(
            device_id=r[0],
            label=r[1],
            lat=r[2],
            lon=r[3],
            speed_kmh=r[4],
            heading=r[5],
            time=r[6].isoformat(),
        )
        for r in rows
    ]


@router.post("/stream/ticket", response_model=PositionStreamTicket)
async def create_position_stream_ticket(
    request: Request,
    user: TokenClaims = Depends(require_non_driver),
) -> PositionStreamTicket:
    # Does not go through get_db (this endpoint touches no business data), so
    # it needs its own revocation check -- without it, an already disabled or
    # deleted user could keep minting fresh tickets indefinitely.
    await assert_session_active(request.app.state.pool, user)
    ticket_store: TicketStore = request.app.state.live_positions.ticket_store
    return PositionStreamTicket(ticket=ticket_store.mint(user), expires_in=30)


@router.get("/stream")
async def stream_positions(request: Request, ticket: str = Query(...)) -> StreamingResponse:
    # No Depends(get_current_user) on purpose: EventSource (the browser SSE
    # API) cannot send an Authorization header, so the normal JWT is never an
    # option here -- see create_position_stream_ticket above and the
    # TicketStore docstring in live_positions.py.
    ticket_store: TicketStore = request.app.state.live_positions.ticket_store
    claims = ticket_store.consume(ticket)
    if claims is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid, expired, or already used ticket")
    # Extra defense, not the real guarantee (that is require_non_driver in
    # create_position_stream_ticket, which already prevents minting a ticket
    # as a driver) -- re-check the role here, just as get_db (deps.py)
    # re-validates driver_id even though the JWT issuer "should" have done it
    # right. Never rely on a single layer for an isolation barrier.
    if claims.role == "driver":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this action is not available for driver accounts")

    # Set of devices THIS session can see -- reuses the already-scoped RLS of
    # devices_select (migration 0032) instead of reimplementing its logic: for
    # bypass/tenant_admin this yields ALL visible devices, for operator/viewer
    # only the assigned ones. Computed on connect AND recomputed periodically
    # (see event_generator below): revoking a user's assignment in the MIDDLE
    # of an open connection must not wait for the client to reconnect (the
    # frontend only reconnects on error) -- it is a security barrier, not just
    # a data-freshness concern.
    pool = request.app.state.pool

    async def _fetch_allowed_device_ids() -> frozenset[str]:
        async with db_module.tenant_scoped_connection(
            pool, tenant_id=claims.tenant_id, bypass=claims.is_platform_bypass,
            driver_id=claims.driver_id, user_id=claims.user_id,
            api_key_device_filter=claims.allowed_device_ids,
        ) as conn:
            device_rows = await (await conn.execute("SELECT id FROM devices")).fetchall()
        return frozenset(str(r[0]) for r in device_rows)

    allowed_device_ids = await _fetch_allowed_device_ids()

    broadcaster: PositionBroadcaster = request.app.state.live_positions.broadcaster
    queue = broadcaster.register(
        tenant_id=claims.tenant_id, bypass=claims.is_platform_bypass, allowed_device_ids=allowed_device_ids
    )

    async def event_generator():
        last_sent = time.monotonic()
        last_active_check = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=_DISCONNECT_POLL_SECONDS)
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    # Periodically re-validate the session AND recompute the
                    # visible devices (same interval as the keepalive):
                    # without this, an already open stream ignored that the
                    # user was disabled/deleted, the tenant
                    # suspended/cancelled, or a device unassigned AFTER
                    # connecting, and kept delivering GPS positions
                    # indefinitely. No HTTP error is raised -- the response has
                    # already started -- the stream simply ends (inactive
                    # session) or the filter is updated in place (devices);
                    # the frontend already retries with a new ticket if the
                    # stream closes.
                    if now - last_active_check >= _KEEPALIVE_SECONDS:
                        last_active_check = now
                        if not await is_session_active(pool, claims):
                            break
                        broadcaster.update_allowed_devices(
                            queue,
                            tenant_id=claims.tenant_id,
                            bypass=claims.is_platform_bypass,
                            allowed_device_ids=await _fetch_allowed_device_ids(),
                        )
                    if now - last_sent >= _KEEPALIVE_SECONDS:
                        yield ": keepalive\n\n"
                        last_sent = now
                    continue
                yield f"data: {json.dumps(payload)}\n\n"
                last_sent = time.monotonic()
        finally:
            # ALWAYS unregister, however the generator ends (client
            # disconnected, exception, cancellation) -- a client left
            # registered after disconnecting is exactly the classic bug where
            # logging out did not close the socket and a different tenant
            # could keep receiving its events.
            broadcaster.unregister(queue, tenant_id=claims.tenant_id, bypass=claims.is_platform_bypass)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
