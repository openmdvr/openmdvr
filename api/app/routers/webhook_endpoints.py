"""webhook_endpoints management (0035_webhooks.sql) -- create/list/edit/
rotate secret/delete, always require_tenant_admin (the tenant's own
tenant_admin, or platform) -- not even the owner of an external integration
can self-manage this without being tenant_admin, same rule as
api-keys/device-assignments/notification-settings. The feature itself
requires tenants.webhooks_enabled=true, approved by the PLATFORM via
PATCH /tenants/{id} (tenants.py) -- never something enabled from here."""
from __future__ import annotations

import datetime as dt
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from psycopg import AsyncConnection
from psycopg.errors import CheckViolation, InsufficientPrivilege, RaiseException

from .. import db as db_module
from ..deps import get_db, require_tenant_admin, require_tenant_admin_or_super_admin
from ..rate_limit import FixedWindowRateLimiter
from ..schemas import (
    Page,
    WebhookDeliveryOut,
    WebhookEndpointCreate,
    WebhookEndpointCreatedOut,
    WebhookEndpointOut,
    WebhookEndpointUpdate,
    WebhookTestOut,
)
from ..security import TokenClaims
from ..webhooks import build_signed_headers, deliver_webhook, generate_webhook_secret, is_webhook_url_safe
from .. import limits

router = APIRouter(prefix="/webhook-endpoints", tags=["webhooks"])

_SELECT_COLUMNS = """id, tenant_id, url, event_types, enabled, consecutive_failures,
                     disabled_at, disabled_reason, last_attempt_at, last_success_at, created_by, created_at"""

# Without a cap, a tenant_admin could create dozens/hundreds of endpoints all
# pointing at the same third-party URL -- each alarm fans out to N endpoints x
# up to 6 retries, a real outbound traffic amplifier. Same pattern as
# _assert_device_quota_not_exceeded() in devices.py -- a simple operational
# limit, not a new quota table.
_MAX_ENDPOINTS_PER_TENANT = limits.WEBHOOK_MAX_ENDPOINTS_PER_TENANT

_UNSAFE_URL_DETAIL = "destination URL not allowed"

# "Send test" makes a real outbound POST per request -- rate-limited per user
# so the button cannot become a traffic amplifier toward third parties.
_test_rate_limiter = FixedWindowRateLimiter(max_requests=limits.WEBHOOK_TEST_RATE_LIMIT_PER_MINUTE, window_seconds=60.0)


async def _assert_endpoint_quota_not_exceeded(conn: AsyncConnection, tenant_id) -> None:
    count_row = await (
        await conn.execute("SELECT count(*) FROM webhook_endpoints WHERE tenant_id = %s", (tenant_id,))
    ).fetchone()
    if count_row and count_row[0] >= _MAX_ENDPOINTS_PER_TENANT:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"limit of {_MAX_ENDPOINTS_PER_TENANT} webhooks per tenant reached -- delete one to create another",
        )


def _endpoint_out(row) -> WebhookEndpointOut:
    (
        id_, tenant_id, url, event_types, enabled, consecutive_failures,
        disabled_at, disabled_reason, last_attempt_at, last_success_at, created_by, created_at,
    ) = row
    return WebhookEndpointOut(
        id=id_, tenant_id=tenant_id, url=url, event_types=list(event_types), enabled=enabled,
        consecutive_failures=consecutive_failures,
        disabled_at=disabled_at.isoformat() if disabled_at else None,
        disabled_reason=disabled_reason,
        last_attempt_at=last_attempt_at.isoformat() if last_attempt_at else None,
        last_success_at=last_success_at.isoformat() if last_success_at else None,
        created_by=created_by,
        created_at=created_at.isoformat(),
    )


