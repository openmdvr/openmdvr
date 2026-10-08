"""Geofences (0052_geofences.sql + routers/geofences.py).

The evaluation engine lives in insert_gps_position() (the same single entry
point that jt808server and gt06server already call) -- these tests drive it
through direct SQL, like test_vehicle_max_speed.py, without a protocol
simulator: the logic is protocol-agnostic by construction."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import db as db_module
from app.security import hash_password
from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio

CENTER = (32.5, -116.9)
FAR = (32.0, -117.0)


async def _pos(pool, tenant_id, device_id, lat, lon, when, speed=10.0):
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "SELECT insert_gps_position(%s, %s, %s, %s, %s, %s::real, NULL, NULL, NULL)",
            (tenant_id, device_id, when, lat, lon, speed),
        )


async def _create_user(pool, tenant_id, role):
    email = f"{role}-{uuid.uuid4().hex[:8]}@example.com"
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (%s, %s, %s, %s) RETURNING id",
                (tenant_id, email, hash_password(TEST_PASSWORD), role),
            )
        ).fetchone()
    return row[0], email


def _circle(**overrides):
    body = {"name": f"Warehouse {uuid.uuid4().hex[:6]}", "shape": "circle",
            "center_lat": CENTER[0], "center_lon": CENTER[1], "radius_m": 500}
    body.update(overrides)
    return body


def _window():
    now = datetime.now(timezone.utc)
    return {"from": (now - timedelta(days=1)).isoformat(), "to": (now + timedelta(hours=1)).isoformat()}


async def test_create_list_update_delete_circle_and_polygon(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post("/geofences", json=_circle(), headers=auth_header(token))
    assert resp.status_code == 201, resp.text
    circle = resp.json()
    assert circle["tenant_id"] == str(two_tenants["a"]["tenant_id"])
    assert circle["notify_on_enter"] and circle["notify_on_exit"] and circle["enabled"]

    poly = [[32.49, -116.91], [32.49, -116.89], [32.51, -116.89], [32.51, -116.91]]
    resp = await client.post(
        "/geofences",
        json={"name": "Yard", "shape": "polygon", "polygon": poly, "color": "#10b981", "dwell_minutes": 15},
        headers=auth_header(token),
    )
    assert resp.status_code == 201, resp.text
    polygon_id = resp.json()["id"]
    assert resp.json()["polygon"] == poly

    listed = await client.get("/geofences", headers=auth_header(token))
    assert listed.status_code == 200
    assert listed.json()["total"] == 2

    # PATCH: redrawing as a circle replaces the whole geometry.
    patched = await client.patch(
        f"/geofences/{polygon_id}",
        json={"shape": "circle", "center_lat": 32.6, "center_lon": -116.8, "radius_m": 200, "dwell_minutes": None},
        headers=auth_header(token),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["polygon"] is None and patched.json()["radius_m"] == 200
    assert patched.json()["dwell_minutes"] is None

    deleted = await client.delete(f"/geofences/{polygon_id}", headers=auth_header(token))
    assert deleted.status_code == 204
    assert (await client.get(f"/geofences/{polygon_id}", headers=auth_header(token))).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x", "shape": "circle", "center_lat": 32.5, "center_lon": -116.9},  # no radius
        {"name": "x", "shape": "circle", "center_lat": 32.5, "center_lon": -116.9, "radius_m": 1},  # below minimum radius
        {"name": "x", "shape": "polygon", "polygon": [[1, 1], [2, 2]]},  # < 3 vertices
        {"name": "x", "shape": "polygon", "polygon": [[1, 1], [2, 2], [3, 3]]},  # no area
        {"name": "x", "shape": "polygon", "polygon": [[1, 1], [95, 2], [3, 1]]},  # out of range
        {"name": "x", "shape": "circle", "center_lat": 1, "center_lon": 1, "radius_m": 50, "color": "red"},
    ],
)
async def test_invalid_geometry_rejected(client, two_tenants, body):
    token = await login(client, two_tenants["a"]["email"])
    resp = await client.post("/geofences", json=body, headers=auth_header(token))
    assert resp.status_code == 422, resp.text


async def test_patch_geometry_without_shape_rejected(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    gid = (await client.post("/geofences", json=_circle(), headers=auth_header(token))).json()["id"]
    resp = await client.patch(f"/geofences/{gid}", json={"radius_m": 900}, headers=auth_header(token))
    assert resp.status_code == 422


async def test_duplicate_name_conflict(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    body = _circle(name="Same name")
    assert (await client.post("/geofences", json=body, headers=auth_header(token))).status_code == 201
    assert (await client.post("/geofences", json=body, headers=auth_header(token))).status_code == 409


async def test_tenant_isolation(client, two_tenants):
    token_a = await login(client, two_tenants["a"]["email"])
    token_b = await login(client, two_tenants["b"]["email"])
    gid = (await client.post("/geofences", json=_circle(), headers=auth_header(token_a))).json()["id"]

    assert (await client.get("/geofences", headers=auth_header(token_b))).json()["total"] == 0
    assert (await client.get(f"/geofences/{gid}", headers=auth_header(token_b))).status_code == 404
    assert (await client.patch(f"/geofences/{gid}", json={"name": "hack"}, headers=auth_header(token_b))).status_code == 404
    assert (await client.delete(f"/geofences/{gid}", headers=auth_header(token_b))).status_code == 404
    assert (await client.get(f"/geofences/{gid}/occupancy", headers=auth_header(token_b))).status_code == 404

    # B tries to create a geofence in A's tenant.
    resp = await client.post(
        "/geofences", json=_circle(tenant_id=str(two_tenants["a"]["tenant_id"])), headers=auth_header(token_b)
    )
    assert resp.status_code == 403

    # B tries to put one of A's devices in the scope of its own geofence.
    resp = await client.post(
        "/geofences",
        json=_circle(applies_to_all_devices=False, device_ids=[str(two_tenants["a"]["device_id"])]),
        headers=auth_header(token_b),
    )
    assert resp.status_code == 422


async def test_non_admin_roles_cannot_write(client, two_tenants, pool):
    tenant_id = two_tenants["a"]["tenant_id"]
    token_admin = await login(client, two_tenants["a"]["email"])
    gid = (await client.post("/geofences", json=_circle(), headers=auth_header(token_admin))).json()["id"]
    for role in ("tenant_operator", "tenant_viewer"):
        _, email = await _create_user(pool, tenant_id, role)
        token = await login(client, email)
        assert (await client.get("/geofences", headers=auth_header(token))).status_code == 200
        assert (await client.post("/geofences", json=_circle(), headers=auth_header(token))).status_code == 403
        assert (await client.patch(f"/geofences/{gid}", json={"name": "x"}, headers=auth_header(token))).status_code == 403
        assert (await client.delete(f"/geofences/{gid}", headers=auth_header(token))).status_code == 403


async def test_platform_must_specify_tenant(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    assert (await client.post("/geofences", json=_circle(), headers=auth_header(token))).status_code == 422
    resp = await client.post(
        "/geofences", json=_circle(tenant_id=str(two_tenants["b"]["tenant_id"])), headers=auth_header(token)
    )
    assert resp.status_code == 201
    assert resp.json()["tenant_id"] == str(two_tenants["b"]["tenant_id"])


async def test_enter_exit_generates_events_alarms_and_notifications(client, two_tenants, pool, superuser_conn):
    a = two_tenants["a"]
    token = await login(client, a["email"])
    gid = (await client.post("/geofences", json=_circle(name="North Customer"), headers=auth_header(token))).json()["id"]

    t0 = datetime.now(timezone.utc) - timedelta(minutes=30)
    await _pos(pool, a["tenant_id"], a["device_id"], *FAR, t0)
    await _pos(pool, a["tenant_id"], a["device_id"], *CENTER, t0 + timedelta(minutes=1))
    # several pings inside: never one event per ping
    for i in range(2, 5):
        await _pos(pool, a["tenant_id"], a["device_id"], *CENTER, t0 + timedelta(minutes=i))

    occ = await client.get(f"/geofences/{gid}/occupancy", headers=auth_header(token))
    assert [o["device_id"] for o in occ.json()] == [str(a["device_id"])]
    assert (await client.get(f"/geofences/{gid}", headers=auth_header(token))).json()["inside_count"] == 1

    await _pos(pool, a["tenant_id"], a["device_id"], *FAR, t0 + timedelta(minutes=11))

    events = (await client.get("/geofences/events", params=_window(), headers=auth_header(token))).json()
    assert [e["event_type"] for e in events["items"]] == ["exit", "enter"]
    exit_event = events["items"][0]
    assert exit_event["duration_s"] == 600
    assert exit_event["geofence_name"] == "North Customer"
    assert exit_event["alarm_id"] is not None

    cur = superuser_conn.cursor()
    await cur.execute(
        "SELECT alarm_type FROM alarms WHERE device_id = %s AND alarm_type LIKE 'geofence_%%' ORDER BY time",
        (a["device_id"],),
    )
    assert [r[0] for r in await cur.fetchall()] == ["geofence_enter", "geofence_exit"]

    notifs = (await client.get("/notifications", headers=auth_header(token))).json()["items"]
    bodies = {n["body"] for n in notifs}
    assert "Entered «North Customer»" in bodies and "Exited «North Customer»" in bodies

    report = (await client.get("/geofences/report", params=_window(), headers=auth_header(token))).json()
    row = report["geofences"][0]
    assert (row["enters"], row["exits"], row["unique_devices"], row["total_inside_s"]) == (1, 1, 1, 600)
    assert report["visits"][0]["duration_s"] == 600 and report["visits"][0]["open"] is False


async def test_notify_disabled_still_records_event_without_alarm(client, two_tenants, pool, superuser_conn):
    a = two_tenants["a"]
    token = await login(client, a["email"])
    await client.post("/geofences", json=_circle(notify_on_enter=False), headers=auth_header(token))
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    await _pos(pool, a["tenant_id"], a["device_id"], *FAR, t0)
    await _pos(pool, a["tenant_id"], a["device_id"], *CENTER, t0 + timedelta(minutes=1))

    events = (await client.get("/geofences/events", params=_window(), headers=auth_header(token))).json()["items"]
    assert len(events) == 1 and events[0]["event_type"] == "enter" and events[0]["alarm_id"] is None
    cur = superuser_conn.cursor()
    await cur.execute("SELECT count(*) FROM alarms WHERE device_id = %s AND alarm_type LIKE 'geofence_%%'", (a["device_id"],))
    assert (await cur.fetchone())[0] == 0


async def test_creating_geofence_over_parked_unit_is_silent(client, two_tenants, pool):
    a = two_tenants["a"]
    await _pos(pool, a["tenant_id"], a["device_id"], *CENTER, datetime.now(timezone.utc) - timedelta(minutes=5))
    token = await login(client, a["email"])
    gid = (await client.post("/geofences", json=_circle(), headers=auth_header(token))).json()["id"]

    occ = (await client.get(f"/geofences/{gid}/occupancy", headers=auth_header(token))).json()
    assert len(occ) == 1 and occ[0]["entry_estimated"] is True
    # Another ping inside does not produce a late "entered".
    await _pos(pool, a["tenant_id"], a["device_id"], *CENTER, datetime.now(timezone.utc))
    events = (await client.get("/geofences/events", params=_window(), headers=auth_header(token))).json()
    assert events["total"] == 0


async def test_scope_limited_to_selected_devices(client, two_tenants, pool):
    a = two_tenants["a"]
    token = await login(client, a["email"])
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        other = (await (await conn.execute(
            "INSERT INTO devices (tenant_id, jt808_terminal_id, label, status) VALUES (%s, %s, 'other', 'active') RETURNING id",
            (a["tenant_id"], str(int(uuid.uuid4().hex[:8], 16))),
        )).fetchone())[0]
    resp = await client.post(
        "/geofences", json=_circle(applies_to_all_devices=False, device_ids=[str(other)]), headers=auth_header(token)
    )
    assert resp.status_code == 201 and resp.json()["device_ids"] == [str(other)]

    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    for dev in (a["device_id"], other):
        await _pos(pool, a["tenant_id"], dev, *FAR, t0)
        await _pos(pool, a["tenant_id"], dev, *CENTER, t0 + timedelta(minutes=1))
    events = (await client.get("/geofences/events", params=_window(), headers=auth_header(token))).json()["items"]
    assert [e["device_id"] for e in events] == [str(other)]


async def test_operator_without_assignment_sees_no_events(client, two_tenants, pool):
    a = two_tenants["a"]
    token_admin = await login(client, a["email"])
    await client.post("/geofences", json=_circle(), headers=auth_header(token_admin))
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    await _pos(pool, a["tenant_id"], a["device_id"], *FAR, t0)
    await _pos(pool, a["tenant_id"], a["device_id"], *CENTER, t0 + timedelta(minutes=1))

    _, email = await _create_user(pool, a["tenant_id"], "tenant_operator")
    token = await login(client, email)
    events = (await client.get("/geofences/events", params=_window(), headers=auth_header(token))).json()
    assert events["total"] == 0
    listed = (await client.get("/geofences", headers=auth_header(token))).json()["items"]
    # Sees the geofence (tenant configuration), but not the occupancy of
    # units that are not assigned to it.
    assert len(listed) == 1 and listed[0]["inside_count"] == 0


async def test_driver_forbidden(client, two_tenants, pool):
    a = two_tenants["a"]
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        driver_id = (await (await conn.execute(
            "INSERT INTO drivers (tenant_id, name) VALUES (%s, 'Driver GF') RETURNING id", (a["tenant_id"],)
        )).fetchone())[0]
        email = f"driver-{uuid.uuid4().hex[:8]}@example.com"
        await conn.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role, driver_id) VALUES (%s, %s, %s, 'driver', %s)",
            (a["tenant_id"], email, hash_password(TEST_PASSWORD), driver_id),
        )
    token = await login(client, email)
    assert (await client.get("/geofences", headers=auth_header(token))).status_code == 403
    assert (await client.get("/geofences/events", params=_window(), headers=auth_header(token))).status_code == 403


async def test_report_window_capped(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    now = datetime.now(timezone.utc)
    params = {"from": (now - timedelta(days=120)).isoformat(), "to": now.isoformat()}
    assert (await client.get("/geofences/report", params=params, headers=auth_header(token))).status_code == 422
    assert (await client.get("/geofences/events", params=params, headers=auth_header(token))).status_code == 422


async def test_app_user_cannot_forge_events_or_bbox(two_tenants, pool):
    """State/events have no write GRANT, and bbox_* is outside the per-column
    UPDATE GRANT -- no user can fabricate an event for a report or "shrink" the
    bbox so a geofence stops being evaluated."""
    import psycopg

    a = two_tenants["a"]
    async with db_module.tenant_scoped_connection(pool, tenant_id=str(a["tenant_id"]), bypass=False, user_id=str(a["user_id"])) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO geofence_events (tenant_id, geofence_name, device_id, event_type, time, lat, lon)
                       VALUES (%s, 'fake', %s, 'enter', now(), 0, 0)""",
                    (a["tenant_id"], a["device_id"]),
                )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with conn.transaction():
                await conn.execute("UPDATE geofences SET bbox_min_lat = 0")


