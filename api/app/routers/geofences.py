"""Geofences (0052_geofences.sql) -- map areas that generate enter/exit/
dwell events for ANY device protocol. The actual evaluation lives in the
database (insert_gps_position -> evaluate_geofences_for_position), not
here: this API only manages geofences and exposes their events for
reports.

Permissions (same double barrier as device_groups.py -- API + RLS):
  - read geofences / events / report: any fleet role (require_non_driver).
    Events and occupancy also go through app_can_view_device in RLS: a
    tenant_operator only sees those of ITS assigned units.
  - create/edit/delete: tenant_admin or platform (require_tenant_admin
    + app_is_tenant_admin() in the RLS policy).
"""
from __future__ import annotations

import datetime as dt
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from psycopg.errors import (
    CheckViolation,
    ForeignKeyViolation,
    InsufficientPrivilege,
    InvalidParameterValue,
    ProgramLimitExceeded,
    QueryCanceled,
    RaiseException,
    UniqueViolation,
)

from ..deps import get_db, require_non_driver, require_tenant_admin
from ..schemas import (
    GeofenceCreate,
    GeofenceEventOut,
    GeofenceEventType,
    GeofenceOccupant,
    GeofenceOut,
    GeofenceReport,
    GeofenceReportRow,
    GeofenceUpdate,
    GeofenceVisit,
    Page,
)
from ..security import TokenClaims
from .. import limits

router = APIRouter(prefix="/geofences", tags=["geofences"])

# Maximum window for events/reports. geofence_events is indexed by
# (tenant|device|geofence, time), but an unbounded range over a large fleet
# is still a query a user should not be able to fire by accident.
# 93 days = one quarter, the most common reporting case.
_MAX_REPORT_DAYS = limits.GEOFENCE_REPORT_MAX_DAYS
_MAX_VISITS = limits.GEOFENCE_REPORT_MAX_VISITS
_QUERY_STATEMENT_TIMEOUT_MS = limits.REPORT_QUERY_TIMEOUT_MS

# device_ids and inside_count are resolved with correlated subqueries over
# RLS tables (geofence_devices / geofence_device_state, both using
# app_can_view_device) -- a session never sees ids or counts of units it is
# not assigned to.
_SELECT = """
SELECT g.id, g.tenant_id, g.name, g.description, g.color, g.shape,
       g.center_lat, g.center_lon, g.radius_m, g.polygon, g.enabled,
       g.notify_on_enter, g.notify_on_exit, g.dwell_minutes, g.severity,
       g.hysteresis_m, g.applies_to_all_devices,
       COALESCE((SELECT array_agg(gd.device_id) FROM geofence_devices gd WHERE gd.geofence_id = g.id), '{}'),
       (SELECT count(*) FROM geofence_device_state s WHERE s.geofence_id = g.id AND s.inside),
       g.created_at, g.updated_at
FROM geofences g
"""


def _out(r) -> GeofenceOut:
    polygon = r[9]
    if isinstance(polygon, str):
        polygon = json.loads(polygon)
    return GeofenceOut(
        id=r[0], tenant_id=r[1], name=r[2], description=r[3], color=r[4], shape=r[5],
        center_lat=r[6], center_lon=r[7], radius_m=r[8],
        polygon=[(p[0], p[1]) for p in polygon] if polygon else None,
        enabled=r[10], notify_on_enter=r[11], notify_on_exit=r[12], dwell_minutes=r[13],
        severity=r[14], hysteresis_m=r[15], applies_to_all_devices=r[16],
        device_ids=list(r[17] or []), inside_count=r[18],
        created_at=r[19].isoformat(), updated_at=r[20].isoformat(),
    )


async def _fetch_one(conn: AsyncConnection, geofence_id: uuid.UUID) -> GeofenceOut:
    row = await (await conn.execute(f"{_SELECT} WHERE g.id = %s", (geofence_id,))).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "geofence not found")
    return _out(row)


