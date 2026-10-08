"""Shared fixtures. Runs against the REAL Postgres from infra/ (same pattern
as infra/postgres/tests/conftest.py) -- no database mocks: tenant isolation
depends on RLS, which can only be tested for real against Postgres."""
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest
import pytest_asyncio
import uvicorn
from httpx import ASGITransport, AsyncClient

# Async psycopg does not work with the ProactorEventLoop, asyncio's default
# on Windows -- it needs the SelectorEventLoop. Without this, ANY async
# connection fails with "Psycopg cannot use the 'ProactorEventLoop'".
# Windows only; on Linux (Docker/CI/prod) this is a no-op.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _load_dotenv_if_needed():
    env_path = Path(__file__).resolve().parents[2] / "infra" / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv_if_needed()
os.environ.setdefault("PGHOST", "127.0.0.1")
os.environ.setdefault("PGPORT", "55432")
os.environ.setdefault("PGDATABASE", "openmdvr")
os.environ.setdefault("PGUSER", "app_user")
os.environ.setdefault("JWT_SECRET", "test-secret-never-use-in-production")
os.environ.setdefault("API_KEY_PEPPER", "test-api-key-pepper-never-use-in-production")
os.environ.setdefault("JT1078_BRIDGE_BASE_URL", "http://127.0.0.1:8082")

from app import db as db_module  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.live_positions import LivePositionsState, PositionBroadcaster, TicketStore, run_listener  # noqa: E402
from app.rate_limit import FixedWindowRateLimiter  # noqa: E402
from app.webhooks import WebhooksState  # noqa: E402
from app.main import app  # noqa: E402
from app.notifications import NotificationBroadcaster, NotificationsState, run_notification_listener  # noqa: E402
from app.security import hash_password  # noqa: E402

TEST_PASSWORD = "correct-horse-battery-staple"


@pytest_asyncio.fixture
async def pool():
    settings = get_settings()
    p = db_module.build_pool(settings)
    await p.open()
    yield p
    await p.close()


