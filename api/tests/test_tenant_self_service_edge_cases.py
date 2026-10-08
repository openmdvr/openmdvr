"""Edge cases found by the security review of tenant self-service that
were not covered in test_tenant_self_service.py:
  - tenant_viewer explicitly rejected (previously only operator/driver were tested)
  - forbidden/unknown fields (status, name, quota) really ignored
  - NaN/Infinity in a float field against Field(ge=, le=) -- see the fix in
    main.py (validation_exception_handler): without it, this took the API
    down with a raw 500 instead of a clean 422
  - a driver holding a REAL alert id (not just the list) still cannot acknowledge it
  - the exact status code of a driver against /tenants/{id}/settings (403, not 500)
  - PATCH /settings replaces the whole body, it does not partially merge
    (documented on purpose, unlike PATCH /tenants/{id} -- see TenantSettingsUpdate)."""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from httpx import ASGITransport

from app.main import app as real_app
from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _create_driver_login(client, token, tenant_id, driver_id):
    email = f"driver-edge-{uuid.uuid4().hex[:8]}@example.com"
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


def _future_meal_window() -> tuple[str, str]:
    start = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%H:%M")
    end = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M")
    return start, end


async def test_tenant_viewer_cannot_update_settings(client, two_tenants, pool):
    from app import db as db_module
    from app.security import hash_password

    tenant_id = two_tenants["a"]["tenant_id"]
    email = f"viewer-edge-{uuid.uuid4().hex[:8]}@example.com"
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (%s, %s, %s, 'tenant_viewer')",
            (tenant_id, email, hash_password(TEST_PASSWORD)),
        )
    token = await login(client, email)
    resp = await client.patch(f"/tenants/{tenant_id}/settings", json={"display_name": "Nope"}, headers=auth_header(token))
    assert resp.status_code == 403


async def test_driver_settings_rejection_is_403_not_500(client, two_tenants):
    """A driver's JWT always carries driver_id -- confirms a well-formed token
    fails in require_tenant_admin (403), never falling into the defensive 500
    in deps.get_db or a raw database error."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver 403"}, headers=auth_header(token))
    ).json()
    email = await _create_driver_login(client, token, tenant_id, driver["id"])
    driver_token = await login(client, email)

    resp = await client.patch(f"/tenants/{tenant_id}/settings", json={"display_name": "Nope"}, headers=auth_header(driver_token))
    assert resp.status_code == 403


async def test_settings_endpoint_ignores_status_and_name(client, two_tenants):
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    resp = await client.patch(
        f"/tenants/{tenant_id}/settings",
        json={"display_name": "Brand X", "status": "suspended", "name": "Hijacked Name", "max_live_view_seconds": 5},
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] != "suspended"
    assert body["name"] != "Hijacked Name"
    check = await client.get("/tenants", params={"search": ""}, headers=auth_header(token))
    row = next(t for t in check.json()["items"] if t["id"] == str(tenant_id))
    assert row["name"] != "Hijacked Name"
    assert row["status"] == "active"


async def test_settings_rejects_nan_and_infinity_max_shift_hours(client, two_tenants):
    """Security finding: a NaN/Infinity in max_shift_hours made FastAPI's
    DEFAULT handler echo the raw value in the error, and json.dumps blew up
    while serving the error RESPONSE (500 instead of 422) -- see
    validation_exception_handler in main.py. raise_app_exceptions=False
    mimics a real uvicorn deployment (the normal `client` fixture propagates
    the exception as a Python exception instead of an HTTP response, which
    would hide this bug)."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    transport = ASGITransport(app=real_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as raw_client:
        for payload in ('{"max_shift_hours": NaN}', '{"max_shift_hours": Infinity}'):
            resp = await raw_client.patch(
                f"/tenants/{tenant_id}/settings",
                content=payload,
                headers={**auth_header(token), "Content-Type": "application/json"},
            )
            assert resp.status_code == 422, resp.text


