"""Tenant device groups -- used to assign visibility and notifications to
several users at once (see infra/postgres/migrations/
0031_device_groups_and_assignments.sql). Creating/editing/deleting a group, or
changing its members, is tenant_admin work (self-service, like vehicles/
drivers) -- RLS already isolates by tenant, this only narrows by role."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from psycopg.errors import InsufficientPrivilege, RaiseException, UniqueViolation

from ..deps import get_db, require_non_driver, require_tenant_admin
from ..schemas import DeviceGroupCreate, DeviceGroupMembersUpdate, DeviceGroupOut, DeviceGroupUpdate, Page
from ..security import TokenClaims

router = APIRouter(prefix="/device-groups", tags=["device-groups"])

_SELECT_WITH_COUNT = """SELECT g.id, g.tenant_id, g.name, count(m.device_id)
                         FROM device_groups g
                         LEFT JOIN device_group_members m ON m.device_group_id = g.id"""


@router.post("", response_model=DeviceGroupOut, status_code=201)
async def create_device_group(
    body: DeviceGroupCreate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> DeviceGroupOut:
    try:
        row = await (
            await conn.execute(
                "INSERT INTO device_groups (tenant_id, name) VALUES (%s, %s) RETURNING id, tenant_id, name",
                (body.tenant_id, body.name),
            )
        ).fetchone()
    except UniqueViolation:
        raise HTTPException(status.HTTP_409_CONFLICT, "a group with that name already exists in this tenant")
    except InsufficientPrivilege:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you cannot create a group for another tenant")
    return DeviceGroupOut(id=row[0], tenant_id=row[1], name=row[2], device_count=0)


@router.get("", response_model=Page[DeviceGroupOut])
async def list_device_groups(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
    search: str | None = Query(None, max_length=200),
    tenant_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Page[DeviceGroupOut]:
    where_parts: list[str] = []
    params: list[object] = []
    if search:
        where_parts.append("g.name ILIKE %s")
        params.append(f"%{search}%")
    if tenant_id is not None:
        where_parts.append("g.tenant_id = %s")
        params.append(tenant_id)
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    total_row = await (await conn.execute(f"SELECT count(*) FROM device_groups g {where}", params)).fetchone()
    total = total_row[0] if total_row else 0

    rows = await (
        await conn.execute(
            f"""{_SELECT_WITH_COUNT} {where}
                GROUP BY g.id ORDER BY g.name LIMIT %s OFFSET %s""",
            [*params, limit, offset],
        )
    ).fetchall()
    items = [DeviceGroupOut(id=r[0], tenant_id=r[1], name=r[2], device_count=r[3]) for r in rows]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.patch("/{group_id}", response_model=DeviceGroupOut)
async def update_device_group(
    group_id: uuid.UUID,
    body: DeviceGroupUpdate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> DeviceGroupOut:
    try:
        row = await (
            await conn.execute(
                "UPDATE device_groups SET name = %s WHERE id = %s RETURNING id, tenant_id, name",
                (body.name, group_id),
            )
        ).fetchone()
    except UniqueViolation:
        raise HTTPException(status.HTTP_409_CONFLICT, "a group with that name already exists in this tenant")
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "group not found")
    count_row = await (
        await conn.execute("SELECT count(*) FROM device_group_members WHERE device_group_id = %s", (group_id,))
    ).fetchone()
    return DeviceGroupOut(id=row[0], tenant_id=row[1], name=row[2], device_count=count_row[0] if count_row else 0)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_device_group(
    group_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> None:
    # ON DELETE CASCADE on device_group_members/user_device_group_assignments
    # (migration 0031) already cleans up memberships and user assignments --
    # deleting a group never leaves orphaned references.
    deleted = await (await conn.execute("DELETE FROM device_groups WHERE id = %s RETURNING id", (group_id,))).fetchone()
    if deleted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "group not found")


@router.get("/{group_id}/members", response_model=list[uuid.UUID])
async def list_device_group_members(
    group_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    # require_tenant_admin, not require_non_driver: this returns raw
    # device_ids without going through app_can_view_device -- a
    # tenant_operator/tenant_viewer with no access to any of those devices
    # could still enumerate their real UUIDs this way (not exploitable on its
    # own, since /devices/{id}, /alarms?device_id=, POST video, etc. still
    # filter, but it was the input that made another real finding practical).
    _: TokenClaims = Depends(require_tenant_admin),
) -> list[uuid.UUID]:
    group_row = await (await conn.execute("SELECT id FROM device_groups WHERE id = %s", (group_id,))).fetchone()
    if group_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "group not found")
    rows = await (
        await conn.execute("SELECT device_id FROM device_group_members WHERE device_group_id = %s", (group_id,))
    ).fetchall()
    return [r[0] for r in rows]


@router.put("/{group_id}/members", response_model=list[uuid.UUID])
async def replace_device_group_members(
    group_id: uuid.UUID,
    body: DeviceGroupMembersUpdate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> list[uuid.UUID]:
    async with conn.transaction():
        group_row = await (
            await conn.execute("SELECT tenant_id FROM device_groups WHERE id = %s", (group_id,))
        ).fetchone()
        if group_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "group not found")
        tenant_id = group_row[0]

        await conn.execute("DELETE FROM device_group_members WHERE device_group_id = %s", (group_id,))
        try:
            for device_id in dict.fromkeys(body.device_ids):  # dedupe preserving order
                await conn.execute(
                    "INSERT INTO device_group_members (device_group_id, device_id, tenant_id) VALUES (%s, %s, %s)",
                    (group_id, device_id, tenant_id),
                )
        except RaiseException:
            # enforce_device_group_member_tenant_match() (migration 0031)
            # rejects a device_id that does not belong to THIS tenant.
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "one of the devices does not belong to this tenant")

    rows = await (
        await conn.execute("SELECT device_id FROM device_group_members WHERE device_group_id = %s", (group_id,))
    ).fetchall()
    return [r[0] for r in rows]
