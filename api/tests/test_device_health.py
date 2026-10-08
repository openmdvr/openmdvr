"""Device health for the platform (migration 0053): OPERATIONAL device
problems, deduplicated with a counter and visible only to
super_admin/support."""

import pytest

from app import db as db_module
from conftest import auth_header, login

pytestmark = pytest.mark.asyncio


async def _record(pool, device_id, kind="upload_unrequested", key="EVENT_x_F_05.ts", bytes_=1_600_000):
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "SELECT record_device_health_event(%s, %s, %s, 'warning', %s, '{}'::jsonb, %s)",
            (device_id, kind, key, "The camera uploaded a file nobody requested", bytes_),
        )


async def _cleanup(superuser_conn, device_id):
    # app_user has no DELETE on this table (on purpose): cleanup uses the
    # tests' superuser connection.
    await superuser_conn.cursor().execute("DELETE FROM device_health_events WHERE device_id = %s", (device_id,))


async def test_same_problem_is_one_row_with_counter(client, two_tenants, platform_users, pool, superuser_conn):
    device_id = two_tenants["a"]["device_id"]
    try:
        for _ in range(3):
            await _record(pool, device_id)
        token = await login(client, platform_users["super_admin"]["email"])
        resp = await client.get("/platform/device-health", headers=auth_header(token))
        assert resp.status_code == 200, resp.text
        mine = [e for e in resp.json()["items"] if e["device_id"] == str(device_id)]
        assert len(mine) == 1, "the same repeated problem must be ONE row, not one per occurrence"
        assert mine[0]["occurrences"] == 3
        assert mine[0]["bytes_wasted"] == 3 * 1_600_000
        assert mine[0]["tenant_name"]
    finally:
        await _cleanup(superuser_conn, device_id)


async def test_support_sees_it_tenant_roles_do_not(client, two_tenants, platform_users, pool, superuser_conn):
    device_id = two_tenants["a"]["device_id"]
    try:
        await _record(pool, device_id)
        support = await login(client, platform_users["support"]["email"])
        assert (await client.get("/platform/device-health", headers=auth_header(support))).status_code == 200
        tenant_admin = await login(client, two_tenants["a"]["email"])
        resp = await client.get("/platform/device-health", headers=auth_header(tenant_admin))
        assert resp.status_code == 403
    finally:
        await _cleanup(superuser_conn, device_id)


async def test_resolve_closes_event_and_new_occurrence_opens_a_new_one(client, two_tenants, platform_users, pool, superuser_conn):
    device_id = two_tenants["a"]["device_id"]
    try:
        await _record(pool, device_id)
        token = await login(client, platform_users["super_admin"]["email"])
        items = (await client.get("/platform/device-health", headers=auth_header(token))).json()["items"]
        event_id = next(e["id"] for e in items if e["device_id"] == str(device_id))

        resp = await client.post(f"/platform/device-health/{event_id}/resolve", headers=auth_header(token))
        assert resp.status_code == 204
        again = await client.post(f"/platform/device-health/{event_id}/resolve", headers=auth_header(token))
        assert again.status_code == 404

        open_items = (await client.get("/platform/device-health", headers=auth_header(token))).json()["items"]
        assert not [e for e in open_items if e["device_id"] == str(device_id)]

        # If the problem happens again, it is a NEW event (the resolved one is not reopened).
        await _record(pool, device_id)
        open_items = (await client.get("/platform/device-health", headers=auth_header(token))).json()["items"]
        mine = [e for e in open_items if e["device_id"] == str(device_id)]
        assert len(mine) == 1 and mine[0]["occurrences"] == 1 and mine[0]["id"] != event_id
    finally:
        await _cleanup(superuser_conn, device_id)


async def test_app_user_cannot_insert_directly(pool, two_tenants):
    # Only through the SECURITY DEFINER function: nobody fabricates events by hand.
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        with pytest.raises(Exception):
            await conn.execute(
                "INSERT INTO device_health_events (tenant_id, device_id, kind, severity, title) VALUES (%s, %s, 'x_y_z', 'info', 't')",
                (two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"]),
            )
