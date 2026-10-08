"""In-app notification mailbox (see
infra/postgres/migrations/0033_notifications.sql). Focus: the real fan-out
inside insert_alarm() (who gets a row and with which email_status
according to their preferences) and the per-USER isolation of
notifications_select/update (the first table in the project that is not
tenant-wide)."""
import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _insert_alarm(pool, tenant_id, device_id, alarm_type="over_speed", severity="warning"):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute("SELECT insert_alarm(%s, %s, now(), %s, %s)", (tenant_id, device_id, alarm_type, severity))
        ).fetchone()
        return row[0]


async def _create_role(client, admin_token, tenant_id, role, suffix):
    resp = await client.post(
        "/users",
        json={"email": f"{role}-{suffix}@example.com", "password": TEST_PASSWORD, "role": role, "tenant_id": str(tenant_id)},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_alarm_notifies_tenant_admin_without_any_assignment(client, two_tenants, pool):
    """tenant_admin is ALWAYS a recipient (app_device_recipients), without
    needing any assignment row -- already tested at the RLS level; this
    confirms it also produces the notification row."""
    admin_token = await login(client, two_tenants["a"]["email"])
    await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"], "collision_warning", "critical")

    resp = await client.get("/notifications", headers=auth_header(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["unread_count"] == 1
    assert body["items"][0]["event_type"] == "device_alarm"
    assert body["items"][0]["severity"] == "critical"
    assert body["items"][0]["device_id"] == str(two_tenants["a"]["device_id"])
    assert body["items"][0]["read_at"] is None


async def test_alarm_notifies_only_assigned_viewer_not_unassigned(client, two_tenants, pool):
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]

    assigned = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "notifassigned")
    await client.put(
        f"/users/{assigned['id']}/device-assignments",
        json={"device_ids": [str(device_id)], "device_group_ids": []},
        headers=auth_header(admin_token),
    )
    unassigned = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "notifunassigned")

    await _insert_alarm(pool, tenant_id, device_id)

    assigned_token = await login(client, assigned["email"])
    resp_assigned = await client.get("/notifications", headers=auth_header(assigned_token))
    assert resp_assigned.json()["total"] == 1

    unassigned_token = await login(client, unassigned["email"])
    resp_unassigned = await client.get("/notifications", headers=auth_header(unassigned_token))
    assert resp_unassigned.json()["total"] == 0


async def test_notification_email_status_follows_recipient_settings(client, two_tenants, pool):
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]

    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "notifemail")
    await client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [str(device_id)], "device_group_ids": []},
        headers=auth_header(admin_token),
    )
    await client.patch(
        f"/users/{viewer['id']}/notification-settings",
        json={"email_enabled": True},
        headers=auth_header(admin_token),
    )

    await _insert_alarm(pool, tenant_id, device_id)

    # email_status is not part of NotificationOut (an internal delivery
    # detail, not business data) -- it is checked directly in the DB with
    # bypass, the same way other tests verify internal columns without
    # exposing them through the API.
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute(
                "SELECT email_status FROM notifications WHERE recipient_user_id = %s", (viewer["id"],)
            )
        ).fetchone()
    assert row[0] == "pending"


async def test_notification_not_created_when_both_channels_disabled(client, two_tenants, pool):
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]

    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "notifoff")
    await client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [str(device_id)], "device_group_ids": []},
        headers=auth_header(admin_token),
    )
    await client.patch(
        f"/users/{viewer['id']}/notification-settings",
        json={"in_app_enabled": False, "email_enabled": False},
        headers=auth_header(admin_token),
    )

    await _insert_alarm(pool, tenant_id, device_id)

    viewer_token = await login(client, viewer["email"])
    resp = await client.get("/notifications", headers=auth_header(viewer_token))
    assert resp.json()["total"] == 0


async def test_mark_notification_read_is_idempotent(client, two_tenants, pool):
    admin_token = await login(client, two_tenants["a"]["email"])
    await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])
    notif_id = (await client.get("/notifications", headers=auth_header(admin_token))).json()["items"][0]["id"]

    first = await client.post(f"/notifications/{notif_id}/read", headers=auth_header(admin_token))
    assert first.status_code == 200
    read_at_first = first.json()["read_at"]
    assert read_at_first is not None

    second = await client.post(f"/notifications/{notif_id}/read", headers=auth_header(admin_token))
    assert second.status_code == 200
    assert second.json()["read_at"] == read_at_first  # the real timestamp is not overwritten

    listing = await client.get("/notifications", headers=auth_header(admin_token))
    assert listing.json()["unread_count"] == 0


