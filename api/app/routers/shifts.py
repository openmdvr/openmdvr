from __future__ import annotations

import datetime as dt
import logging
import uuid

from fastapi import APIRouter, Depends, Query
from psycopg import AsyncConnection
from psycopg.types.json import Json

from ..deps import get_current_user, get_db, require_driver
from ..schemas import Page, ShiftEventCreate, ShiftEventOut
from ..security import TokenClaims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shifts", tags=["shifts"])

_SELECT_COLUMNS = """e.id, e.tenant_id, e.driver_id, d.name, e.event_type, e.occurred_at,
                      e.lat, e.lon, e.source"""
_FROM_JOIN = "FROM driver_shift_events e JOIN drivers d ON d.id = e.driver_id"


@router.post("/clock", response_model=ShiftEventOut, status_code=201)
async def clock_event(
    body: ShiftEventCreate,
    conn: AsyncConnection = Depends(get_db),
    # A driver only records THEIR OWN event -- driver_id comes from the JWT
    # (user.driver_id), never from the body, so a driver cannot clock
    # another driver in/out (RLS backs this up too; this also keeps the body
    # from even offering the option).
    user: TokenClaims = Depends(require_driver),
) -> ShiftEventOut:
    inserted = await (
        await conn.execute(
            """INSERT INTO driver_shift_events (tenant_id, driver_id, event_type, lat, lon, source)
               VALUES (%s, %s, %s, %s, %s, 'driver_app')
               RETURNING id, occurred_at""",
            (user.tenant_id, user.driver_id, body.event_type, body.lat, body.lon),
        )
    ).fetchone()
    new_id, occurred_at = inserted
    # Tenant policy: NEVER blocks or fails this request. get_db (deps.py)
    # opens ONE transaction for the whole request (see
    # db.tenant_scoped_connection) -- without the savepoint below, an
    # exception here would also roll back the shift event INSERT above,
    # breaking exactly the guarantee this comment promises. A nested
    # conn.transaction() inside an open transaction creates a SAVEPOINT: if
    # the block raises, only THIS block is undone, not the INSERT above.
    try:
        async with conn.transaction():
            await _check_driver_policy(conn, user.tenant_id, user.driver_id, body.event_type, occurred_at)
    except Exception:
        logger.exception("failed evaluating shift policy for tenant %s (driver %s) -- the real event is unaffected", user.tenant_id, user.driver_id)
    row = await (
        await conn.execute(f"SELECT {_SELECT_COLUMNS} {_FROM_JOIN} WHERE e.id = %s", (new_id,))
    ).fetchone()
    return _shift_event_out(row)


async def _check_driver_policy(
    conn: AsyncConnection, tenant_id: str, driver_id: str, event_type: str, occurred_at: dt.datetime
) -> None:
    """Evaluates the tenant's meal/hours policy against the event just
    inserted and records an alert (driver_shift_alerts) when applicable.
    Alert-only: it never raises to prevent the clock-in/out that triggered it
    -- a failure here would be a real bug that must show in the logs, not
    something to silence with a try/except."""
    policy = await (
        await conn.execute(
            "SELECT meal_break_window_start, meal_break_window_end, max_shift_hours FROM tenants WHERE id = %s",
            (tenant_id,),
        )
    ).fetchone()
    if policy is None:
        return
    meal_start, meal_end, max_shift_hours = policy

    if event_type == "meal_start" and meal_start is not None and meal_end is not None:
        t = occurred_at.timetz().replace(tzinfo=None)
        if meal_start <= meal_end:
            outside = not (meal_start <= t <= meal_end)
        else:
            # Window crossing midnight (e.g. 22:00-02:00) -- night shifts are
            # a real case, so start < end is not required.
            outside = not (t >= meal_start or t <= meal_end)
        if outside:
            await conn.execute(
                """INSERT INTO driver_shift_alerts (tenant_id, driver_id, alert_type, details, occurred_at)
                   VALUES (%s, %s, 'meal_outside_window', %s, %s)""",
                (tenant_id, driver_id, Json({"attempted_at": t.isoformat()}), occurred_at),
            )

    if max_shift_hours is not None:
        last_clock_in = await (
            await conn.execute(
                """SELECT occurred_at FROM driver_shift_events
                   WHERE driver_id = %s AND event_type = 'clock_in'
                   ORDER BY occurred_at DESC LIMIT 1""",
                (driver_id,),
            )
        ).fetchone()
        if last_clock_in is None:
            return
        shift_start = last_clock_in[0]
        hours_on_shift = (occurred_at - shift_start).total_seconds() / 3600
        if hours_on_shift > float(max_shift_hours):
            # Avoid alerting on every subsequent event of the SAME exceeded
            # shift -- one alert per shift over the maximum, not one per
            # meal_end/clock_out that follows.
            already_alerted = await (
                await conn.execute(
                    """SELECT 1 FROM driver_shift_alerts
                       WHERE driver_id = %s AND alert_type = 'shift_exceeds_max_hours'
                         AND occurred_at >= %s
                       LIMIT 1""",
                    (driver_id, shift_start),
                )
            ).fetchone()
            if already_alerted is None:
                await conn.execute(
                    """INSERT INTO driver_shift_alerts (tenant_id, driver_id, alert_type, details, occurred_at)
                       VALUES (%s, %s, 'shift_exceeds_max_hours', %s, %s)""",
                    (
                        tenant_id,
                        driver_id,
                        Json({"hours_on_shift": round(hours_on_shift, 1), "max_shift_hours": float(max_shift_hours)}),
                        occurred_at,
                    ),
                )


@router.get("", response_model=Page[ShiftEventOut])
async def list_shift_events(
    conn: AsyncConnection = Depends(get_db),
    # No role gate: RLS does the real work (driver_shift_events_select in
    # migration 0015) -- a driver session only sees its own events even
    # without an extra role restriction here; tenant_admin/operator/viewer
    # see all of the tenant's.
    _: TokenClaims = Depends(get_current_user),
    driver_id: uuid.UUID | None = Query(None),
    date_from: dt.date | None = Query(None, alias="from"),
    date_to: dt.date | None = Query(None, alias="to"),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Page[ShiftEventOut]:
    where_parts: list[str] = []
    params: list[object] = []
    if driver_id is not None:
        where_parts.append("e.driver_id = %s")
        params.append(driver_id)
    if date_from is not None:
        where_parts.append('e.occurred_at >= %s')
        params.append(date_from)
    if date_to is not None:
        where_parts.append('e.occurred_at < %s')
        params.append(date_to + dt.timedelta(days=1))
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    total_row = await (await conn.execute(f"SELECT count(*) {_FROM_JOIN} {where}", params)).fetchone()
    total = total_row[0] if total_row else 0

    rows = await (
        await conn.execute(
            f"SELECT {_SELECT_COLUMNS} {_FROM_JOIN} {where} ORDER BY e.occurred_at DESC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
    ).fetchall()
    return Page(items=[_shift_event_out(r) for r in rows], total=total, limit=limit, offset=offset)


def _shift_event_out(row) -> ShiftEventOut:
    return ShiftEventOut(
        id=row[0], tenant_id=row[1], driver_id=row[2], driver_name=row[3], event_type=row[4],
        occurred_at=row[5].isoformat(), lat=row[6], lon=row[7], source=row[8],
    )
