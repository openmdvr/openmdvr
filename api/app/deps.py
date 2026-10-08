"""FastAPI dependencies: who the authenticated user is (from a JWT or an API
key, see api_key_auth.py) and a database connection already scoped to their
tenant via RLS."""
from __future__ import annotations

import datetime as dt
import logging
from typing import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import AsyncConnection

from . import db as db_module
from .api_key_auth import resolve_api_key
from .config import Settings, get_settings
from .security import API_KEY_PREFIX, InvalidToken, TokenClaims, decode_access_token

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Router tags (APIRouter(..., tags=[...])) reachable with an API key --
# explicit ALLOWLIST (deny by default), not an exclusion list: exposing a new
# resource to API keys is ONE line here and nowhere else. Deliberately OUT
# forever, regardless of key scope (not even with can_write):
#   - auth: how a JWT is obtained; meaningless for a credential that already
#     authenticates on its own.
#   - billing/tenants/users: money and account management -- never delegable
#     to an M2M integration, however "trusted".
#   - platform: platform configuration (super_admin/support).
#   - device-commands: engine cut/restore -- real physical danger to a
#     vehicle, not something that should depend on an admin remembering not
#     to tick "can write" on a key. ALWAYS excluded, not only when
#     can_write=false.
#   - video: real bandwidth budget (the most expensive variable cost) plus its
#     own single-use ticket system, deliberately outside this mechanism until
#     a real use case justifies it.
#   - device-groups: organizational management (creating/naming groups), not
#     something a data read/write integration needs today.
#   - webhooks: webhook_endpoints management (0035_webhooks.sql) --
#     creating/rotating/deleting a webhook endpoint is integration
#     configuration (and the signing secret travels in plain text in the
#     response), same sensitivity as api-keys/users -- an API key must not be
#     able to create ANOTHER credential / outbound data channel.
_API_KEY_ALLOWED_TAGS = frozenset(
    {
        "devices",
        "positions",
        "vehicles",
        "drivers",
        "routes",
        "notifications",
        "alarms",
        "shifts",
        "driver-shift-alerts",
        # Geofences: enter/exit events reportable by an M2M integration (ERP,
        # TMS). Events/occupancy already go through app_can_view_device, so the
        # key's allowed_device_ids narrows them like alarms/positions;
        # creating/editing additionally requires can_write AND that the
        # underlying user is tenant_admin.
        "geofences",
    }
)


def get_settings_dep() -> Settings:
    return get_settings()


async def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings_dep),
) -> TokenClaims:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing authentication token")
    token = creds.credentials

    if token.startswith(API_KEY_PREFIX):
        # An attempt with a key that DOES NOT EXIST never hit any rate limit
        # (the limiter below is keyed by api_key_id, which only exists for a
        # valid key) and was never audited (api_key_usage_log requires a real
        # api_key_id via FK) -- a brute-force / credential-stuffing campaign
        # against the API key space was invisible and, worse, every attempt
        # paid for a pool connection + a full SELECT before failing (DoS by
        # pool exhaustion). Limited per IP, BEFORE touching the database, and
        # logged (logger, not the audit table -- there is no real api_key_id
        # to attribute).
        client_ip = request.client.host if request.client else None
        fail_limiter = getattr(request.app.state, "api_key_fail_limiter", None)
        if fail_limiter is not None and client_ip is not None and not fail_limiter.allow(client_ip):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, "too many failed authentication attempts"
            )
        claims = await resolve_api_key(request.app.state.pool, settings, token)
        if claims is None:
            logger.warning(
                "deps: authentication attempt with an invalid/revoked/expired API key from %s (prefix %r)",
                client_ip, token[:12],
            )
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
        # Stored BEFORE the per-key rate limit and the scope gate below (which
        # may raise their own exception): the usage-audit middleware (main.py)
        # only records when this is set, so a 429/403 rejected by ANY of the
        # following checks must be audited too, not only 200s -- repeated
        # rejections from the same key are themselves a security signal.
        request.state.auth_claims = claims
        limiter = getattr(request.app.state, "api_key_rate_limiter", None)
        if limiter is not None and not limiter.allow(claims.api_key_id):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests with this API key")
    else:
        try:
            claims = decode_access_token(settings, token)
        except InvalidToken:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")
        request.state.auth_claims = claims

    if claims.auth_method == "api_key":
        # Single enforcement point for EVERY present and future endpoint that
        # depends on get_current_user (directly or via require_*) -- never
        # something a new router has to remember to repeat. Requires ALL of the
        # route's tags to be allowed (subset), not just an intersection: an
        # intersection check would fail OPEN if a future router combined an
        # allowed tag with an excluded one (e.g. tags=["devices","video"]
        # passing via "devices"). A route with no tags is rejected (an empty
        # route_tags is explicitly not authorized).
        route = request.scope.get("route")
        route_tags = set(getattr(route, "tags", None) or ())
        if not route_tags or not (route_tags <= _API_KEY_ALLOWED_TAGS):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "this API key does not have access to this resource")
        # The SSE stream endpoints (POST .../stream/ticket + GET .../stream)
        # are meant for the interactive dashboard (browser EventSource,
        # single-use ticket with a 30s TTL) -- a real M2M integration should
        # poll over plain REST. ALWAYS excluded for an API key, allowed tag or
        # not: excluding by method alone would block POST /stream/ticket for a
        # read-only key but ALLOW it for a can_write=true key, minting a
        # ticket for a mechanism not meant for this kind of credential.
        if request.url.path.endswith(("/stream", "/stream/ticket")):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "API keys cannot use the real-time stream, use REST polling"
            )
        if request.method not in _SAFE_METHODS and not claims.can_write:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "this API key is read-only")

    return claims


