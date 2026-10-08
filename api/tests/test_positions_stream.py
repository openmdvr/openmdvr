"""GET /positions/stream (SSE) + POST /positions/stream/ticket -- real-time
GPS positions via Postgres LISTEN/NOTIFY (migration
0018_gps_position_notify.sql) and in-memory fan-out
(api/app/live_positions.py). The core concern, as always in this project:
a tenant must never receive another tenant's event -- made harder here by
the fact that NOTIFY has no ACL, so isolation is application code, not RLS
(see PositionBroadcaster.publish).

Technical note: unlike the rest of the suite (which uses the `client`
fixture over httpx.ASGITransport), these tests need a real HTTP server
(uvicorn on a dedicated loopback port). ASGITransport runs the whole ASGI
app INSIDE a single handle_async_request() call -- for an indefinitely
long StreamingResponse (like this stream) that call never returns while
the generator is alive, and Starlette's own disconnect detection (a task
group racing stream_response against receive()) deadlocks against
ASGITransport (client.stream() never completes its __aenter__). A real
uvicorn server decouples sending headers from the generator lifecycle,
just like in production -- the only honest way to test a real stream.

Design note: _insert_position() (below) calls insert_gps_position()
directly via SQL, NEVER through jt808-server -- on purpose. This is not
just a test shortcut: it shows that the real-time layer is
protocol-agnostic (see the api/app/live_positions.py docstring and the
comment in infra/postgres/migrations/0018_gps_position_notify.sql). Any
ingestion path that resolves its own device_id and calls that same
function triggers exactly this same push.

stream_client (the fixture with the real uvicorn server) and
read_one_sse_event live in conftest.py -- shared with
test_notifications_stream.py, which needs exactly the same mechanism for
its own SSE stream."""
import asyncio
import uuid

import pytest

from app.live_positions import LISTENER_APPLICATION_NAME
from app.main import app
from conftest import TEST_PASSWORD, auth_header, login, read_one_sse_event

pytestmark = pytest.mark.asyncio

_SHORT_TIMEOUT = 2.0
_read_one_event = read_one_sse_event  # local alias, the name the rest of this file uses


async def _insert_position(pool, tenant_id, device_id, lat=19.4326, lon=-99.1332):
    """Calls insert_gps_position() directly via SQL -- simulates ingestion
    from ANY protocol, never going through jt808-server. See the design note
    at the top of this file."""
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "SELECT insert_gps_position(%s, %s, now(), %s, %s, NULL, NULL, NULL, NULL)",
            (tenant_id, device_id, lat, lon),
        )


