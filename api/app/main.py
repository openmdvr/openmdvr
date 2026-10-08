from __future__ import annotations

import asyncio
import math
import os
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from . import db as db_module
from .api_key_auth import log_api_key_usage
from .config import get_settings
from .live_positions import LivePositionsState, PositionBroadcaster, TicketStore, run_listener
from .notifications import NotificationBroadcaster, NotificationsState, run_notification_listener
from .rate_limit import FixedWindowRateLimiter
from .routers import (
    alarms,
    auth,
    billing,
    device_commands,
    device_config_commands,
    device_groups,
    devices,
    driver_shift_alerts,
    drivers,
    geofences,
    notifications as notifications_router,
    platform,
    positions,
    routes,
    shifts,
    tenants,
    users,
    vehicles,
    video,
    webhook_endpoints,
)
from .webhooks import WebhooksState, run_webhook_delivery_worker, run_webhook_dispatch_listener
from . import limits


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    pool = db_module.build_pool(settings)
    await pool.open()
    app.state.pool = pool

    # Real-time GPS positions (see api/app/live_positions.py): a single
    # Postgres listener per process, started as a background task and
    # cancelled cleanly on shutdown -- never one connection per SSE client.
    app.state.live_positions = LivePositionsState(
        broadcaster=PositionBroadcaster(), ticket_store=TicketStore()
    )
    listener_stop = asyncio.Event()
    listener_task = asyncio.create_task(
        run_listener(settings, app.state.live_positions, listener_stop)
    )

    # Real-time in-app notifications (see api/app/notifications.py) -- same
    # pattern: a single Postgres listener per process, never one connection
    # per SSE client.
    app.state.notifications = NotificationsState(
        broadcaster=NotificationBroadcaster(), ticket_store=TicketStore()
    )
    notif_listener_stop = asyncio.Event()
    notif_listener_task = asyncio.create_task(
        run_notification_listener(settings, pool, app.state.notifications, notif_listener_stop)
    )

    # API keys (0034_api_keys.sql, see deps.py::get_current_user) --
    # per-process limit, see rate_limit.py for the rationale. Configurable in
    # case a legitimate integrator needs more volume (adjust the variable, no
    # code change); 120/min is deliberately generous for normal polling (every
    # few seconds) while still stopping a runaway client or brute force.
    app.state.api_key_rate_limiter = FixedWindowRateLimiter(
        max_requests=limits.API_KEY_RATE_LIMIT_PER_MINUTE, window_seconds=60.0
    )
    # SEPARATE per-IP limit for auth attempts with an invalid/nonexistent API
    # key -- the limiter above is keyed by api_key_id, which only exists for a
    # real key; without this one, guessing random keys had no brake and went
    # unaudited. More generous than the one above (200/min per IP, not per
    # key) because one real IP can represent several legitimate integrators
    # behind a shared NAT/proxy.
    app.state.api_key_fail_limiter = FixedWindowRateLimiter(
        max_requests=limits.API_KEY_FAIL_RATE_LIMIT_PER_MINUTE, window_seconds=60.0
    )

    # Route history (GET /devices/{id}/route-history, see devices.py) -- a
    # user must never be able to take the system down by running a report.
    # The window/points/statement_timeout caps of that query already bound the
    # cost of ONE request; this is the extra layer against MANY cheap requests
    # in a row (repeated refresh, a frontend bug in a loop, or deliberate
    # abuse) -- per user, not per API key (this is an interactive dashboard
    # screen, never M2M). Generous (30/min) for normal human use.
    app.state.route_history_rate_limiter = FixedWindowRateLimiter(
        max_requests=limits.ROUTE_HISTORY_RATE_LIMIT_PER_MINUTE, window_seconds=60.0
    )

    # Preview snapshot (POST /devices/{id}/snapshot, see video.py) -- per
    # device, not per user: several viewers of the SAME camera must not count
    # against ANOTHER camera's limit. 12/min easily covers the real frontend
    # pattern (one photo every 2 min while the tile is visible, capped at 3
    # per viewing session) with headroom for several tiles of the same unit
    # open at once, without leaving the door open to a runaway loop.
    app.state.snapshot_rate_limiter = FixedWindowRateLimiter(
        max_requests=limits.SNAPSHOT_RATE_LIMIT_PER_MINUTE, window_seconds=60.0
    )

    # Outgoing webhooks (0035_webhooks.sql, see api/app/webhooks.py) -- TWO
    # separate background tasks on purpose: the dispatcher (listens on the
    # SAME Postgres channel as the in-app mailbox, cheaply decides whether
    # there is anything to enqueue) and the delivery worker (the one that
    # actually does outbound HTTP with retries) -- so a slow/reconnecting
    # dispatcher never blocks already-queued deliveries, and vice versa.
    app.state.webhooks = WebhooksState()
    webhook_dispatch_stop = asyncio.Event()
    webhook_dispatch_task = asyncio.create_task(
        run_webhook_dispatch_listener(settings, pool, app.state.webhooks, webhook_dispatch_stop)
    )
    webhook_worker_stop = asyncio.Event()
    webhook_worker_task = asyncio.create_task(
        run_webhook_delivery_worker(pool, app.state.webhooks, webhook_worker_stop)
    )
    try:
        yield
    finally:
        listener_stop.set()
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass
        notif_listener_stop.set()
        notif_listener_task.cancel()
        try:
            await notif_listener_task
        except asyncio.CancelledError:
            pass
        webhook_dispatch_stop.set()
        webhook_dispatch_task.cancel()
        try:
            await webhook_dispatch_task
        except asyncio.CancelledError:
            pass
        webhook_worker_stop.set()
        webhook_worker_task.cancel()
        try:
            await webhook_worker_task
        except asyncio.CancelledError:
            pass
        await pool.close()


