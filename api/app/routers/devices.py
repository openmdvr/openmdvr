from __future__ import annotations

import datetime as dt
import logging
import math
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from psycopg import AsyncConnection
from psycopg.errors import CheckViolation, QueryCanceled, RaiseException, UniqueViolation

from ..deps import get_db, require_bypass, require_non_driver, require_super_admin
from ..schemas import (
    DeviceCreate,
    DeviceModelCreate,
    DeviceModelOut,
    DeviceOut,
    DevicePosition,
    DeviceProtocol,
    DeviceUpdate,
    Page,
    RouteHistoryEvent,
    RouteHistoryPoint,
    RouteHistoryReport,
)
from ..security import TokenClaims
from .. import limits

logger = logging.getLogger(__name__)

# A jt808 device (camera/MDVR) consumes quota from the 'camera' billing_plans
# category; a gt06 (GPS-only) consumes 'gps' -- independent quotas.
# gt06_video (Jimi IoT JC261/JC400: GT06 telemetry + RTMP video) is also
# 'camera' -- it shares the quota/cost pool with jt808, being a camera from a
# business standpoint even though its video transport differs. 'addon' is
# never a device type and does not appear here.
_PROTOCOL_TO_CATEGORY: dict[str, str] = {"jt808": "camera", "gt06": "gps", "gt06_video": "camera"}

router = APIRouter(prefix="/devices", tags=["devices"])

# device_model_name: scalar subquery instead of a JOIN -- works the same in a
# plain SELECT and in INSERT/UPDATE ... RETURNING (Postgres allows subqueries
# there, not a JOIN), so _SELECT_COLUMNS is reused as-is in all three places.
_SELECT_COLUMNS = (
    "id, tenant_id, protocol, jt808_terminal_id, gt06_imei, label, vehicle_id, notes, status, last_seen_at, "
    "status_changed_by, status_changed_at, device_model_id, "
    "(SELECT dm.name FROM device_models dm WHERE dm.id = devices.device_model_id) AS device_model_name, "
    "sim_number, sim_carrier, "
    "ignition_on, ignition_changed_at, power_connected, power_changed_at"
)


# /models BEFORE /{device_id} on purpose -- Starlette tries routes in
# registration order; if /{device_id} (device_id: uuid.UUID) came first,
# "GET /devices/models" would try to coerce "models" to a UUID and fail with a
# 422 before even considering this literal route.
@router.get("/models", response_model=list[DeviceModelOut])
async def list_device_models(
    conn: AsyncConnection = Depends(get_db),
    # bypass-only: only the platform creates/edits devices (create_device/
    # update_device are already require_bypass), so only the platform needs
    # this catalog -- no tenant session queries it directly (the resolved
    # name already travels in DeviceOut.device_model_name).
    _: TokenClaims = Depends(require_bypass),
) -> list[DeviceModelOut]:
    rows = await (
        await conn.execute("SELECT id, name, protocol, notes, created_at FROM device_models ORDER BY name")
    ).fetchall()
    return [
        DeviceModelOut(id=r[0], name=r[1], protocol=r[2], notes=r[3], created_at=r[4].isoformat()) for r in rows
    ]


@router.post("/models", response_model=DeviceModelOut, status_code=201)
async def create_device_model(
    body: DeviceModelCreate,
    conn: AsyncConnection = Depends(get_db),
    # require_super_admin, not require_bypass -- adding a new model to the
    # catalog is a product decision (same as creating a billing_plan), not
    # day-to-day operational work that support should do.
    _: TokenClaims = Depends(require_super_admin),
) -> DeviceModelOut:
    try:
        row = await (
            await conn.execute(
                "INSERT INTO device_models (name, protocol, notes) VALUES (%s, %s, %s) "
                "RETURNING id, name, protocol, notes, created_at",
                (body.name, body.protocol, body.notes),
            )
        ).fetchone()
    except UniqueViolation:
        raise HTTPException(status.HTTP_409_CONFLICT, "a model with that name already exists")
    return DeviceModelOut(id=row[0], name=row[1], protocol=row[2], notes=row[3], created_at=row[4].isoformat())


