from __future__ import annotations

import datetime as dt
import math
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from psycopg.errors import CheckViolation, InsufficientPrivilege, UniqueViolation

from ..deps import get_db, require_non_driver, require_tenant_admin
from ..schemas import (
    AssignDriverRequest,
    DistanceDay,
    EngineHoursDay,
    Page,
    VehicleCreate,
    VehicleDistanceReport,
    VehicleEngineHoursReport,
    VehicleOut,
    VehicleUpdate,
)
from ..security import TokenClaims
from .. import limits

router = APIRouter(prefix="/vehicles", tags=["vehicles"])

# Distance report window cap -- same rule as `minutes` on
# GET /devices/{id}/positions (devices.py): distance is summed between
# consecutive GPS points without any precomputed summary table (gps_positions
# has no continuous aggregates -- see docs/architecture.md). If real volume
# makes this slow, the next option is a TimescaleDB continuous aggregate, not
# blindly enlarging this cap.
_MAX_DISTANCE_REPORT_DAYS = limits.DISTANCE_REPORT_MAX_DAYS

_EARTH_RADIUS_KM = 6371.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance between two GPS points on the Earth sphere -- good enough for
    a distance-traveled report (<0.5% error versus the real ellipsoid,
    irrelevant at this scale). Computed in Python, not SQL: PostGIS is not
    installed (see infra/postgres/migrations/0001_extensions.sql) and volume
    is already bounded by _MAX_DISTANCE_REPORT_DAYS."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))

# LEFT JOIN to the active assignment (ended_at IS NULL) and its driver -- a
# vehicle may have no driver assigned, hence LEFT instead of INNER.
_SELECT_COLUMNS = """v.id, v.tenant_id, v.plate, v.make, v.model, v.year, v.status, v.notes,
                      dr.id, dr.name, v.max_speed_kmh"""
_FROM_JOIN = """FROM vehicles v
                LEFT JOIN driver_vehicle_assignments dva ON dva.vehicle_id = v.id AND dva.ended_at IS NULL
                LEFT JOIN drivers dr ON dr.id = dva.driver_id"""


@router.post("", response_model=VehicleOut, status_code=201)
async def create_vehicle(
    body: VehicleCreate,
    conn: AsyncConnection = Depends(get_db),
    # tenant_admin self-service (unlike devices: no globally unique
    # identifier is involved, it is the customer's own inventory) -- see the
    # header comment of migration 0014.
    _: TokenClaims = Depends(require_tenant_admin),
) -> VehicleOut:
    try:
        row = await (
            await conn.execute(
                """INSERT INTO vehicles (tenant_id, plate, make, model, year, notes, max_speed_kmh)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   RETURNING id, tenant_id, plate, make, model, year, status, notes, max_speed_kmh""",
                (body.tenant_id, body.plate, body.make, body.model, body.year, body.notes, body.max_speed_kmh),
            )
        ).fetchone()
    except UniqueViolation:
        raise HTTPException(status.HTTP_409_CONFLICT, "a vehicle with that plate already exists in this tenant")
    except CheckViolation:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid vehicle year or maximum speed")
    except InsufficientPrivilege:
        # The WITH CHECK of vehicles_insert (migration 0014) rejects a
        # tenant_id that is not the session's own -- RLS is what really
        # enforces it.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you cannot create a vehicle for another tenant")

    return VehicleOut(
        id=row[0], tenant_id=row[1], plate=row[2], make=row[3], year=row[5],
        model=row[4], status=row[6], notes=row[7], current_driver_id=None, current_driver_name=None,
        max_speed_kmh=float(row[8]) if row[8] is not None else None,
    )


