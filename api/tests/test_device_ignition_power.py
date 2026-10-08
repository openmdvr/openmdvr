"""Ignition (ACC) and external power state (devices.ignition_on /
power_connected, migration 0046). Focus: they are telemetry reported by
the device itself (never editable via PATCH), visible to any role that can
already see the device (same as last_seen_at, no extra restriction), and
NULL until the device reports for the first time.

The real write is done by jt808server/gt06server (see
jt808-server/internal/db/devices.go::UpdateDeviceStatus) -- these tests
simulate that write with a direct UPDATE (same approach as
test_billing_sim_usage.py with record_device_data_usage); they do not
exercise protocol parsing (that lives in each package's Go tests)."""
import uuid

import pytest

from conftest import auth_header, login

pytestmark = pytest.mark.asyncio


async def _set_device_status(pool, device_id, ignition_on, power_connected):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "UPDATE devices SET ignition_on = %s, ignition_changed_at = now(), "
            "power_connected = %s, power_changed_at = now() WHERE id = %s",
            (ignition_on, power_connected, device_id),
        )


async def test_device_out_defaults_to_null_before_any_report(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.get(f"/devices/{two_tenants['a']['device_id']}", headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ignition_on"] is None
    assert body["ignition_changed_at"] is None
    assert body["power_connected"] is None
    assert body["power_changed_at"] is None


async def test_device_out_reflects_reported_status(client, two_tenants, platform_users, pool):
    device_id = two_tenants["a"]["device_id"]
    await _set_device_status(pool, device_id, ignition_on=True, power_connected=False)

    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.get(f"/devices/{device_id}", headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ignition_on"] is True
    assert body["ignition_changed_at"] is not None
    assert body["power_connected"] is False
    assert body["power_changed_at"] is not None


async def test_tenant_admin_sees_ignition_power_like_last_seen_at(client, two_tenants, pool):
    """Same as sim_number: visible to the tenant's own tenant_admin without
    bypass -- it is not cost/billing data."""
    device_id = two_tenants["a"]["device_id"]
    await _set_device_status(pool, device_id, ignition_on=False, power_connected=True)

    tenant_token = await login(client, two_tenants["a"]["email"])
    resp = await client.get(f"/devices/{device_id}", headers=auth_header(tenant_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ignition_on"] is False
    assert body["power_connected"] is True


async def test_ignition_on_transition_creates_info_alarm_and_notification(client, two_tenants, pool, superuser_conn):
    """A notification is raised on every change, not just a status icon. The
    devices_notify_status_change trigger (migration 0048) calls insert_alarm()
    -- the SAME function that builds the notification fan-out for any other
    alarm -- so a real tenant_admin (the two_tenants fixture has one) must
    receive the in-app notification, not just the alarms row."""
    device_id = two_tenants["a"]["device_id"]
    tenant_id = two_tenants["a"]["tenant_id"]
    await _set_device_status(pool, device_id, ignition_on=True, power_connected=True)

    su_cur = superuser_conn.cursor()
    await su_cur.execute(
        "SELECT alarm_type, severity FROM alarms WHERE device_id = %s ORDER BY time DESC LIMIT 1", (device_id,)
    )
    alarm_row = await su_cur.fetchone()
    assert alarm_row == ("ignition_on", "info")

    await su_cur.execute(
        "SELECT title, severity FROM notifications WHERE tenant_id = %s AND device_id = %s ORDER BY created_at DESC LIMIT 1",
        (tenant_id, device_id),
    )
    notif_row = await su_cur.fetchone()
    assert notif_row is not None, "the fixture's real tenant_admin should have received the in-app notification"
    assert notif_row[0] == "Alarm: ignition_on"
    assert notif_row[1] == "info"


async def test_power_cut_transition_creates_warning_alarm(client, two_tenants, pool, superuser_conn):
    """Power cut gets warning severity -- the same severity as gt06_power_cut
    (the existing explicit signal for the same kind of real event,
    theft/tampering)."""
    device_id = two_tenants["a"]["device_id"]
    # Starts connected (the real default -- see 0046) so that False is a real
    # transition, not a no-op.
    await _set_device_status(pool, device_id, ignition_on=False, power_connected=True)
    await _set_device_status(pool, device_id, ignition_on=False, power_connected=False)

    su_cur = superuser_conn.cursor()
    await su_cur.execute(
        "SELECT alarm_type, severity FROM alarms WHERE device_id = %s ORDER BY time DESC LIMIT 1", (device_id,)
    )
    row = await su_cur.fetchone()
    assert row == ("power_cut", "warning")


async def test_repeated_same_status_does_not_duplicate_alarm(client, two_tenants, pool, superuser_conn):
    """The trigger only fires on a REAL transition (IS DISTINCT FROM) -- a
    heartbeat repeating the same state (the normal case; most heartbeats of a
    real device change nothing) must never raise a new alarm -- that would be
    noise ("on every change", not "on every heartbeat")."""
    device_id = two_tenants["a"]["device_id"]
    await _set_device_status(pool, device_id, ignition_on=True, power_connected=True)
    await _set_device_status(pool, device_id, ignition_on=True, power_connected=True)
    await _set_device_status(pool, device_id, ignition_on=True, power_connected=True)

    su_cur = superuser_conn.cursor()
    await su_cur.execute(
        "SELECT count(*) FROM alarms WHERE device_id = %s AND alarm_type = 'ignition_on'", (device_id,)
    )
    (count,) = await su_cur.fetchone()
    assert count == 1


async def test_patch_device_ignores_ignition_power_fields(client, two_tenants, platform_users, pool):
    """These fields are not editable (they are not in DeviceUpdate) -- sending
    them in a PATCH must not fail or have any effect; they are simply ignored
    (Pydantic's default behavior for an undeclared extra field, never an
    error)."""
    device_id = two_tenants["a"]["device_id"]
    await _set_device_status(pool, device_id, ignition_on=True, power_connected=True)

    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.patch(
        f"/devices/{device_id}",
        json={"label": "renamed unit", "ignition_on": False, "power_connected": False},
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["label"] == "renamed unit"
    # The PATCH must not have touched the real state reported by the device.
    assert body["ignition_on"] is True
    assert body["power_connected"] is True