async def test_driver_cannot_acknowledge_real_alert_by_id(client, two_tenants):
    """With a REAL alert id (not just the list), a driver still cannot
    acknowledge it -- 403 from require_non_driver, with no side effect."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_id), "name": "Driver Ack Attack"}, headers=auth_header(token))
    ).json()
    email = await _create_driver_login(client, token, tenant_id, driver["id"])
    driver_token = await login(client, email)

    start, end = _future_meal_window()
    await client.patch(
        f"/tenants/{tenant_id}/settings",
        json={"meal_break_window_start": start, "meal_break_window_end": end},
        headers=auth_header(token),
    )
    await client.post("/shifts/clock", json={"event_type": "clock_in"}, headers=auth_header(driver_token))
    await client.post("/shifts/clock", json={"event_type": "meal_start"}, headers=auth_header(driver_token))

    alerts = (await client.get("/driver-shift-alerts", headers=auth_header(token))).json()["items"]
    alert = next(a for a in alerts if a["driver_id"] == driver["id"])

    resp = await client.post(f"/driver-shift-alerts/{alert['id']}/acknowledge", headers=auth_header(driver_token))
    assert resp.status_code == 403

    still = (await client.get("/driver-shift-alerts", headers=auth_header(token))).json()["items"]
    still_alert = next(a for a in still if a["id"] == alert["id"])
    assert still_alert["acknowledged_at"] is None


async def test_cross_tenant_alert_ack_returns_404_and_does_not_ack(client, two_tenants):
    token_a = await login(client, two_tenants["a"]["email"])
    token_b = await login(client, two_tenants["b"]["email"])
    tenant_a = two_tenants["a"]["tenant_id"]
    driver = (
        await client.post("/drivers", json={"tenant_id": str(tenant_a), "name": "Driver Cross"}, headers=auth_header(token_a))
    ).json()
    email = await _create_driver_login(client, token_a, tenant_a, driver["id"])
    driver_token = await login(client, email)

    start, end = _future_meal_window()
    await client.patch(
        f"/tenants/{tenant_a}/settings",
        json={"meal_break_window_start": start, "meal_break_window_end": end},
        headers=auth_header(token_a),
    )
    await client.post("/shifts/clock", json={"event_type": "clock_in"}, headers=auth_header(driver_token))
    await client.post("/shifts/clock", json={"event_type": "meal_start"}, headers=auth_header(driver_token))

    alerts = (await client.get("/driver-shift-alerts", headers=auth_header(token_a))).json()["items"]
    alert = next(a for a in alerts if a["driver_id"] == driver["id"])

    resp = await client.post(f"/driver-shift-alerts/{alert['id']}/acknowledge", headers=auth_header(token_b))
    assert resp.status_code == 404

    still = (await client.get("/driver-shift-alerts", headers=auth_header(token_a))).json()["items"]
    still_alert = next(a for a in still if a["id"] == alert["id"])
    assert still_alert["acknowledged_at"] is None


async def test_settings_body_replaces_not_merges_as_documented(client, two_tenants):
    """TenantSettingsUpdate replaces the whole body (no partial COALESCE like
    TenantUpdate) -- confirms that sending only display_name genuinely CLEARS
    logo_url/meal window/max hours, instead of leaving them intact. This is
    the documented behavior (see schemas.py); this test makes it visible so
    nobody changes it by accident."""
    token = await login(client, two_tenants["a"]["email"])
    tenant_id = two_tenants["a"]["tenant_id"]
    await client.patch(
        f"/tenants/{tenant_id}/settings",
        json={
            "display_name": "Full Fleet",
            "logo_url": "https://cdn.example.com/x.png",
            "meal_break_window_start": "12:00",
            "meal_break_window_end": "13:00",
            "max_shift_hours": 9,
        },
        headers=auth_header(token),
    )
    resp = await client.patch(f"/tenants/{tenant_id}/settings", json={"display_name": "Name Only"}, headers=auth_header(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "Name Only"
    assert body["logo_url"] is None
    assert body["meal_break_window_start"] is None
    assert body["max_shift_hours"] is None