async def _assert_device_quota_not_exceeded(
    conn: AsyncConnection, tenant_id: uuid.UUID, protocol: DeviceProtocol
) -> None:
    """Quota PER CATEGORY = SUM of quantity of the tenant's ACTIVE
    tenant_subscription_items lines (ended_at IS NULL) whose category matches
    this device's protocol -- 'jt808' consumes 'camera' quota, 'gt06'
    consumes 'gps'. A plan line's category comes from billing_plans.category;
    a custom line (no billing_plan_id) carries its own explicit `category`
    column (see migration 0027). A tenant with no active line of that category
    has ZERO quota -- on purpose, not "unlimited": provisioning must never get
    ahead of what was contracted.

    `used` is counted per CATEGORY (all protocols sharing that category), not
    per exact protocol: counting only protocol = %s would let a tenant whose
    jt808 devices already fill its 'camera' quota add a gt06_video anyway (and
    vice versa), because each protocol would be counted as a separate pool
    even though they share the same subscription line."""
    category = _PROTOCOL_TO_CATEGORY[protocol]
    protocols_in_category = [p for p, c in _PROTOCOL_TO_CATEGORY.items() if c == category]
    row = await (
        await conn.execute(
            """SELECT
                   (SELECT COALESCE(SUM(tsi.quantity), 0)
                    FROM tenant_subscription_items tsi
                    LEFT JOIN billing_plans bp ON bp.id = tsi.billing_plan_id
                    WHERE tsi.tenant_id = %s AND tsi.ended_at IS NULL
                      AND COALESCE(bp.category, tsi.category)::text = %s) AS quota,
                   (SELECT count(*) FROM devices
                    WHERE tenant_id = %s AND status = 'active' AND protocol = ANY(%s)) AS used""",
            (tenant_id, category, tenant_id, protocols_in_category),
        )
    ).fetchone()
    quota, used = row
    if used >= quota:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"this tenant already has {used} of {quota} contracted devices ({category}) -- "
            "increase its subscription before adding another",
        )


@router.post("", response_model=DeviceOut, status_code=201)
async def create_device(
    body: DeviceCreate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_bypass),
) -> DeviceOut:
    # Creating devices is a platform action, never tenant self-service (see
    # the extended note in infra/postgres/migrations/0008_rls_policies.sql on
    # why: real physical hardware installation, and it keeps the
    # jt808_terminal_id uniqueness error from becoming a cross-tenant oracle).
    await _assert_device_quota_not_exceeded(conn, body.tenant_id, body.protocol)
    try:
        row = await (
            await conn.execute(
                f"""INSERT INTO devices (tenant_id, protocol, jt808_terminal_id, gt06_imei, label, vehicle_id, notes,
                                          device_model_id, sim_number, sim_carrier)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING {_SELECT_COLUMNS}""",
                (
                    body.tenant_id,
                    body.protocol,
                    body.jt808_terminal_id,
                    body.gt06_imei,
                    body.label,
                    body.vehicle_id,
                    body.notes,
                    body.device_model_id,
                    body.sim_number,
                    body.sim_carrier,
                ),
            )
        ).fetchone()
    except UniqueViolation:
        raise HTTPException(status.HTTP_409_CONFLICT, "a device with that identifier already exists (jt808_terminal_id/gt06_imei)")
    except CheckViolation:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid device identifier")
    except RaiseException:
        # enforce_vehicle_tenant_match() (migration 0014) rejects a vehicle_id
        # that does not belong to this device's tenant_id.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid vehicle_id for this tenant")

    return _device_out(row)