@pytest.fixture(autouse=True, scope="session")
def _cleanup_billing_test_plans():
    """billing_plans is a GLOBAL catalog with no fixture of its own (unlike
    two_tenants/platform_users, which clean up after themselves). Without
    this, every pytest run would orphan each plan test_billing_catalog.py
    creates (SKU-xxxxxxxx/CAM-xxxxxxxx), piling up junk in the billing
    catalog of a dev database. Cleanup runs at the END of the whole pytest
    session (not per test) to avoid fighting the teardown order of fixtures
    that still reference these plans (tenant_subscription_items via
    two_tenants): first the lines referencing them are released, then the
    plans. Synchronous superuser connection (not async): sidesteps the
    pytest-asyncio issue with session-scoped async fixtures in a
    function-scoped event loop."""
    yield
    conninfo = (
        f"host={os.environ['PGHOST']} port={os.environ['PGPORT']} "
        f"dbname={os.environ['PGDATABASE']} user=postgres "
        f"password={os.environ['POSTGRES_SUPERUSER_PASSWORD']}"
    )
    with psycopg.connect(conninfo, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM tenant_subscription_items
                   WHERE billing_plan_id IN (SELECT id FROM billing_plans WHERE sku LIKE %s OR sku LIKE %s)""",
                ("SKU-%", "CAM-%"),
            )
            cur.execute("DELETE FROM billing_plans WHERE sku LIKE %s OR sku LIKE %s", ("SKU-%", "CAM-%"))


@pytest_asyncio.fixture
async def superuser_conn():
    conninfo = (
        f"host={os.environ['PGHOST']} port={os.environ['PGPORT']} "
        f"dbname={os.environ['PGDATABASE']} user=postgres "
        f"password={os.environ['POSTGRES_SUPERUSER_PASSWORD']}"
    )
    conn = await psycopg.AsyncConnection.connect(conninfo, autocommit=True)
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def client(pool):
    # ASGITransport does NOT run the FastAPI lifespan (main.py sets
    # app.state.pool/live_positions and starts the listener there) -- this
    # fixture replicates by hand exactly what the lifespan would do. Without
    # it, GET /positions/stream would fail with an AttributeError in every
    # test, not because of a real bug.
    app.state.pool = pool
    app.state.live_positions = LivePositionsState(broadcaster=PositionBroadcaster(), ticket_store=TicketStore())
    listener_stop = asyncio.Event()
    listener_task = asyncio.create_task(
        run_listener(get_settings(), app.state.live_positions, listener_stop)
    )
    # Same pattern for alerts, see notifications.py -- without this, GET
    # /health (and any /notifications endpoint) would fail with an
    # AttributeError in every test.
    app.state.notifications = NotificationsState(broadcaster=NotificationBroadcaster(), ticket_store=TicketStore())
    notif_listener_stop = asyncio.Event()
    notif_listener_task = asyncio.create_task(
        run_notification_listener(get_settings(), pool, app.state.notifications, notif_listener_stop)
    )
    # API keys (0034_api_keys.sql) -- same default as the real lifespan
    # (main.py); a dedicated rate-limiting test may replace this with a small
    # limit before making its requests.
    app.state.api_key_rate_limiter = FixedWindowRateLimiter(max_requests=120, window_seconds=60.0)
    app.state.api_key_fail_limiter = FixedWindowRateLimiter(max_requests=200, window_seconds=60.0)
    # Route history (GET /devices/{id}/route-history, devices.py) -- same as
    # above: a dedicated rate-limiting test may replace this with a small
    # limit before making its requests.
    app.state.route_history_rate_limiter = FixedWindowRateLimiter(max_requests=30, window_seconds=60.0)
    # Preview snapshot (POST /devices/{id}/snapshot, video.py) -- same idea, a
    # dedicated rate-limiting test may replace this.
    app.state.snapshot_rate_limiter = FixedWindowRateLimiter(max_requests=12, window_seconds=60.0)
    # Webhooks (0035_webhooks.sql) -- state only (health/tickets do not apply
    # here, but this avoids an AttributeError in any code reading
    # request.app.state.webhooks). Dispatch/delivery tests call the internal
    # webhooks.py functions directly (without a real LISTEN/NOTIFY listener
    # running in the background) -- same approach test_notifications.py uses
    # to test fan-out without the real listener.
    app.state.webhooks = WebhooksState()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
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


# Dedicated loopback port for a REAL uvicorn server -- shared by any test of
# an indefinitely long StreamingResponse (SSE). ASGITransport runs the whole
# ASGI app INSIDE a single handle_async_request() call; for a stream that
# never returns while the generator is alive, Starlette's own disconnect
# detection deadlocks against ASGITransport. A real uvicorn server decouples
# sending headers from the generator lifecycle, just like in production --
# the only honest way to test a real stream.
_SERVER_PORT = 18765
_SERVER_BASE_URL = f"http://127.0.0.1:{_SERVER_PORT}"


@pytest_asyncio.fixture
async def stream_client(pool):
    """Starts a real loopback uvicorn server (FastAPI lifespan disabled --
    app.state is wired by hand, same as the `client` fixture) and returns a
    real AsyncClient against it. Used by both test_positions_stream.py and
    test_notifications_stream.py -- both SSE streams share exactly this need."""
    app.state.pool = pool
    app.state.live_positions = LivePositionsState(broadcaster=PositionBroadcaster(), ticket_store=TicketStore())
    listener_stop = asyncio.Event()
    listener_task = asyncio.create_task(
        run_listener(get_settings(), app.state.live_positions, listener_stop)
    )
    app.state.notifications = NotificationsState(broadcaster=NotificationBroadcaster(), ticket_store=TicketStore())
    notif_listener_stop = asyncio.Event()
    notif_listener_task = asyncio.create_task(
        run_notification_listener(get_settings(), pool, app.state.notifications, notif_listener_stop)
    )
    app.state.api_key_rate_limiter = FixedWindowRateLimiter(max_requests=120, window_seconds=60.0)
    app.state.api_key_fail_limiter = FixedWindowRateLimiter(max_requests=200, window_seconds=60.0)
    app.state.route_history_rate_limiter = FixedWindowRateLimiter(max_requests=30, window_seconds=60.0)
    app.state.snapshot_rate_limiter = FixedWindowRateLimiter(max_requests=12, window_seconds=60.0)
    app.state.webhooks = WebhooksState()

    config = uvicorn.Config(app, host="127.0.0.1", port=_SERVER_PORT, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("the test uvicorn server did not start in time")

    try:
        async with httpx.AsyncClient(base_url=_SERVER_BASE_URL) as http_client:
            yield http_client
    finally:
        server.should_exit = True
        await server_task
        notif_listener_stop.set()
        notif_listener_task.cancel()
        try:
            await notif_listener_task
        except asyncio.CancelledError:
            pass
        listener_stop.set()
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass


async def read_one_sse_event(line_iter, timeout: float = 2.0) -> dict:
    """Reads the first real `data: ...` line of an SSE stream, ignoring
    keepalives -- with an explicit timeout so that "never arrives" (the case
    that matters most: isolation) fails fast instead of hanging the suite.

    Takes the ITERATOR (resp.aiter_lines(), created once by the caller) instead
    of the Response -- httpx only allows consuming a response stream once in
    total, so reading more than one event from the same stream requires
    reusing the same iterator across calls."""
    async def _read():
        async for line in line_iter:
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise AssertionError("the stream closed without sending any event")

    return await asyncio.wait_for(_read(), timeout=timeout)


@pytest_asyncio.fixture
async def two_tenants(pool, superuser_conn):
    """Two independent tenants, each with a tenant_admin (known password:
    TEST_PASSWORD) and a device. As in infra/postgres/tests/conftest.py,
    cleanup uses the superuser connection because usage_events (if a test ever
    creates any) has no deletion path via app_user."""
    data = {}
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        for key in ("a", "b"):
            suffix = uuid.uuid4().hex[:8]
            tenant_id = (
                await (await conn.execute(
                    "INSERT INTO tenants (name) VALUES (%s) RETURNING id", (f"tenant-{key}-{suffix}",)
                )).fetchone()
            )[0]
            user_id = (
                await (await conn.execute(
                    """INSERT INTO users (tenant_id, email, password_hash, role)
                       VALUES (%s, %s, %s, 'tenant_admin') RETURNING id""",
                    (tenant_id, f"admin-{key}-{suffix}@example.com", hash_password(TEST_PASSWORD)),
                )).fetchone()
            )[0]
            terminal_id = str(int(uuid.uuid4().hex[:8], 16))  # digits only, never starts with 0
            device_id = (
                await (await conn.execute(
                    """INSERT INTO devices (tenant_id, jt808_terminal_id, label, status)
                       VALUES (%s, %s, %s, 'active') RETURNING id""",
                    (tenant_id, terminal_id, f"device-{key}"),
                )).fetchone()
            )[0]
            data[key] = {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "email": f"admin-{key}-{suffix}@example.com",
                "device_id": device_id,
            }

    yield data

    tenant_ids = (data["a"]["tenant_id"], data["b"]["tenant_id"])
    su_cur = superuser_conn.cursor()
    await su_cur.execute("DELETE FROM usage_events WHERE tenant_id IN (%s, %s)", tenant_ids)
    await su_cur.execute("DELETE FROM alarms WHERE tenant_id IN (%s, %s)", tenant_ids)
    await su_cur.execute("DELETE FROM gps_positions WHERE tenant_id IN (%s, %s)", tenant_ids)
    await su_cur.execute("DELETE FROM tenants WHERE id IN (%s, %s)", tenant_ids)


@pytest_asyncio.fixture
async def platform_users(pool, superuser_conn):
    """A real super_admin and support (known password: TEST_PASSWORD) to test
    that both bypass RLS but do NOT have the same app permissions -- support
    must not be able to create tenants or other platform accounts (see
    require_super_admin in deps.py)."""
    data = {}
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        for role in ("super_admin", "support"):
            suffix = uuid.uuid4().hex[:8]
            email = f"{role}-{suffix}@example.com"
            user_id = (
                await (await conn.execute(
                    """INSERT INTO users (tenant_id, email, password_hash, role, is_platform_bypass)
                       VALUES (NULL, %s, %s, %s, true) RETURNING id""",
                    (email, hash_password(TEST_PASSWORD), role),
                )).fetchone()
            )[0]
            data[role] = {"user_id": user_id, "email": email}

    yield data

    user_ids = tuple(v["user_id"] for v in data.values())
    su_cur = superuser_conn.cursor()
    await su_cur.execute("DELETE FROM users WHERE id IN (%s, %s)", user_ids)


async def login(client: AsyncClient, email: str, password: str = TEST_PASSWORD) -> str:
    resp = await client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
