"""Creating/editing devices (vehicle_id, notes) and pagination/search on
GET /devices. Plate/make/model/year/driver live in vehicles.py/drivers.py
(see test_vehicles.py/test_drivers.py) -- devices only stores its own
installation notes and the linked vehicle_id."""
import pytest

from conftest import login, auth_header

pytestmark = pytest.mark.asyncio


async def _set_device_quota(client, token, tenant_id, quantity, category="camera"):
    """two_tenants has NO subscription (on purpose -- injecting one there broke
    billing tests that need exact control over which lines exist). Any test
    that wants to add a device BEYOND the one two_tenants already creates
    (device-a/device-b) must set an explicit quota first.

    category defaults to 'camera' because these tests create jt808 devices
    (the default of DeviceCreate.protocol) -- the quota is computed per
    category, see devices.py::_assert_device_quota_not_exceeded."""
    items = (
        await client.get(
            "/billing/subscription-items",
            params={"tenant_id": str(tenant_id), "active_only": True},
            headers=auth_header(token),
        )
    ).json()["items"]
    for item in items:
        await client.patch(f"/billing/subscription-items/{item['id']}", json={"end_now": True}, headers=auth_header(token))
    if quantity > 0:
        resp = await client.post(
            "/billing/subscription-items",
            json={
                "tenant_id": str(tenant_id),
                "custom_description": "test quota",
                "category": category,
                "quantity": quantity,
            },
            headers=auth_header(token),
        )
        assert resp.status_code == 201, resp.text