async def test_latest_positions_returns_newest_per_device(client, two_tenants, pool):
    a = two_tenants["a"]
    now = datetime.now(timezone.utc)
    await _pos(pool, a["tenant_id"], a["device_id"], 10.0, 10.0, now - timedelta(hours=2))
    await _pos(pool, a["tenant_id"], a["device_id"], 11.0, 11.0, now - timedelta(minutes=1))
    token = await login(client, a["email"])
    rows = (await client.get("/positions/latest", headers=auth_header(token))).json()
    assert [(r["device_id"], r["lat"]) for r in rows] == [(str(a["device_id"]), 11.0)]


async def test_polygon_vertex_budget_enforced_in_database(client, two_tenants, pool):
    """Performance regression: evaluation runs on EVERY GPS position, so a
    tenant cannot accumulate polygon vertices without a limit (worst case
    measured without the limit: ~65 s per position with the original jsonb
    implementation; with arrays + this limit, ~50 ms). The limit lives in the
    database, not only in the API."""
    import math

    a = two_tenants["a"]
    ring = [[32 + 0.1 * math.sin(2 * math.pi * k / 500), -117 + 0.1 * math.cos(2 * math.pi * k / 500)] for k in range(500)]
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        import json as _json

        for i in range(50):
            await conn.execute(
                """INSERT INTO geofences (tenant_id, name, shape, polygon, bbox_min_lat, bbox_max_lat, bbox_min_lon, bbox_max_lon)
                   VALUES (%s, %s, 'polygon', %s::jsonb, 0, 0, 0, 0)""",
                (a["tenant_id"], f"budget-{i}", _json.dumps(ring)),
            )
    token = await login(client, a["email"])
    resp = await client.post(
        "/geofences", json={"name": "one more", "shape": "polygon", "polygon": ring[:10]}, headers=auth_header(token)
    )
    assert resp.status_code == 409, resp.text
    # Circles do not consume the budget (constant cost).
    assert (await client.post("/geofences", json=_circle(), headers=auth_header(token))).status_code == 201


