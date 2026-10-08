"""GET /devices/{id}/route-history -- the route history module (see
api/app/routers/devices.py::device_route_history). The focus is that the
result is ALWAYS bounded no matter how many raw rows exist in the window
(downsampling via time_bucket()+last() inside Postgres, never loading raw
rows into the app), the explicit truncation of events, and that no
combination of parameters can take the process down (max window,
statement_timeout, rate limiting)."""
import datetime as dt
import uuid

import pytest

from app.rate_limit import FixedWindowRateLimiter
from conftest import auth_header, login

pytestmark = pytest.mark.asyncio


async def _insert_positions(pool, tenant_id, device_id, *, count: int, start: dt.datetime, step: dt.timedelta):
    # app_user has no direct GRANT on the gps_positions hypertable (see
    # 0009_timeseries_access.sql) -- every write goes through the SECURITY
    # DEFINER function insert_gps_position(), same as
    # test_vehicle_distance.py/test_vehicle_engine_hours.py.
    from app import db as db_module

    rows = [
        (tenant_id, device_id, start + step * i, 32.5 + i * 1e-6, -116.9 + i * 1e-6, float(i % 120), float(i % 360))
        for i in range(count)
    ]
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                "SELECT insert_gps_position(%s, %s, %s, %s, %s, %s::real, %s::real, NULL, NULL)",
                rows,
            )


async def _insert_alarms(pool, tenant_id, device_id, *, count: int, start: dt.datetime, step: dt.timedelta):
    # Same idea -- insert_alarm() SECURITY DEFINER, never a direct INSERT
    # (see test_alarms.py::_insert_alarm).
    from app import db as db_module

    rows = [(tenant_id, device_id, start + step * i, "gt06_sos", "critical") for i in range(count)]
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                "SELECT insert_alarm(%s, %s, %s, %s, %s)",
                rows,
            )


