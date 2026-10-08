from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from psycopg.errors import CheckViolation

from ..deps import get_db, require_bypass, require_non_driver, require_super_admin, require_tenant_admin
from ..schemas import Page, TenantCreate, TenantOut, TenantSettingsUpdate, TenantUpdate
from ..security import TokenClaims

router = APIRouter(prefix="/tenants", tags=["tenants"])

# Start of the calendar month in UTC, as timestamptz -- same rule as
# db.GetTenantLiveViewSecondsConsumedThisMonth in jt808-server (Go): there is
# no "cycle start" column, the balance simply renews each month. The double AT
# TIME ZONE is the standard Postgres idiom for "midnight of day 1 in UTC" as
# timestamptz without depending on the session time zone.
_MONTH_START_UTC = "date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"


async def _live_view_seconds_consumed_this_month(conn: AsyncConnection, tenant_id: uuid.UUID) -> int:
    """Real month-to-date consumption for ONE tenant -- used by create/update
    (a single row). list_tenants does the same computation for all visible
    tenants in a single aggregate query (see below), it does not call this in
    a loop."""
    row = await (
        await conn.execute(
            f"""SELECT COALESCE(SUM(NULLIF(metadata->>'duration_s', '')::int), 0)
                FROM usage_events_v
                WHERE tenant_id = %s AND event_type = 'live_view' AND "time" >= {_MONTH_START_UTC}""",
            (tenant_id,),
        )
    ).fetchone()
    return row[0] if row else 0


async def _device_quota(conn: AsyncConnection, tenant_id: uuid.UUID) -> tuple[int, int]:
    """Same computation as devices.py::_assert_device_quota_not_exceeded, but
    for BOTH categories at once (camera and GPS quotas are separate pools).
    Exposed on TenantOut so the UI can show "N of M contracted" per category
    BEFORE the 409 on device creation happens."""
    rows = await (
        await conn.execute(
            """SELECT COALESCE(bp.category, tsi.category)::text AS category, SUM(tsi.quantity)
               FROM tenant_subscription_items tsi
               LEFT JOIN billing_plans bp ON bp.id = tsi.billing_plan_id
               WHERE tsi.tenant_id = %s AND tsi.ended_at IS NULL
               GROUP BY 1""",
            (tenant_id,),
        )
    ).fetchall()
    by_category = {category: total for category, total in rows}
    return by_category.get("camera", 0), by_category.get("gps", 0)


_TENANT_COLUMNS = """id, name, status, max_live_view_seconds, live_view_monthly_quota_seconds,
                     display_name, logo_url, meal_break_window_start, meal_break_window_end, max_shift_hours,
                     gps_retention_days, billing_period, webhooks_enabled"""


def _tenant_out(row, consumed: int, camera_device_quota: int, gps_device_quota: int) -> TenantOut:
    """Builds TenantOut from a row with the _TENANT_COLUMNS columns, read in
    create/list/update/settings -- avoids repeating the same construction (and
    the TIME -> "HH:MM" formatting) in four endpoints."""
    (
        tenant_id, name, status_, max_live_view_seconds, live_view_monthly_quota_seconds,
        display_name, logo_url, meal_start, meal_end, max_shift_hours, gps_retention_days, billing_period,
        webhooks_enabled,
    ) = row
    return TenantOut(
        id=tenant_id,
        name=name,
        status=status_,
        max_live_view_seconds=max_live_view_seconds,
        live_view_monthly_quota_seconds=live_view_monthly_quota_seconds,
        live_view_seconds_remaining=live_view_monthly_quota_seconds - consumed,
        display_name=display_name,
        logo_url=logo_url,
        meal_break_window_start=meal_start.strftime("%H:%M") if meal_start else None,
        meal_break_window_end=meal_end.strftime("%H:%M") if meal_end else None,
        max_shift_hours=float(max_shift_hours) if max_shift_hours is not None else None,
        gps_retention_days=gps_retention_days,
        billing_period=billing_period,
        camera_device_quota=camera_device_quota,
        gps_device_quota=gps_device_quota,
        webhooks_enabled=webhooks_enabled,
    )


