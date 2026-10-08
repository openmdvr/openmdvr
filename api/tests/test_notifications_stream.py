"""GET /notifications/stream (SSE) + POST /notifications/stream/ticket --
real-time push of the in-app mailbox (see api/app/notifications.py). Same
underlying rule as test_positions_stream.py: pg_notify() has no ACL of its
own, so the real isolation is NotificationBroadcaster.publish() -- here by
recipient_user_id instead of by tenant. stream_client/read_one_sse_event
live in conftest.py, shared with test_positions_stream.py."""
import asyncio

import pytest

from conftest import TEST_PASSWORD, auth_header, login, read_one_sse_event

pytestmark = pytest.mark.asyncio


async def _insert_alarm(pool, tenant_id, device_id, alarm_type="over_speed"):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute("SELECT insert_alarm(%s, %s, now(), %s)", (tenant_id, device_id, alarm_type))
        ).fetchone()
        return row[0]


async def _create_viewer(client, admin_token, tenant_id):
    import uuid

    email = f"viewer-notifstream-{uuid.uuid4().hex[:8]}@example.com"
    resp = await client.post(
        "/users",
        json={"email": email, "password": TEST_PASSWORD, "role": "tenant_viewer", "tenant_id": str(tenant_id)},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json(), email


async def _mint_ticket(client, token) -> str:
    resp = await client.post("/notifications/stream/ticket", headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["ticket"]


async def test_stream_delivers_alarm_notification_to_admin(stream_client, two_tenants, pool):
    token = await login(stream_client, two_tenants["a"]["email"])
    ticket = await _mint_ticket(stream_client, token)

    async with stream_client.stream("GET", f"/notifications/stream?ticket={ticket}") as resp:
        assert resp.status_code == 200
        lines = resp.aiter_lines()
        alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])
        event = await read_one_sse_event(lines)
        # Each client's payload carries ITS OWN row (singular
        # id/recipient_user_id), never the full recipient list -- finding F4
        # (see the api/app/notifications.py docstring).
        assert event["alarm_id"] == str(alarm_id)
        assert event["recipient_user_id"] == str(two_tenants["a"]["user_id"])
        assert "recipient_user_ids" not in event


async def test_stream_does_not_deliver_to_unassigned_viewer(stream_client, two_tenants, pool):
    admin_token = await login(stream_client, two_tenants["a"]["email"])
    viewer, viewer_email = await _create_viewer(stream_client, admin_token, two_tenants["a"]["tenant_id"])
    viewer_token = await login(stream_client, viewer_email)
    ticket = await _mint_ticket(stream_client, viewer_token)

    async with stream_client.stream("GET", f"/notifications/stream?ticket={ticket}") as resp:
        assert resp.status_code == 200
        lines = resp.aiter_lines()
        await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])
        with pytest.raises(asyncio.TimeoutError):
            await read_one_sse_event(lines, timeout=1.5)


async def test_stream_delivers_to_assigned_viewer_but_not_other_tenant_admin(stream_client, two_tenants, pool):
    """Confirms in a single event both the positive case (the assigned viewer
    DOES receive it) and cross isolation (ANOTHER tenant's admin, with their
    own stream open, never receives it)."""
    admin_a_token = await login(stream_client, two_tenants["a"]["email"])
    viewer, viewer_email = await _create_viewer(stream_client, admin_a_token, two_tenants["a"]["tenant_id"])
    device_id = str(two_tenants["a"]["device_id"])
    await stream_client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [device_id], "device_group_ids": []},
        headers=auth_header(admin_a_token),
    )
    viewer_token = await login(stream_client, viewer_email)
    admin_b_token = await login(stream_client, two_tenants["b"]["email"])

    ticket_viewer = await _mint_ticket(stream_client, viewer_token)
    ticket_admin_b = await _mint_ticket(stream_client, admin_b_token)

    async with stream_client.stream("GET", f"/notifications/stream?ticket={ticket_viewer}") as resp_viewer:
        assert resp_viewer.status_code == 200
        lines_viewer = resp_viewer.aiter_lines()
        async with stream_client.stream("GET", f"/notifications/stream?ticket={ticket_admin_b}") as resp_b:
            assert resp_b.status_code == 200
            lines_b = resp_b.aiter_lines()

            await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])

            event = await read_one_sse_event(lines_viewer)
            assert event["recipient_user_id"] == viewer["id"]

            with pytest.raises(asyncio.TimeoutError):
                await read_one_sse_event(lines_b, timeout=1.0)


async def test_driver_cannot_mint_notification_stream_ticket(stream_client, two_tenants):
    token = await login(stream_client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await stream_client.post(
            "/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Notif"}, headers=auth_header(token)
        )
    ).json()
    import uuid as _uuid

    email = f"driver-notifstream-{_uuid.uuid4().hex[:8]}@example.com"
    resp = await stream_client.post(
        "/users",
        json={"email": email, "password": TEST_PASSWORD, "role": "driver", "tenant_id": str(tenant_id), "driver_id": driver["id"]},
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    driver_token = await login(stream_client, email)

    resp2 = await stream_client.post("/notifications/stream/ticket", headers=auth_header(driver_token))
    assert resp2.status_code == 403
