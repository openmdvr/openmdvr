"""GT06 CONFIGURATION commands (SERVER/APN/TIMEZONE/UPLOAD/FILELIST/UPLOADSW/
TIMER/ANGLEREP/SOSALM/COREKITSW/etc., see gt06_config_commands.py for the full
catalog). Same two-short-writes audit pattern as device_commands.py (the audit
row is never in the same transaction as the rest of the request).

Deliberate permission asymmetry, same as "create an API key"
(require_tenant_admin_or_super_admin) vs. "revoke/list it" (require_tenant_admin,
looser): SENDING a command (POST) is `require_super_admin` -- not even
`support` -- because the catalog includes high-risk commands (arbitrary
firmware, redirecting the live video destination, disabling crash/fatigue
sensors). VIEWING the history (GET) stays at `require_bypass` (super_admin or
support) -- auditing what was already sent is legitimate support work without
the risk of firing a new command. Neither is tenant self-service -- these are
provisioning/technical-support commands. The raw text is ALWAYS built
server-side (gt06_config_commands.build_raw_command) from a validated
`command_key` + `params` -- the client never sends free text that goes
straight to the GT06 socket."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from psycopg import AsyncConnection
from psycopg.types.json import Json
from pydantic import ValidationError

from .. import db as db_module
from ..config import Settings
from ..deps import get_db, get_settings_dep, require_bypass, require_super_admin
from ..gt06_config_commands import NO_REPLY_COMMAND_KEYS, build_raw_command
from ..schemas import DeviceConfigCommandCreate, DeviceConfigCommandOut, Page
from ..security import TokenClaims

router = APIRouter(prefix="/devices", tags=["device-config-commands"])
logger = logging.getLogger(__name__)

_SELECT_COLUMNS = """
    c.id, c.device_id, c.command_key, c.params, c.raw_text, c.requested_by, u.email,
    c.requested_at, c.status, c.device_reply, c.completed_at
"""
# LEFT JOIN, not INNER: same as device_commands.py -- a command issued by a
# platform account without tenant_id must not make the row disappear for
# anyone querying it (only bypass sessions reach here anyway, but the pattern
# stays consistent).
_FROM_JOIN = "FROM device_config_commands c LEFT JOIN users u ON u.id = c.requested_by"

# Same sanitization as device_commands.py -- device_reply comes from the
# DEVICE (untrusted input).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_device_reply(text: str | None) -> str | None:
    if not text:
        return None
    return _CONTROL_CHARS_RE.sub("", text) or None


def _command_out(row) -> DeviceConfigCommandOut:
    return DeviceConfigCommandOut(
        id=row[0],
        device_id=row[1],
        command_key=row[2],
        params=row[3],
        raw_text=row[4],
        requested_by=row[5],
        requested_by_email=row[6] or "platform",
        requested_at=row[7].isoformat(),
        status=row[8],
        device_reply=row[9],
        completed_at=row[10].isoformat() if row[10] else None,
    )


@router.post("/{device_id}/config-commands", response_model=DeviceConfigCommandOut)
async def send_device_config_command(
    device_id: uuid.UUID,
    body: DeviceConfigCommandCreate,
    request: Request,
    conn: AsyncConnection = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user: TokenClaims = Depends(require_super_admin),
) -> DeviceConfigCommandOut:
    row = await (
        await conn.execute(
            "SELECT d.gt06_imei, d.protocol, d.tenant_id FROM devices d WHERE d.id = %s AND d.status = 'active'",
            (device_id,),
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found or inactive")
    device_key, protocol, device_tenant_id = row
    if protocol not in ("gt06", "gt06_video"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "this device does not support GT06 configuration commands")

    try:
        raw_text = build_raw_command(body.command_key, body.params, device_key)
    except ValidationError as exc:
        # exc.errors() includes "ctx" by default, which can carry the RAW
        # ValueError object from a field_validator (e.g. _validate_host) --
        # not JSON-serializable, and since HTTPException.detail is serialized
        # as-is, the expected 422 turned into a real 500 ("TypeError: Object of
        # type ValueError is not JSON serializable").
        # include_context=False/include_url=False leave plain text only.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.errors(include_context=False, include_url=False)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    pool = request.app.state.pool

    # 'pending' audit row on its OWN short connection/transaction -- same
    # hardening as device_commands.py.
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as audit_conn:
        command_row = await (
            await audit_conn.execute(
                """INSERT INTO device_config_commands (tenant_id, device_id, command_key, params, raw_text, requested_by)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (device_tenant_id, device_id, body.command_key, Json(body.params), raw_text, user.user_id),
            )
        ).fetchone()
        command_id = command_row[0]

    new_status = "failed"
    device_reply: str | None = None
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{settings.jt1078_bridge_base_url}/api/v1/gt06-raw-command",
                json={"protocol": "gt06", "deviceKey": device_key, "text": raw_text},
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
        if code != 0:
            logger.warning(
                "command server responded code=%s msg=%s for device_id=%s",
                code,
                payload.get("msg"),
                device_id,
            )
        device_reply = _sanitize_device_reply(payload.get("reply"))
        # Unlike device_commands.py (_classify_success, engine): there the
        # reply text MUST be read because the official Concox protocol document
        # says the device can reject DYD#/HFYD# with a real 0x15 ("DYD=Speed
        # Limit or Zero GPS Signal!"). For these configuration commands there
        # is no evidence (documented or observed) of an equivalent
        # reject-via-text -- code=0 (a real reply arrived from the device) is
        # treated as transport success without inspecting the text. Adjust if
        # real hardware shows otherwise.
        if code == 0:
            new_status = "success"
        elif code == 404:
            new_status = "device_offline"
        elif code == 504 and body.command_key in NO_REPLY_COMMAND_KEYS:
            # No reply by design (see NO_REPLY_COMMAND_KEYS): it reached the
            # device; the result shows in whether it reports in again.
            new_status = "success"
            device_reply = "sent (this command has no device reply)"
        elif code == 504:
            new_status = "timeout"
        else:
            new_status = "failed"

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as audit_conn:
        await audit_conn.execute(
            "UPDATE device_config_commands SET status = %s, device_reply = %s, completed_at = now() WHERE id = %s",
            (new_status, device_reply, command_id),
        )
        final_row = await (
            await audit_conn.execute(f"SELECT {_SELECT_COLUMNS} {_FROM_JOIN} WHERE c.id = %s", (command_id,))
        ).fetchone()
    return _command_out(final_row)


@router.get("/{device_id}/config-commands", response_model=Page[DeviceConfigCommandOut])
async def list_device_config_commands(
    device_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_bypass),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> Page[DeviceConfigCommandOut]:
    # device_config_commands RLS is bypass-only in all three directions
    # (0041) -- unlike device_commands.py, no device-ownership check is needed
    # here: whoever gets this far ALREADY has full bypass, there is no
    # "foreign tenant" to isolate from.
    device_row = await (await conn.execute("SELECT id FROM devices WHERE id = %s", (device_id,))).fetchone()
    if device_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")

    where_parts = ["c.device_id = %s"]
    params: list[object] = [device_id]
    if date_from is not None:
        where_parts.append("c.requested_at >= %s")
        params.append(date_from)
    if date_to is not None:
        where_parts.append("c.requested_at <= %s")
        params.append(date_to)
    where = " AND ".join(where_parts)

    total_row = await (await conn.execute(f"SELECT count(*) {_FROM_JOIN} WHERE {where}", params)).fetchone()
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