async def test_cannot_read_another_users_notification(client, two_tenants, pool):
    """The first table in the project isolated per USER, not per tenant -- not
    even a tenant_admin of the SAME tenant can touch another user's mailbox."""
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]

    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "notifprivate")
    await client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [str(device_id)], "device_group_ids": []},
        headers=auth_header(admin_token),
    )
    await _insert_alarm(pool, tenant_id, device_id)
    viewer_token = await login(client, viewer["email"])
    notif_id = (await client.get("/notifications", headers=auth_header(viewer_token))).json()["items"][0]["id"]

    # the tenant_admin (same tenant, but not THEIR notification)
    resp_get = await client.get("/notifications", headers=auth_header(admin_token))
    assert all(n["id"] != notif_id for n in resp_get.json()["items"])

    resp_read = await client.post(f"/notifications/{notif_id}/read", headers=auth_header(admin_token))
    assert resp_read.status_code == 404


async def test_cannot_read_other_tenants_notification(client, two_tenants, pool):
    await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])
    admin_a_notif = (
        await client.get("/notifications", headers=auth_header(await login(client, two_tenants["a"]["email"])))
    ).json()["items"][0]["id"]

    token_b = await login(client, two_tenants["b"]["email"])
    resp = await client.post(f"/notifications/{admin_a_notif}/read", headers=auth_header(token_b))
    assert resp.status_code == 404


async def test_unread_only_filter(client, two_tenants, pool):
    admin_token = await login(client, two_tenants["a"]["email"])
    await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"], "over_speed")
    await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"], "fatigue_driving")
    items = (await client.get("/notifications", headers=auth_header(admin_token))).json()["items"]
    await client.post(f"/notifications/{items[0]['id']}/read", headers=auth_header(admin_token))

    resp = await client.get("/notifications", params={"unread_only": "true"}, headers=auth_header(admin_token))
    body = resp.json()
    assert body["total"] == 1
    assert body["unread_count"] == 1
    assert all(n["read_at"] is None for n in body["items"])


async def test_in_app_disabled_suppresses_mailbox_even_with_email_enabled(client, two_tenants, pool):
    """Finding F5: before the fix, in_app_enabled=false hid nothing -- the
    only way to silence the mailbox was to turn off BOTH channels. The row
    must still exist (the email worker needs it), it just must not show up in
    that user's GET /notifications."""
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]

    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "notifinappoff")
    await client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [str(device_id)], "device_group_ids": []},
        headers=auth_header(admin_token),
    )
    await client.patch(
        f"/users/{viewer['id']}/notification-settings",
        json={"in_app_enabled": False, "email_enabled": True},
        headers=auth_header(admin_token),
    )
    await _insert_alarm(pool, tenant_id, device_id, "email_only")

    viewer_token = await login(client, viewer["email"])
    resp = await client.get("/notifications", headers=auth_header(viewer_token))
    assert resp.json()["total"] == 0

    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute(
                "SELECT email_status, in_app_enabled FROM notifications WHERE recipient_user_id = %s", (viewer["id"],)
            )
        ).fetchone()
    assert row == ("pending", False)


async def test_direct_and_group_assignment_does_not_duplicate_notification(client, two_tenants, pool):
    """A user assigned DIRECTLY and via a group to the same device must get
    ONE notification per alarm -- app_device_recipients() dedupes with UNION,
    not UNION ALL (see 0031/0033)."""
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]

    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "notifdedupe")
    group = (
        await client.post("/device-groups", json={"tenant_id": str(tenant_id), "name": "Dedupe"}, headers=auth_header(admin_token))
    ).json()
    await client.put(f"/device-groups/{group['id']}/members", json={"device_ids": [str(device_id)]}, headers=auth_header(admin_token))
    await client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [str(device_id)], "device_group_ids": [group["id"]]},
        headers=auth_header(admin_token),
    )

    await _insert_alarm(pool, tenant_id, device_id)
    viewer_token = await login(client, viewer["email"])
    resp = await client.get("/notifications", headers=auth_header(viewer_token))
    assert resp.json()["total"] == 1


async def test_driver_never_becomes_recipient_even_with_forced_group_assignment(client, two_tenants, pool):
    """Finding F6: no data layer explicitly excluded the driver role from
    app_device_recipients() -- only _ASSIGNABLE_ROLES on the API side, which a
    direct SQL INSERT bypasses entirely. This test forces that row by hand (as
    a future bug or direct DB access would) and confirms the 0033
    redefinition no longer counts it as a recipient."""
    from app import db as db_module

    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]

    driver_resp = await client.post(
        "/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Notif Forced"}, headers=auth_header(admin_token)
    )
    driver_id = driver_resp.json()["id"]
    import uuid as _uuid

    driver_email = f"driver-forced-{_uuid.uuid4().hex[:8]}@example.com"
    driver_user = (
        await client.post(
            "/users",
            json={
                "email": driver_email, "password": TEST_PASSWORD, "role": "driver",
                "tenant_id": str(tenant_id), "driver_id": driver_id,
            },
            headers=auth_header(admin_token),
        )
    ).json()

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "INSERT INTO user_device_assignments (user_id, device_id, tenant_id) VALUES (%s, %s, %s)",
            (driver_user["id"], device_id, tenant_id),
        )
    # app_device_recipients() is not called directly -- on purpose (same F6
    # finding, fixed alongside): the function is SECURITY DEFINER WITHOUT a
    # grant to app_user, not even bypass can call it directly, only
    # insert_alarm() calls it internally. The real effect is confirmed by
    # firing a real alarm.

    await _insert_alarm(pool, tenant_id, device_id)
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        count_row = await (
            await conn.execute(
                "SELECT count(*) FROM notifications WHERE recipient_user_id = %s", (driver_user["id"],)
            )
        ).fetchone()
    assert count_row[0] == 0


