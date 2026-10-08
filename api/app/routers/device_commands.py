"""Remote commands to devices (engine cut/restore, GT06 today) -- the most
dangerous endpoint in the project: cutting fuel to a real vehicle. Same
pattern as video.py (verify ownership via RLS, then talk to jt808-server over
its trusted internal API, no auth of its own -- docker network), but STRICTER
on permission (require_tenant_admin, not require_non_driver) and with a real
audit trail (device_commands table, 0029_device_commands.sql): who asked for
what, when, and what the device ACTUALLY replied, never just "it was sent".

command_type is protocol-agnostic (see schemas.py) -- this router only maps
protocol -> internal host (GT06 lives in the same jt808-server process already
reachable at jt1078_bridge_base_url) and knows nothing about any protocol's
wire format."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from psycopg import AsyncConnection

from .. import db as db_module
from ..config import Settings
from ..deps import get_db, get_settings_dep, require_non_driver, require_tenant_admin
from ..schemas import DeviceCommandCreate, DeviceCommandOut, Page
from ..security import TokenClaims

router = APIRouter(prefix="/devices", tags=["device-commands"])
logger = logging.getLogger(__name__)

_SELECT_COLUMNS = """
    c.id, c.device_id, c.command_type, c.requested_by, u.email, c.requested_at,
    c.status, c.device_reply, c.completed_at
