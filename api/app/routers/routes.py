from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from psycopg.errors import InsufficientPrivilege, RaiseException

from ..deps import get_current_user, get_db, require_tenant_admin
from ..schemas import Page, RouteCreate, RouteOut, RouteUpdate
from ..security import TokenClaims

router = APIRouter(prefix="/routes", tags=["routes"])

_SELECT_COLUMNS = """r.id, r.tenant_id, r.name, r.description, r.date, r.driver_id, dr.name,
                      r.vehicle_id, v.plate, r.status"""
_FROM_JOIN = """FROM routes r
                LEFT JOIN drivers dr ON dr.id = r.driver_id
                LEFT JOIN vehicles v ON v.id = r.vehicle_id"""


@router.post("", response_model=RouteOut, status_code=201)
async def create_route(
    body: RouteCreate,
    conn: AsyncConnection = Depends(get_db),
    # tenant_admin self-service, like vehicles/drivers -- assigning routes is
    # dispatch work, not driver self-service (a driver CAN read their own
    # route via GET /routes below, but never create/edit one).
    _: TokenClaims = Depends(require_tenant_admin),
) -> RouteOut:
    try:
        new_id = (
            await (
                await conn.execute(
                    """INSERT INTO routes (tenant_id, name, description, date, driver_id, vehicle_id)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (body.tenant_id, body.name, body.description, body.date, body.driver_id, body.vehicle_id),
                )
            ).fetchone()
        )[0]
    except RaiseException:
        # enforce_driver_tenant_match()/enforce_vehicle_tenant_match()
        # (migrations 0014/0015, reused as-is) reject a driver_id/vehicle_id
        # that does not belong to the route's tenant.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid driver_id or vehicle_id for this tenant")
    except InsufficientPrivilege:
        # RLS already correctly blocks a tenant_id from ANOTHER tenant
        # (fail-closed, no cross-tenant write) -- without this catch the
        # Postgres exception surfaced as a raw 500 instead of the clean 403
        # that vehicles.py/drivers.py/devices.py use for the same case.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you cannot create a route for another tenant")

    row = await (await conn.execute(f"SELECT {_SELECT_COLUMNS} {_FROM_JOIN} WHERE r.id = %s", (new_id,))).fetchone()
    return _route_out(row)


@router.get("", response_model=Page[RouteOut])
async def list_routes(
    conn: AsyncConnection = Depends(get_db),
    # get_current_user, NOT require_non_driver: unlike the fleet endpoints
    # (devices/positions/alarms/etc.), a driver MUST be able to read here.
    # RLS (routes_select, migration 0016) already restricts them to their own
    # assigned routes -- the mechanism behind "your route today" in the driver
    # view, without the backend handing them anything else.
    _: TokenClaims = Depends(get_current_user),
    # dt.date (not str): an unvalidated string reached the WHERE raw, and
    # "zzz" failed with a 500 instead of the clean 422 that
    # /vehicles/{id}/distance and /drivers/{id}/hours return for the same
    # kind of parameter.
    date_from: dt.date | None = Query(None, alias="from"),
    date_to: dt.date | None = Query(None, alias="to"),
    # Optional -- RLS already isolates (by tenant, and by driver for a driver
    # session); this only narrows further within what RLS allows.
    tenant_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Page[RouteOut]:
    where_parts: list[str] = []
    params: list[object] = []
    if date_from is not None:
        where_parts.append("r.date >= %s")
        params.append(date_from)
    if date_to is not None:
        where_parts.append("r.date <= %s")
        params.append(date_to)
    if tenant_id is not None:
        where_parts.append("r.tenant_id = %s")
        params.append(tenant_id)
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    total_row = await (await conn.execute(f"SELECT count(*) {_FROM_JOIN} {where}", params)).fetchone()
    total = total_row[0] if total_row else 0

    rows = await (
        await conn.execute(
            f"SELECT {_SELECT_COLUMNS} {_FROM_JOIN} {where} ORDER BY r.date DESC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
    ).fetchall()
    return Page(items=[_route_out(r) for r in rows], total=total, limit=limit, offset=offset)


@router.patch("/{route_id}", response_model=RouteOut)
async def update_route(
    route_id: uuid.UUID,
    body: RouteUpdate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> RouteOut:
    try:
        updated = await (
            await conn.execute(
                """UPDATE routes
                   SET name = COALESCE(%s, name), description = COALESCE(%s, description),
                       date = COALESCE(%s, date), driver_id = COALESCE(%s, driver_id),
                       vehicle_id = COALESCE(%s, vehicle_id), status = COALESCE(%s, status)
                   WHERE id = %s
                   RETURNING id""",
                (body.name, body.description, body.date, body.driver_id, body.vehicle_id, body.status, route_id),
            )
        ).fetchone()
    except RaiseException:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid driver_id or vehicle_id for this tenant")
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "route not found")

    row = await (await conn.execute(f"SELECT {_SELECT_COLUMNS} {_FROM_JOIN} WHERE r.id = %s", (route_id,))).fetchone()
    return _route_out(row)


def _route_out(row) -> RouteOut:
    return RouteOut(
        id=row[0], tenant_id=row[1], name=row[2], description=row[3],
        date=row[4].isoformat(), driver_id=row[5], driver_name=row[6],
        vehicle_id=row[7], vehicle_plate=row[8], status=row[9],
    )
