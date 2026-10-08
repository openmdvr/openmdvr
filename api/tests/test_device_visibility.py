"""Device/group -> user assignment (migration 0031) also restricts
VISIBILITY -- devices, /positions/latest, /alarms -- for
tenant_operator/tenant_viewer (migration 0032, app_can_view_device).
tenant_admin and platform still see everything, without assignment. This
is a security-sensitive change, so the role matrix is tested explicitly on
all three surfaces."""
import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _insert_position(pool, tenant_id, device_id, lat=19.4326, lon=-99.1332):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "SELECT insert_gps_position(%s, %s, now(), %s, %s, NULL, NULL, NULL, NULL)",
            (tenant_id, device_id, lat, lon),
        )


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


async def test_tenant_admin_sees_all_devices_without_assignment(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.get("/devices", headers=auth_header(token))
    assert resp.status_code == 200
    ids = {d["id"] for d in resp.json()["items"]}
    assert str(two_tenants["a"]["device_id"]) in ids


async def test_viewer_without_assignment_sees_no_devices(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "novis")
    viewer_token = await login(client, viewer["email"])

    devices_resp = await client.get("/devices", headers=auth_header(viewer_token))
    assert devices_resp.status_code == 200
    assert devices_resp.json()["items"] == []

    positions_resp = await client.get("/positions/latest", headers=auth_header(viewer_token))
    assert positions_resp.status_code == 200
    assert positions_resp.json() == []

    alarms_resp = await client.get("/alarms", headers=auth_header(viewer_token))
    assert alarms_resp.status_code == 200
    assert alarms_resp.json() == []


async def test_viewer_with_direct_assignment_sees_only_that_device(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = str(two_tenants["a"]["device_id"])
    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "direct")
    await client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [device_id], "device_group_ids": []},
        headers=auth_header(admin_token),
    )
    viewer_token = await login(client, viewer["email"])

    resp = await client.get("/devices", headers=auth_header(viewer_token))
    ids = {d["id"] for d in resp.json()["items"]}
    assert ids == {device_id}


async def test_viewer_with_group_assignment_sees_group_devices(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = str(two_tenants["a"]["device_id"])
    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "group")

    group = (
        await client.post("/device-groups", json={"tenant_id": str(tenant_id), "name": "G-vis"}, headers=auth_header(admin_token))
    ).json()
    await client.put(f"/device-groups/{group['id']}/members", json={"device_ids": [device_id]}, headers=auth_header(admin_token))
    await client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [], "device_group_ids": [group["id"]]},
        headers=auth_header(admin_token),
    )
    viewer_token = await login(client, viewer["email"])

    resp = await client.get("/devices", headers=auth_header(viewer_token))
    ids = {d["id"] for d in resp.json()["items"]}
    assert ids == {device_id}


async def test_viewer_cannot_see_other_tenant_device_even_with_its_own_id(client, two_tenants):
    """RLS remains the real TENANT barrier first -- an assignment can never
    widen scope beyond the user's own tenant (the trigger from migration 0031
    already rejects such an assignment, but this confirms there is no other
    way to see it either)."""
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_b = str(two_tenants["b"]["device_id"])
    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "cross")
    viewer_token = await login(client, viewer["email"])

    resp = await client.get(f"/devices/{device_b}", headers=auth_header(viewer_token))
    assert resp.status_code == 404


async def test_video_request_404_for_unassigned_device(client, two_tenants):
    """POST /devices/{id}/video already reuses get_db (RLS-scoped) for its
    first SELECT (api/app/routers/video.py) -- an unassigned device must give
    404 right there, without even trying to talk to the JT1078 bridge."""
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = str(two_tenants["a"]["device_id"])
    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "novideo")
    viewer_token = await login(client, viewer["email"])

    resp = await client.post(f"/devices/{device_id}/video", json={}, headers=auth_header(viewer_token))
    assert resp.status_code == 404


async def test_viewer_with_assignment_sees_positions_and_alarms_of_that_device(client, two_tenants, pool):
    """Complement of the negative case covered above (no assignment -> empty
    lists): confirms the positive -- a viewer WITH an assignment does see the
    real position/alarm of THAT device, not just that the filter lets nothing
    extra through. Finding F10: a bug that filtered TOO MUCH on these two
    surfaces would be just as invisible as one filtering too little without
    this case."""
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]
    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "posalarm")
    await client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [str(device_id)], "device_group_ids": []},
        headers=auth_header(admin_token),
    )
    await _insert_position(pool, tenant_id, device_id)
    await _insert_alarm(pool, tenant_id, device_id)
    viewer_token = await login(client, viewer["email"])

    positions = await client.get("/positions/latest", headers=auth_header(viewer_token))
    assert positions.status_code == 200
    assert [p["device_id"] for p in positions.json()] == [str(device_id)]

    alarms = await client.get("/alarms", headers=auth_header(viewer_token))
    assert alarms.status_code == 200
    assert [a["device_id"] for a in alarms.json()] == [str(device_id)]


async def test_device_commands_history_404_for_unassigned_device(client, two_tenants):
    """Finding F1: GET /devices/{id}/commands did not go through
    app_can_view_device -- a tenant_operator/tenant_viewer without that device
    assigned could still read the engine command history (the most dangerous
    action in the project)."""
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]
    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "nocmd")
    viewer_token = await login(client, viewer["email"])

    resp = await client.get(f"/devices/{device_id}/commands", headers=auth_header(viewer_token))
    assert resp.status_code == 404


async def test_acknowledge_alarm_404_for_unassigned_device(client, two_tenants, pool):
    """Finding F5: acknowledge_alarm() only validated the tenant, never
    whether the session can see the DEVICE -- a user holding an old alarm UUID
    (of a device since unassigned) could keep acknowledging that device's
    alarms indefinitely."""
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = two_tenants["a"]["device_id"]
    alarm_id = await _insert_alarm(pool, tenant_id, device_id)
    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "noack")
    viewer_token = await login(client, viewer["email"])

    resp = await client.post(f"/alarms/{alarm_id}/acknowledge", headers=auth_header(viewer_token))
    assert resp.status_code == 404

    # With the device assigned, they can acknowledge it -- confirms the fix
    # is not over-restrictive.
    await client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [str(device_id)], "device_group_ids": []},
        headers=auth_header(admin_token),
    )
    resp2 = await client.post(f"/alarms/{alarm_id}/acknowledge", headers=auth_header(viewer_token))
    assert resp2.status_code == 204


async def test_removing_assignment_removes_visibility(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    device_id = str(two_tenants["a"]["device_id"])
    viewer = await _create_role(client, admin_token, tenant_id, "tenant_viewer", "revoke")
    await client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [device_id], "device_group_ids": []},
        headers=auth_header(admin_token),
    )
    viewer_token = await login(client, viewer["email"])
    before = await client.get("/devices", headers=auth_header(viewer_token))
    assert {d["id"] for d in before.json()["items"]} == {device_id}

    await client.put(
        f"/users/{viewer['id']}/device-assignments",
        json={"device_ids": [], "device_group_ids": []},
        headers=auth_header(admin_token),
    )
    after = await client.get("/devices", headers=auth_header(viewer_token))
    assert after.json()["items"] == []