async def _replace_devices(conn: AsyncConnection, geofence_id: uuid.UUID, tenant_id: uuid.UUID,
                           device_ids: list[uuid.UUID]) -> None:
    await conn.execute("DELETE FROM geofence_devices WHERE geofence_id = %s", (geofence_id,))
    unique_ids = list(dict.fromkeys(device_ids))
    if not unique_ids:
        return
    try:
        # A single INSERT ... SELECT unnest instead of one round-trip per
        # device (up to 1000). ON CONFLICT DO NOTHING: the DELETE above only
        # removes rows VISIBLE to this session (RLS via app_can_view_device).
        # Without it, re-inserting an out-of-scope device that was already
        # assigned raised UniqueViolation, a membership oracle (found in
        # adversarial review).
        await conn.execute(
            """INSERT INTO geofence_devices (geofence_id, device_id, tenant_id)
               SELECT %s, d, %s FROM unnest(%s::uuid[]) AS d
               ON CONFLICT (geofence_id, device_id) DO NOTHING""",
            (geofence_id, tenant_id, unique_ids),
        )
    except (RaiseException, InsufficientPrivilege):
        # enforce_geofence_device_tenant_match() (device of another tenant
        # or nonexistent) or RLS (device outside this session's scope, e.g.
        # an API key with allowed_device_ids) -- same message, no oracle.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "one of the devices is not valid for this geofence")


def _db_error(exc: Exception) -> HTTPException:
    if isinstance(exc, UniqueViolation):
        return HTTPException(status.HTTP_409_CONFLICT, "a geofence with that name already exists in this tenant")
    if isinstance(exc, InsufficientPrivilege):
        return HTTPException(status.HTTP_403_FORBIDDEN, "you do not have permission to manage this tenant's geofences")
    if isinstance(exc, ProgramLimitExceeded):
        # Text of our own RAISE in geofences_prepare (0052) -- no internal
        # detail; distinguishes "500 geofences" from "25000 vertices".
        detail = getattr(exc.diag, "message_primary", None) or "tenant geofence limit reached"
        return HTTPException(status.HTTP_409_CONFLICT, detail)
    if isinstance(exc, ForeignKeyViolation):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "tenant does not exist")
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid geofence geometry or configuration")


def _as_utc(value: dt.datetime) -> dt.datetime:
    # Adversarial review finding: a naive `from` with a timezone-aware `to`
    # ("...T00:00:00" vs "...Z") made the comparison below raise TypeError
    # -> raw 500. A datetime without a timezone is interpreted as UTC.
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def _check_window(date_from: dt.datetime, date_to: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    date_from, date_to = _as_utc(date_from), _as_utc(date_to)
    if date_to <= date_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "'to' must be later than 'from'")
    if (date_to - date_from) > dt.timedelta(days=_MAX_REPORT_DAYS):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"the window cannot exceed {_MAX_REPORT_DAYS} days"
        )
    return date_from, date_to


async def _set_statement_timeout(conn: AsyncConnection) -> None:
    await conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(_QUERY_STATEMENT_TIMEOUT_MS),))


# --- LITERAL routes first: /events and /report must be registered BEFORE
# /{geofence_id} (Starlette matches in order -- same gotcha documented for
# /devices/models in devices.py).


@router.get("/events", response_model=Page[GeofenceEventOut])
async def list_geofence_events(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
    date_from: dt.datetime = Query(..., alias="from"),
    date_to: dt.datetime = Query(..., alias="to"),
    device_id: uuid.UUID | None = Query(None),
    geofence_id: uuid.UUID | None = Query(None),
    event_type: GeofenceEventType | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=100_000),
) -> Page[GeofenceEventOut]:
    date_from, date_to = _check_window(date_from, date_to)
    where = ["e.time >= %s", "e.time < %s"]
    params: list[object] = [date_from, date_to]
    if device_id is not None:
        where.append("e.device_id = %s")
        params.append(device_id)
    if geofence_id is not None:
        where.append("e.geofence_id = %s")
        params.append(geofence_id)
    if event_type is not None:
        where.append("e.event_type = %s")
        params.append(event_type)
    where_sql = " AND ".join(where)
    try:
        await _set_statement_timeout(conn)
        total_row = await (
            await conn.execute(f"SELECT count(*) FROM geofence_events e WHERE {where_sql}", params)
        ).fetchone()
        rows = await (
            await conn.execute(
                f"""SELECT e.id, e.geofence_id, e.geofence_name, e.device_id, d.label, e.event_type, e.time,
                           e.lat, e.lon, e.speed_kmh, e.entered_at, e.duration_s, e.entry_estimated, e.alarm_id
                    FROM geofence_events e JOIN devices d ON d.id = e.device_id
                    WHERE {where_sql}
                    ORDER BY e.time DESC
                    LIMIT %s OFFSET %s""",
                [*params, limit, offset],
            )
        ).fetchall()
    except QueryCanceled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the query took too long; narrow the range")
    items = [
        GeofenceEventOut(
            id=r[0], geofence_id=r[1], geofence_name=r[2], device_id=r[3], device_label=r[4], event_type=r[5],
            time=r[6].isoformat(), lat=r[7], lon=r[8], speed_kmh=r[9],
            entered_at=r[10].isoformat() if r[10] else None, duration_s=r[11], entry_estimated=r[12],
            alarm_id=r[13],
        )
        for r in rows
    ]
    return Page(items=items, total=total_row[0] if total_row else 0, limit=limit, offset=offset)