async def _fetch_session_status(pool, user: TokenClaims) -> tuple[bool, bool]:
    """(user_active, tenant_active) for these ALREADY VALIDATED claims, RIGHT
    NOW -- not when the JWT was issued. tenant_active is True when not
    applicable (platform session with no tenant).

    get_current_user only verifies the token's SIGNATURE, never whether the
    account or tenant is still active -- without this check, disabling or
    deleting a user, or suspending/cancelling a tenant (including the
    automatic cutoff for non-payment, see enforce_billing_suspension), had no
    real effect until the JWT expired on its own (up to 8h); a CANCELLED
    tenant even kept receiving its real-time GPS position stream. Same rule as
    login (auth.py), repeated here on every use because a JWT cannot "revoke"
    itself once issued.

    bypass=True on purpose (like auth.py) -- this is a SESSION integrity
    check on the user's own row by id, never a read of another tenant's
    business data.

    For an API key session (auth_method == "api_key") it ALSO checks that the
    key is still unrevoked/unexpired -- resolve_api_key() (see
    api_key_auth.py) already validates this on EVERY new request, but a
    long-lived connection (the positions/notifications SSE stream,
    periodically revalidated via is_session_active) never goes through
    resolve_api_key again once open. Without this check, revoking a key with a
    stream already open would have no real effect until the client
    disconnected on its own.
    """
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute(
                """SELECT u.status, t.status FROM users u LEFT JOIN tenants t ON t.id = u.tenant_id
                   WHERE u.id = %s""",
                (user.user_id,),
            )
        ).fetchone()
        if row is None:
            return False, False
        user_status, tenant_status = row
        user_active = user_status == "active"
        tenant_active = user.tenant_id is None or tenant_status == "active"

        if user.auth_method == "api_key" and user.api_key_id:
            key_row = await (
                await conn.execute(
                    "SELECT revoked_at, expires_at FROM api_keys WHERE id = %s", (user.api_key_id,)
                )
            ).fetchone()
            if key_row is None:
                return False, False
            revoked_at, expires_at = key_row
            if revoked_at is not None or expires_at <= dt.datetime.now(dt.timezone.utc):
                return False, False

    return user_active, tenant_active


async def is_session_active(pool, user: TokenClaims) -> bool:
    """Simple boolean variant for the SSE stream loop (positions.py) -- there
    the reason does not matter, only whether to stay alive."""
    user_active, tenant_active = await _fetch_session_status(pool, user)
    return user_active and tenant_active


async def assert_session_active(pool, user: TokenClaims) -> None:
    user_active, tenant_active = await _fetch_session_status(pool, user)
    if not user_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session is no longer valid")
    if not tenant_active:
        # 402, not 401: the user is still who they claim to be -- it is the
        # TENANT that has no active service (suspended or cancelled, typically
        # for non-payment). Same code video.py already used, generalized here
        # to EVERY authenticated endpoint.
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED, "the tenant does not have an active service (suspended or cancelled)"
        )