"""
# LEFT JOIN, not INNER: a command issued by a PLATFORM account
# (super_admin/support, tenant_id NULL in users) fails the users_select RLS
# policy for a normal tenant session -- with INNER JOIN the whole
# device_commands row vanished from THAT tenant's history even though the
# command really ran on one of its devices. LEFT JOIN keeps the row;
# _command_out covers the resulting NULL email.
_FROM_JOIN = "FROM device_commands c LEFT JOIN users u ON u.id = c.requested_by"

# device_reply comes from the DEVICE (untrusted input) -- strip NUL and other
# control bytes before it touches the database. A raw NUL in device_reply made
# the final UPDATE fail with psycopg.errors.DataError (PostgreSQL does not
# support NUL in text), and since that exception escaped inside the SAME
# transaction as the whole request, it also rolled back the 'pending' INSERT
# of step 2 -- the audit record that step promised would survive any later
# failure did not. This sanitization, plus putting each write on its own short
# connection/transaction (see below), closes both sides of the issue.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_device_reply(text: str | None) -> str | None:
    if not text:
        return None
    return _CONTROL_CHARS_RE.sub("", text) or None


def _classify_success(device_reply: str | None) -> bool:
    """code=0 from the Go server only confirms that a REAL 0x15 arrived --
    never that the device accepted the command. Its own speed/GPS-fix
    guardrail (section 6.4 of the official protocol document) can reject the
    cut and still answer with a real 0x15 (e.g. "DYD=Speed Limit or Zero GPS
    Signal!"). With no reply text, success is assumed (optimistic default);
    with text, it only counts as success if the device itself says "success"
    -- anything else is treated as a rejection, never as success by default."""
    if not device_reply:
        return True
    return "success" in device_reply.lower()


def _command_out(row) -> DeviceCommandOut:
    return DeviceCommandOut(
        id=row[0],
        device_id=row[1],
        command_type=row[2],
        requested_by=row[3],
        requested_by_email=row[4] or "platform",
        requested_at=row[5].isoformat(),
        status=row[6],
        device_reply=row[7],
        completed_at=row[8].isoformat() if row[8] else None,
    )


@router.post("/{device_id}/commands", response_model=DeviceCommandOut)
async def send_device_command(
    device_id: uuid.UUID,
    body: DeviceCommandCreate,
    request: Request,
    conn: AsyncConnection = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user: TokenClaims = Depends(require_tenant_admin),
) -> DeviceCommandOut:
    # Step 1: does this device exist FOR THIS USER? Same pattern as video.py
    # -- the query runs on the RLS-scoped connection, another tenant's device
    # does not show up.
    row = await (
        await conn.execute(
            "SELECT d.gt06_imei, d.protocol, d.tenant_id FROM devices d WHERE d.id = %s AND d.status = 'active'",
            (device_id,),
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found or inactive")
    device_key, protocol, device_tenant_id = row
    if protocol != "gt06":
        # Only GT06 has remote commands implemented today (a JT808 camera has
        # no fuel-cut relay) -- clean 400, mirror of the gate in video.py.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "this device does not support remote commands")

    pool = request.app.state.pool

    # Step 2: 'pending' audit row on its OWN short connection/transaction --
    # COMMITTED immediately, independent of the request-wide transaction
    # opened by get_db (see db.py). Otherwise a failure in step 4 rolls this
    # insert back too, so "if the process dies between steps 2 and 3 a real
    # record remains" would not hold. bypass=True: tenant_id was already
    # resolved above via RLS on the real device; this new connection does not
    # need to repeat the user's session context for a single authorized write.
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as audit_conn:
        command_row = await (
            await audit_conn.execute(
                """INSERT INTO device_commands (tenant_id, device_id, command_type, requested_by)
                   VALUES (%s, %s, %s, %s)
                   RETURNING id""",
                (device_tenant_id, device_id, body.command_type, user.user_id),
            )
        ).fetchone()
        command_id = command_row[0]

    # Step 3: ask the Go server for the command over its internal API (trusted
    # network, never exposed to the browser -- same host:port video.py uses
    # for the bridge, the SAME jt808-server process). No database transaction
    # stays open during this call (up to 20s), so no pool connection
    # (max_size=10) is tied up for that long.
    new_status = "failed"
    device_reply: str | None = None
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{settings.jt1078_bridge_base_url}/api/v1/commands",
                json={"protocol": protocol, "deviceKey": device_key, "commandType": body.command_type},
            )
        payload = resp.json()
    except httpx.HTTPError:
        logger.exception("failed to reach the command server for device_id=%s", device_id)
        new_status = "failed"
    except ValueError:
        logger.exception("non-JSON response from the command server for device_id=%s", device_id)
        new_status = "failed"
    else:
        code = payload.get("code")
        # The Go server's "msg" is for ITS own logs (same as the bridge in
        # video.py) -- never forwarded to the client as-is, only logged
        # server-side.
        if code != 0:
            logger.warning(
                "command server responded code=%s msg=%s for device_id=%s",
                code,
                payload.get("msg"),
                device_id,
            )
        device_reply = _sanitize_device_reply(payload.get("reply"))
        if code == 0:
            # See _classify_success: code=0 only confirms a real 0x15
            # arrived, never that the device accepted the command.
            new_status = "success" if _classify_success(device_reply) else "failed"
        elif code == 404:
            new_status = "device_offline"
        elif code == 504:
            new_status = "timeout"
        else:
            new_status = "failed"

    # Step 4: complete the audit row with the REAL result -- on its OWN short
    # connection/transaction, like step 2 -- if anything fails here
    # (device_reply is already sanitized, but just in case) the 'pending' row
    # from step 2 is already durable and never disappears. Migration 0029's
    # trigger prevents reopening a finished command (defense in depth; this
    # path only completes each row once).
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as audit_conn:
        await audit_conn.execute(
            "UPDATE device_commands SET status = %s, device_reply = %s, completed_at = now() WHERE id = %s",
            (new_status, device_reply, command_id),
        )
        final_row = await (
            await audit_conn.execute(f"SELECT {_SELECT_COLUMNS} {_FROM_JOIN} WHERE c.id = %s", (command_id,))
        ).fetchone()
    return _command_out(final_row)


@router.get("/{device_id}/commands", response_model=Page[DeviceCommandOut])
async def list_device_commands(
    device_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
    # Full datetime (UTC instant), NOT date: Postgres runs in UTC but users
    # perceive "days" in their local time zone. With a plain date interpreted
    # in UTC, "search today" returned commands from hours before the user's
    # local day and excluded the last hours of it. The frontend computes the
    # start/end of that day in the browser's time zone and sends the exact UTC
    # instant -- this endpoint never guesses a time zone, and stays typed
    # (datetime, not str) to reject garbage with a clean 422, same as
    # routes.py.
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> Page[DeviceCommandOut]:
    # Does this device exist FOR THIS USER? Same pattern as video.py /
    # send_device_command above -- the query runs on get_db's RLS-scoped
    # connection, so another tenant's device OR one of THIS tenant that the
    # session is not assigned (app_can_view_device) does not show up. Without
    # this check, device_commands (tenant-wide RLS only,
    # 0029_device_commands.sql) would expose the full engine-command history
    # -- the most dangerous action in the project -- of any device in the
    # tenant, including one not assigned to this session.
    device_row = await (await conn.execute("SELECT id FROM devices WHERE id = %s", (device_id,))).fetchone()
    if device_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")

    # Pagination + date filter -- same pattern (count + LIMIT/OFFSET) as
    # GET /devices. date_from/date_to are instants ALREADY resolved by the
    # frontend (start/end of the day in its local time zone) -- both bounds
    # inclusive, no date arithmetic on this side.
    where_parts = ["c.device_id = %s"]
    params: list[object] = [device_id]
    if date_from is not None:
        where_parts.append("c.requested_at >= %s")
        params.append(date_from)
    if date_to is not None:
        where_parts.append("c.requested_at <= %s")
        params.append(date_to)
    where = " AND ".join(where_parts)

    total_row = await (
        await conn.execute(f"SELECT count(*) {_FROM_JOIN} WHERE {where}", params)
    ).fetchone()
    total = total_row[0] if total_row else 0

    rows = await (
        await conn.execute(
            f"""SELECT {_SELECT_COLUMNS} {_FROM_JOIN}
                WHERE {where}
                ORDER BY c.requested_at DESC
                LIMIT %s OFFSET %s""",
            [*params, limit, offset],
        )
    ).fetchall()
    return Page(items=[_command_out(r) for r in rows], total=total, limit=limit, offset=offset)