@router.get("", response_model=Page[DeviceOut])
async def list_devices(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
    search: str | None = Query(None, max_length=200),
    # Optional -- RLS already isolates, this only narrows further (see
    # users.py::list_users for the same rule).
    tenant_id: uuid.UUID | None = Query(None),
    # Operational views (map, units, live...) hide deactivated units. Opt-in
    # so other consumers (API keys) keep receiving what they already did.
    exclude_inactive: bool = Query(False),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Page[DeviceOut]:
    # Search by label/terminal -- plate/driver live in vehicles/drivers (see
    # vehicles.py/drivers.py to search those fields). RLS already filters by
    # tenant before this WHERE, so counting/paging only narrows an ALREADY
    # isolated result.
    where_parts: list[str] = []
    params: list[object] = []
    if search:
        where_parts.append("(label ILIKE %s OR jt808_terminal_id ILIKE %s OR gt06_imei ILIKE %s)")
        like = f"%{search}%"
        params.extend([like, like, like])
    if tenant_id is not None:
        where_parts.append("tenant_id = %s")
        params.append(tenant_id)
    if exclude_inactive:
        where_parts.append("status <> 'inactive'")
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    total_row = await (await conn.execute(f"SELECT count(*) FROM devices {where}", params)).fetchone()
    total = total_row[0] if total_row else 0

    rows = await (
        await conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM devices {where} ORDER BY label LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
    ).fetchall()
    return Page(items=[_device_out(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{device_id}", response_model=DeviceOut)
async def get_device(
    device_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
) -> DeviceOut:
    row = await (await conn.execute(f"SELECT {_SELECT_COLUMNS} FROM devices WHERE id = %s", (device_id,))).fetchone()
    if row is None:
        # RLS already hid another tenant's device as if it did not exist --
        # this 404 is genuinely "does not exist (for you)" and does not reveal
        # whether the id belongs to another tenant. See the IDOR test in tests/.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")
    return _device_out(row)


@router.patch("/{device_id}", response_model=DeviceOut)
async def update_device(
    device_id: uuid.UUID,
    body: DeviceUpdate,
    conn: AsyncConnection = Depends(get_db),
    # Same require_bypass as create_device: reinstalling in another vehicle is
    # the same platform boundary as creating it, not a new permission
    # decision. tenant_id and jt808_terminal_id are not editable through this
    # endpoint (see DeviceUpdate).
    admin: TokenClaims = Depends(require_bypass),
) -> DeviceOut:
    # gt06<->gt06_video protocol change (see DeviceUpdate.protocol): the IMEI
    # does not change, only the classification -- but the quota/billing
    # category DOES change (gps -> camera or vice versa), so the DESTINATION
    # category's quota must be checked before applying, as in create_device.
    # The source category is freed automatically (this device stops counting
    # there once the UPDATE commits), so no symmetric check is needed.
    if body.protocol is not None:
        current = await (
            await conn.execute("SELECT protocol::text, tenant_id FROM devices WHERE id = %s", (device_id,))
        ).fetchone()
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")
        current_protocol, device_tenant_id = current
        if current_protocol not in ("gt06", "gt06_video"):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "protocol change only applies between gt06 and gt06_video (same IMEI) -- "
                "a jt808 device requires removal and a new registration",
            )
        if body.protocol != current_protocol:
            await _assert_device_quota_not_exceeded(conn, device_tenant_id, body.protocol)

    # Boolean computed in Python (not "%s IS NOT NULL" in SQL): a NULL
    # parameter with no other typed operand fails with
    # "IndeterminateDatatype". Same pattern as the manual webhook re-enable.
    status_provided = body.status is not None
    try:
        row = await (
            await conn.execute(
                f"""UPDATE devices
                    SET label = COALESCE(%s, label),
                        vehicle_id = COALESCE(%s, vehicle_id),
                        notes = COALESCE(%s, notes),
                        status = COALESCE(%s, status),
                        protocol = COALESCE(%s, protocol),
                        device_model_id = COALESCE(%s, device_model_id),
                        sim_number = COALESCE(%s, sim_number),
                        sim_carrier = COALESCE(%s, sim_carrier),
                        sim_plan_cost_mxn_month = COALESCE(%s, sim_plan_cost_mxn_month),
                        sim_plan_data_cap_mb = COALESCE(%s, sim_plan_data_cap_mb),
                        status_changed_by = CASE WHEN %s THEN %s ELSE status_changed_by END,
                        status_changed_at = CASE WHEN %s THEN now() ELSE status_changed_at END
                    WHERE id = %s
                    RETURNING {_SELECT_COLUMNS}""",
                (
                    body.label, body.vehicle_id, body.notes, body.status, body.protocol,
                    body.device_model_id, body.sim_number, body.sim_carrier,
                    body.sim_plan_cost_mxn_month, body.sim_plan_data_cap_mb,
                    status_provided, admin.user_id, status_provided,
                    device_id,
                ),
            )
        ).fetchone()
    except RaiseException:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid vehicle_id for this tenant")
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")
    return _device_out(row)


@router.get("/{device_id}/positions", response_model=list[DevicePosition])
async def device_position_history(
    device_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
    minutes: int = Query(60, ge=1, le=1440),  # 24h cap to bound query cost
) -> list[DevicePosition]:
    # A device's trail for the map -- as in positions.py, gps_positions_v
    # (security_barrier) already filters by tenant without a manual WHERE; the
    # device_id filter here is only "which device", not an ownership check
    # (RLS already does that: another tenant's device does not show up, same
    # pattern as get_device above).
    rows = await (
        await conn.execute(
            """SELECT p.device_id, d.label, p.lat, p.lon, p.speed_kmh, p.heading, p.time
               FROM gps_positions_v p
               JOIN devices d ON d.id = p.device_id
               WHERE p.device_id = %s AND p.time > now() - (%s || ' minutes')::interval
               ORDER BY p.time ASC""",
            (device_id, minutes),
        )
    ).fetchall()
    return [
        DevicePosition(
            device_id=r[0], label=r[1], lat=r[2], lon=r[3], speed_kmh=r[4], heading=r[5], time=r[6].isoformat()
        )
        for r in rows
    ]


# Route history -- a user must never be able to take the system down by
# running a report. See docs/architecture.md for the full design.
#
# Same window cap as the existing distance/hours reports (vehicles.py) --
# consistency, not a new invented number.
_MAX_ROUTE_HISTORY_DAYS = limits.ROUTE_HISTORY_MAX_DAYS
# Unlike those reports (which sum ALL raw points in Python, acceptable because
# the result is a single number per day), this endpoint returns the full ROUTE
# to draw on the map -- loading hundreds of thousands of raw rows into the app
# and only then trimming is exactly the pattern that can take the system down.
# Compression happens INSIDE Postgres via time_bucket()+last() (native
# TimescaleDB functions, nothing new to install) -- Postgres never
# materializes more than ~max_points rows toward the app, however many raw
# positions exist in the requested window.
_MIN_ROUTE_POINTS = 100
_MAX_ROUTE_POINTS = limits.ROUTE_HISTORY_MAX_POINTS
_DEFAULT_ROUTE_POINTS = 1500
# Events (alarms) ARE truly truncated beyond this -- they are real discrete
# occurrences and cannot be "compressed" without losing information (unlike
# position points).
_MIN_ROUTE_EVENTS = 10
_MAX_ROUTE_EVENTS = limits.ROUTE_HISTORY_MAX_EVENTS
_DEFAULT_ROUTE_EVENTS = 200
# Final backstop, independent of the caps above -- protects against a
# pathological case (e.g. a device with corrupt data producing far more rows
# than expected for its real reporting interval) that would still fit within
# the allowed window/points. Transaction-local: it only applies to THIS
# request's transaction, never to other pool connections.
_ROUTE_QUERY_STATEMENT_TIMEOUT_MS = limits.REPORT_QUERY_TIMEOUT_MS
# Each alarm is correlated with the GPS position closest in time within this
# window -- alarms has no lat/lon of its own (see 0007_timeseries_tables.sql).
# 5 minutes is generous versus the typical reporting interval (seconds to
# minutes), without risking a pin in the wrong place from too loose a
# correlation.
_ALARM_POSITION_CORRELATION_WINDOW_SECONDS = 300


@router.get("/{device_id}/route-history", response_model=RouteHistoryReport)
async def device_route_history(
    device_id: uuid.UUID,
    request: Request,
    conn: AsyncConnection = Depends(get_db),
    user: TokenClaims = Depends(require_non_driver),
    date_from: dt.datetime = Query(..., alias="from"),
    date_to: dt.datetime = Query(..., alias="to"),
    max_points: int = Query(_DEFAULT_ROUTE_POINTS, ge=_MIN_ROUTE_POINTS, le=_MAX_ROUTE_POINTS),
    max_events: int = Query(_DEFAULT_ROUTE_EVENTS, ge=_MIN_ROUTE_EVENTS, le=_MAX_ROUTE_EVENTS),
) -> RouteHistoryReport:
    if date_to <= date_from:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "'to' must be later than 'from'")
    if (date_to - date_from) > dt.timedelta(days=_MAX_ROUTE_HISTORY_DAYS):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"the history window cannot exceed {_MAX_ROUTE_HISTORY_DAYS} days",
        )

    # Extra layer against MANY cheap requests in a row (see
    # main.py::route_history_rate_limiter) -- separate from the window/points
    # cap, which bounds the cost of ONE request.
    limiter = getattr(request.app.state, "route_history_rate_limiter", None)
    if limiter is not None and not limiter.allow(str(user.user_id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many history queries, please wait a moment")

    range_seconds = (date_to - date_from).total_seconds()
    bucket_seconds = max(1, math.ceil(range_seconds / max_points))

    try:
        # set_config(..., true) instead of an interpolated `SET LOCAL` literal
        # -- same parameterized pattern db.py uses for session GUCs, never
        # hand-built SQL text even for a fixed in-code constant.
        await conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(_ROUTE_QUERY_STATEMENT_TIMEOUT_MS),))

        point_rows = await (
            await conn.execute(
                """SELECT last(p.time, p.time), last(p.lat, p.time), last(p.lon, p.time),
                          last(p.speed_kmh, p.time), last(p.heading, p.time)
                   FROM gps_positions_v p
                   WHERE p.device_id = %s AND p.time >= %s AND p.time < %s
                   GROUP BY time_bucket(%s::interval, p.time)
                   ORDER BY 1 ASC""",
                (device_id, date_from, date_to, f"{bucket_seconds} seconds"),
            )
        ).fetchall()

        event_rows = await (
            await conn.execute(
                """SELECT a.id, a.time, a.alarm_type, a.severity, pos.lat, pos.lon,
                          a.video_evidence_key IS NOT NULL AS has_video_clip
                   FROM alarms_v a
                   LEFT JOIN LATERAL (
                       SELECT gp.lat, gp.lon
                       FROM gps_positions_v gp
                       WHERE gp.device_id = a.device_id
                         AND gp.time BETWEEN a.time - (%s || ' seconds')::interval
                                          AND a.time + (%s || ' seconds')::interval
                       ORDER BY abs(extract(epoch FROM (gp.time - a.time)))
                       LIMIT 1
                   ) pos ON true
                   WHERE a.device_id = %s AND a.time >= %s AND a.time < %s
                   ORDER BY a.time ASC
                   LIMIT %s""",
                (
                    _ALARM_POSITION_CORRELATION_WINDOW_SECONDS,
                    _ALARM_POSITION_CORRELATION_WINDOW_SECONDS,
                    device_id,
                    date_from,
                    date_to,
                    max_events + 1,
                ),
            )
        ).fetchall()
    except QueryCanceled:
        # statement_timeout fired -- never leave the connection/client hanging,
        # and never a raw 500. The client should narrow the range or lower
        # max_points, not retry the same query as-is.
        logger.warning("device_route_history: query cancelled by statement_timeout, device_id=%s", device_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the query took too long -- reduce the date range or the number of points",
        )

    events_truncated = len(event_rows) > max_events
    event_rows = event_rows[:max_events]

    return RouteHistoryReport(
        device_id=device_id,
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        points=[
            RouteHistoryPoint(time=r[0].isoformat(), lat=r[1], lon=r[2], speed_kmh=r[3], heading=r[4])
            for r in point_rows
        ],
        bucket_seconds=bucket_seconds,
        events=[
            RouteHistoryEvent(
                id=r[0], time=r[1].isoformat(), alarm_type=r[2], severity=r[3], lat=r[4], lon=r[5],
                has_video_clip=r[6],
            )
            for r in event_rows
        ],
        events_truncated=events_truncated,
    )


def _device_out(row) -> DeviceOut:
    return DeviceOut(
        id=row[0],
        tenant_id=row[1],
        protocol=row[2],
        jt808_terminal_id=row[3],
        gt06_imei=row[4],
        label=row[5],
        vehicle_id=row[6],
        notes=row[7],
        status=row[8],
        last_seen_at=row[9].isoformat() if row[9] else None,
        status_changed_by=row[10],
        status_changed_at=row[11].isoformat() if row[11] else None,
        device_model_id=row[12],
        device_model_name=row[13],
        sim_number=row[14],
        sim_carrier=row[15],
        ignition_on=row[16],
        ignition_changed_at=row[17].isoformat() if row[17] else None,
        power_connected=row[18],
        power_changed_at=row[19].isoformat() if row[19] else None,
    )
