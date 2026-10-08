"""Billing.

Stage 1: plan catalog (`billing_plans`) + what each tenant has contracted
(`tenant_subscription_items`). See 0020_billing_catalog.sql for the full
RLS rationale.

Stage 2: promotions (`tenant_promotions`) + invoices (`invoices`/
`invoice_line_items`, snapshotted by the `generate_invoices` job). See
0021_billing_invoices.sql (including why `invoices` IS readable by the
tenant itself, unlike everything else in this router).

Stage 3: payments (`payments`) + automatic service suspension. See
0022_billing_payments.sql (schema/RLS/`enforce_billing_suspension` job)
and `api/app/payments.py` (`PaymentProvider` is ALWAYS the entry point for
recording a payment, never a direct INSERT in this router).

Stage 4: estimated cost and margin (`platform_billing_settings` +
`GET /billing/profitability`). See 0023_billing_cost_estimation.sql.
Neither the cost assumptions nor the profitability report are EVER
reachable by a tenant_admin.

Stage 5: dashboards. The only new backend piece is
`GET /billing/my-subscription`, the narrow view of a tenant's own
subscription (see that function below). The rest of the tenant/platform
billing pages reuse existing endpoints: `GET /billing/invoices` and
`GET /billing/payments` (both `require_tenant_admin`, which already
resolve "mine" via RLS without a parameter).

Access deliberately differs per action (same rule documented in
api/README.md for require_bypass vs require_super_admin):
- Create/edit a catalog PLAN: require_super_admin. It is a product
  decision (which SKUs exist and at what list price), like creating a
  tenant.
- Add/edit/end a tenant line, create a promotion, void an invoice, record
  a payment: require_bypass. Day-to-day support work, same as adjusting
  an existing tenant's max_live_view_seconds.
- Read invoices, payments, or the tenant's OWN subscription
  (`GET /billing/invoices`, `GET /billing/payments`,
  `GET /billing/my-subscription`): require_tenant_admin (not
  require_non_driver: financial data, tenant_operator/tenant_viewer cannot
  reach it), with no manual tenant_id filter. RLS isolates (a tenant sees
  only its own rows, bypass sees all, optionally filtered by the tenant_id
  query param). These are the only sub-resources of this router a
  tenant_admin can reach.
- Everything else (catalog, subscriptions, promotions): NOT reachable by a
  tenant_admin, not even for reading. RLS already blocks billing_plans/
  tenant_promotions without exception, and tenant_subscription_items is
  also gated explicitly in the API (the tenant's own resolved view lives
  in /my-subscription)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from psycopg.errors import CheckViolation, ForeignKeyViolation, InsufficientPrivilege, NumericValueOutOfRange, UniqueViolation

from ..deps import get_db, require_bypass, require_super_admin, require_tenant_admin
from ..payments import DEFAULT_PROVIDER
from ..schemas import (
    BillingPlanCreate,
    BillingPlanOut,
    BillingPlanUpdate,
    DeviceDataUsageMonthOut,
    DeviceDataUsageOut,
    InvoiceLineItemOut,
    InvoiceOut,
    InvoiceStatus,
    Page,
    PaymentCreate,
    PaymentOut,
    PlatformBillingSettingsOut,
    PlatformBillingSettingsUpdate,
    TenantPromotionCreate,
    TenantPromotionOut,
    TenantProfitabilityOut,
    TenantSubscriptionItemCreate,
    TenantSubscriptionItemOut,
    TenantSubscriptionItemUpdate,
)
from ..security import TokenClaims

router = APIRouter(prefix="/billing", tags=["billing"])

# Start of the calendar month in UTC. Same idiom as tenants.py uses for
# monthly live-view consumption (_MONTH_START_UTC); reused here for "bytes
# transferred THIS month" in the estimated-cost calculation.
_MONTH_START_UTC = "date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"


def _plan_out(row) -> BillingPlanOut:
    return BillingPlanOut(
        id=row[0], name=row[1], sku=row[2], category=row[3],
        unit_price=float(row[4]), currency=row[5], billing_period=row[6], active=row[7],
    )


_PLAN_COLUMNS = "id, name, sku, category, unit_price, currency, billing_period, active"


@router.post("/plans", response_model=BillingPlanOut, status_code=201)
async def create_billing_plan(
    body: BillingPlanCreate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_super_admin),
) -> BillingPlanOut:
    try:
        row = await (
            await conn.execute(
                f"""INSERT INTO billing_plans (name, sku, category, unit_price, currency, billing_period)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING {_PLAN_COLUMNS}""",
                (body.name, body.sku, body.category, body.unit_price, body.currency, body.billing_period),
            )
        ).fetchone()
    except UniqueViolation:
        raise HTTPException(status.HTTP_409_CONFLICT, f"a plan with sku '{body.sku}' already exists")
    except CheckViolation:
        # Whitespace-only name/sku pass Pydantic's min_length (it counts
        # characters) but violate the migration's CHECK btrim(...) <> ''.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name and sku cannot be empty")
    except InsufficientPrivilege:
        # billing_plans_insert WITH CHECK requires app_bypass_rls(). This
        # should never fail here (the endpoint is already
        # require_super_admin), but RLS is the real last line of defense,
        # not just the dependency.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not authorized to create plans")
    return _plan_out(row)


@router.get("/plans", response_model=Page[BillingPlanOut])
async def list_billing_plans(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_bypass),
    active_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page[BillingPlanOut]:
    where = "WHERE active = true" if active_only else ""
    total_row = await (await conn.execute(f"SELECT count(*) FROM billing_plans {where}")).fetchone()
    total = total_row[0] if total_row else 0
    rows = await (
        await conn.execute(
            f"SELECT {_PLAN_COLUMNS} FROM billing_plans {where} ORDER BY category, name LIMIT %s OFFSET %s",
            (limit, offset),
        )
    ).fetchall()
    return Page(items=[_plan_out(r) for r in rows], total=total, limit=limit, offset=offset)


@router.patch("/plans/{plan_id}", response_model=BillingPlanOut)
async def update_billing_plan(
    plan_id: uuid.UUID,
    body: BillingPlanUpdate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_super_admin),
) -> BillingPlanOut:
    try:
        row = await (
            await conn.execute(
                f"""UPDATE billing_plans
                    SET name = COALESCE(%s, name), unit_price = COALESCE(%s, unit_price),
                        billing_period = COALESCE(%s, billing_period), active = COALESCE(%s, active)
                    WHERE id = %s
                    RETURNING {_PLAN_COLUMNS}""",
                (body.name, body.unit_price, body.billing_period, body.active, plan_id),
            )
        ).fetchone()
    except CheckViolation:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name cannot be empty")
    except InsufficientPrivilege:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not authorized to edit plans")
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plan not found")
    return _plan_out(row)


_ITEM_COLUMNS = """si.id, si.tenant_id, si.billing_plan_id, bp.name, bp.sku, si.custom_description,
                    si.quantity, si.unit_price_override, COALESCE(si.unit_price_override, bp.unit_price),
                    si.started_at, si.ended_at"""
_ITEM_FROM_JOIN = "FROM tenant_subscription_items si LEFT JOIN billing_plans bp ON bp.id = si.billing_plan_id"


def _item_out(row) -> TenantSubscriptionItemOut:
    return TenantSubscriptionItemOut(
        id=row[0], tenant_id=row[1], billing_plan_id=row[2], plan_name=row[3], plan_sku=row[4],
        custom_description=row[5], quantity=row[6],
        unit_price_override=float(row[7]) if row[7] is not None else None,
        effective_unit_price=float(row[8]) if row[8] is not None else 0.0,
        started_at=row[9].isoformat(), ended_at=row[10].isoformat() if row[10] else None,
    )


@router.post("/subscription-items", response_model=TenantSubscriptionItemOut, status_code=201)
async def create_subscription_item(
    body: TenantSubscriptionItemCreate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_bypass),
) -> TenantSubscriptionItemOut:
    try:
        new_id = (
            await (
                await conn.execute(
                    """INSERT INTO tenant_subscription_items
                           (tenant_id, billing_plan_id, custom_description, category, quantity, unit_price_override)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (
                        body.tenant_id,
                        body.billing_plan_id,
                        body.custom_description,
                        body.category,
                        body.quantity,
                        body.unit_price_override,
                    ),
                )
            ).fetchone()
        )[0]
    except ForeignKeyViolation:
        # tenant_id or billing_plan_id does not exist. The
        # tenant_subscription_items_insert policy only requires bypass and
        # does not check that the tenant exists (the FK does), so this is
        # the only real error path for an invalid id.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid tenant_id or billing_plan_id")
    except CheckViolation:
        # Neither billing_plan_id nor custom_description, or neither
        # billing_plan_id nor category (the two CHECKs that require something
        # when there is no plan). Clean 422 instead of a raw 500. Pydantic
        # already validates both; this catch is the last line.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "this line requires billing_plan_id, or custom_description and category",
        )
    except InsufficientPrivilege:
        # tenant_subscription_items_insert WITH CHECK requires
        # app_bypass_rls(). Should never fail here (require_bypass already
        # enforces it), but RLS is the real last line of defense.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not authorized to create this subscription line")

    row = await (await conn.execute(f"SELECT {_ITEM_COLUMNS} {_ITEM_FROM_JOIN} WHERE si.id = %s", (new_id,))).fetchone()
    return _item_out(row)


