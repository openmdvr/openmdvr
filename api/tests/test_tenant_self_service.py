"""Tenant self-service: PATCH /tenants/{id}/settings (branding + driver
policy) and the alerts the policy generates (driver_shift_alerts).
SECURITY-SENSITIVE: it widens tenants_update (RLS) -- this file focuses on
the same kind of attack tested for drivers: cross-tenant IDOR and role
escalation, this time on the `tenants` row itself."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _create_driver_login(client, token, tenant_id, driver_id):
    email = f"driver-ss-{uuid.uuid4().hex[:8]}@example.com"
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


async def _insert_event_at(pool, tenant_id, driver_id, event_type, when):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "INSERT INTO driver_shift_events (tenant_id, driver_id, event_type, occurred_at) VALUES (%s, %s, %s, %s)",
            (tenant_id, driver_id, event_type, when),
        )


# ---------------------------------------------------------------------------
# PATCH /tenants/{id}/settings -- permissions and IDOR
# ---------------------------------------------------------------------------

async def test_tenant_admin_can_update_own_settings(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    resp = await client.patch(
        f"/tenants/{tenant_id}/settings",
        json={
            "display_name": "Acme Fleet",
            "logo_url": "https://cdn.example.com/acme-logo.png",
            "meal_break_window_start": "12:00",
            "meal_break_window_end": "13:30",
            "max_shift_hours": 10,
        },
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_name"] == "Acme Fleet"
    assert body["logo_url"] == "https://cdn.example.com/acme-logo.png"
    assert body["meal_break_window_start"] == "12:00"
    assert body["meal_break_window_end"] == "13:30"
    assert body["max_shift_hours"] == 10.0


async def test_tenant_admin_can_clear_policy_to_null(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    await client.patch(
        f"/tenants/{tenant_id}/settings",
        json={"meal_break_window_start": "12:00", "meal_break_window_end": "13:00", "max_shift_hours": 8},
        headers=auth_header(token),
    )
    resp = await client.patch(f"/tenants/{tenant_id}/settings", json={}, headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meal_break_window_start"] is None
    assert body["meal_break_window_end"] is None
    assert body["max_shift_hours"] is None


async def test_meal_window_requires_both_fields(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    resp = await client.patch(
        f"/tenants/{tenant_id}/settings",
        json={"meal_break_window_start": "12:00"},
        headers=auth_header(token),
    )
    assert resp.status_code == 422


async def test_tenant_admin_cannot_update_other_tenants_settings(client, two_tenants):
    """The central requirement: tenant A's tenant_admin cannot touch B's row
    no matter if they know its id."""
    token_a = await login(client, two_tenants["a"]["email"])
    tenant_b = two_tenants["b"]["tenant_id"]
    resp = await client.patch(
        f"/tenants/{tenant_b}/settings",
        json={"display_name": "Hijacked"},
        headers=auth_header(token_a),
    )
    assert resp.status_code == 404

    # Confirms B's row genuinely did not change, not just that the API
    # returned 404 while the write went through some other way.
    token_b = await login(client, two_tenants["b"]["email"])
    check = await client.get("/tenants", params={"search": ""}, headers=auth_header(token_b))
    assert check.status_code == 200
    row = next(t for t in check.json()["items"] if t["id"] == str(tenant_b))
    assert row["display_name"] is None


async def test_tenant_operator_cannot_update_settings(client, two_tenants, pool):
    from app import db as db_module
    from app.security import hash_password

    tenant_id = two_tenants["a"]["tenant_id"]
    email = f"operator-ss-{uuid.uuid4().hex[:8]}@example.com"
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (%s, %s, %s, 'tenant_operator')",
            (tenant_id, email, hash_password(TEST_PASSWORD)),
        )
    token = await login(client, email)
    resp = await client.patch(f"/tenants/{tenant_id}/settings", json={"display_name": "Nope"}, headers=auth_header(token))
    assert resp.status_code == 403


async def test_driver_cannot_update_settings(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver SS"}, headers=auth_header(token))
    ).json()
    email = await _create_driver_login(client, token, tenant_id, driver["id"])
    driver_token = await login(client, email)

    resp = await client.patch(f"/tenants/{tenant_id}/settings", json={"display_name": "Nope"}, headers=auth_header(driver_token))
    assert resp.status_code == 403


async def test_settings_endpoint_cannot_touch_billing_fields(client, two_tenants):
    """Escalation to test: the TenantSettingsUpdate body has no quota field --
    sending one anyway must change nothing (Pydantic ignores unknown fields
    by default; this confirms it still does)."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    before = await client.get("/tenants", params={"search": ""}, headers=auth_header(token))
    before_quota = next(t for t in before.json()["items"] if t["id"] == str(tenant_id))["live_view_monthly_quota_seconds"]

    resp = await client.patch(
        f"/tenants/{tenant_id}/settings",
        json={"display_name": "X", "live_view_monthly_quota_seconds": 999999},
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["live_view_monthly_quota_seconds"] == before_quota


async def test_platform_admin_can_update_any_tenant_settings(client, two_tenants, platform_users):
    token = await login(client, platform_users["super_admin"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    resp = await client.patch(
        f"/tenants/{tenant_id}/settings", json={"display_name": "Configured by support"}, headers=auth_header(token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "Configured by support"


# ---------------------------------------------------------------------------
# Policy alerts -- informational only, they never block the event
# ---------------------------------------------------------------------------

async def test_meal_start_outside_window_creates_alert_but_still_succeeds(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Meal"}, headers=auth_header(token))
    ).json()
    email = await _create_driver_login(client, token, tenant_id, driver["id"])
    driver_token = await login(client, email)

    # Window in the future relative to "now" -- guarantees the next
    # meal_start falls OUTSIDE regardless of what time the test runs.
    future_start = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%H:%M")
    future_end = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M")
    await client.patch(
        f"/tenants/{tenant_id}/settings",
        json={"meal_break_window_start": future_start, "meal_break_window_end": future_end},
        headers=auth_header(token),
    )

    clock_in = await client.post("/shifts/clock", json={"event_type": "clock_in"}, headers=auth_header(driver_token))
    assert clock_in.status_code == 201
    meal = await client.post("/shifts/clock", json={"event_type": "meal_start"}, headers=auth_header(driver_token))
    assert meal.status_code == 201, "alert-only: the real event must always be recorded anyway"

    alerts = await client.get("/driver-shift-alerts", headers=auth_header(token))
    assert alerts.status_code == 200
    items = [a for a in alerts.json()["items"] if a["driver_id"] == driver["id"]]
    assert len(items) == 1
    assert items[0]["alert_type"] == "meal_outside_window"
    assert items[0]["acknowledged_at"] is None


async def test_meal_start_inside_window_creates_no_alert(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Meal OK"}, headers=auth_header(token))
    ).json()
    email = await _create_driver_login(client, token, tenant_id, driver["id"])
    driver_token = await login(client, email)

    await client.patch(
        f"/tenants/{tenant_id}/settings",
        json={"meal_break_window_start": "00:00", "meal_break_window_end": "23:59"},
        headers=auth_header(token),
    )
    await client.post("/shifts/clock", json={"event_type": "clock_in"}, headers=auth_header(driver_token))
    await client.post("/shifts/clock", json={"event_type": "meal_start"}, headers=auth_header(driver_token))

    alerts = await client.get("/driver-shift-alerts", headers=auth_header(token))
    assert all(a["driver_id"] != driver["id"] for a in alerts.json()["items"])


async def test_shift_exceeding_max_hours_creates_single_alert(client, two_tenants, pool):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Long Shift"}, headers=auth_header(token))
    ).json()
    email = await _create_driver_login(client, token, tenant_id, driver["id"])
    driver_token = await login(client, email)

    await client.patch(f"/tenants/{tenant_id}/settings", json={"max_shift_hours": 4}, headers=auth_header(token))
    # clock_in inserted directly 6h in the past -- simulates an already long
    # shift without waiting 6 real hours.
    await _insert_event_at(pool, tenant_id, driver["id"], "clock_in", datetime.now(timezone.utc) - timedelta(hours=6))

    r1 = await client.post("/shifts/clock", json={"event_type": "meal_start"}, headers=auth_header(driver_token))
    assert r1.status_code == 201
    r2 = await client.post("/shifts/clock", json={"event_type": "meal_end"}, headers=auth_header(driver_token))
    assert r2.status_code == 201
    r3 = await client.post("/shifts/clock", json={"event_type": "clock_out"}, headers=auth_header(driver_token))
    assert r3.status_code == 201, "alert-only: never blocks, not even the clock_out that closes the exceeded shift"

    alerts = (await client.get("/driver-shift-alerts", headers=auth_header(token))).json()["items"]
    matching = [a for a in alerts if a["driver_id"] == driver["id"] and a["alert_type"] == "shift_exceeds_max_hours"]
    assert len(matching) == 1, "a single alert per exceeded shift, not one for every later event"


async def test_acknowledge_alert(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Acknowledges"}, headers=auth_header(token))
    ).json()
    email = await _create_driver_login(client, token, tenant_id, driver["id"])
    driver_token = await login(client, email)
    future_start = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%H:%M")
    future_end = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M")
    await client.patch(
        f"/tenants/{tenant_id}/settings",
        json={"meal_break_window_start": future_start, "meal_break_window_end": future_end},
        headers=auth_header(token),
    )
    await client.post("/shifts/clock", json={"event_type": "clock_in"}, headers=auth_header(driver_token))
    await client.post("/shifts/clock", json={"event_type": "meal_start"}, headers=auth_header(driver_token))
    alert = next(
        a for a in (await client.get("/driver-shift-alerts", headers=auth_header(token))).json()["items"]
        if a["driver_id"] == driver["id"]
    )

    resp = await client.post(f"/driver-shift-alerts/{alert['id']}/acknowledge", headers=auth_header(token))
    assert resp.status_code == 204

    again = await client.post(f"/driver-shift-alerts/{alert['id']}/acknowledge", headers=auth_header(token))
    assert again.status_code == 404, "already acknowledged -- cannot be acknowledged twice"


async def test_driver_cannot_list_or_acknowledge_alerts(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Without Alerts"}, headers=auth_header(token))
    ).json()
    email = await _create_driver_login(client, token, tenant_id, driver["id"])
    driver_token = await login(client, email)

    resp = await client.get("/driver-shift-alerts", headers=auth_header(driver_token))
    assert resp.status_code == 403


async def test_tenant_b_cannot_see_tenant_a_alerts(client, two_tenants):
    token_a = await login(client, two_tenants["a"]["email"])
    token_b = await login(client, two_tenants["b"]["email"])
    tenant_a = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_a), "name": "Driver A Alerts"}, headers=auth_header(token_a))
    ).json()
    email = await _create_driver_login(client, token_a, tenant_a, driver["id"])
    driver_token = await login(client, email)
    future_start = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%H:%M")
    future_end = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M")
    await client.patch(
        f"/tenants/{tenant_a}/settings",
        json={"meal_break_window_start": future_start, "meal_break_window_end": future_end},
        headers=auth_header(token_a),
    )
    await client.post("/shifts/clock", json={"event_type": "clock_in"}, headers=auth_header(driver_token))
    await client.post("/shifts/clock", json={"event_type": "meal_start"}, headers=auth_header(driver_token))

    resp = await client.get("/driver-shift-alerts", headers=auth_header(token_b))
    assert resp.status_code == 200
    assert all(a["driver_id"] != driver["id"] for a in resp.json()["items"])
