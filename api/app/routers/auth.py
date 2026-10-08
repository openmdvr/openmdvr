"""Login. The only API endpoint that CANNOT go through the normal get_db
dependency (deps.py) -- that dependency needs an already valid JWT to know
which tenant to scope the session to, and at login there is no JWT yet: the
email must be resolved WITHOUT knowing the tenant in advance, so this specific
query uses a bypass connection, just like the JT808 server resolves a device
by terminal_id without knowing its tenant (jt808-server/internal/db/devices.go)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from .. import db as db_module
from ..config import Settings
from ..deps import get_settings_dep
from ..schemas import LoginRequest, LoginResponse
from ..security import DUMMY_PASSWORD_HASH, TokenClaims, create_access_token, verify_password

router = APIRouter(tags=["auth"])


@router.post("/auth/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    settings: Settings = Depends(get_settings_dep),
) -> LoginResponse:
    pool = request.app.state.pool
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute(
                """SELECT u.id, u.tenant_id, u.role, u.is_platform_bypass, u.password_hash,
                          u.status, t.status, u.driver_id, t.display_name, t.logo_url
                   FROM users u LEFT JOIN tenants t ON t.id = u.tenant_id
                   WHERE u.email = %s""",
                (body.email,),
            )
        ).fetchone()

    # Identical message whether the email does not exist or the password does
    # not match -- no way for an attacker to enumerate registered emails via
    # the login endpoint. Equally important: the "missing" path does the same
    # bcrypt work (against a fixed dummy hash) as the "exists" path so that
    # response LATENCY does not leak it either -- bcrypt.checkpw is
    # deliberately slow (~100-300ms), and skipping it on one path creates a
    # timing side channel as exploitable as the message itself.
    # run_in_threadpool: bcrypt.checkpw is synchronous and used to block the
    # process's single asyncio event loop (~150-220ms per attempt), so a burst
    # of concurrent logins (successful or not -- DUMMY_PASSWORD_HASH pays the
    # same cost) degraded latency for the WHOLE API, not just login (12
    # concurrent logins raised /health from ~10ms to 1.25s).
    invalid = HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if row is None:
        await run_in_threadpool(verify_password, body.password, DUMMY_PASSWORD_HASH)
        raise invalid
    (
        user_id, tenant_id, role, is_platform_bypass, password_hash, user_status, tenant_status,
        driver_id, tenant_display_name, tenant_logo_url,
    ) = row
    password_ok = await run_in_threadpool(verify_password, body.password, password_hash)
    if user_status != "active" or (tenant_id is not None and tenant_status != "active") or not password_ok:
        raise invalid

    claims = TokenClaims(
        user_id=str(user_id),
        tenant_id=str(tenant_id) if tenant_id else None,
        role=role,
        is_platform_bypass=is_platform_bypass,
        driver_id=str(driver_id) if driver_id else None,
    )
    token = create_access_token(settings, claims)
    return LoginResponse(
        access_token=token,
        role=role,
        user_id=user_id,
        tenant_id=tenant_id,
        tenant_display_name=tenant_display_name,
        tenant_logo_url=tenant_logo_url,
    )