@router.post("", response_model=WebhookEndpointCreatedOut, status_code=201)
async def create_webhook_endpoint(
    body: WebhookEndpointCreate,
    conn: AsyncConnection = Depends(get_db),
    admin: TokenClaims = Depends(require_tenant_admin_or_super_admin),
) -> WebhookEndpointCreatedOut:
    # Rejecting BEFORE touching the database when a tenant session asks for a
    # foreign tenant_id keeps the enforce_webhook_endpoint_tenant_enabled()
    # trigger (0035, SECURITY DEFINER) from acting as a 1-bit oracle on other
    # tenants' tenants.webhooks_enabled (403 if the UUID is a real, approved
    # tenant vs. 422 if not) -- not applicable to a platform session, which
    # sees the whole `tenants` table anyway.
    if not admin.is_platform_bypass and str(body.tenant_id) != str(admin.tenant_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you cannot create a webhook for another tenant")
    if not await is_webhook_url_safe(body.url):
        # SSRF protection also runs at delivery time (webhooks.py), but a URL
        # pointing at localhost / internal network / cloud metadata must also
        # be rejected at configuration time -- never rely on a single layer at
        # the very end.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, _UNSAFE_URL_DETAIL)
    await _assert_endpoint_quota_not_exceeded(conn, body.tenant_id)
    secret = generate_webhook_secret()
    try:
        row = await (
            await conn.execute(
                f"""INSERT INTO webhook_endpoints (tenant_id, url, event_types, secret, enabled, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING {_SELECT_COLUMNS}""",
                (body.tenant_id, body.url, body.event_types, secret, body.enabled, admin.user_id),
            )
        ).fetchone()
    except InsufficientPrivilege:
        # The WITH CHECK of webhook_endpoints_insert (RLS) rejects a tenant_id
        # that is not the session's own -- same as vehicles.py/drivers.py.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you cannot create a webhook for another tenant")
    except RaiseException:
        # enforce_webhook_endpoint_tenant_enabled() (0035) rejects a tenant
        # without tenants.webhooks_enabled=true -- the feature has not been
        # approved by the platform for this tenant yet.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "the webhooks feature is not enabled for this tenant -- contact support",
        )
    out = _endpoint_out(row)
    return WebhookEndpointCreatedOut(**out.model_dump(), secret=secret)