app = FastAPI(title="OpenMDVR API", lifespan=lifespan)

# CORS: the dashboard (web/) runs on a different origin during development
# (Vite on :5173) and possibly on its own subdomain in production.
# allow_credentials=False because auth is a Bearer token in the Authorization
# header, not a cookie -- there is no CSRF to mitigate with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def audit_api_key_usage(request: Request, call_next):
    """Audit log of every API-key-authenticated request (0034_api_keys.sql).
    get_current_user (deps.py) sets `request.state.auth_claims` BEFORE its own
    scope checks (tag allowlist/read-only), so this also records attempts
    REJECTED by that gate (403), not only the allowed ones -- many 403s from
    the same key is itself a security signal worth auditing later. Never
    records anything for a normal JWT session (auth_claims.auth_method ==
    "password") or for requests that never authenticated (401 before
    auth_claims is set -- there is no real key to attribute)."""
    response = await call_next(request)
    claims = getattr(request.state, "auth_claims", None)
    if claims is not None and claims.auth_method == "api_key" and claims.api_key_id:
        # A direct `await` here blocked the real response until the audit
        # INSERT finished (another pool connection, on the critical path) --
        # with the pool saturated, an already-resolved API key request could
        # wait for the pool timeout just to write the log. BackgroundTask runs
        # it AFTER the response is sent. No endpoint in this project uses
        # response.background today, so assigning it directly is safe; if one
        # ever needs it, it must be chained here instead of overwritten.
        response.background = BackgroundTask(
            log_api_key_usage,
            request.app.state.pool,
            api_key_id=claims.api_key_id,
            tenant_id=claims.tenant_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            ip_address=request.client.host if request.client else None,
        )
    return response


app.include_router(auth.router)
app.include_router(tenants.router)
app.include_router(users.router)
app.include_router(devices.router)
app.include_router(device_commands.router)
app.include_router(device_config_commands.router)
app.include_router(device_groups.router)
app.include_router(notifications_router.router)
app.include_router(vehicles.router)
app.include_router(drivers.router)
app.include_router(shifts.router)
app.include_router(routes.router)
app.include_router(video.router)
app.include_router(positions.router)
app.include_router(alarms.router)
app.include_router(driver_shift_alerts.router)
app.include_router(billing.router)
app.include_router(platform.router)
app.include_router(webhook_endpoints.router)
app.include_router(geofences.router)


def _sanitize_non_finite(value: object) -> object:
    """NaN/Infinity are not valid JSON -- a body with one of these in a float
    field (e.g. max_shift_hours) makes Pydantic correctly reject the value,
    but FastAPI's DEFAULT handler echoes the raw value in the "input" field of
    the error detail, and json.dumps(allow_nan=False) fails serializing that
    ERROR RESPONSE -- a raw 500 instead of a clean 422. Affects any float
    field with ge/le across the API, so the fix lives once here."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _sanitize_non_finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_non_finite(v) for v in value]
    return value


# The same "input" field echoes the FULL body that failed validation -- a
# POST /users request missing the required "role" field returns the password
# in plain text inside the 422 detail. Not reachable from the dashboard forms
# (they always send every required field), but the API would hand it to any
# consumer calling it directly. Redacted by field name, at any depth of
# "input".
_SENSITIVE_INPUT_KEYS = {
    "password", "password_hash", "token", "access_token", "jwt_secret", "secret",
    "raw_key", "key_hash", "api_key", "api_key_pepper",
}


def _redact_sensitive_input(value: object) -> object:
    if isinstance(value, dict):
        return {
            k: "***" if isinstance(k, str) and k.lower() in _SENSITIVE_INPUT_KEYS else _redact_sensitive_input(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_input(v) for v in value]
    return value


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = _sanitize_non_finite(_redact_sensitive_input(jsonable_encoder(exc.errors())))
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": errors})


@app.exception_handler(psycopg.errors.DataError)
async def data_error_handler(request: Request, exc: psycopg.errors.DataError) -> JSONResponse:
    # A NUL byte (\x00) in any free-text field (`search`, `name`, etc.) passes
    # Pydantic validation (it is a valid string) but Postgres rejects NUL in
    # text columns -- an uncaught psycopg.errors.DataError, raw 500. A single
    # global handler (same idea as validation_exception_handler above) turns
    # it into a clean 422 for ANY present or future endpoint that accepts free
    # text, without sanitizing every field one by one -- in practice a
    # Postgres DataError always means "the client sent a value the column type
    # rejects" (NUL bytes, invalid encoding, numeric out of range), never an
    # internal bug that should look like a 500.
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": "invalid input"})


@app.get("/health")
async def health(request: Request) -> dict:
    # listener_connected: if this stays `false` persistently in production,
    # the live position push is down for ALL tenants even though the rest of
    # the API responds normally (see live_positions.py).
    return {
        "status": "ok",
        "listener_connected": request.app.state.live_positions.listener_connected,
        "notifications_listener_connected": request.app.state.notifications.listener_connected,
        "webhook_dispatch_listener_connected": request.app.state.webhooks.dispatch_listener_connected,
    }