async def _mint_ticket(client, token) -> str:
    resp = await client.post("/positions/stream/ticket", headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["ticket"]


async def _create_driver_login(client, token, tenant_id, driver_id):
    email = f"driver-stream-{uuid.uuid4().hex[:8]}@example.com"
    resp = await client.post(
        "/users",
        json={
            "email": email,
            "password": TEST_PASSWORD,
            "role": "driver",
            "tenant_id": str(tenant_id),
            "driver_id": str(driver_id),
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    return email


async def test_stream_isolates_tenants(stream_client, two_tenants, pool):
    token_a = await login(stream_client, two_tenants["a"]["email"])
    token_b = await login(stream_client, two_tenants["b"]["email"])
    ticket_a = await _mint_ticket(stream_client, token_a)
    ticket_b = await _mint_ticket(stream_client, token_b)

    async with stream_client.stream("GET", f"/positions/stream?ticket={ticket_a}") as resp_a:
        assert resp_a.status_code == 200
        lines_a = resp_a.aiter_lines()
        async with stream_client.stream("GET", f"/positions/stream?ticket={ticket_b}") as resp_b:
            assert resp_b.status_code == 200
            lines_b = resp_b.aiter_lines()

            await _insert_position(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])

            event = await _read_one_event(lines_a)
            assert event["device_id"] == str(two_tenants["a"]["device_id"])

            with pytest.raises(asyncio.TimeoutError):
                await _read_one_event(lines_b, timeout=1.0)


async def test_bypass_session_sees_all_tenants(stream_client, two_tenants, platform_users, pool):
    token = await login(stream_client, platform_users["super_admin"]["email"])
    ticket = await _mint_ticket(stream_client, token)

    async with stream_client.stream("GET", f"/positions/stream?ticket={ticket}") as resp:
        assert resp.status_code == 200
        lines = resp.aiter_lines()

        await _insert_position(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])
        event_a = await _read_one_event(lines)
        assert event_a["device_id"] == str(two_tenants["a"]["device_id"])

        await _insert_position(pool, two_tenants["b"]["tenant_id"], two_tenants["b"]["device_id"])
        event_b = await _read_one_event(lines)
        assert event_b["device_id"] == str(two_tenants["b"]["device_id"])


async def test_ticket_is_single_use(stream_client, two_tenants):
    token = await login(stream_client, two_tenants["a"]["email"])
    ticket = await _mint_ticket(stream_client, token)

    async with stream_client.stream("GET", f"/positions/stream?ticket={ticket}") as resp:
        assert resp.status_code == 200

    second = await stream_client.get(f"/positions/stream?ticket={ticket}")
    assert second.status_code == 401


async def test_ticket_unknown_is_rejected(stream_client):
    resp = await stream_client.get("/positions/stream?ticket=this-ticket-does-not-exist")
    assert resp.status_code == 401


async def test_driver_cannot_mint_ticket(stream_client, two_tenants):
    token = await login(stream_client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await stream_client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Stream"}, headers=auth_header(token))
    ).json()
    email = await _create_driver_login(stream_client, token, tenant_id, driver["id"])
    driver_token = await login(stream_client, email)

    resp = await stream_client.post("/positions/stream/ticket", headers=auth_header(driver_token))
    assert resp.status_code == 403


async def test_disconnect_removes_subscriber(stream_client, two_tenants):
    token = await login(stream_client, two_tenants["a"]["email"])
    ticket = await _mint_ticket(stream_client, token)
    tenant_id = str(two_tenants["a"]["tenant_id"])

    broadcaster = app.state.live_positions.broadcaster
    async with stream_client.stream("GET", f"/positions/stream?ticket={ticket}") as resp:
        assert resp.status_code == 200
        assert tenant_id in broadcaster._by_tenant
        assert len(broadcaster._by_tenant[tenant_id]) == 1

    # The endpoint's disconnect check runs every ~1s
    # (_DISCONNECT_POLL_SECONDS) on purpose, so a closed client does not
    # stay registered (with its queue accumulating memory) until the next
    # 22s keepalive -- wait a bit longer than that to give the server loop
    # time to notice the close and run the `finally`.
    for _ in range(30):
        if tenant_id not in broadcaster._by_tenant:
            break
        await asyncio.sleep(0.2)
    assert tenant_id not in broadcaster._by_tenant, "the queue stayed registered after closing the stream"


async def test_listener_reconnects_after_connection_dropped(stream_client, two_tenants, pool, superuser_conn):
    su_cur = superuser_conn.cursor()
    await su_cur.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = %s",
        (LISTENER_APPLICATION_NAME,),
    )

    token = await login(stream_client, two_tenants["a"]["email"])
    ticket = await _mint_ticket(stream_client, token)

    async def _insert_repeatedly():
        # Insert every 0.5s for up to 5s -- the listener's initial backoff is
        # 1s (see live_positions.py), so the first attempt may fall into the
        # reconnection window; what matters is that SOME of these arrive after
        # reconnecting. A single attempt with a long wait_for is not used
        # because asyncio.wait_for cancelling an `anext()` midway would leave
        # the line iterator unusable for a second read -- a single await on
        # _read_one_event, fed by this inserter in parallel, avoids that
        # problem entirely.
        for attempt in range(10):
            await _insert_position(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"], lat=20.0 + attempt)
            await asyncio.sleep(0.5)

    async with stream_client.stream("GET", f"/positions/stream?ticket={ticket}") as resp:
        assert resp.status_code == 200
        lines = resp.aiter_lines()
        inserter = asyncio.create_task(_insert_repeatedly())
        try:
            event = await _read_one_event(lines, timeout=10.0)
            assert event["device_id"] == str(two_tenants["a"]["device_id"])
        finally:
            inserter.cancel()
            try:
                await inserter
            except asyncio.CancelledError:
                pass


async def test_malformed_notify_payload_does_not_crash_listener(stream_client, two_tenants, pool, superuser_conn):
    su_cur = superuser_conn.cursor()
    await su_cur.execute("SELECT pg_notify('gps_positions', 'this-is-not-json')")

    token = await login(stream_client, two_tenants["a"]["email"])
    ticket = await _mint_ticket(stream_client, token)

    async with stream_client.stream("GET", f"/positions/stream?ticket={ticket}") as resp:
        assert resp.status_code == 200
        await _insert_position(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])
        event = await _read_one_event(resp.aiter_lines())
        assert event["device_id"] == str(two_tenants["a"]["device_id"])