@router.get("", response_model=Page[WebhookEndpointOut])
async def list_webhook_endpoints(
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
    tenant_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[WebhookEndpointOut]:
    # Optional filter -- RLS is the real isolation (a tenant session never
    # sees anything outside its own); this only narrows FURTHER within what
    # RLS allows, for a platform session looking at one tenant.
    where = "WHERE tenant_id = %s" if tenant_id else ""
    params: list[object] = [tenant_id] if tenant_id else []

    total_row = await (await conn.execute(f"SELECT count(*) FROM webhook_endpoints {where}", params)).fetchone()
    total = total_row[0] if total_row else 0
    rows = await (
        await conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM webhook_endpoints {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
    ).fetchall()
    return Page(items=[_endpoint_out(r) for r in rows], total=total, limit=limit, offset=offset)


@router.patch("/{endpoint_id}", response_model=WebhookEndpointOut)
async def update_webhook_endpoint(
    endpoint_id: uuid.UUID,
    body: WebhookEndpointUpdate,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> WebhookEndpointOut:
    if body.url is not None and not await is_webhook_url_safe(body.url):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, _UNSAFE_URL_DETAIL)
    try:
        row = await (
            await conn.execute(
                f"""UPDATE webhook_endpoints
                    SET url = COALESCE(%s, url),
                        event_types = COALESCE(%s, event_types),
                        enabled = COALESCE(%s, enabled),
                        -- Manually re-enabling (enabled: true) an endpoint
                        -- the circuit breaker turned off used to leave the
                        -- row contradictory ("enabled=true" alongside
                        -- "disabled_reason=too many failures"). An explicit
                        -- re-enable is a new admin decision: clear the
                        -- previous breaker state and give the endpoint a
                        -- clean chance (if it keeps failing, it is disabled
                        -- again soon anyway).
                        consecutive_failures = CASE WHEN %s THEN 0 ELSE consecutive_failures END,
                        disabled_at = CASE WHEN %s THEN NULL ELSE disabled_at END,
                        disabled_reason = CASE WHEN %s THEN NULL ELSE disabled_reason END
                    WHERE id = %s
                    RETURNING {_SELECT_COLUMNS}""",
                (
                    body.url, body.event_types, body.enabled,
                    body.enabled is True, body.enabled is True, body.enabled is True,
                    endpoint_id,
                ),
            )
        ).fetchone()
    except CheckViolation:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "event_types cannot be empty")
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webhook not found")
    return _endpoint_out(row)


@router.post("/{endpoint_id}/rotate-secret", response_model=WebhookEndpointCreatedOut)
async def rotate_webhook_secret(
    endpoint_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin_or_super_admin),
) -> WebhookEndpointCreatedOut:
    """Losing the secret means rotating, never "recovering" -- same as API
    keys. The new secret is returned ONCE, as on creation.

    require_tenant_admin_or_super_admin (not require_tenant_admin) on
    purpose: otherwise `support` (RLS bypass, but not super_admin) could
    rotate ANY tenant's secret and keep a valid signing secret (letting it
    forge events "signed by OpenMDVR"), while also breaking the customer's
    integration. Obtaining a credential/secret is more sensitive than an
    operational tweak, and support must not be able to do it alone."""
    new_secret = generate_webhook_secret()
    row = await (
        await conn.execute(
            f"""UPDATE webhook_endpoints SET secret = %s WHERE id = %s
                RETURNING {_SELECT_COLUMNS}""",
            (new_secret, endpoint_id),
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webhook not found")
    out = _endpoint_out(row)
    return WebhookEndpointCreatedOut(**out.model_dump(), secret=new_secret)


@router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_webhook_endpoint(
    endpoint_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
) -> None:
    row = await (
        await conn.execute("DELETE FROM webhook_endpoints WHERE id = %s RETURNING id", (endpoint_id,))
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webhook not found")


@router.get("/{endpoint_id}/deliveries", response_model=Page[WebhookDeliveryOut])
async def list_webhook_deliveries(
    endpoint_id: uuid.UUID,
    conn: AsyncConnection = Depends(get_db),
    _: TokenClaims = Depends(require_tenant_admin),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[WebhookDeliveryOut]:
    # webhook_endpoints RLS (not webhook_deliveries) is what actually
    # confirms this session can see THIS endpoint -- if the row does not show
    # up here, it does not exist or belongs to another tenant (neither confirm
    # nor deny).
    endpoint_row = await (
        await conn.execute("SELECT id FROM webhook_endpoints WHERE id = %s", (endpoint_id,))
    ).fetchone()
    if endpoint_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webhook not found")

    total_row = await (
        await conn.execute(
            "SELECT count(*) FROM webhook_deliveries WHERE webhook_endpoint_id = %s", (endpoint_id,)
        )
    ).fetchone()
    total = total_row[0] if total_row else 0
    rows = await (
        await conn.execute(
            """SELECT id, event_type, status, attempt_count, next_attempt_at, response_status_code,
                      last_error, created_at, delivered_at
               FROM webhook_deliveries WHERE webhook_endpoint_id = %s
               ORDER BY created_at DESC LIMIT %s OFFSET %s""",
            (endpoint_id, limit, offset),
        )
    ).fetchall()
    items = [
        WebhookDeliveryOut(
            id=r[0], event_type=r[1], status=r[2], attempt_count=r[3], next_attempt_at=r[4].isoformat(),
            response_status_code=r[5], last_error=r[6], created_at=r[7].isoformat(),
            delivered_at=r[8].isoformat() if r[8] else None,
        )
        for r in rows
    ]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.post("/{endpoint_id}/test", response_model=WebhookTestOut)
async def test_webhook_endpoint(
    endpoint_id: uuid.UUID,
    request: Request,
    conn: AsyncConnection = Depends(get_db),
    user: TokenClaims = Depends(require_tenant_admin),
) -> WebhookTestOut:
    """Synchronous delivery of a `ping` event signed exactly like a real
    delivery (same X-OpenMDVR-* headers, same HMAC signature, same SSRF
    protection with pinned IP) -- without enqueuing it or affecting the
    endpoint's circuit breaker. Gives the UI a way to see why an endpoint
    fails."""
    if not _test_rate_limiter.allow(str(user.user_id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many tests in a row, please wait a moment")
    # webhook_endpoints RLS confirms visibility (404 if it belongs to another
    # tenant); the secret is read separately with bypass, only after that
    # confirmation, and never leaves this process.
    row = await (
        await conn.execute(
            """SELECT e.url, t.webhooks_enabled FROM webhook_endpoints e JOIN tenants t ON t.id = e.tenant_id
               WHERE e.id = %s""",
            (endpoint_id,),
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webhook not found")
    url, webhooks_enabled = row
    if not webhooks_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "webhooks are not enabled for this tenant")
    async with db_module.tenant_scoped_connection(request.app.state.pool, tenant_id=None, bypass=True) as bypass_conn:
        secret_row = await (
            await bypass_conn.execute("SELECT secret FROM webhook_endpoints WHERE id = %s", (endpoint_id,))
        ).fetchone()
    if secret_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webhook not found")

    delivery_id = f"test-{uuid.uuid4()}"
    body = json.dumps(
        {
            "event": "ping",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "data": {"message": "Webhook test from OpenMDVR", "webhook_endpoint_id": str(endpoint_id)},
        }
    ).encode("utf-8")
    result = await deliver_webhook(url, body, build_signed_headers(secret_row[0], "ping", delivery_id, body))
    return WebhookTestOut(
        success=result.success, status_code=result.status_code, error=result.error, elapsed_ms=result.elapsed_ms
    )