@router.get("", response_model=Page[VehicleOut])
async def list_vehicles(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
    search: str | None = Query(None, max_length=200),
    # Optional -- RLS already isolates, this only narrows further (see
    # users.py::list_users for the same rule).
    tenant_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Page[VehicleOut]:
    where_parts: list[str] = []
    params: list[object] = []
    if search:
        where_parts.append("(v.plate ILIKE %s OR v.make ILIKE %s OR v.model ILIKE %s)")
        like = f"%{search}%"
        params.extend([like, like, like])
    if tenant_id is not None:
        where_parts.append("v.tenant_id = %s")
        params.append(tenant_id)
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    total_row = await (await conn.execute(f"SELECT count(*) FROM vehicles v {where}", params)).fetchone()
    total = total_row[0] if total_row else 0

    rows = await (
        await conn.execute(
            f"SELECT {_SELECT_COLUMNS} {_FROM_JOIN} {where} ORDER BY v.plate NULLS LAST LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
    ).fetchall()
    return Page(items=[_vehicle_out(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{vehicle_id}", response_model=VehicleOut)
async def get_vehicle(
    vehicle_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
) -> VehicleOut:
    row = await (
        await conn.execute(f"SELECT {_SELECT_COLUMNS} {_FROM_JOIN} WHERE v.id = %s", (vehicle_id,))
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "vehicle not found")
    return _vehicle_out(row)


@router.get("/{vehicle_id}/distance", response_model=VehicleDistanceReport)
async def vehicle_distance_report(
    vehicle_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
    date_from: dt.date = Query(..., alias="from"),
    date_to: dt.date = Query(..., alias="to"),
) -> VehicleDistanceReport:
    # Kilometers traveled per day, summing haversine distance between
    # consecutive GPS points -- see docs/architecture.md. This is data for an
    # hours/distance report, NOT a report certified for any specific regulator
    # (e.g. hours-of-service/ELD rules) until each one's exact format is
    # confirmed.
    if date_to < date_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "'to' cannot be earlier than 'from'")
    if (date_to - date_from).days > _MAX_DISTANCE_REPORT_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"the report window cannot exceed {_MAX_DISTANCE_REPORT_DAYS} days",
        )

    vehicle_row = await (await conn.execute("SELECT id FROM vehicles WHERE id = %s", (vehicle_id,))).fetchone()
    if vehicle_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "vehicle not found")

    # The CURRENTLY linked device (devices.vehicle_id) -- if the vehicle had a
    # different device installed earlier within the same window, that history
    # is not reconstructed in v1 (there is no device-installation history
    # table today, only the driver<->vehicle assignment).
    device_row = await (
        await conn.execute("SELECT id FROM devices WHERE vehicle_id = %s", (vehicle_id,))
    ).fetchone()
    if device_row is None:
        return VehicleDistanceReport(
            vehicle_id=vehicle_id, device_id=None, date_from=date_from.isoformat(),
            date_to=date_to.isoformat(), days=[], total_distance_km=0.0,
        )
    device_id = device_row[0]

    rows = await (
        await conn.execute(
            """SELECT lat, lon, "time" FROM gps_positions_v
               WHERE device_id = %s AND "time" >= %s AND "time" < %s
               ORDER BY "time" ASC""",
            (device_id, date_from, date_to + dt.timedelta(days=1)),
        )
    ).fetchall()

    by_day: dict[dt.date, list[float]] = {}
    counts: dict[dt.date, int] = {}
    prev: tuple[float, float] | None = None
    prev_day: dt.date | None = None
    for lat, lon, time in rows:
        day = time.date()
        counts[day] = counts.get(day, 0) + 1
        # A segment's distance is attributed to the DESTINATION point's day --
        # a hop from 23:59 to 00:01 crosses days, and counting it on the
        # arrival day is more intuitive for a daily report than splitting or
        # dropping it.
        if prev is not None and prev_day is not None:
            by_day.setdefault(day, [0.0])[0] += _haversine_km(prev[0], prev[1], lat, lon)
        else:
            by_day.setdefault(day, [0.0])
        prev = (lat, lon)
        prev_day = day

    days = [
        DistanceDay(date=day.isoformat(), distance_km=round(dist[0], 2), position_count=counts[day])
        for day, dist in sorted(by_day.items())
    ]
    total = round(sum(d.distance_km for d in days), 2)

    return VehicleDistanceReport(
        vehicle_id=vehicle_id, device_id=device_id, date_from=date_from.isoformat(),
        date_to=date_to.isoformat(), days=days, total_distance_km=total,
    )


# "Stopped" vs "driving" threshold: 5 km/h, not exactly 0 -- real GPS almost
# never reports exactly 0 with the vehicle stopped (signal noise), so an exact
# 0 would underestimate idle time.
_IDLE_SPEED_THRESHOLD_KMH = 5.0


def _split_interval_by_day(
    start: dt.datetime, end: dt.datetime
) -> list[tuple[dt.date, dt.datetime, dt.datetime]]:
    """Splits [start, end) into sub-intervals, one per calendar day (UTC) --
    needed because an ignition-on period can cross midnight and the report is
    per day."""
    parts: list[tuple[dt.date, dt.datetime, dt.datetime]] = []
    cur = start
    while cur < end:
        day = cur.date()
        day_end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=cur.tzinfo)
        part_end = min(end, day_end)
        parts.append((day, cur, part_end))
        cur = part_end
    return parts


@router.get("/{vehicle_id}/engine-hours", response_model=VehicleEngineHoursReport)
async def vehicle_engine_hours_report(
    vehicle_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
    date_from: dt.date = Query(..., alias="from"),
    date_to: dt.date = Query(..., alias="to"),
) -> VehicleEngineHoursReport:
    # Driving / engine-on-idle / engine-off hours, crossing ignition events
    # (alarms, migration 0048) with GPS speed (gps_positions). Only covers the
    # period since ignition events started being recorded -- with no data
    # before that there is no way to reconstruct history, so earlier days
    # simply come out as zero.
    if date_to < date_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "'to' cannot be earlier than 'from'")
    if (date_to - date_from).days > _MAX_DISTANCE_REPORT_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"the report window cannot exceed {_MAX_DISTANCE_REPORT_DAYS} days",
        )

    vehicle_row = await (await conn.execute("SELECT id FROM vehicles WHERE id = %s", (vehicle_id,))).fetchone()
    if vehicle_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "vehicle not found")

    device_row = await (
        await conn.execute("SELECT id FROM devices WHERE vehicle_id = %s", (vehicle_id,))
    ).fetchone()
    if device_row is None:
        return VehicleEngineHoursReport(
            vehicle_id=vehicle_id, device_id=None, date_from=date_from.isoformat(), date_to=date_to.isoformat(),
            days=[], total_driving_hours=0.0, total_idle_hours=0.0, total_engine_off_hours=0.0,
        )
    device_id = device_row[0]

    window_start = dt.datetime.combine(date_from, dt.time.min, tzinfo=dt.timezone.utc)
    window_end = min(
        dt.datetime.combine(date_to + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc),
        dt.datetime.now(dt.timezone.utc),
    )
    if window_end <= window_start:
        return VehicleEngineHoursReport(
            vehicle_id=vehicle_id, device_id=device_id, date_from=date_from.isoformat(), date_to=date_to.isoformat(),
            days=[], total_driving_hours=0.0, total_idle_hours=0.0, total_engine_off_hours=0.0,
        )

    # ignition_on -> ignition_off pairs, in order -- same consecutive pairing
    # rule as GET /drivers/{id}/hours (clock_in/clock_out). An 'off' with no
    # prior 'on' (already running before our data window) is ignored --
    # nothing to close. An 'on' with no 'off' yet (open period, the vehicle is
    # still running) is closed at window_end.
    alarm_rows = await (
        await conn.execute(
            """SELECT alarm_type, "time" FROM alarms_v
               WHERE device_id = %s AND alarm_type IN ('ignition_on', 'ignition_off') AND "time" < %s
               ORDER BY "time" ASC""",
            (device_id, window_end),
        )
    ).fetchall()

    intervals: list[tuple[dt.datetime, dt.datetime]] = []
    pending_on: dt.datetime | None = None
    for alarm_type, time in alarm_rows:
        if alarm_type == "ignition_on":
            pending_on = time
        elif alarm_type == "ignition_off" and pending_on is not None:
            intervals.append((pending_on, time))
            pending_on = None
    if pending_on is not None:
        intervals.append((pending_on, window_end))

    clipped_intervals = [
        (max(on, window_start), min(off, window_end)) for on, off in intervals if max(on, window_start) < min(off, window_end)
    ]

    driving_by_day: dict[dt.date, float] = {}
    idle_by_day: dict[dt.date, float] = {}
    on_seconds_by_day: dict[dt.date, float] = {}

    for start, end in clipped_intervals:
        for day, part_start, part_end in _split_interval_by_day(start, end):
            on_seconds_by_day[day] = on_seconds_by_day.get(day, 0.0) + (part_end - part_start).total_seconds()

        position_rows = await (
            await conn.execute(
                """SELECT speed_kmh, "time" FROM gps_positions_v
                   WHERE device_id = %s AND "time" >= %s AND "time" < %s
                   ORDER BY "time" ASC""",
                (device_id, start, end),
            )
        ).fetchall()
        prev_time: dt.datetime | None = None
        prev_speed: float | None = None
        for speed_kmh, time in position_rows:
            if prev_time is not None:
                delta_seconds = (time - prev_time).total_seconds()
                # Attributed to the DESTINATION point -- same rule as the
                # distance report, so both reports stay consistent across a
                # segment that crosses midnight.
                day = time.date()
                if (prev_speed or 0.0) >= _IDLE_SPEED_THRESHOLD_KMH:
                    driving_by_day[day] = driving_by_day.get(day, 0.0) + delta_seconds
                else:
                    idle_by_day[day] = idle_by_day.get(day, 0.0) + delta_seconds
            prev_time, prev_speed = time, speed_kmh

    all_days = sorted(set(on_seconds_by_day) | set(driving_by_day) | set(idle_by_day))
    days: list[EngineHoursDay] = []
    for day in all_days:
        driving_s = driving_by_day.get(day, 0.0)
        idle_s = idle_by_day.get(day, 0.0)
        on_s = on_seconds_by_day.get(day, 0.0)
        day_window_start = max(window_start, dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc))
        day_window_end = min(window_end, dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc))
        day_total_s = max(0.0, (day_window_end - day_window_start).total_seconds())
        # "engine off" = the rest of the day within the report window, outside
        # any ignition-on interval -- never negative by construction (on_s is
        # always a subset of the day).
        off_s = max(0.0, day_total_s - on_s)
        days.append(
            EngineHoursDay(
                date=day.isoformat(),
                driving_hours=round(driving_s / 3600, 2),
                idle_hours=round(idle_s / 3600, 2),
                engine_off_hours=round(off_s / 3600, 2),
            )
        )

    return VehicleEngineHoursReport(
        vehicle_id=vehicle_id, device_id=device_id, date_from=date_from.isoformat(), date_to=date_to.isoformat(),
        days=days,
        total_driving_hours=round(sum(d.driving_hours for d in days), 2),
        total_idle_hours=round(sum(d.idle_hours for d in days), 2),
        total_engine_off_hours=round(sum(d.engine_off_hours for d in days), 2),
    )