@router.get("/report", response_model=GeofenceReport)
async def geofence_report(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
    date_from: dt.datetime = Query(..., alias="from"),
    date_to: dt.datetime = Query(..., alias="to"),
    device_id: uuid.UUID | None = Query(None),
    geofence_id: uuid.UUID | None = Query(None),
) -> GeofenceReport:
    """Per-geofence summary + visits (enter -> exit) within the window.
    A CLOSED visit comes straight from its 'exit' event (which already
    carries entered_at/duration_s -- no enter/exit pairing at query time);
    an OPEN visit (the unit is still inside) comes from
    geofence_device_state."""
    date_from, date_to = _check_window(date_from, date_to)
    filters = ""
    params: list[object] = []
    if device_id is not None:
        filters += " AND e.device_id = %s"
        params.append(device_id)
    if geofence_id is not None:
        filters += " AND e.geofence_id = %s"
        params.append(geofence_id)
    state_filters = filters.replace("e.", "s.")

    try:
        await _set_statement_timeout(conn)
        summary_rows = await (
            await conn.execute(
                f"""SELECT e.geofence_id, (array_agg(e.geofence_name ORDER BY e.time DESC))[1],
                           count(*) FILTER (WHERE e.event_type = 'enter'),
                           count(*) FILTER (WHERE e.event_type = 'exit'),
                           count(*) FILTER (WHERE e.event_type = 'dwell'),
                           count(DISTINCT e.device_id),
                           COALESCE(sum(e.duration_s) FILTER (WHERE e.event_type = 'exit'), 0),
                           avg(e.duration_s) FILTER (WHERE e.event_type = 'exit')
                    FROM geofence_events e
                    WHERE e.time >= %s AND e.time < %s {filters}
                    GROUP BY e.geofence_id
                    ORDER BY 3 DESC""",
                [date_from, date_to, *params],
            )
        ).fetchall()

        closed_rows = await (
            await conn.execute(
                f"""SELECT e.device_id, d.label, e.geofence_id, e.geofence_name, e.entered_at, e.time,
                           e.duration_s, e.entry_estimated
                    FROM geofence_events e JOIN devices d ON d.id = e.device_id
                    WHERE e.event_type = 'exit' AND e.time >= %s AND e.time < %s {filters}
                    ORDER BY e.time DESC
                    LIMIT %s""",
                [date_from, date_to, *params, _MAX_VISITS + 1],
            )
        ).fetchall()

        open_rows = await (
            await conn.execute(
                f"""SELECT s.device_id, d.label, s.geofence_id, g.name, s.entered_at, s.entry_estimated
                    FROM geofence_device_state s
                    JOIN devices d ON d.id = s.device_id
                    JOIN geofences g ON g.id = s.geofence_id
                    WHERE s.inside AND (s.entered_at IS NULL OR s.entered_at < %s) {state_filters}
                    ORDER BY s.entered_at DESC NULLS LAST
                    LIMIT %s""",
                [date_to, *params, _MAX_VISITS],
            )
        ).fetchall()
    except QueryCanceled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the query took too long; narrow the range")

    now = dt.datetime.now(dt.timezone.utc)
    visits = [
        GeofenceVisit(
            device_id=r[0], device_label=r[1], geofence_id=r[2], geofence_name=r[3],
            entered_at=r[4].isoformat() if r[4] else None, exited_at=r[5].isoformat(),
            duration_s=r[6], entry_estimated=r[7], open=False,
        )
        for r in closed_rows[:_MAX_VISITS]
    ]
    for r in open_rows:
        visits.append(
            GeofenceVisit(
                device_id=r[0], device_label=r[1], geofence_id=r[2], geofence_name=r[3],
                entered_at=r[4].isoformat() if r[4] else None, exited_at=None,
                duration_s=int((now - r[4]).total_seconds()) if r[4] else None,
                entry_estimated=r[5], open=True,
            )
        )
    visits.sort(key=lambda v: v.entered_at or "", reverse=True)

    return GeofenceReport(
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        geofences=[
            GeofenceReportRow(
                geofence_id=r[0], geofence_name=r[1], enters=r[2], exits=r[3], dwells=r[4],
                unique_devices=r[5], total_inside_s=int(r[6]), avg_visit_s=int(r[7]) if r[7] is not None else None,
            )
            for r in summary_rows
        ],
        visits=visits[:_MAX_VISITS],
        visits_truncated=len(closed_rows) > _MAX_VISITS or len(visits) > _MAX_VISITS,
    )