@router.get("/subscription-items", response_model=Page[TenantSubscriptionItemOut])
async def list_subscription_items(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_bypass),
    tenant_id: uuid.UUID | None = Query(None),
    active_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Page[TenantSubscriptionItemOut]:
    where_parts: list[str] = []
    params: list[object] = []
    if tenant_id is not None:
        where_parts.append("si.tenant_id = %s")
        params.append(tenant_id)
    if active_only:
        where_parts.append("si.ended_at IS NULL")
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    total_row = await (await conn.execute(f"SELECT count(*) {_ITEM_FROM_JOIN} {where}", params)).fetchone()
    total = total_row[0] if total_row else 0
    rows = await (
        await conn.execute(
            f"SELECT {_ITEM_COLUMNS} {_ITEM_FROM_JOIN} {where} ORDER BY si.started_at DESC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
    ).fetchall()
    return Page(items=[_item_out(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/my-subscription", response_model=list[TenantSubscriptionItemOut])
async def my_subscription(
    conn: AsyncConnection = Depends(get_db),
    user: TokenClaims = Depends(require_tenant_admin),
) -> list[TenantSubscriptionItemOut]:
    """Narrow view of the caller's own subscription. A tenant CAN read its
    own tenant_subscription_items rows via RLS, but a JOIN against
    billing_plans from that session returns nothing. Deliberately a new,
    narrow endpoint instead of widening `GET /subscription-items` (which
    stays bypass-only for the support/platform view), same rule as
    `PATCH /tenants/{id}/settings` vs `PATCH /tenants/{id}`. No `tenant_id`
    parameter: it always resolves the caller's own tenant, never another."""
    if user.is_platform_bypass:
        # A platform session has no "own" tenant. This endpoint is for a
        # real tenant_admin; the platform view already has
        # GET /subscription-items?tenant_id=... for the same purpose.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "this route is only for a tenant session")
    # Does NOT use _ITEM_FROM_JOIN (direct LEFT JOIN billing_plans): that
    # table is bypass-only under RLS (0020_billing_catalog.sql), so from a
    # tenant session the JOIN returns nothing (name/unit_price come back
    # NULL, effective_unit_price ends up 0 -- verified, not assumed).
    # resolve_billing_plan_public() (SECURITY DEFINER,
    # 0024_billing_resolve_plan.sql) resolves each line's plan without that
    # block. Safe because tenant_subscription_items is ALREADY filtered by
    # normal RLS (WHERE si.tenant_id = %s, this tenant), so the
    # billing_plan_id passed in is always one this tenant legitimately
    # has contracted.
    rows = await (
        await conn.execute(
            """SELECT si.id, si.tenant_id, si.billing_plan_id, rp.name, rp.sku, si.custom_description,
                      si.quantity, si.unit_price_override, COALESCE(si.unit_price_override, rp.unit_price),
                      si.started_at, si.ended_at
               FROM tenant_subscription_items si
               LEFT JOIN LATERAL resolve_billing_plan_public(si.billing_plan_id) rp
                   ON si.billing_plan_id IS NOT NULL
               WHERE si.tenant_id = %s
               ORDER BY si.started_at DESC""",
            (user.tenant_id,),
        )
    ).fetchall()
    return [_item_out(r) for r in rows]


@router.patch("/subscription-items/{item_id}", response_model=TenantSubscriptionItemOut)
async def update_subscription_item(
    item_id: uuid.UUID,
    body: TenantSubscriptionItemUpdate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_bypass),
) -> TenantSubscriptionItemOut:
    ended_at_value = "now()" if body.end_now else "ended_at"
    updated = await (
        await conn.execute(
            f"""UPDATE tenant_subscription_items
                SET quantity = COALESCE(%s, quantity),
                    unit_price_override = COALESCE(%s, unit_price_override),
                    ended_at = {ended_at_value}
                WHERE id = %s
                RETURNING id""",
            (body.quantity, body.unit_price_override, item_id),
        )
    ).fetchone()
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subscription line not found")

    row = await (await conn.execute(f"SELECT {_ITEM_COLUMNS} {_ITEM_FROM_JOIN} WHERE si.id = %s", (item_id,))).fetchone()
    return _item_out(row)


# ---------------------------------------------------------------------------
# tenant_promotions -- bypass-only in both directions (see the module
# docstring and 0021_billing_invoices.sql). No PATCH: shortening a promotion
# means deciding what happens to invoices already generated inside its
# window, deliberately out of scope. Creating a new one with a different
# window covers the real use case.
# ---------------------------------------------------------------------------
def _promotion_out(row) -> TenantPromotionOut:
    return TenantPromotionOut(
        id=row[0], tenant_id=row[1], description=row[2],
        starts_at=row[3].isoformat(), ends_at=row[4].isoformat(),
        discount_type=row[5], discount_value=float(row[6]),
    )


_PROMOTION_COLUMNS = "id, tenant_id, description, starts_at, ends_at, discount_type, discount_value"


@router.post("/promotions", response_model=TenantPromotionOut, status_code=201)
async def create_promotion(
    body: TenantPromotionCreate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_bypass),
) -> TenantPromotionOut:
    try:
        row = await (
            await conn.execute(
                f"""INSERT INTO tenant_promotions (tenant_id, description, starts_at, ends_at, discount_type, discount_value)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING {_PROMOTION_COLUMNS}""",
                (body.tenant_id, body.description, body.starts_at, body.ends_at, body.discount_type, body.discount_value),
            )
        ).fetchone()
    except ForeignKeyViolation:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid tenant_id")
    except CheckViolation:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid date range or discount")
    except InsufficientPrivilege:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not authorized to create promotions")
    return _promotion_out(row)


@router.get("/promotions", response_model=Page[TenantPromotionOut])
async def list_promotions(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_bypass),
    tenant_id: uuid.UUID | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Page[TenantPromotionOut]:
    where = "WHERE tenant_id = %s" if tenant_id is not None else ""
    params = [tenant_id] if tenant_id is not None else []
    total_row = await (await conn.execute(f"SELECT count(*) FROM tenant_promotions {where}", params)).fetchone()
    total = total_row[0] if total_row else 0
    rows = await (
        await conn.execute(
            f"SELECT {_PROMOTION_COLUMNS} FROM tenant_promotions {where} ORDER BY starts_at DESC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
    ).fetchall()
    return Page(items=[_promotion_out(r) for r in rows], total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# invoices -- generated by the generate_invoices() job
# (0021_billing_invoices.sql), snapshotted, never edited except to void
# them. SELECT uses require_tenant_admin (not require_bypass nor
# require_non_driver): financial data, deliberately narrower than "any
# tenant role" -- tenant_operator/tenant_viewer cannot reach it. RLS does
# the real isolation: a tenant_admin sees only its own invoices without
# passing tenant_id; bypass sees all and can filter.
# ---------------------------------------------------------------------------
_INVOICE_COLUMNS = "id, tenant_id, period_start, period_end, issued_at, due_date, subtotal, discount_total, total, currency, status"


def _invoice_out(row, line_items: list[InvoiceLineItemOut]) -> InvoiceOut:
    return InvoiceOut(
        id=row[0], tenant_id=row[1],
        period_start=row[2].isoformat(), period_end=row[3].isoformat(),
        issued_at=row[4].isoformat(), due_date=row[5].isoformat(),
        subtotal=float(row[6]), discount_total=float(row[7]), total=float(row[8]),
        currency=row[9], status=row[10], line_items=line_items,
    )


async def _line_items_for(conn: AsyncConnection, invoice_id: uuid.UUID) -> list[InvoiceLineItemOut]:
    rows = await (
        await conn.execute(
            "SELECT id, description, quantity, unit_price, subtotal FROM invoice_line_items WHERE invoice_id = %s ORDER BY description",
            (invoice_id,),
        )
    ).fetchall()
    return [
        InvoiceLineItemOut(id=r[0], description=r[1], quantity=r[2], unit_price=float(r[3]), subtotal=float(r[4]))
        for r in rows
    ]


@router.get("/invoices", response_model=Page[InvoiceOut])
async def list_invoices(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
    tenant_id: uuid.UUID | None = Query(None),
    # InvoiceStatus (Literal), not str: a value outside the 5 options would
    # raise an uncaught Postgres error when compared against the
    # invoice_status enum column (found in security review). Typed as a
    # Literal, FastAPI rejects it with 422 before it reaches SQL.
    status_filter: InvoiceStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page[InvoiceOut]:
    # No manual tenant_id filter beyond the query param: invoices_select
    # (RLS) already narrows a tenant session to its own invoices; this only
    # narrows further within what RLS allows -- same rule as list_tenants.
    where_parts: list[str] = []
    params: list[object] = []
    if tenant_id is not None:
        where_parts.append("tenant_id = %s")
        params.append(tenant_id)
    if status_filter is not None:
        where_parts.append("status = %s")
        params.append(status_filter)
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    total_row = await (await conn.execute(f"SELECT count(*) FROM invoices {where}", params)).fetchone()
    total = total_row[0] if total_row else 0
    rows = await (
        await conn.execute(
            f"SELECT {_INVOICE_COLUMNS} FROM invoices {where} ORDER BY issued_at DESC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
    ).fetchall()
    items = [_invoice_out(r, await _line_items_for(conn, r[0])) for r in rows]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> InvoiceOut:
    row = await (await conn.execute(f"SELECT {_INVOICE_COLUMNS} FROM invoices WHERE id = %s", (invoice_id,))).fetchone()
    if row is None:
        # RLS hides another tenant's invoice as if it did not exist. Same
        # generic message in both cases: never confirm that the id belongs
        # to a real invoice of ANOTHER tenant.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invoice not found")
    return _invoice_out(row, await _line_items_for(conn, invoice_id))


@router.post("/invoices/{invoice_id}/void", response_model=InvoiceOut)
async def void_invoice(
    invoice_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_bypass),
) -> InvoiceOut:
    # FOR UPDATE: without it, two concurrent POST /void could both read the
    # same "issued" state before either writes. Harmless today (both end in
    # 'void'), but defense in depth consistent with the rest of the codebase
    # (robustness finding from security review).
    row = await (
        await conn.execute(f"SELECT {_INVOICE_COLUMNS} FROM invoices WHERE id = %s FOR UPDATE", (invoice_id,))
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invoice not found")
    if row[10] == "paid":
        # A paid invoice is never voided. That calls for a credit note/
        # refund (out of scope), not a status change that would erase the
        # record that it was collected.
        raise HTTPException(status.HTTP_409_CONFLICT, "a paid invoice cannot be voided")
    if row[10] == "void":
        raise HTTPException(status.HTTP_409_CONFLICT, "this invoice is already void")

    updated = await (
        await conn.execute(
            f"UPDATE invoices SET status = 'void' WHERE id = %s RETURNING {_INVOICE_COLUMNS}", (invoice_id,)
        )
    ).fetchone()
    return _invoice_out(updated, await _line_items_for(conn, invoice_id))


# ---------------------------------------------------------------------------
# payments -- ALWAYS via DEFAULT_PROVIDER.record_payment()
# (api/app/payments.py), never a direct INSERT here. That single place
# decides whether the invoice becomes 'paid' and whether the tenant is
# reactivated, so this logic lives in one spot regardless of the HTTP path.
# Recording is bypass-only (require_bypass, support work); READING an
# invoice's payments is require_tenant_admin -- same as invoices, a tenant
# must be able to see which payments were credited.
# ---------------------------------------------------------------------------
_PAYMENT_COLUMNS = "id, invoice_id, tenant_id, amount, method, received_at, recorded_by, reference_note, external_provider, external_payment_id"
_PAYMENT_COLUMNS_P = """p.id, p.invoice_id, p.tenant_id, p.amount, p.method, p.received_at,
                         p.recorded_by, p.reference_note, p.external_provider, p.external_payment_id"""


def _payment_out(row, invoice_status: str) -> PaymentOut:
    return PaymentOut(
        id=row[0], invoice_id=row[1], tenant_id=row[2], amount=float(row[3]), method=row[4],
        received_at=row[5].isoformat(), recorded_by=row[6], reference_note=row[7],
        external_provider=row[8], external_payment_id=row[9], invoice_status=invoice_status,
    )


@router.post("/payments", response_model=PaymentOut, status_code=201)
async def create_payment(
    body: PaymentCreate,
    conn: AsyncConnection = Depends(get_db),
    user: TokenClaims = Depends(require_bypass),
) -> PaymentOut:
    invoice_row = await (
        await conn.execute("SELECT tenant_id, status FROM invoices WHERE id = %s FOR UPDATE", (body.invoice_id,))
    ).fetchone()
    if invoice_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invoice not found")
    tenant_id, invoice_status = invoice_row
    if invoice_status in ("paid", "void"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"this invoice is already {invoice_status} and accepts no more payments")

    result = await DEFAULT_PROVIDER.record_payment(
        conn,
        invoice_id=body.invoice_id,
        tenant_id=tenant_id,
        amount=body.amount,
        method=body.method,
        recorded_by=uuid.UUID(user.user_id),
        reference_note=body.reference_note,
    )
    row = await (await conn.execute(f"SELECT {_PAYMENT_COLUMNS} FROM payments WHERE id = %s", (result.payment_id,))).fetchone()
    return _payment_out(row, result.invoice_status)


@router.get("/payments", response_model=Page[PaymentOut])
async def list_payments(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
    invoice_id: uuid.UUID | None = Query(None),
    tenant_id: uuid.UUID | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page[PaymentOut]:
    where_parts: list[str] = []
    params: list[object] = []
    if invoice_id is not None:
        where_parts.append("p.invoice_id = %s")
        params.append(invoice_id)
    if tenant_id is not None:
        where_parts.append("p.tenant_id = %s")
        params.append(tenant_id)
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    total_row = await (await conn.execute(f"SELECT count(*) FROM payments p {where}", params)).fetchone()
    total = total_row[0] if total_row else 0
    rows = await (
        await conn.execute(
            f"""SELECT {_PAYMENT_COLUMNS_P}, i.status
                FROM payments p JOIN invoices i ON i.id = p.invoice_id
                {where}
                ORDER BY p.received_at DESC LIMIT %s OFFSET %s""",
            [*params, limit, offset],
        )
    ).fetchall()
    items = [_payment_out(r[:10], r[10]) for r in rows]
    return Page(items=items, total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# platform_billing_settings + estimated profitability per tenant.
# Bypass-only in both directions, no exceptions: neither the cost
# assumptions nor the profitability report may ever reach a tenant session.
# Reading the assumptions: require_bypass (support needs to know what feeds
# the report it also reads). EDITING them: require_super_admin (what the
# business assumes each camera costs is a product decision, like the price
# catalog).
# ---------------------------------------------------------------------------
def _settings_out(row) -> PlatformBillingSettingsOut:
    return PlatformBillingSettingsOut(
        cost_usd_per_device_month=float(row[0]), cost_usd_per_gps_device_month=float(row[1]),
        cost_usd_per_gb=float(row[2]), exchange_rate_mxn_per_usd=float(row[3]), updated_at=row[4].isoformat(),
    )


_SETTINGS_COLUMNS = (
    "cost_usd_per_device_month, cost_usd_per_gps_device_month, cost_usd_per_gb, exchange_rate_mxn_per_usd, updated_at"
)


@router.get("/settings", response_model=PlatformBillingSettingsOut)
async def get_billing_settings(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_bypass),
) -> PlatformBillingSettingsOut:
    row = await (await conn.execute(f"SELECT {_SETTINGS_COLUMNS} FROM platform_billing_settings")).fetchone()
    return _settings_out(row)


@router.patch("/settings", response_model=PlatformBillingSettingsOut)
async def update_billing_settings(
    body: PlatformBillingSettingsUpdate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_super_admin),
) -> PlatformBillingSettingsOut:
    try:
        row = await (
            await conn.execute(
                f"""UPDATE platform_billing_settings
                    SET cost_usd_per_device_month = COALESCE(%s, cost_usd_per_device_month),
                        cost_usd_per_gps_device_month = COALESCE(%s, cost_usd_per_gps_device_month),
                        cost_usd_per_gb = COALESCE(%s, cost_usd_per_gb),
                        exchange_rate_mxn_per_usd = COALESCE(%s, exchange_rate_mxn_per_usd)
                    RETURNING {_SETTINGS_COLUMNS}""",
                (
                    body.cost_usd_per_device_month,
                    body.cost_usd_per_gps_device_month,
                    body.cost_usd_per_gb,
                    body.exchange_rate_mxn_per_usd,
                ),
            )
        ).fetchone()
    except NumericValueOutOfRange:
        # Defense in depth, not the real barrier (that is
        # _MAX_COST_SETTING in schemas.py, calibrated to these columns'
        # NUMERIC(10,4)). If that limit ever drifts from the column again,
        # a clean 422 beats the raw 500 found in security review.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "value out of range for cost assumptions")
    return _settings_out(row)


def _profitability_out(row) -> TenantProfitabilityOut:
    return TenantProfitabilityOut(
        tenant_id=row[0], tenant_name=row[1], active_devices=row[2], bytes_this_month=row[3],
        estimated_cost_usd=float(row[4]), estimated_cost_mxn=float(row[5]),
        monthly_revenue_mxn=float(row[6]), margin_mxn=float(row[7]),
        margin_pct=float(row[8]) if row[8] is not None else None,
    )


@router.get("/profitability", response_model=Page[TenantProfitabilityOut])
async def list_tenant_profitability(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_bypass),
    tenant_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page[TenantProfitabilityOut]:
    # CTEs: active devices, bytes transferred THIS calendar month
    # (usage_events_v, the security_barrier view -- never the hypertable
    # directly, see 0009_timeseries_access.sql), and the EQUIVALENT monthly
    # revenue of active subscription lines (a semiannual/annual line is
    # normalized /6 or /12 to compare against an inherently monthly cost).
    # Estimated cost uses the platform_billing_settings assumptions (single
    # row, CROSS JOIN) and is converted to MXN with exchange_rate_mxn_per_usd
    # (a MANUAL rate, see 0023_billing_cost_estimation.sql) so the margin is
    # a subtraction of two figures in the SAME currency.
    where = "WHERE base.tenant_id = %s" if tenant_id is not None else ""
    params: list[object] = [tenant_id] if tenant_id is not None else []

    base_cte = f"""
        WITH dev AS (
            -- Split by protocol: a gt06 device (GPS-only, no video) costs
            -- far less to operate than a jt808 one (camera); a single
            -- per-device rate would inflate the estimated cost of a GT06
            -- fleet. active_devices in TenantProfitabilityOut stays the
            -- combined TOTAL; only the internal cost calculation uses
            -- different rates. gt06_video (Jimi IoT JC261/JC400) is a
            -- camera for cost purposes even though its video transport is
            -- RTMP rather than JT1078, so it counts with jt808.
            SELECT tenant_id,
                   count(*) FILTER (WHERE protocol IN ('jt808', 'gt06_video')) AS active_camera_devices,
                   count(*) FILTER (WHERE protocol = 'gt06') AS active_gps_devices
            FROM devices WHERE status = 'active' GROUP BY tenant_id
        ), usage AS (
            SELECT tenant_id, SUM(bytes_transferred) AS bytes_this_month
            FROM usage_events_v WHERE "time" >= {_MONTH_START_UTC}
            GROUP BY tenant_id
        ), rev AS (
            SELECT si.tenant_id,
                   SUM(si.quantity * COALESCE(si.unit_price_override, bp.unit_price))
                       / (CASE MAX(t2.billing_period) WHEN 'monthly' THEN 1 WHEN 'semiannual' THEN 6 WHEN 'annual' THEN 12 END)
                       AS monthly_revenue_mxn
            FROM tenant_subscription_items si
            LEFT JOIN billing_plans bp ON bp.id = si.billing_plan_id
            JOIN tenants t2 ON t2.id = si.tenant_id
            WHERE si.ended_at IS NULL
            GROUP BY si.tenant_id
        ), base AS (
            SELECT
                t.id AS tenant_id, t.name AS tenant_name,
                COALESCE(dev.active_camera_devices, 0) + COALESCE(dev.active_gps_devices, 0) AS active_devices,
                COALESCE(usage.bytes_this_month, 0) AS bytes_this_month,
                (COALESCE(dev.active_camera_devices, 0) * s.cost_usd_per_device_month
                    + COALESCE(dev.active_gps_devices, 0) * s.cost_usd_per_gps_device_month
                    + (COALESCE(usage.bytes_this_month, 0) / 1073741824.0) * s.cost_usd_per_gb) AS estimated_cost_usd,
                (COALESCE(dev.active_camera_devices, 0) * s.cost_usd_per_device_month
                    + COALESCE(dev.active_gps_devices, 0) * s.cost_usd_per_gps_device_month
                    + (COALESCE(usage.bytes_this_month, 0) / 1073741824.0) * s.cost_usd_per_gb) * s.exchange_rate_mxn_per_usd
                    AS estimated_cost_mxn,
                COALESCE(rev.monthly_revenue_mxn, 0) AS monthly_revenue_mxn
            FROM tenants t
            CROSS JOIN platform_billing_settings s
            LEFT JOIN dev ON dev.tenant_id = t.id
            LEFT JOIN usage ON usage.tenant_id = t.id
            LEFT JOIN rev ON rev.tenant_id = t.id
        )
        SELECT base.*,
               (base.monthly_revenue_mxn - base.estimated_cost_mxn) AS margin_mxn,
               CASE WHEN base.monthly_revenue_mxn > 0
                    THEN (base.monthly_revenue_mxn - base.estimated_cost_mxn) / base.monthly_revenue_mxn * 100
                    ELSE NULL END AS margin_pct
        FROM base
        {where}
    """

    total_row = await (await conn.execute(f"SELECT count(*) FROM ({base_cte}) counted", params)).fetchone()
    total = total_row[0] if total_row else 0
    rows = await (
        await conn.execute(
            f"SELECT * FROM ({base_cte}) ranked ORDER BY margin_pct ASC NULLS LAST, tenant_name LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
    ).fetchall()
    return Page(items=[_profitability_out(r) for r in rows], total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Real data usage per SIM line (device_data_usage_monthly). REAL TCP-level
# bytes (jt808server/gt06server, internal/datausage.CountingConn), never
# estimated -- same dimension as usage_events but on the DEVICE side
# instead of the browser. Platform-only: a tenant_admin only sees
# sim_number/sim_carrier (GET /devices), never this report.
# ---------------------------------------------------------------------------
def _device_data_usage_out(device_row, months_raw: list[tuple]) -> DeviceDataUsageOut:
    (
        device_id, tenant_id, tenant_name, label, device_model_name,
        sim_number, sim_carrier, sim_plan_cost_mxn_month, sim_plan_data_cap_mb,
    ) = device_row
    months = [
        DeviceDataUsageMonthOut(year_month=ym.isoformat(), bytes_rx=rx, bytes_tx=tx) for ym, rx, tx in months_raw
    ]
    total_bytes_12m = sum(rx + tx for _, rx, tx in months_raw)
    avg_monthly_bytes = (total_bytes_12m / len(months_raw)) if months_raw else None
    # over_cap is evaluated on the MOST RECENT month with data (months_raw is
    # ordered by year_month ASC, see the query below) -- the real "is this
    # month over the cap?" signal, not an average that dilutes spikes.
    over_cap = False
    if sim_plan_data_cap_mb is not None and months_raw:
        last_month_bytes = months_raw[-1][1] + months_raw[-1][2]
        over_cap = (last_month_bytes / (1024 * 1024)) > sim_plan_data_cap_mb
    return DeviceDataUsageOut(
        device_id=device_id,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        label=label,
        device_model_name=device_model_name,
        sim_number=sim_number,
        sim_carrier=sim_carrier,
        sim_plan_cost_mxn_month=float(sim_plan_cost_mxn_month) if sim_plan_cost_mxn_month is not None else None,
        sim_plan_data_cap_mb=sim_plan_data_cap_mb,
        months=months,
        total_bytes_12m=total_bytes_12m,
        avg_monthly_bytes=avg_monthly_bytes,
        over_cap=over_cap,
    )


@router.get("/sim-usage", response_model=Page[DeviceDataUsageOut])
async def list_device_data_usage(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_bypass),
    tenant_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page[DeviceDataUsageOut]:
    where = "WHERE d.tenant_id = %s" if tenant_id is not None else ""
    params: list[object] = [tenant_id] if tenant_id is not None else []

    total_row = await (
        await conn.execute(f"SELECT count(*) FROM devices d {where}", params)
    ).fetchone()
    total = total_row[0] if total_row else 0

    device_rows = await (
        await conn.execute(
            f"""SELECT d.id, d.tenant_id, t.name, d.label,
                       (SELECT dm.name FROM device_models dm WHERE dm.id = d.device_model_id),
                       d.sim_number, d.sim_carrier, d.sim_plan_cost_mxn_month, d.sim_plan_data_cap_mb
                FROM devices d
                JOIN tenants t ON t.id = d.tenant_id
                {where}
                ORDER BY t.name, d.label
                LIMIT %s OFFSET %s""",
            [*params, limit, offset],
        )
    ).fetchall()
    if not device_rows:
        return Page(items=[], total=total, limit=limit, offset=offset)

    device_ids = [row[0] for row in device_rows]
    usage_rows = await (
        await conn.execute(
            """SELECT device_id, year_month, bytes_rx, bytes_tx
               FROM device_data_usage_monthly
               WHERE device_id = ANY(%s) AND year_month >= (date_trunc('month', now()) - interval '11 months')::date
               ORDER BY device_id, year_month""",
            (device_ids,),
        )
    ).fetchall()
    usage_by_device: dict[uuid.UUID, list[tuple]] = {}
    for device_id, year_month, bytes_rx, bytes_tx in usage_rows:
        usage_by_device.setdefault(device_id, []).append((year_month, bytes_rx, bytes_tx))

    items = [_device_data_usage_out(row, usage_by_device.get(row[0], [])) for row in device_rows]
    return Page(items=items, total=total, limit=limit, offset=offset)