async def test_mark_read_also_acknowledges_the_underlying_alarm(client, two_tenants, pool):
    """A single button does both things -- Notifications is the only alarm
    surface. Also covers the real case (not just tenant_admin, who can always
    see any device of their tenant): a tenant_viewer ASSIGNED to the device
    must be able to acknowledge the alarm through this same endpoint, truly
    exercising the app_can_view_device() check in acknowledge_alarm() (0032),
    not just an admin's bypass."""
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]

    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "notifackviewer")
    await client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [str(device_id)], "device_group_ids": []},
        headers=auth_header(admin_token),
    )
    alarm_id = await _insert_alarm(pool, tenant_id, device_id, "collision_warning", "critical")

    viewer_token = await login(client, viewer["email"])
    notif_id = (await client.get("/notifications", headers=auth_header(viewer_token))).json()["items"][0]["id"]

    resp = await client.post(f"/notifications/{notif_id}/read", headers=auth_header(viewer_token))
    assert resp.status_code == 200
    assert resp.json()["read_at"] is not None

    resp_alarms = await client.get("/alarms", headers=auth_header(admin_token))
    matching = [a for a in resp_alarms.json() if a["id"] == str(alarm_id)]
    assert len(matching) == 1
    assert matching[0]["acknowledged_at"] is not None


async def test_device_id_filter_only_returns_that_devices_notifications(client, two_tenants, pool):
    """Filter used by DeviceAlarmsPreview (DeviceDetailPanel.tsx) and by the
    "See all" link to /notifications?device_id=... -- a convenience, never the
    real isolation barrier (that is still recipient_user_id, covered by other
    tests in this file)."""
    from app import db as db_module

    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]

    # Created directly via SQL (bypass), not through POST /devices -- that
    # endpoint remains require_bypass (platform only), not something a
    # tenant_admin can call, and not what this test covers.
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        other_device_id = (
            await (
                await conn.execute(
                    """INSERT INTO devices (tenant_id, jt808_terminal_id, label, status)
                       VALUES (%s, %s, %s, 'active') RETURNING id""",
                    (tenant_id, "19988877700", "Other device"),
                )
            ).fetchone()
        )[0]

    await _insert_alarm(pool, tenant_id, device_id, "over_speed")
    await _insert_alarm(pool, tenant_id, other_device_id, "fatigue_driving")

    resp = await client.get(
        "/notifications", params={"device_id": str(device_id)}, headers=auth_header(admin_token)
    )
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["device_id"] == str(device_id)


async def test_large_recipient_count_does_not_lose_the_entire_fanout(client, two_tenants, pool):
    """Regression of finding F1 (the most severe in this area): pg_notify()
    has a hard 8000-byte payload limit. The original version put the full
    recipient list there, in the SAME BEGIN/EXCEPTION block as the INSERT --
    from ~205 recipients on it failed with "payload string too long" and ALSO
    rolled back the rows already written, silently losing the WHOLE fan-out.
    250 directly assigned users (well above the real threshold) are seeded to
    reproduce the exact scenario that triggered the bug."""
    from app import db as db_module

    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        for i in range(250):
            row = await (
                await conn.execute(
                    """INSERT INTO users (tenant_id, email, password_hash, role)
                       VALUES (%s, %s, 'x', 'tenant_viewer') RETURNING id""",
                    (tenant_id, f"bulkviewer-{i}-{tenant_id}@example.com"),
                )
            ).fetchone()
            await conn.execute(
                "INSERT INTO user_device_assignments (user_id, device_id, tenant_id) VALUES (%s, %s, %s)",
                (row[0], device_id, tenant_id),
            )

    alarm_id = await _insert_alarm(pool, tenant_id, device_id, "mass_fanout")

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        count_row = await (
            await conn.execute("SELECT count(*) FROM notifications WHERE alarm_id = %s", (alarm_id,))
        ).fetchone()
    # 250 assigned viewers + 1 tenant_admin from the fixture = 251.
    assert count_row[0] == 251