@router.patch("/{vehicle_id}", response_model=VehicleOut)
async def update_vehicle(
    vehicle_id: uuid.UUID,
    body: VehicleUpdate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> VehicleOut:
    try:
        updated = await (
            await conn.execute(
                """UPDATE vehicles
                   SET plate = COALESCE(%s, plate), make = COALESCE(%s, make),
                       model = COALESCE(%s, model), year = COALESCE(%s, year),
                       notes = COALESCE(%s, notes), max_speed_kmh = COALESCE(%s, max_speed_kmh)
                   WHERE id = %s
                   RETURNING id""",
                (body.plate, body.make, body.model, body.year, body.notes, body.max_speed_kmh, vehicle_id),
            )
        ).fetchone()
    except UniqueViolation:
        raise HTTPException(status.HTTP_409_CONFLICT, "a vehicle with that plate already exists in this tenant")
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "vehicle not found")

    row = await (
        await conn.execute(f"SELECT {_SELECT_COLUMNS} {_FROM_JOIN} WHERE v.id = %s", (vehicle_id,))
    ).fetchone()
    return _vehicle_out(row)


@router.post("/{vehicle_id}/assign-driver", response_model=VehicleOut)
async def assign_driver(
    vehicle_id: uuid.UUID,
    body: AssignDriverRequest,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> VehicleOut:
    # Closes THIS vehicle's active assignment (if another driver had it) and
    # THAT driver's active assignment (if they were driving another vehicle)
    # before opening the new one -- keeps the invariant "one active driver per
    # vehicle, one active vehicle per driver" without requiring the client to
    # call a separate "unassign" endpoint first.
    async with conn.transaction():
        await conn.execute(
            "UPDATE driver_vehicle_assignments SET ended_at = now() WHERE vehicle_id = %s AND ended_at IS NULL",
            (vehicle_id,),
        )
        await conn.execute(
            "UPDATE driver_vehicle_assignments SET ended_at = now() WHERE driver_id = %s AND ended_at IS NULL",
            (body.driver_id,),
        )
        vehicle_row = await (await conn.execute("SELECT tenant_id FROM vehicles WHERE id = %s", (vehicle_id,))).fetchone()
        if vehicle_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "vehicle not found")
        await conn.execute(
            "INSERT INTO driver_vehicle_assignments (tenant_id, driver_id, vehicle_id) VALUES (%s, %s, %s)",
            (vehicle_row[0], body.driver_id, vehicle_id),
        )

    row = await (
        await conn.execute(f"SELECT {_SELECT_COLUMNS} {_FROM_JOIN} WHERE v.id = %s", (vehicle_id,))
    ).fetchone()
    return _vehicle_out(row)


@router.post("/{vehicle_id}/unassign-driver", response_model=VehicleOut)
async def unassign_driver(
    vehicle_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> VehicleOut:
    await conn.execute(
        "UPDATE driver_vehicle_assignments SET ended_at = now() WHERE vehicle_id = %s AND ended_at IS NULL",
        (vehicle_id,),
    )
    row = await (
        await conn.execute(f"SELECT {_SELECT_COLUMNS} {_FROM_JOIN} WHERE v.id = %s", (vehicle_id,))
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "vehicle not found")
    return _vehicle_out(row)


def _vehicle_out(row) -> VehicleOut:
    return VehicleOut(
        id=row[0], tenant_id=row[1], plate=row[2], make=row[3], model=row[4], year=row[5],
        status=row[6], notes=row[7], current_driver_id=row[8], current_driver_name=row[9],
        max_speed_kmh=float(row[10]) if row[10] is not None else None,
    )