# --- Security review regressions ---


async def test_future_timestamp_does_not_freeze_state(client, two_tenants, pool):
    """Finding 1: a single position with a future timestamp froze the unit's
    geofence state forever (JT808 does not bound the timestamp)."""
    a = two_tenants["a"]
    token = await login(client, a["email"])
    gid = (await client.post("/geofences", json=_circle(), headers=auth_header(token))).json()["id"]
    now = datetime.now(timezone.utc)
    await _pos(pool, a["tenant_id"], a["device_id"], *FAR, now - timedelta(minutes=10))
    await _pos(pool, a["tenant_id"], a["device_id"], *CENTER, now - timedelta(minutes=8))
    await _pos(pool, a["tenant_id"], a["device_id"], *CENTER, datetime(2099, 1, 1, tzinfo=timezone.utc))
    await _pos(pool, a["tenant_id"], a["device_id"], *FAR, now)
    events = (await client.get("/geofences/events", params=_window(), headers=auth_header(token))).json()["items"]
    assert [e["event_type"] for e in events] == ["exit", "enter"]
    assert (await client.get(f"/geofences/{gid}/occupancy", headers=auth_header(token))).json() == []


async def test_border_jitter_is_debounced(client, two_tenants, pool):
    """Finding 2 (debounce): entries/exits < 60 s after the previous
    transition do not change the state, even with hysteresis_m=0."""
    a = two_tenants["a"]
    token = await login(client, a["email"])
    await client.post("/geofences", json=_circle(hysteresis_m=0), headers=auth_header(token))
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    await _pos(pool, a["tenant_id"], a["device_id"], *FAR, t0)
    await _pos(pool, a["tenant_id"], a["device_id"], *CENTER, t0 + timedelta(minutes=1))
    for i in range(1, 6):  # exits and enters every 5 s
        point = FAR if i % 2 else CENTER
        await _pos(pool, a["tenant_id"], a["device_id"], *point, t0 + timedelta(minutes=1, seconds=5 * i))
    events = (await client.get("/geofences/events", params=_window(), headers=auth_header(token))).json()["items"]
    assert [e["event_type"] for e in events] == ["enter"]