@router.post("", response_model=TenantOut, status_code=201)
async def create_tenant(
    body: TenantCreate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_super_admin),
) -> TenantOut:
    row = await (
        await conn.execute(
            f"INSERT INTO tenants (name) VALUES (%s) RETURNING {_TENANT_COLUMNS}",
            (body.name,),
        )
    ).fetchone()
    # A newly created tenant has no usage_events yet, but it is computed
    # anyway (instead of assuming 0) so the formula is not duplicated.
    consumed = await _live_view_seconds_consumed_this_month(conn, row[0])
    camera_quota, gps_quota = await _device_quota(conn, row[0])
    return _tenant_out(row, consumed, camera_quota, gps_quota)


@router.get("", response_model=Page[TenantOut])
async def list_tenants(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_non_driver),
    search: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Page[TenantOut]:
    # No tenant_id filter in the query on purpose: RLS handles it -- a normal
    # tenant session can only see its own row (tenants_select in
    # 0008_rls_policies.sql), a bypass session sees all of them. Repeating
    # that filter here would duplicate a security rule in two places that can
    # drift apart. usage_events_v applies the same rule (security_barrier,
    # never the hypertable directly) -- the LEFT JOIN computes the remaining
    # balance of ALL visible tenants in one query, not one per tenant.
    where = "WHERE t.name ILIKE %s" if search else ""
    where_params = [f"%{search}%"] if search else []

    total_row = await (await conn.execute(f"SELECT count(*) FROM tenants t {where}", where_params)).fetchone()
    total = total_row[0] if total_row else 0

    rows = await (
        await conn.execute(
            f"""SELECT t.id, t.name, t.status, t.max_live_view_seconds, t.live_view_monthly_quota_seconds,
                       t.display_name, t.logo_url, t.meal_break_window_start, t.meal_break_window_end,
                       t.max_shift_hours, t.gps_retention_days, t.billing_period, t.webhooks_enabled,
                       COALESCE(u.consumed, 0) AS consumed,
                       COALESCE(q.camera_device_quota, 0) AS camera_device_quota,
                       COALESCE(q.gps_device_quota, 0) AS gps_device_quota
                FROM tenants t
                LEFT JOIN (
                    SELECT tenant_id, SUM(NULLIF(metadata->>'duration_s', '')::int) AS consumed
                    FROM usage_events_v
                    WHERE event_type = 'live_view' AND "time" >= {_MONTH_START_UTC}
                    GROUP BY tenant_id
                ) u ON u.tenant_id = t.id
                LEFT JOIN (
                    SELECT tsi.tenant_id,
                           SUM(tsi.quantity) FILTER (WHERE COALESCE(bp.category, tsi.category) = 'camera') AS camera_device_quota,
                           SUM(tsi.quantity) FILTER (WHERE COALESCE(bp.category, tsi.category) = 'gps') AS gps_device_quota
                    FROM tenant_subscription_items tsi
                    LEFT JOIN billing_plans bp ON bp.id = tsi.billing_plan_id
                    WHERE tsi.ended_at IS NULL
                    GROUP BY tsi.tenant_id
                ) q ON q.tenant_id = t.id
                {where}
                ORDER BY t.name
                LIMIT %s OFFSET %s""",
            [*where_params, limit, offset],
        )
    ).fetchall()
    items = [_tenant_out(r[:13], r[13], r[14], r[15]) for r in rows]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.patch("/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: uuid.UUID,
    body: TenantUpdate,
    conn: AsyncConnection = Depends(get_db),
    # require_bypass (not require_super_admin): adjusting an EXISTING tenant's
    # video limit is support operational work, unlike onboarding a customer
    # (create_tenant), which is super_admin-only -- see api/README.md,
    # require_bypass vs require_super_admin.
    user: TokenClaims = Depends(require_bypass),
) -> TenantOut:
    # webhooks_enabled differs from the other fields of this endpoint:
    # approving the webhooks feature for a tenant is closer to "granting a new
    # credential/capability" than to an operational quota tweak -- same rule
    # as API keys ("support" must not be able to grant something it cannot
    # later review or revoke on its own). Approval is super_admin only, not
    # any platform bypass session.
    if body.webhooks_enabled is not None and user.role != "super_admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "only super_admin can approve/revoke the webhooks feature for a tenant"
        )
    # COALESCE: partial PATCH -- a None field leaves the column untouched
    # instead of overwriting it with NULL (the columns are NOT NULL anyway; the
    # point is not forcing the client to resend every field each time).
    row = await (
        await conn.execute(
            f"""UPDATE tenants
                SET max_live_view_seconds = COALESCE(%s, max_live_view_seconds),
                    live_view_monthly_quota_seconds = COALESCE(%s, live_view_monthly_quota_seconds),
                    gps_retention_days = COALESCE(%s, gps_retention_days),
                    billing_period = COALESCE(%s, billing_period),
                    webhooks_enabled = COALESCE(%s, webhooks_enabled)
                WHERE id = %s
                RETURNING {_TENANT_COLUMNS}""",
            (
                body.max_live_view_seconds, body.live_view_monthly_quota_seconds, body.gps_retention_days,
                body.billing_period, body.webhooks_enabled, tenant_id,
            ),
        )
    ).fetchone()
    if row is None:
        # RLS hides an out-of-scope tenant as if it did not exist (bypass sees
        # all, so this only happens with a genuinely nonexistent id).
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
    consumed = await _live_view_seconds_consumed_this_month(conn, row[0])
    camera_quota, gps_quota = await _device_quota(conn, row[0])
    return _tenant_out(row, consumed, camera_quota, gps_quota)