async def test_downsamples_to_at_most_max_points(client, two_tenants, pool):
    device_id = two_tenants["a"]["device_id"]
    tenant_id = two_tenants["a"]["tenant_id"]
    token = await login(client, two_tenants["a"]["email"])

    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6)
    # 6 hours at a 5s interval = 4320 raw rows -- more than max_points.
    await _insert_positions(pool, tenant_id, device_id, count=4320, start=start, step=dt.timedelta(seconds=5))

    resp = await client.get(
        f"/devices/{device_id}/route-history",
        params={
            "from": start.isoformat(),
            "to": (start + dt.timedelta(hours=6)).isoformat(),
            "max_points": 500,
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The server NEVER returns more than max_points, no matter how many raw
    # rows are in the window -- the core property of this endpoint.
    assert len(body["points"]) <= 500
    assert len(body["points"]) > 1
    assert body["bucket_seconds"] >= 1
    # Points come sorted by ascending time.
    times = [p["time"] for p in body["points"]]
    assert times == sorted(times)


async def test_returns_all_raw_points_when_under_max_points(client, two_tenants, pool):
    device_id = two_tenants["a"]["device_id"]
    tenant_id = two_tenants["a"]["tenant_id"]
    token = await login(client, two_tenants["a"]["email"])

    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    await _insert_positions(pool, tenant_id, device_id, count=10, start=start, step=dt.timedelta(minutes=1))

    resp = await client.get(
        f"/devices/{device_id}/route-history",
        params={"from": start.isoformat(), "to": (start + dt.timedelta(minutes=10)).isoformat()},
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["points"]) == 10


async def test_events_correlated_to_nearest_position(client, two_tenants, pool):
    device_id = two_tenants["a"]["device_id"]
    tenant_id = two_tenants["a"]["tenant_id"]
    token = await login(client, two_tenants["a"]["email"])

    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    await _insert_positions(pool, tenant_id, device_id, count=60, start=start, step=dt.timedelta(minutes=1))
    alarm_time = start + dt.timedelta(minutes=30, seconds=10)  # near a real point
    await _insert_alarms(pool, tenant_id, device_id, count=1, start=alarm_time, step=dt.timedelta(0))

    resp = await client.get(
        f"/devices/{device_id}/route-history",
        params={"from": start.isoformat(), "to": (start + dt.timedelta(hours=1)).isoformat()},
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["events"]) == 1
    event = body["events"][0]
    assert event["alarm_type"] == "gt06_sos"
    assert event["severity"] == "critical"
    assert event["lat"] is not None and event["lon"] is not None
    assert event["has_video_clip"] is False
    assert body["events_truncated"] is False


async def test_event_without_nearby_position_has_null_coordinates(client, two_tenants, pool):
    device_id = two_tenants["a"]["device_id"]
    tenant_id = two_tenants["a"]["tenant_id"]
    token = await login(client, two_tenants["a"]["email"])

    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
    # A single position, VERY far in time from the alarm (outside the
    # 5-minute correlation window) -- the pin must not invent a location.
    await _insert_positions(pool, tenant_id, device_id, count=1, start=start, step=dt.timedelta(0))
    await _insert_alarms(
        pool, tenant_id, device_id, count=1, start=start + dt.timedelta(hours=1), step=dt.timedelta(0)
    )

    resp = await client.get(
        f"/devices/{device_id}/route-history",
        params={"from": start.isoformat(), "to": (start + dt.timedelta(hours=2)).isoformat()},
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["events"]) == 1
    assert body["events"][0]["lat"] is None
    assert body["events"][0]["lon"] is None


async def test_events_truncated_flag_when_over_max_events(client, two_tenants, pool):
    device_id = two_tenants["a"]["device_id"]
    tenant_id = two_tenants["a"]["tenant_id"]
    token = await login(client, two_tenants["a"]["email"])

    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    await _insert_alarms(pool, tenant_id, device_id, count=15, start=start, step=dt.timedelta(minutes=1))

    resp = await client.get(
        f"/devices/{device_id}/route-history",
        params={
            "from": start.isoformat(),
            "to": (start + dt.timedelta(hours=1)).isoformat(),
            "max_events": 10,
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["events"]) == 10
    assert body["events_truncated"] is True


async def test_window_over_31_days_rejected(client, two_tenants):
    device_id = two_tenants["a"]["device_id"]
    token = await login(client, two_tenants["a"]["email"])

    date_to = dt.datetime.now(dt.timezone.utc)
    date_from = date_to - dt.timedelta(days=32)
    resp = await client.get(
        f"/devices/{device_id}/route-history",
        params={"from": date_from.isoformat(), "to": date_to.isoformat()},
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_to_before_from_rejected(client, two_tenants):
    device_id = two_tenants["a"]["device_id"]
    token = await login(client, two_tenants["a"]["email"])

    now = dt.datetime.now(dt.timezone.utc)
    resp = await client.get(
        f"/devices/{device_id}/route-history",
        params={"from": now.isoformat(), "to": (now - dt.timedelta(hours=1)).isoformat()},
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_cross_tenant_device_returns_empty_never_leaks(client, two_tenants, pool):
    # Same rule as GET /devices/{id}/positions: another tenant's device does
    # not show up, not even to confirm it exists -- RLS filters it silently,
    # never a 403/404 that would confirm its existence.
    device_id_b = two_tenants["b"]["device_id"]
    tenant_id_b = two_tenants["b"]["tenant_id"]
    token_a = await login(client, two_tenants["a"]["email"])

    now = dt.datetime.now(dt.timezone.utc)
    await _insert_positions(
        pool, tenant_id_b, device_id_b, count=5, start=now - dt.timedelta(hours=1), step=dt.timedelta(minutes=1)
    )

    resp = await client.get(
        f"/devices/{device_id_b}/route-history",
        params={"from": (now - dt.timedelta(hours=1)).isoformat(), "to": now.isoformat()},
        headers=auth_header(token_a),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["points"] == []
    assert body["events"] == []


async def test_random_device_id_returns_empty_not_404(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    now = dt.datetime.now(dt.timezone.utc)
    resp = await client.get(
        f"/devices/{uuid.uuid4()}/route-history",
        params={"from": (now - dt.timedelta(hours=1)).isoformat(), "to": now.isoformat()},
        headers=auth_header(token),
    )
    assert resp.status_code == 200
    assert resp.json()["points"] == []


async def test_driver_rejected(client, two_tenants, pool):
    from app import db as db_module
    from app.security import hash_password

    from conftest import TEST_PASSWORD

    device_id = two_tenants["a"]["device_id"]
    tenant_id = two_tenants["a"]["tenant_id"]

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        driver_id = (
            await (await conn.execute(
                "INSERT INTO drivers (tenant_id, name, license_number) VALUES (%s, %s, %s) RETURNING id",
                (tenant_id, "Route History Driver", uuid.uuid4().hex[:12]),
            )).fetchone()
        )[0]
        email = f"driver-{uuid.uuid4().hex[:8]}@example.com"
        await conn.execute(
            """INSERT INTO users (tenant_id, email, password_hash, role, driver_id)
               VALUES (%s, %s, %s, 'driver', %s)""",
            (tenant_id, email, hash_password(TEST_PASSWORD), driver_id),
        )

    try:
        token = await login(client, email)
        now = dt.datetime.now(dt.timezone.utc)
        resp = await client.get(
            f"/devices/{device_id}/route-history",
            params={"from": (now - dt.timedelta(hours=1)).isoformat(), "to": now.isoformat()},
            headers=auth_header(token),
        )
        assert resp.status_code == 403
    finally:
        async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
            await conn.execute("DELETE FROM users WHERE email = %s", (email,))
            await conn.execute("DELETE FROM drivers WHERE tenant_id = %s", (tenant_id,))


async def test_max_points_and_max_events_bounds_enforced(client, two_tenants):
    device_id = two_tenants["a"]["device_id"]
    token = await login(client, two_tenants["a"]["email"])
    now = dt.datetime.now(dt.timezone.utc)

    resp = await client.get(
        f"/devices/{device_id}/route-history",
        params={
            "from": (now - dt.timedelta(hours=1)).isoformat(),
            "to": now.isoformat(),
            "max_points": 999999,
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 422

    resp = await client.get(
        f"/devices/{device_id}/route-history",
        params={
            "from": (now - dt.timedelta(hours=1)).isoformat(),
            "to": now.isoformat(),
            "max_events": 0,
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_rate_limit_returns_429_never_500(client, two_tenants):
    device_id = two_tenants["a"]["device_id"]
    token = await login(client, two_tenants["a"]["email"])
    now = dt.datetime.now(dt.timezone.utc)
    params = {"from": (now - dt.timedelta(hours=1)).isoformat(), "to": now.isoformat()}

    from app.main import app as fastapi_app

    original_limiter = fastapi_app.state.route_history_rate_limiter
    fastapi_app.state.route_history_rate_limiter = FixedWindowRateLimiter(max_requests=2, window_seconds=60.0)
    try:
        for _ in range(2):
            resp = await client.get(f"/devices/{device_id}/route-history", params=params, headers=auth_header(token))
            assert resp.status_code == 200
        resp = await client.get(f"/devices/{device_id}/route-history", params=params, headers=auth_header(token))
        assert resp.status_code == 429
    finally:
        fastapi_app.state.route_history_rate_limiter = original_limiter


async def test_statement_timeout_returns_503_not_500(client, two_tenants, monkeypatch):
    """Dedicated regression for the statement_timeout backstop -- never a raw
    500 or a hung connection if the query takes too long. Instead of trying to
    cause a REAL timeout through data volume (machine-dependent, flaky),
    QueryCanceled is simulated at the exact point where Postgres would raise
    it -- a deterministic test of the exception handling, not of Postgres
    timing."""
    import psycopg

    device_id = two_tenants["a"]["device_id"]
    token = await login(client, two_tenants["a"]["email"])
    now = dt.datetime.now(dt.timezone.utc)

    original_execute = psycopg.AsyncConnection.execute

    async def _execute_raising_on_time_bucket(self, query, *args, **kwargs):
        if isinstance(query, str) and "time_bucket" in query:
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
        return await original_execute(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.AsyncConnection, "execute", _execute_raising_on_time_bucket)

    resp = await client.get(
        f"/devices/{device_id}/route-history",
        params={"from": (now - dt.timedelta(hours=1)).isoformat(), "to": now.isoformat()},
        headers=auth_header(token),
    )
    assert resp.status_code == 503, resp.text