async def test_notifications_capped_per_position(client, two_tenants, pool, superuser_conn):
    """Finding 2 (amplification): 30 overlapping geofences do not produce 30
    alarms from a single ping -- capped at 20; all 30 events remain in the
    report."""
    a = two_tenants["a"]
    token = await login(client, a["email"])
    for _ in range(30):
        await client.post("/geofences", json=_circle(), headers=auth_header(token))
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    await _pos(pool, a["tenant_id"], a["device_id"], *FAR, t0)
    await _pos(pool, a["tenant_id"], a["device_id"], *CENTER, t0 + timedelta(minutes=1))
    events = (await client.get("/geofences/events", params=_window(), headers=auth_header(token))).json()
    assert events["total"] == 30
    cur = superuser_conn.cursor()
    await cur.execute("SELECT count(*) FROM alarms WHERE device_id = %s AND alarm_type = 'geofence_enter'", (a["device_id"],))
    assert (await cur.fetchone())[0] == 20


async def test_naive_and_aware_dates_do_not_500(client, two_tenants):
    """Finding 3: a naive `from` + timezone-aware `to` raised TypeError -> 500."""
    token = await login(client, two_tenants["a"]["email"])
    params = {"from": "2026-01-01T00:00:00", "to": "2026-01-02T00:00:00Z"}
    assert (await client.get("/geofences/events", params=params, headers=auth_header(token))).status_code == 200
    assert (await client.get("/geofences/report", params=params, headers=auth_header(token))).status_code == 200