@router.get("", response_model=Page[GeofenceOut])
async def list_geofences(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
    search: str | None = Query(None, max_length=200),
    tenant_id: uuid.UUID | None = Query(None),
    enabled: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page[GeofenceOut]:
    where_parts: list[str] = []
    params: list[object] = []
    if search:
        where_parts.append("g.name ILIKE %s")
        params.append(f"%{search}%")
    if tenant_id is not None:
        where_parts.append("g.tenant_id = %s")
        params.append(tenant_id)
    if enabled is not None:
        where_parts.append("g.enabled = %s")
        params.append(enabled)
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    total_row = await (await conn.execute(f"SELECT count(*) FROM geofences g {where}", params)).fetchone()
    rows = await (
        await conn.execute(f"{_SELECT} {where} ORDER BY g.name LIMIT %s OFFSET %s", [*params, limit, offset])
    ).fetchall()
    return Page(items=[_out(r) for r in rows], total=total_row[0] if total_row else 0, limit=limit, offset=offset)


@router.post("", response_model=GeofenceOut, status_code=201)
async def create_geofence(
    body: GeofenceCreate,
    conn: AsyncConnection = Depends(get_db),
    user: TokenClaims = Depends(require_tenant_admin),
) -> GeofenceOut:
    tenant_id = body.tenant_id or user.tenant_id
    if tenant_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a platform session must specify tenant_id")
    if user.tenant_id is not None and tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you cannot create a geofence for another tenant")

    try:
        async with conn.transaction():
            row = await (
                await conn.execute(
                    """INSERT INTO geofences (
                           tenant_id, name, description, color, shape, center_lat, center_lon, radius_m, polygon,
                           enabled, notify_on_enter, notify_on_exit, dwell_minutes, severity, hysteresis_m,
                           applies_to_all_devices, created_by,
                           bbox_min_lat, bbox_max_lat, bbox_min_lon, bbox_max_lon)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0, 0, 0)
                       RETURNING id""",
                    (
                        tenant_id, body.name.strip(), body.description, body.color or "#037dfe", body.shape,
                        body.center_lat, body.center_lon, body.radius_m,
                        json.dumps(body.polygon) if body.polygon is not None else None,
                        True if body.enabled is None else body.enabled,
                        True if body.notify_on_enter is None else body.notify_on_enter,
                        True if body.notify_on_exit is None else body.notify_on_exit,
                        body.dwell_minutes, body.severity or "info",
                        20 if body.hysteresis_m is None else body.hysteresis_m,
                        True if body.applies_to_all_devices is None else body.applies_to_all_devices,
                        user.user_id,
                    ),
                )
            ).fetchone()
            geofence_id = row[0]
            if body.device_ids:
                await _replace_devices(conn, geofence_id, tenant_id, body.device_ids)
    except (UniqueViolation, InsufficientPrivilege, ProgramLimitExceeded, CheckViolation, InvalidParameterValue,
            ForeignKeyViolation) as exc:
        raise _db_error(exc)
    return await _fetch_one(conn, geofence_id)


@router.get("/{geofence_id}", response_model=GeofenceOut)
async def get_geofence(
    geofence_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
) -> GeofenceOut:
    return await _fetch_one(conn, geofence_id)


# Columns a PATCH may touch -- same as the per-column GRANT UPDATE in 0052
# (never bbox_*/tenant_id).
_NULLABLE_UPDATE_FIELDS = {"description", "dwell_minutes"}
_UPDATE_FIELDS = (
    "name", "description", "color", "enabled", "notify_on_enter", "notify_on_exit",
    "dwell_minutes", "severity", "hysteresis_m", "applies_to_all_devices",
)


@router.patch("/{geofence_id}", response_model=GeofenceOut)
async def update_geofence(
    geofence_id: uuid.UUID,
    body: GeofenceUpdate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> GeofenceOut:
    sets: list[str] = []
    params: list[object] = []
    for field in _UPDATE_FIELDS:
        if field not in body.model_fields_set:
            continue
        value = getattr(body, field)
        if value is None and field not in _NULLABLE_UPDATE_FIELDS:
            continue
        if field == "name":
            value = value.strip()
        sets.append(f"{field} = %s")
        params.append(value)
    if body.shape is not None:
        # Geometry is replaced WHOLE (the validator already requires it
        # complete): a circle never keeps a stale polygon.
        sets += ["shape = %s", "center_lat = %s", "center_lon = %s", "radius_m = %s", "polygon = %s::jsonb"]
        params += [
            body.shape, body.center_lat, body.center_lon, body.radius_m,
            json.dumps(body.polygon) if body.polygon is not None else None,
        ]

    try:
        async with conn.transaction():
            row = await (
                await conn.execute("SELECT tenant_id FROM geofences WHERE id = %s FOR UPDATE", (geofence_id,))
            ).fetchone()
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "geofence not found")
            tenant_id = row[0]
            if sets:
                updated = await (
                    await conn.execute(
                        f"UPDATE geofences SET {', '.join(sets)} WHERE id = %s RETURNING id",
                        [*params, geofence_id],
                    )
                ).fetchone()
                if updated is None:
                    # Visible (SELECT) but rejected by the UPDATE RLS policy.
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "you do not have permission to edit this geofence")
            if body.device_ids is not None:
                await _replace_devices(conn, geofence_id, tenant_id, body.device_ids)
    except (UniqueViolation, InsufficientPrivilege, ProgramLimitExceeded, CheckViolation, InvalidParameterValue,
            ForeignKeyViolation) as exc:
        raise _db_error(exc)
    return await _fetch_one(conn, geofence_id)


