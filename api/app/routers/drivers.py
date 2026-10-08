from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from psycopg.errors import InsufficientPrivilege, UniqueViolation

from ..deps import get_current_user, get_db, require_non_driver, require_tenant_admin
from ..schemas import DriverCreate, DriverHoursReport, DriverOut, DriverShiftStatusOut, DriverUpdate, Page, WorkedDay
from ..security import TokenClaims
from .. import limits

router = APIRouter(prefix="/drivers", tags=["drivers"])

# Same cap as the distance report (vehicles.py) and for the same reason:
# driver_shift_events has no precomputed summary table.
_MAX_HOURS_REPORT_DAYS = limits.HOURS_REPORT_MAX_DAYS

# LEFT JOIN to the vehicle of this driver's active assignment, if any.
_SELECT_COLUMNS = """d.id, d.tenant_id, d.name, d.license_number, d.phone, d.status, d.notes,
                      v.id, v.plate"""
_FROM_JOIN = """FROM drivers d
                LEFT JOIN driver_vehicle_assignments dva ON dva.driver_id = d.id AND dva.ended_at IS NULL
                LEFT JOIN vehicles v ON v.id = dva.vehicle_id"""


@router.post("", response_model=DriverOut, status_code=201)
async def create_driver(
    body: DriverCreate,
    conn: AsyncConnection = Depends(get_db),
    # Same self-service rule as vehicles.py -- see migration 0014.
    _: TokenClaims = Depends(require_tenant_admin),
) -> DriverOut:
    try:
        row = await (
            await conn.execute(
                """INSERT INTO drivers (tenant_id, name, license_number, phone, notes)
                   VALUES (%s, %s, %s, %s, %s)
                   RETURNING id, tenant_id, name, license_number, phone, status, notes""",
                (body.tenant_id, body.name, body.license_number, body.phone, body.notes),
            )
        ).fetchone()
    except InsufficientPrivilege:
        # Same as vehicles.py: the WITH CHECK of drivers_insert (migration
        # 0014) rejects a tenant_id that is not the caller's own.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you cannot create a driver for another tenant")
    except UniqueViolation:
        # drivers_tenant_license_number_unique
        # (0025_driver_license_number_unique.sql) -- the name is deliberately
        # NOT unique (two real drivers can share a name); the license number
        # is the real key.
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a driver with this license number already exists in this tenant"
        )

    return DriverOut(
        id=row[0], tenant_id=row[1], name=row[2], license_number=row[3], phone=row[4],
        status=row[5], notes=row[6], current_vehicle_id=None, current_vehicle_plate=None,
    )