async def test_create_device_with_notes(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    await _set_device_quota(client, token, two_tenants["a"]["tenant_id"], 2)  # already has 1 (device-a) from the fixture
    resp = await client.post(
        "/devices",
        json={
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
            "jt808_terminal_id": "19988877766",
            "label": "Truck 12",
            "notes": "Installed on the rear door",
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["notes"] == "Installed on the rear door"
    assert body["vehicle_id"] is None


async def test_create_device_with_vehicle_id(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = str(two_tenants["a"]["tenant_id"])
    await _set_device_quota(client, token, tenant_id, 2)  # already has 1 (device-a) from the fixture
    v_resp = await client.post(
        "/vehicles", json={"tenant_id": tenant_id, "plate": "ABC-123"}, headers=auth_header(token)
    )
    assert v_resp.status_code == 201, v_resp.text
    vehicle_id = v_resp.json()["id"]

    resp = await client.post(
        "/devices",
        json={
            "tenant_id": tenant_id,
            "jt808_terminal_id": "19988877756",
            "label": "Truck 14",
            "vehicle_id": vehicle_id,
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["vehicle_id"] == vehicle_id


async def test_create_device_rejects_vehicle_from_other_tenant(client, two_tenants, platform_users):
    """enforce_vehicle_tenant_match() (migration 0014) must reject a
    vehicle_id from a different tenant than the device's, even in a bypass
    session that evades RLS but not schema triggers."""
    token = await login(client, platform_users["super_admin"]["email"])
    await _set_device_quota(client, token, two_tenants["a"]["tenant_id"], 2)  # already has 1 (device-a) from the fixture
    v_resp = await client.post(
        "/vehicles",
        json={"tenant_id": str(two_tenants["b"]["tenant_id"]), "plate": "XYZ-999"},
        headers=auth_header(token),
    )
    vehicle_id = v_resp.json()["id"]

    resp = await client.post(
        "/devices",
        json={
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
            "jt808_terminal_id": "19988877700",
            "label": "Crossed truck",
            "vehicle_id": vehicle_id,
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_tenant_admin_cannot_create_device(client, two_tenants):
    """Creating devices remains a platform action (see the devices.py
    docstring)."""
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/devices",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "jt808_terminal_id": "19988877744", "label": "x"},
        headers=auth_header(token),
    )
    assert resp.status_code == 403


async def test_update_device_partial_does_not_touch_other_fields(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    device_id = two_tenants["a"]["device_id"]

    resp = await client.patch(
        f"/devices/{device_id}",
        json={"notes": "reviewed"},
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["notes"] == "reviewed"
    assert body["label"] == "device-a"  # untouched, comes from the two_tenants fixture


async def test_update_device_switches_gt06_to_gt06_video(client, two_tenants, platform_users):
    """An IMEI registered as gt06 (GPS-only) can be switched to gt06_video
    without deleting and re-creating it -- same physical device, same IMEI,
    only its quota/category changes (gps -> camera)."""
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    await _set_device_quota(client, token, tenant_id, 1, category="gps")
    device = await client.post(
        "/devices",
        json={"tenant_id": str(tenant_id), "protocol": "gt06", "gt06_imei": "490154203237518", "label": "Misclassified JC261"},
        headers=auth_header(token),
    )
    assert device.status_code == 201, device.text
    device_id = device.json()["id"]

    # 'camera' quota at 2 (device-a from the fixture already uses one) so the
    # category change has real room.
    await _set_device_quota(client, token, tenant_id, 2, category="camera")
    # _set_device_quota ends ALL active lines -- restore the 'gps' line that
    # was just closed too, so the scenario is "both categories have quota",
    # not "gps accidentally dropped to 0".
    resp = await client.post(
        "/billing/subscription-items",
        json={"tenant_id": str(tenant_id), "custom_description": "GPS", "category": "gps", "quantity": 1},
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text

    resp = await client.patch(
        f"/devices/{device_id}",
        json={"protocol": "gt06_video"},
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["protocol"] == "gt06_video"
    assert body["gt06_imei"] == "490154203237518"  # the identifier never changes
    assert body["jt808_terminal_id"] is None


async def test_update_device_rejects_protocol_switch_without_target_quota(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    await _set_device_quota(client, token, tenant_id, 1, category="gps")
    device = await client.post(
        "/devices",
        json={"tenant_id": str(tenant_id), "protocol": "gt06", "gt06_imei": "359000000000001", "label": "GPS only"},
        headers=auth_header(token),
    )
    assert device.status_code == 201, device.text
    device_id = device.json()["id"]

    # No 'camera' quota -- the change must be rejected and the device stays
    # as it was (gt06).
    resp = await client.patch(
        f"/devices/{device_id}",
        json={"protocol": "gt06_video"},
        headers=auth_header(token),
    )
    assert resp.status_code == 409, resp.text

    get_resp = await client.get(f"/devices/{device_id}", headers=auth_header(token))
    assert get_resp.json()["protocol"] == "gt06"


async def test_update_device_rejects_protocol_field_for_jt808(client, two_tenants, platform_users):
    """jt808 uses jt808_terminal_id, not gt06_imei -- this narrow mechanism
    (same IMEI, only the classification changes) does not apply there; it is
    still delete + create, as documented in DeviceUpdate.protocol."""
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.patch(
        f"/devices/{two_tenants['a']['device_id']}",  # jt808, from the two_tenants fixture
        json={"protocol": "gt06_video"},
        headers=auth_header(token),
    )
    assert resp.status_code == 409, resp.text


async def test_tenant_admin_cannot_update_device(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.patch(
        f"/devices/{two_tenants['a']['device_id']}",
        json={"notes": "x"},
        headers=auth_header(token),
    )
    assert resp.status_code == 403


async def test_devices_search_by_label(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.get("/devices", params={"search": "device-a"}, headers=auth_header(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(two_tenants["a"]["device_id"])


async def test_devices_pagination_respects_limit_and_total(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.get("/devices", params={"limit": 1, "offset": 0}, headers=auth_header(token))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["limit"] == 1
    assert body["offset"] == 0
    assert body["total"] >= 2  # at least the two two_tenants devices (visible because super_admin bypasses RLS)


# ---------------------------------------------------------------------------
# Device quota (devices.py::_assert_device_quota_not_exceeded): a tenant
# with 2 contracted licenses must not end up with 3 devices. Quota = SUM of
# quantity over the tenant's ACTIVE tenant_subscription_items in the
# category matching the device's protocol (jt808->camera, gt06->gps) -- not
# a single shared pool.
# ---------------------------------------------------------------------------

async def test_device_within_quota_succeeds(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    # two_tenants already has 1 active device -- a quota of 2 leaves room for one more.
    await _set_device_quota(client, token, tenant_id, 2)

    resp = await client.post(
        "/devices",
        json={"tenant_id": str(tenant_id), "jt808_terminal_id": "19900000001", "label": "Within quota"},
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text


async def test_device_at_quota_rejected_with_clear_message(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    # two_tenants already has 1 active device -- a quota of 1 is exactly at the limit.
    await _set_device_quota(client, token, tenant_id, 1)

    resp = await client.post(
        "/devices",
        json={"tenant_id": str(tenant_id), "jt808_terminal_id": "19900000002", "label": "Over quota"},
        headers=auth_header(token),
    )
    assert resp.status_code == 409
    assert "1 of 1" in resp.text


async def test_device_with_no_active_subscription_rejected(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    await _set_device_quota(client, token, tenant_id, 0)

    resp = await client.post(
        "/devices",
        json={"tenant_id": str(tenant_id), "jt808_terminal_id": "19900000003", "label": "No contract"},
        headers=auth_header(token),
    )
    assert resp.status_code == 409


async def test_ending_subscription_line_reduces_quota_for_next_attempt(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    await _set_device_quota(client, token, tenant_id, 2)

    ok = await client.post(
        "/devices",
        json={"tenant_id": str(tenant_id), "jt808_terminal_id": "19900000004", "label": "Second device"},
        headers=auth_header(token),
    )
    assert ok.status_code == 201, ok.text

    # Now the tenant downgrades to 1 license -- it already has 2 active
    # devices, so a third must be rejected.
    await _set_device_quota(client, token, tenant_id, 1)
    resp = await client.post(
        "/devices",
        json={"tenant_id": str(tenant_id), "jt808_terminal_id": "19900000005", "label": "Third device"},
        headers=auth_header(token),
    )
    assert resp.status_code == 409


async def test_quota_ignores_other_tenants(client, two_tenants, platform_users):
    """One tenant's quota must never be affected by what another tenant has
    contracted or provisioned."""
    token = await login(client, platform_users["super_admin"]["email"])
    await _set_device_quota(client, token, two_tenants["a"]["tenant_id"], 0)
    await _set_device_quota(client, token, two_tenants["b"]["tenant_id"], 50)

    resp = await client.post(
        "/devices",
        json={"tenant_id": str(two_tenants["b"]["tenant_id"]), "jt808_terminal_id": "19900000006", "label": "Tenant B"},
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# GT06 -- protocol + quota split by category (camera/gps).
# ---------------------------------------------------------------------------

async def test_create_gt06_device_with_imei(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    await _set_device_quota(client, token, tenant_id, 1, category="gps")

    resp = await client.post(
        "/devices",
        json={
            "tenant_id": str(tenant_id),
            "protocol": "gt06",
            "gt06_imei": "123456789012345",
            "label": "GPS tracker 1",
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["protocol"] == "gt06"
    assert body["gt06_imei"] == "123456789012345"
    assert body["jt808_terminal_id"] is None


async def test_create_gt06_video_device_with_imei(client, two_tenants, platform_users):
    """JC261/JC400 (gt06_video): same identifier (IMEI) as plain gt06, but it
    counts against the 'camera' category (sharing quota with jt808), not 'gps'."""
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    # 2: device-a (jt808) from the two_tenants fixture already uses 1.
    await _set_device_quota(client, token, tenant_id, 2, category="camera")

    resp = await client.post(
        "/devices",
        json={
            "tenant_id": str(tenant_id),
            "protocol": "gt06_video",
            "gt06_imei": "490154203237518",
            "label": "JC261",
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["protocol"] == "gt06_video"
    assert body["gt06_imei"] == "490154203237518"
    assert body["jt808_terminal_id"] is None


async def test_gt06_video_shares_camera_quota_with_jt808(client, two_tenants, platform_users):
    """Bug fixed in devices.py::_assert_device_quota_not_exceeded: 'used'
    counted by EXACT protocol, not by category -- with gt06_video added (also
    'camera' category), a tenant whose 'camera' quota was already full with
    jt808 could still add a gt06_video (each protocol was counted as a
    separate pool even though they shared the same subscription line). Quota
    at 1: device-a (jt808) from the fixture already uses it -- a new
    gt06_video must be rejected with the SAME 409 a second jt808 would get."""
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    await _set_device_quota(client, token, tenant_id, 1, category="camera")

    resp = await client.post(
        "/devices",
        json={
            "tenant_id": str(tenant_id),
            "protocol": "gt06_video",
            "gt06_imei": "359000000000001",
            "label": "One JC261 too many",
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 409, resp.text
    assert "camera" in resp.text


async def test_create_device_rejects_mismatched_protocol_and_identifier(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    # protocol=gt06 but carrying jt808_terminal_id instead of gt06_imei --
    # Pydantic 422 (DeviceCreate._imei_shape_and_protocol_match), never
    # reaches the database.
    resp = await client.post(
        "/devices",
        json={
            "tenant_id": str(tenant_id),
            "protocol": "gt06",
            "jt808_terminal_id": "19900000099",
            "label": "Malformed",
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_create_gt06_video_device_rejects_mismatched_identifier(client, two_tenants, platform_users):
    """Bug fixed in schemas.py::DeviceCreate._identifier_matches_protocol: the
    branch only covered protocol in ("gt06", "jt808") -- gt06_video matched NO
    branch, so this mismatch passed Pydantic unchecked and only blew up on the
    Postgres CHECK (raw 500 instead of a clean 422)."""
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    resp = await client.post(
        "/devices",
        json={
            "tenant_id": str(tenant_id),
            "protocol": "gt06_video",
            "jt808_terminal_id": "19900000099",
            "label": "Malformed",
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_gt06_quota_is_independent_of_camera_quota(client, two_tenants, platform_users):
    """The GPS quota (gt06) and the camera quota (jt808) are independent pools
    -- exhausting one must not affect the other."""
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    # Camera quota at 1 (already used by device-a from two_tenants) and GPS
    # quota at 1 (still unused).
    await _set_device_quota(client, token, tenant_id, 1, category="camera")
    # _set_device_quota ends ALL active lines before creating the new one --
    # to have both categories active at once, the GPS line is added
    # separately without going through that helper.
    resp = await client.post(
        "/billing/subscription-items",
        json={"tenant_id": str(tenant_id), "custom_description": "GPS", "category": "gps", "quantity": 1},
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text

    # A new jt808 must be rejected (camera quota already full with device-a),
    # but a gt06 must pass (GPS quota still unused).
    jt808_resp = await client.post(
        "/devices",
        json={"tenant_id": str(tenant_id), "jt808_terminal_id": "19900000007", "label": "Another camera"},
        headers=auth_header(token),
    )
    assert jt808_resp.status_code == 409

    gt06_resp = await client.post(
        "/devices",
        json={
            "tenant_id": str(tenant_id),
            "protocol": "gt06",
            "gt06_imei": "999999999999999",
            "label": "Free tracker",
        },
        headers=auth_header(token),
    )
    assert gt06_resp.status_code == 201, gt06_resp.text


async def test_devices_search_matches_gt06_imei(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    await _set_device_quota(client, token, tenant_id, 1, category="gps")
    create = await client.post(
        "/devices",
        json={
            "tenant_id": str(tenant_id),
            "protocol": "gt06",
            "gt06_imei": "555555555555555",
            "label": "Search by imei",
        },
        headers=auth_header(token),
    )
    assert create.status_code == 201, create.text

    resp = await client.get("/devices", params={"search": "555555555555555"}, headers=auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


# --- Device status (soft delete/deactivation) ------------------------------
# devices.status (active/inactive/maintenance) has a real effect in
# video.py/device_commands.py (they require status='active') and is
# changed through PATCH /devices/{id} with an audit trail -- see migration
# 0038_user_device_status_audit.sql.


async def test_bypass_can_set_device_status_and_records_who(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    device_id = two_tenants["a"]["device_id"]
    resp = await client.patch(f"/devices/{device_id}", json={"status": "maintenance"}, headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "maintenance"
    assert body["status_changed_by"] is not None
    assert body["status_changed_at"] is not None


async def test_setting_status_does_not_touch_other_fields(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    device_id = two_tenants["a"]["device_id"]
    before = (await client.get(f"/devices/{device_id}", headers=auth_header(token))).json()
    resp = await client.patch(f"/devices/{device_id}", json={"status": "inactive"}, headers=auth_header(token))
    assert resp.status_code == 200
    after = resp.json()
    assert after["label"] == before["label"]
    assert after["notes"] == before["notes"]
    assert after["status"] == "inactive"


async def test_setting_other_fields_does_not_touch_status(client, two_tenants, platform_users):
    """Confirms the boolean CASE WHEN (the fix for an IndeterminateDatatype
    bug) really leaves status_changed_by/at untouched when the PATCH does not
    touch status at all."""
    token = await login(client, platform_users["super_admin"]["email"])
    device_id = two_tenants["a"]["device_id"]
    resp = await client.patch(f"/devices/{device_id}", json={"notes": "reviewed"}, headers=auth_header(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    assert body["status_changed_by"] is None
    assert body["status_changed_at"] is None


async def test_tenant_admin_cannot_change_device_status(client, two_tenants):
    """update_device remains require_bypass -- changing a device's status is
    the same platform boundary as creating/editing it, not tenant_admin
    self-service."""
    token = await login(client, two_tenants["a"]["email"])
    device_id = two_tenants["a"]["device_id"]
    resp = await client.patch(f"/devices/{device_id}", json={"status": "inactive"}, headers=auth_header(token))
    assert resp.status_code == 403


async def test_inactive_device_is_excluded_from_quota_usage(client, two_tenants, platform_users):
    """devices.status='active' is part of the quota calculation
    (_assert_device_quota_not_exceeded) -- deactivating a device frees its
    slot for a new one without deleting its telemetry history."""
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    await _set_device_quota(client, token, tenant_id, 1, category="camera")

    # two_tenants already has an active jt808 device for the tenant -- it
    # already uses the quota of 1, so a new one must be rejected.
    rejected = await client.post(
        "/devices",
        json={"tenant_id": str(tenant_id), "protocol": "jt808", "jt808_terminal_id": "918273645", "label": "new"},
        headers=auth_header(token),
    )
    assert rejected.status_code == 409

    await client.patch(f"/devices/{two_tenants['a']['device_id']}", json={"status": "inactive"}, headers=auth_header(token))

    accepted = await client.post(
        "/devices",
        json={"tenant_id": str(tenant_id), "protocol": "jt808", "jt808_terminal_id": "918273646", "label": "new"},
        headers=auth_header(token),
    )
    assert accepted.status_code == 201, accepted.text


async def test_invalid_device_status_value_rejected(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    device_id = two_tenants["a"]["device_id"]
    resp = await client.patch(f"/devices/{device_id}", json={"status": "deleted"}, headers=auth_header(token))
    assert resp.status_code == 422


async def test_list_devices_exclude_inactive(client, two_tenants, superuser_conn):
    """Operational views request exclude_inactive=true and do not get
    deactivated units; without the parameter the list stays complete (API
    keys and the admin table with "Show deactivated")."""
    device_id = two_tenants["a"]["device_id"]
    cur = superuser_conn.cursor()
    await cur.execute("UPDATE devices SET status = 'inactive' WHERE id = %s", (device_id,))
    try:
        token = await login(client, two_tenants["a"]["email"])
        hidden = await client.get("/devices?exclude_inactive=true", headers=auth_header(token))
        assert hidden.status_code == 200, hidden.text
        assert str(device_id) not in [d["id"] for d in hidden.json()["items"]]
        full = await client.get("/devices", headers=auth_header(token))
        assert str(device_id) in [d["id"] for d in full.json()["items"]]
    finally:
        await cur.execute("UPDATE devices SET status = 'active' WHERE id = %s", (device_id,))