async def get_db(
    request: Request,
    user: TokenClaims = Depends(get_current_user),
) -> AsyncIterator[AsyncConnection]:
    """Connection with the authenticated user's RLS context already set. It
    is the ONLY way an endpoint should touch the database -- never a "raw"
    connection that skips this, or tenant isolation stops applying."""
    # Extra defense (not the real guarantee, that is the
    # users_tenant_role_consistency CHECK from migration 0015): a driver-role
    # JWT must ALWAYS carry driver_id. If this fires it is a token issuance
    # bug (auth.py), not something a client can trigger -- better an explicit
    # 500 here than letting RLS silently treat that driver as a "non-driver
    # session" (they would see the whole tenant, see the
    # driver_shift_events_select policy comment in 0015).
    if user.role == "driver" and not user.driver_id:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "driver session without driver_id")

    pool = request.app.state.pool
    await assert_session_active(pool, user)
    async with db_module.tenant_scoped_connection(
        pool,
        tenant_id=user.tenant_id,
        bypass=user.is_platform_bypass,
        driver_id=user.driver_id,
        user_id=user.user_id,
        api_key_device_filter=user.allowed_device_ids,
    ) as conn:
        yield conn


def require_bypass(user: TokenClaims = Depends(get_current_user)) -> TokenClaims:
    """Requires a platform user (super_admin/support). Use it on endpoints
    the RLS policy already restricts to bypass (creating tenants, creating
    devices) -- returning 403 here is just a better error experience; RLS is
    what really enforces the limit in the database, so even if this check had
    a bug, the database would still reject the INSERT."""
    if not user.is_platform_bypass:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this action requires a platform role")
    return user


def require_tenant_admin(user: TokenClaims = Depends(get_current_user)) -> TokenClaims:
    if not (user.is_platform_bypass or user.role == "tenant_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this action requires tenant_admin or a platform role")
    return user


def require_tenant_admin_or_super_admin(user: TokenClaims = Depends(get_current_user)) -> TokenClaims:
    """Stricter than require_tenant_admin: excludes `support`. Issuing an API
    key is issuing a NEW credential for a user -- same rule as "create another
    platform account" (require_super_admin, below) and "create a tenant's
    users" (create_user in users.py: "support" cannot, only tenant_admin or
    super_admin). Otherwise `support` (RLS bypass, but not super_admin) could
    issue a read-write key for ANY tenant's tenant_admin, and that key would
    survive intact even after the `support` account itself was disabled -- a
    backdoor of up to 2 years into a foreign tenant, unrelated to the session
    that created it. Applies only to CREATING -- revoking (which only reduces
    access) and listing/auditing stay at require_tenant_admin."""
    if not (user.role == "tenant_admin" or user.role == "super_admin"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "this action requires tenant_admin or super_admin (not support)"
        )
    return user


def require_non_driver(user: TokenClaims = Depends(get_current_user)) -> TokenClaims:
    """Explicitly excludes the driver role from any endpoint outside /shifts.
    `get_current_user` alone excludes no tenant-scoped role, so without this a
    driver would inherit exactly the same access as tenant_viewer to EVERY
    endpoint that only asks for `get_current_user` -- GET /devices,
    /positions/latest, /alarms (+ POST acknowledge), /vehicles, /drivers,
    /users, /tenants, and POST /devices/{id}/video -- contradicting the intent
    (a driver may only clock THEIR OWN shift, no fleet visibility). The real
    restriction must always live here (backend), never only in the frontend
    router redirect -- that is UX, not the guarantee."""
    if user.role == "driver":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this action is not available for driver accounts")
    return user


def require_driver(user: TokenClaims = Depends(get_current_user)) -> TokenClaims:
    """Requires a driver session (driver role) -- used by POST /shifts/clock: a
    driver can only record THEIR OWN shift event, never someone else's (RLS
    backs this via app_current_driver_id(); this is the better error
    experience, like require_bypass/require_tenant_admin)."""
    if user.role != "driver":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this action requires a driver account")
    return user


def require_super_admin(user: TokenClaims = Depends(get_current_user)) -> TokenClaims:
    """Stricter than require_bypass: excludes `support`. `support` has RLS
    bypass (cross-tenant read, limited support) but docs/architecture.md is
    explicit that onboarding customers (creating tenants) and creating other
    platform accounts are `super_admin` work, not `support` -- RLS alone does
    not distinguish this (its policy only requires bypass), so this is the
    only layer that does."""
    if user.role != "super_admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this action requires the super_admin role")
    return user