@router.delete("/{geofence_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_geofence(
    geofence_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> None:
    # CASCADE removes scope and state; geofence_events keeps the history
    # (geofence_id -> NULL, geofence_name snapshot) for reports.
    deleted = await (await conn.execute("DELETE FROM geofences WHERE id = %s RETURNING id", (geofence_id,))).fetchone()
    if deleted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "geofence not found")


@router.get("/{geofence_id}/occupancy", response_model=list[GeofenceOccupant])
async def geofence_occupancy(
    geofence_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
) -> list[GeofenceOccupant]:
    exists = await (await conn.execute("SELECT 1 FROM geofences WHERE id = %s", (geofence_id,))).fetchone()
    if exists is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "geofence not found")
    rows = await (
        await conn.execute(
            """SELECT s.device_id, d.label, s.entered_at, s.entry_estimated
               FROM geofence_device_state s JOIN devices d ON d.id = s.device_id
               WHERE s.geofence_id = %s AND s.inside
               ORDER BY s.entered_at DESC NULLS LAST""",
            (geofence_id,),
        )
    ).fetchall()
    return [
        GeofenceOccupant(
            device_id=r[0], device_label=r[1], entered_at=r[2].isoformat() if r[2] else None, entry_estimated=r[3]
        )
        for r in rows
    ]