@router.patch("/{tenant_id}/settings", response_model=TenantOut)
async def update_tenant_settings(
    tenant_id: uuid.UUID,
    body: TenantSettingsUpdate,
    conn: AsyncConnection = Depends(get_db),
    # tenant_admin manages THEIR OWN branding/policy -- never another
    # tenant's, even if they know its id. Bypass (platform) can touch any
    # (e.g. to configure it on the customer's behalf). The `PATCH
    # /tenants/{id}` above is deliberately NOT reused: that one stays
    # bypass-only (quota/billing) and this endpoint can only touch the 5
    # self-service columns, never quota -- two narrow endpoints instead of one
    # wide one with more surface than needed.
    user: TokenClaims = Depends(require_tenant_admin),
) -> TenantOut:
    if not user.is_platform_bypass and str(tenant_id) != user.tenant_id:
        # Same message as "not found" below -- never confirm to a tenant_admin
        # that the id they tried DOES belong to a real tenant.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")

    meal_start = body.meal_break_window_start or None
    meal_end = body.meal_break_window_end or None
    try:
        row = await (
            await conn.execute(
                f"""UPDATE tenants
                    SET display_name = %s, logo_url = %s,
                        meal_break_window_start = %s, meal_break_window_end = %s,
                        max_shift_hours = %s
                    WHERE id = %s
                    RETURNING {_TENANT_COLUMNS}""",
                (body.display_name, body.logo_url, meal_start, meal_end, body.max_shift_hours, tenant_id),
            )
        ).fetchone()
    except CheckViolation:
        # Should not happen (the schema already validates "both or neither"
        # and the max_shift_hours range), but if migration 0017's CHECK ever
        # drifts from the Pydantic validator, an explicit 422 beats a raw 500.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid policy configuration")
    if row is None:
        # Rejected by RLS (out-of-scope tenant) or genuinely nonexistent id --
        # same generic message in both cases.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
    consumed = await _live_view_seconds_consumed_this_month(conn, row[0])
    camera_quota, gps_quota = await _device_quota(conn, row[0])
    return _tenant_out(row, consumed, camera_quota, gps_quota)