@router.get("", response_model=Page[DriverOut])
async def list_drivers(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
    search: str | None = Query(None, max_length=200),
    # Optional -- RLS already isolates, this only narrows further (see
    # users.py::list_users for the same rule).
    tenant_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Page[DriverOut]:
    where_parts: list[str] = []
    params: list[object] = []
    if search:
        where_parts.append("(d.name ILIKE %s OR d.license_number ILIKE %s OR d.phone ILIKE %s)")
        like = f"%{search}%"
        params.extend([like, like, like])
    if tenant_id is not None:
        where_parts.append("d.tenant_id = %s")
        params.append(tenant_id)
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    total_row = await (await conn.execute(f"SELECT count(*) FROM drivers d {where}", params)).fetchone()
    total = total_row[0] if total_row else 0

    rows = await (
        await conn.execute(
            f"SELECT {_SELECT_COLUMNS} {_FROM_JOIN} {where} ORDER BY d.name LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
    ).fetchall()
    return Page(items=[_driver_out(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/shift-status", response_model=list[DriverShiftStatusOut])
async def list_driver_shift_status(
    conn: AsyncConnection = Depends(get_db),
    # require_non_driver: backs the "on shift now" card of the operations
    # page, fleet visibility -- never for a driver. Declared BEFORE
    # GET /{driver_id} in this router on purpose: otherwise FastAPI would try
    # to parse "shift-status" as a driver_id uuid.UUID and return 422 instead
    # of reaching here.
    _: TokenClaims = Depends(require_non_driver),
) -> list[DriverShiftStatusOut]:
    # LEFT JOIN LATERAL instead of DISTINCT ON: a driver with no events yet
    # must still appear (last_event_type/at as None), not vanish from the
    # list -- driver_shift_events RLS already isolates by tenant, and this
    # connection is never a driver session (require_non_driver).
    rows = await (
        await conn.execute(
            """SELECT d.id, d.name, e.event_type, e.occurred_at, e.lat, e.lon
               FROM drivers d
               LEFT JOIN LATERAL (
                   SELECT event_type, occurred_at, lat, lon FROM driver_shift_events
                   WHERE driver_id = d.id
                   ORDER BY occurred_at DESC LIMIT 1
               ) e ON true
               ORDER BY d.name"""
        )
    ).fetchall()
    return [
        DriverShiftStatusOut(
            driver_id=r[0], driver_name=r[1], last_event_type=r[2],
            last_event_at=r[3].isoformat() if r[3] else None,
            last_lat=r[4], last_lon=r[5],
        )
        for r in rows
    ]


@router.get("/{driver_id}", response_model=DriverOut)
async def get_driver(
    driver_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
) -> DriverOut:
    row = await (
        await conn.execute(f"SELECT {_SELECT_COLUMNS} {_FROM_JOIN} WHERE d.id = %s", (driver_id,))
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "driver not found")
    return _driver_out(row)


@router.patch("/{driver_id}", response_model=DriverOut)
async def update_driver(
    driver_id: uuid.UUID,
    body: DriverUpdate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> DriverOut:
    try:
        updated = await (
            await conn.execute(
                """UPDATE drivers
                   SET name = COALESCE(%s, name), license_number = COALESCE(%s, license_number),
                       phone = COALESCE(%s, phone), notes = COALESCE(%s, notes)
                   WHERE id = %s
                   RETURNING id""",
                (body.name, body.license_number, body.phone, body.notes, driver_id),
            )
        ).fetchone()
    except UniqueViolation:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a driver with this license number already exists in this tenant"
        )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "driver not found")

    row = await (
        await conn.execute(f"SELECT {_SELECT_COLUMNS} {_FROM_JOIN} WHERE d.id = %s", (driver_id,))
    ).fetchone()
    return _driver_out(row)


@router.get("/{driver_id}/hours", response_model=DriverHoursReport)
async def driver_hours_report(
    driver_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    # get_current_user, NOT require_non_driver, on purpose: a driver must be
    # able to see THEIR OWN hours summary. driver_id existence is not checked
    # separately (unlike vehicle_distance_report in vehicles.py) -- that check
    # would query `drivers`, whose RLS only isolates by tenant (no per-driver
    # dimension), letting a driver session confirm which other driver_ids
    # exist in their tenant. driver_shift_events does have that dimension
    # (migration 0015): if driver_id is not the caller's own (or does not
    # exist, or belongs to another tenant), the query below simply returns no
    # rows -- same behavior in all three cases, no oracle.
    _: TokenClaims = Depends(get_current_user),
    date_from: dt.date = Query(..., alias="from"),
    date_to: dt.date = Query(..., alias="to"),
) -> DriverHoursReport:
    if date_to < date_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "'to' cannot be earlier than 'from'")
    if (date_to - date_from).days > _MAX_HOURS_REPORT_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"the report window cannot exceed {_MAX_HOURS_REPORT_DAYS} days",
        )

    rows = await (
        await conn.execute(
            """SELECT event_type, occurred_at FROM driver_shift_events
               WHERE driver_id = %s AND occurred_at >= %s AND occurred_at < %s
               ORDER BY occurred_at ASC""",
            (driver_id, date_from, date_to + dt.timedelta(days=1)),
        )
    ).fetchall()

    # Pairs clock_in -> clock_out (subtracting meal_start -> meal_end time in
    # between) to compute hours worked per COMPLETE shift. A shift still open
    # at the end of the window does not count -- a deliberate v1
    # simplification: counting it partially would require an arbitrary "until
    # when" (until 'to'? until now?), which can inflate the report for a shift
    # that is in fact still open.
    hours_by_day: dict[dt.date, float] = {}
    shifts_by_day: dict[dt.date, int] = {}
    shift_start: dt.datetime | None = None
    break_start: dt.datetime | None = None
    break_accum = dt.timedelta()
    for event_type, occurred_at in rows:
        if event_type == "clock_in":
            shift_start = occurred_at
            break_accum = dt.timedelta()
            break_start = None
        elif event_type == "meal_start":
            break_start = occurred_at
        elif event_type == "meal_end":
            if break_start is not None:
                break_accum += occurred_at - break_start
                break_start = None
        elif event_type == "clock_out" and shift_start is not None:
            worked = (occurred_at - shift_start) - break_accum
            day = shift_start.date()
            hours_by_day[day] = hours_by_day.get(day, 0.0) + max(0.0, worked.total_seconds() / 3600)
            shifts_by_day[day] = shifts_by_day.get(day, 0) + 1
            shift_start = None
            break_accum = dt.timedelta()

    days = [
        WorkedDay(date=day.isoformat(), hours_worked=round(hours, 2), completed_shifts=shifts_by_day[day])
        for day, hours in sorted(hours_by_day.items())
    ]
    total = round(sum(d.hours_worked for d in days), 2)
    return DriverHoursReport(
        driver_id=driver_id, date_from=date_from.isoformat(), date_to=date_to.isoformat(),
        days=days, total_hours=total,
    )


def _driver_out(row) -> DriverOut:
    return DriverOut(
        id=row[0], tenant_id=row[1], name=row[2], license_number=row[3], phone=row[4],
        status=row[5], notes=row[6], current_vehicle_id=row[7], current_vehicle_plate=row[8],
    )