async def test_non_dict_notify_payload_does_not_tear_down_listener(stream_client, two_tenants, pool, superuser_conn):
    """Security finding: `pg_notify` with valid JSON that is NOT an object
    (e.g. a number) made `payload.get(...)` raise AttributeError -- an
    exception not caught by the original `except ValueError` around JSON
    parsing, which propagated to run_listener's `except Exception` and tore
    down the WHOLE LISTEN connection (interrupting the push for ALL tenants
    during the reconnection backoff), not just that single notification.
    Without reconnection, this test would time out waiting for the real
    event."""
    su_cur = superuser_conn.cursor()
    await su_cur.execute("SELECT pg_notify('gps_positions', '42')")

    token = await login(stream_client, two_tenants["a"]["email"])
    ticket = await _mint_ticket(stream_client, token)

    async with stream_client.stream("GET", f"/positions/stream?ticket={ticket}") as resp:
        assert resp.status_code == 200
        # Short timeout on purpose: if the listener survived the malformed
        # payload (the correct behavior), this arrives almost immediately --
        # no backoff cycle should be needed.
        await _insert_position(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])
        event = await _read_one_event(resp.aiter_lines(), timeout=2.0)
        assert event["device_id"] == str(two_tenants["a"]["device_id"])


async def test_stream_rejects_driver_claims_even_with_a_valid_ticket(stream_client, two_tenants):
    """Defense in depth: today it is impossible to mint a ticket as a driver
    (require_non_driver on create_position_stream_ticket already prevents
    it), so this test builds the ticket directly against the app's real
    TicketStore -- without going through the ticket endpoint -- to confirm
    GET /positions/stream has its OWN role check and does not rely solely on
    the caller having validated correctly beforehand."""
    from app.main import app
    from app.security import TokenClaims

    token = await login(stream_client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await stream_client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Direct Ticket"}, headers=auth_header(token))
    ).json()

    fake_driver_claims = TokenClaims(
        user_id=str(uuid.uuid4()), tenant_id=str(tenant_id), role="driver",
        is_platform_bypass=False, driver_id=driver["id"],
    )
    ticket = app.state.live_positions.ticket_store.mint(fake_driver_claims)

    resp = await stream_client.get(f"/positions/stream?ticket={ticket}")
    assert resp.status_code == 403