async def test_device_scoped_session_cannot_scope_foreign_device(two_tenants, pool):
    """Finding 4: a session scoped to certain devices (API key with
    allowed_device_ids) cannot put a device outside its own scope into a
    geofence's scope."""
    import psycopg

    a = two_tenants["a"]
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        other = (await (await conn.execute(
            "INSERT INTO devices (tenant_id, jt808_terminal_id, label, status) VALUES (%s, %s, 'scoped', 'active') RETURNING id",
            (a["tenant_id"], str(int(uuid.uuid4().hex[:8], 16))),
        )).fetchone())[0]
        gid = (await (await conn.execute(
            """INSERT INTO geofences (tenant_id, name, shape, center_lat, center_lon, radius_m,
                                      bbox_min_lat, bbox_max_lat, bbox_min_lon, bbox_max_lon)
               VALUES (%s, 'scoped-fence', 'circle', 1, 1, 100, 0, 0, 0, 0) RETURNING id""",
            (a["tenant_id"],),
        )).fetchone())[0]
    async with db_module.tenant_scoped_connection(
        pool, tenant_id=str(a["tenant_id"]), bypass=False, user_id=str(a["user_id"]),
        api_key_device_filter=(str(other),),
    ) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO geofence_devices (geofence_id, device_id, tenant_id) VALUES (%s, %s, %s)",
                    (gid, a["device_id"], a["tenant_id"]),
                )
        # The device within its scope can.
        await conn.execute(
            "INSERT INTO geofence_devices (geofence_id, device_id, tenant_id) VALUES (%s, %s, %s)",
            (gid, other, a["tenant_id"]),
        )


async def test_platform_unknown_tenant_is_422(client, platform_users):
    """Finding 7: a nonexistent tenant_id returned 500 (ForeignKeyViolation)."""
    token = await login(client, platform_users["super_admin"]["email"])
    resp = await client.post("/geofences", json=_circle(tenant_id=str(uuid.uuid4())), headers=auth_header(token))
    assert resp.status_code == 422


async def test_support_can_manage_and_viewer_reads(client, two_tenants, platform_users, pool):
    token = await login(client, platform_users["support"]["email"])
    resp = await client.post(
        "/geofences", json=_circle(tenant_id=str(two_tenants["a"]["tenant_id"])), headers=auth_header(token)
    )
    assert resp.status_code == 201
    _, email = await _create_user(pool, two_tenants["a"]["tenant_id"], "tenant_viewer")
    viewer = await login(client, email)
    assert (await client.get(f"/geofences/{resp.json()['id']}", headers=auth_header(viewer))).status_code == 200
    assert (await client.get("/geofences/report", params=_window(), headers=auth_header(viewer))).status_code == 200