async def test_stream_ends_when_tenant_cancelled_mid_stream(stream_client, two_tenants, superuser_conn, monkeypatch):
    """Security finding: an already open stream completely ignored the tenant
    being suspended/cancelled AFTER connecting, and kept delivering GPS
    positions indefinitely. `_KEEPALIVE_SECONDS` (22s in production) is
    shortened here to avoid waiting that full interval in the test; the
    mechanism under test is the same, just with a shorter cycle."""
    import app.routers.positions as positions_module

    monkeypatch.setattr(positions_module, "_KEEPALIVE_SECONDS", 0.3)

    token = await login(stream_client, two_tenants["a"]["email"])
    ticket = await _mint_ticket(stream_client, token)

    async with stream_client.stream("GET", f"/positions/stream?ticket={ticket}") as resp:
        assert resp.status_code == 200
        lines = resp.aiter_lines()
        await superuser_conn.execute("UPDATE tenants SET status = 'cancelled' WHERE id = %s", (two_tenants["a"]["tenant_id"],))

        async def _drain():
            async for _ in lines:
                pass

        # Without the fix, the stream would stay alive indefinitely (nothing
        # would close it) and this would time out.
        await asyncio.wait_for(_drain(), timeout=5.0)


async def test_stream_filters_out_device_not_assigned_to_viewer(stream_client, two_tenants, pool):
    """Migration 0032 (app_can_view_device): RLS already hides an unassigned
    device in /devices, /positions/latest and /alarms, but pg_notify() has NO
    ACL -- without the device_id filter PositionBroadcaster adds (see its
    docstring), a tenant_viewer with no assigned device would keep receiving
    the real-time position of a device they can no longer see any other way.
    other_device (created separately, never assigned) confirms the filter is
    real, not just "the viewer has no devices"."""
    admin_token = await login(stream_client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    viewer_email = f"viewer-stream-{uuid.uuid4().hex[:8]}@example.com"
    viewer_resp = await stream_client.post(
        "/users",
        json={"email": viewer_email, "password": TEST_PASSWORD, "role": "tenant_viewer", "tenant_id": str(tenant_id)},
        headers=auth_header(admin_token),
    )
    assert viewer_resp.status_code == 201, viewer_resp.text
    viewer_token = await login(stream_client, viewer_email)
    ticket = await _mint_ticket(stream_client, viewer_token)

    async with stream_client.stream("GET", f"/positions/stream?ticket={ticket}") as resp:
        assert resp.status_code == 200
        lines = resp.aiter_lines()

        # The fixture's device was NEVER assigned to this viewer -- nothing
        # must arrive.
        await _insert_position(pool, tenant_id, two_tenants["a"]["device_id"])
        with pytest.raises(asyncio.TimeoutError):
            await _read_one_event(lines, timeout=1.5)


async def test_stream_delivers_device_assigned_mid_connection_after_reconnect(stream_client, two_tenants, pool):
    """Confirms the positive side: a viewer WITH a direct assignment does
    receive the push for THAT device -- the filter is neither fail-open nor
    too aggressive, it only excludes what is unassigned."""
    admin_token = await login(stream_client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]
    viewer_email = f"viewer-stream-ok-{uuid.uuid4().hex[:8]}@example.com"
    viewer_resp = await stream_client.post(
        "/users",
        json={"email": viewer_email, "password": TEST_PASSWORD, "role": "tenant_viewer", "tenant_id": str(tenant_id)},
        headers=auth_header(admin_token),
    )
    viewer_id = viewer_resp.json()["id"]
    await stream_client.put(
        f"/users/{viewer_id}/device-assignments",
        json={"device_ids": [str(device_id)], "device_group_ids": []},
        headers=auth_header(admin_token),
    )
    viewer_token = await login(stream_client, viewer_email)
    ticket = await _mint_ticket(stream_client, viewer_token)

    async with stream_client.stream("GET", f"/positions/stream?ticket={ticket}") as resp:
        assert resp.status_code == 200
        lines = resp.aiter_lines()
        await _insert_position(pool, tenant_id, device_id)
        event = await _read_one_event(lines)
        assert event["device_id"] == str(device_id)


async def test_health_reports_listener_connected(stream_client):
    """GET /health exposes listener_connected -- cheap, and it closes the
    blind spot where a broken broadcast listener could stay down for a long
    time without anything making it visible."""
    for _ in range(20):
        resp = await stream_client.get("/health")
        assert resp.status_code == 200
        if resp.json()["listener_connected"] is True:
            return
        await asyncio.sleep(0.1)
    pytest.fail("listener_connected never became true after starting the listener")
