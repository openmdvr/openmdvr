"""Outgoing webhooks (0035_webhooks.sql) -- management (create/list/edit/
rotate/delete, always require_tenant_admin + tenants.webhooks_enabled),
SSRF protection, and the dispatch/delivery logic in webhooks.py (tested by
calling its internal functions directly, the same approach
test_notifications.py uses for fan-out, without depending on a real
LISTEN/NOTIFY listener running in the background)."""
import datetime as dt

import pytest

from conftest import TEST_PASSWORD, auth_header, login

pytestmark = pytest.mark.asyncio


async def _enable_webhooks(client, super_admin_token, tenant_id):
    resp = await client.patch(f"/tenants/{tenant_id}", json={"webhooks_enabled": True}, headers=auth_header(super_admin_token))
    assert resp.status_code == 200, resp.text


async def _create_endpoint(client, admin_token, tenant_id, **overrides) -> dict:
    body = {"tenant_id": str(tenant_id), "url": "https://example.com/hook", "event_types": ["device_alarm"]}
    body.update(overrides)
    resp = await client.post("/webhook-endpoints", json=body, headers=auth_header(admin_token))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_role(client, admin_token, tenant_id, role, suffix):
    resp = await client.post(
        "/users",
        json={"email": f"{role}-{suffix}@example.com", "password": TEST_PASSWORD, "role": role, "tenant_id": str(tenant_id)},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- Management: requires platform approval first ---------------------------


async def test_create_endpoint_rejected_without_platform_approval(client, two_tenants):
    admin_token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/webhook-endpoints",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "url": "https://example.com/hook", "event_types": ["device_alarm"]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422


async def test_create_endpoint_succeeds_once_approved_and_returns_secret_once(client, two_tenants, platform_users):
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])

    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_endpoint(client, admin_token, two_tenants["a"]["tenant_id"])
    assert "secret" in created and len(created["secret"]) > 20

    listing = await client.get("/webhook-endpoints", params={"tenant_id": str(two_tenants["a"]["tenant_id"])}, headers=auth_header(admin_token))
    assert listing.status_code == 200
    assert "secret" not in listing.json()["items"][0]


async def test_only_tenant_admin_can_create_endpoint(client, two_tenants, platform_users):
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])

    admin_token = await login(client, two_tenants["a"]["email"])
    viewer = await _create_role(client, admin_token, two_tenants["a"]["tenant_id"], "tenant_viewer", "webhooksviewer")
    viewer_token = await login(client, viewer["email"])

    resp = await client.post(
        "/webhook-endpoints",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "url": "https://example.com/hook", "event_types": ["device_alarm"]},
        headers=auth_header(viewer_token),
    )
    assert resp.status_code == 403


async def test_cannot_create_endpoint_for_another_tenant(client, two_tenants, platform_users):
    """Finding: the enforce_webhook_endpoint_tenant_enabled() trigger did a
    SELECT on `tenants` WITHOUT SECURITY DEFINER -- running under admin_a's
    session RLS, that query could not see B's (foreign) row and the trigger
    raised "feature not enabled" (422) instead of letting the real
    webhook_endpoints RLS policy (foreign tenant) reject with the correct 403.
    Fixed by making the trigger SECURITY DEFINER (0035) -- this test confirms
    the real 403, not the misleading 422."""
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    await _enable_webhooks(client, super_admin_token, two_tenants["b"]["tenant_id"])

    admin_a_token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/webhook-endpoints",
        json={"tenant_id": str(two_tenants["b"]["tenant_id"]), "url": "https://example.com/hook", "event_types": ["device_alarm"]},
        headers=auth_header(admin_a_token),
    )
    assert resp.status_code == 403


async def test_rejects_unknown_event_type(client, two_tenants, platform_users):
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/webhook-endpoints",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "url": "https://example.com/hook", "event_types": ["not_a_real_event"]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422


async def test_rejects_non_http_scheme(client, two_tenants, platform_users):
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/webhook-endpoints",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "url": "file:///etc/passwd", "event_types": ["device_alarm"]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422


async def test_rotate_secret_returns_new_secret_once(client, two_tenants, platform_users):
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_endpoint(client, admin_token, two_tenants["a"]["tenant_id"])

    rotated = await client.post(f"/webhook-endpoints/{created['id']}/rotate-secret", headers=auth_header(admin_token))
    assert rotated.status_code == 200
    assert rotated.json()["secret"] != created["secret"]


async def test_update_and_delete_endpoint(client, two_tenants, platform_users):
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_endpoint(client, admin_token, two_tenants["a"]["tenant_id"])

    updated = await client.patch(
        f"/webhook-endpoints/{created['id']}", json={"enabled": False}, headers=auth_header(admin_token)
    )
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False

    deleted = await client.delete(f"/webhook-endpoints/{created['id']}", headers=auth_header(admin_token))
    assert deleted.status_code == 204
    listing = await client.get("/webhook-endpoints", params={"tenant_id": str(two_tenants["a"]["tenant_id"])}, headers=auth_header(admin_token))
    assert listing.json()["total"] == 0


async def test_deliveries_endpoint_404_for_unknown_or_foreign_endpoint(client, two_tenants, platform_users):
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_a_token = await login(client, two_tenants["a"]["email"])
    created = await _create_endpoint(client, admin_a_token, two_tenants["a"]["tenant_id"])

    admin_b_token = await login(client, two_tenants["b"]["email"])
    resp = await client.get(f"/webhook-endpoints/{created['id']}/deliveries", headers=auth_header(admin_b_token))
    assert resp.status_code == 404


# --- Regressions: security review findings ----------------------------------


async def test_support_cannot_create_webhook_endpoint(client, two_tenants, platform_users):
    """MEDIUM-4: creating an endpoint returns the secret in the response (like
    rotating) -- support must not be able to obtain it, same rule as API key
    finding F1."""
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    support_token = await login(client, platform_users["support"]["email"])
    resp = await client.post(
        "/webhook-endpoints",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "url": "https://example.com/hook", "event_types": ["device_alarm"]},
        headers=auth_header(support_token),
    )
    assert resp.status_code == 403


async def test_support_cannot_rotate_webhook_secret(client, two_tenants, platform_users):
    """MEDIUM-4: `support` (RLS bypass, not super_admin) could rotate ANY
    tenant's secret and keep a valid signing secret."""
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_endpoint(client, admin_token, two_tenants["a"]["tenant_id"])

    support_token = await login(client, platform_users["support"]["email"])
    resp = await client.post(
        f"/webhook-endpoints/{created['id']}/rotate-secret", headers=auth_header(support_token)
    )
    assert resp.status_code == 403


async def test_super_admin_can_still_rotate_webhook_secret(client, two_tenants, platform_users):
    """The MEDIUM-4 fix specifically excludes support -- super_admin (the real
    platform role) can still rotate any secret."""
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_endpoint(client, admin_token, two_tenants["a"]["tenant_id"])

    resp = await client.post(
        f"/webhook-endpoints/{created['id']}/rotate-secret", headers=auth_header(super_admin_token)
    )
    assert resp.status_code == 200


async def test_endpoint_quota_per_tenant_enforced(client, two_tenants, platform_users):
    """MEDIUM-5: without a cap, a tenant_admin could create dozens/hundreds of
    endpoints pointing at the same third-party URL (60 consecutive POSTs,
    60x201, zero rejections)."""
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    for i in range(10):
        await _create_endpoint(client, admin_token, two_tenants["a"]["tenant_id"], url=f"https://example.com/hook-{i}")

    resp = await client.post(
        "/webhook-endpoints",
        json={"tenant_id": str(two_tenants["a"]["tenant_id"]), "url": "https://example.com/hook-eleven", "event_types": ["device_alarm"]},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 409


async def test_create_endpoint_rejects_url_pointing_to_private_ip(client, two_tenants, platform_users):
    """LOW-1: previously, SSRF protection only ran at the LAST moment (right
    before each delivery attempt) -- a URL pointing at localhost/internal
    network/cloud metadata was accepted at creation time (201)."""
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    resp = await client.post(
        "/webhook-endpoints",
        json={
            "tenant_id": str(two_tenants["a"]["tenant_id"]),
            "url": "http://169.254.169.254/latest/meta-data/",
            "event_types": ["device_alarm"],
        },
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 422


async def test_update_endpoint_rejects_url_pointing_to_private_ip(client, two_tenants, platform_users):
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_endpoint(client, admin_token, two_tenants["a"]["tenant_id"])

    resp = await client.patch(
        f"/webhook-endpoints/{created['id']}", json={"url": "http://127.0.0.1/hook"}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 422


async def test_manual_reenable_clears_circuit_breaker_state(client, two_tenants, platform_users, pool):
    """LOW-3: manually re-enabling (enabled: true) an endpoint the circuit
    breaker had turned off left the row in a contradictory state
    ("enabled=true" alongside "disabled_reason=too many failures")."""
    from app import db as db_module

    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_endpoint(client, admin_token, two_tenants["a"]["tenant_id"])

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "UPDATE webhook_endpoints SET consecutive_failures = 10 WHERE id = %s", (created["id"],)
        )

    resp = await client.patch(f"/webhook-endpoints/{created['id']}", json={"enabled": True}, headers=auth_header(admin_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["consecutive_failures"] == 0
    assert body["disabled_at"] is None
    assert body["disabled_reason"] is None


async def test_cross_tenant_create_returns_403_regardless_of_target_approval(client, two_tenants, platform_users):
    """LOW-4: previously, the 403/422 returned to a tenant_admin trying to
    create a webhook for ANOTHER tenant depended on whether that foreign
    tenant had webhooks_enabled=true -- a 1-bit oracle about foreign tenants
    (403 if the UUID was a real, approved tenant, 422 otherwise). Now the
    own-tenant check happens BEFORE touching the database -- 403 regardless
    of the foreign tenant's approval state."""
    admin_a_token = await login(client, two_tenants["a"]["email"])

    resp_unapproved = await client.post(
        "/webhook-endpoints",
        json={"tenant_id": str(two_tenants["b"]["tenant_id"]), "url": "https://example.com/hook", "event_types": ["device_alarm"]},
        headers=auth_header(admin_a_token),
    )
    assert resp_unapproved.status_code == 403

    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["b"]["tenant_id"])

    resp_approved = await client.post(
        "/webhook-endpoints",
        json={"tenant_id": str(two_tenants["b"]["tenant_id"]), "url": "https://example.com/hook", "event_types": ["device_alarm"]},
        headers=auth_header(admin_a_token),
    )
    assert resp_approved.status_code == 403


# --- SSRF protection --------------------------------------------------------


class _FakeLoop:
    def __init__(self, addrs=None, error=None):
        self._addrs = addrs or []
        self._error = error

    async def getaddrinfo(self, host, port):
        if self._error:
            raise self._error
        return [(2, 1, 6, "", (ip, 0)) for ip in self._addrs]


def _patch_dns(monkeypatch, *, ips=None, error=None):
    from app import webhooks

    monkeypatch.setattr(webhooks.asyncio, "get_running_loop", lambda: _FakeLoop(ips, error))


async def test_ssrf_rejects_loopback(monkeypatch):
    from app import webhooks

    _patch_dns(monkeypatch, ips=["127.0.0.1"])
    assert await webhooks.is_webhook_url_safe("http://anything.example/hook") is False


async def test_ssrf_rejects_private_range(monkeypatch):
    from app import webhooks

    _patch_dns(monkeypatch, ips=["10.0.0.5"])
    assert await webhooks.is_webhook_url_safe("https://internal.example/hook") is False


async def test_ssrf_rejects_cloud_metadata_link_local(monkeypatch):
    """169.254.169.254 -- the AWS/GCP/Azure metadata endpoint, the most cited
    SSRF vector for credential theft."""
    from app import webhooks

    _patch_dns(monkeypatch, ips=["169.254.169.254"])
    assert await webhooks.is_webhook_url_safe("http://metadata.example/") is False


async def test_ssrf_rejects_if_any_resolved_address_is_unsafe(monkeypatch):
    """A hostname can resolve to SEVERAL IPs -- if ANY one is
    private/reserved, the whole destination is rejected."""
    from app import webhooks

    _patch_dns(monkeypatch, ips=["93.184.216.34", "127.0.0.1"])
    assert await webhooks.is_webhook_url_safe("https://mixed.example/hook") is False


async def test_ssrf_accepts_public_ip(monkeypatch):
    from app import webhooks

    _patch_dns(monkeypatch, ips=["93.184.216.34"])
    assert await webhooks.is_webhook_url_safe("https://public.example/hook") is True


async def test_ssrf_rejects_dns_failure(monkeypatch):
    from app import webhooks

    _patch_dns(monkeypatch, error=OSError("no such host"))
    assert await webhooks.is_webhook_url_safe("https://does-not-exist.invalid/hook") is False


async def test_ssrf_rejects_non_http_scheme_directly():
    from app import webhooks

    assert await webhooks.is_webhook_url_safe("ftp://example.com/hook") is False
    assert await webhooks.is_webhook_url_safe("file:///etc/passwd") is False


# --- Dispatch: efficient, it only runs when it has to -----------------------


async def _insert_alarm(pool, tenant_id, device_id, alarm_type="over_speed", severity="warning"):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute("SELECT insert_alarm(%s, %s, now(), %s, %s)", (tenant_id, device_id, alarm_type, severity))
        ).fetchone()
        return row[0]


async def _count_deliveries(pool, tenant_id):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute("SELECT count(*) FROM webhook_deliveries WHERE tenant_id = %s", (tenant_id,))
        ).fetchone()
        return row[0]


async def test_dispatch_does_nothing_when_tenant_not_approved(client, two_tenants, pool):
    """The MOST common case (no tenant with the feature approved) must cost a
    single indexed query and zero delivery rows -- no HTTP call, no extra
    auditing."""
    from app.webhooks import WebhooksState, _dispatch_alarm_event

    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])
    state = WebhooksState()
    await _dispatch_alarm_event(pool, state, str(two_tenants["a"]["tenant_id"]), str(alarm_id))
    assert await _count_deliveries(pool, two_tenants["a"]["tenant_id"]) == 0


async def test_dispatch_does_nothing_without_matching_endpoint(client, two_tenants, platform_users, pool):
    from app.webhooks import WebhooksState, _dispatch_alarm_event

    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    # Enabled, but WITHOUT any webhook_endpoint created yet.
    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])
    state = WebhooksState()
    await _dispatch_alarm_event(pool, state, str(two_tenants["a"]["tenant_id"]), str(alarm_id))
    assert await _count_deliveries(pool, two_tenants["a"]["tenant_id"]) == 0


async def test_dispatch_does_nothing_for_disabled_endpoint(client, two_tenants, platform_users, pool):
    from app.webhooks import WebhooksState, _dispatch_alarm_event

    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_endpoint(client, admin_token, two_tenants["a"]["tenant_id"])
    await client.patch(f"/webhook-endpoints/{created['id']}", json={"enabled": False}, headers=auth_header(admin_token))

    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])
    state = WebhooksState()
    await _dispatch_alarm_event(pool, state, str(two_tenants["a"]["tenant_id"]), str(alarm_id))
    assert await _count_deliveries(pool, two_tenants["a"]["tenant_id"]) == 0


async def test_dispatch_creates_delivery_for_matching_enabled_endpoint(client, two_tenants, platform_users, pool):
    from app.webhooks import WebhooksState, _dispatch_alarm_event

    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_endpoint(client, admin_token, two_tenants["a"]["tenant_id"])

    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"], "collision_warning", "critical")
    state = WebhooksState()
    await _dispatch_alarm_event(pool, state, str(two_tenants["a"]["tenant_id"]), str(alarm_id))

    resp = await client.get(f"/webhook-endpoints/{created['id']}/deliveries", headers=auth_header(admin_token))
    assert resp.json()["total"] == 1
    delivery = resp.json()["items"][0]
    assert delivery["status"] == "pending"
    assert delivery["event_type"] == "device_alarm"
    assert state.delivery_wake_event.is_set()  # wakes the worker immediately


async def test_dispatch_ignores_non_subscribed_event_type(client, two_tenants, platform_users, pool):
    """An endpoint subscribed to an event type that does not exist today (the
    creation validator already prevents it, but this confirms the dispatcher
    would not over-assume either if there were ever more than one real type)
    -- tested by seeding the row directly, bypassing creation validation."""
    from app import db as db_module
    from app.webhooks import WebhooksState, _dispatch_alarm_event

    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            """INSERT INTO webhook_endpoints (tenant_id, url, event_types, secret, created_by)
               VALUES (%s, 'https://example.com/hook', %s, 'x', %s)""",
            (two_tenants["a"]["tenant_id"], ["some_future_event"], two_tenants["a"]["user_id"]),
        )

    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])
    state = WebhooksState()
    await _dispatch_alarm_event(pool, state, str(two_tenants["a"]["tenant_id"]), str(alarm_id))
    assert await _count_deliveries(pool, two_tenants["a"]["tenant_id"]) == 0


async def test_dispatch_fans_out_to_multiple_matching_endpoints(client, two_tenants, platform_users, pool):
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    await _create_endpoint(client, admin_token, two_tenants["a"]["tenant_id"], url="https://example.com/hook-1")
    await _create_endpoint(client, admin_token, two_tenants["a"]["tenant_id"], url="https://example.com/hook-2")

    from app.webhooks import WebhooksState, _dispatch_alarm_event

    alarm_id = await _insert_alarm(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"])
    state = WebhooksState()
    await _dispatch_alarm_event(pool, state, str(two_tenants["a"]["tenant_id"]), str(alarm_id))
    assert await _count_deliveries(pool, two_tenants["a"]["tenant_id"]) == 2


async def test_dispatch_rejects_mismatched_tenant_and_alarm(client, two_tenants, platform_users, pool):
    """MEDIUM-2: _ALARM_SELECT_SQL now requires a.tenant_id = %s in addition to
    a.id = %s -- previously, a mismatched NOTIFY payload (the 'notifications'
    channel is shared and explicitly "not exclusive to webhooks") could deliver
    the payload of ANOTHER tenant's alarm under the tenant_id/endpoint that
    did match. Reproduced by forging a NOTIFY with A's tenant_id + B's
    alarm_id."""
    from app.webhooks import WebhooksState, _dispatch_alarm_event

    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_endpoint(client, admin_token, two_tenants["a"]["tenant_id"])

    # A real alarm of TENANT B (which has no endpoint of its own).
    alarm_id = await _insert_alarm(
        pool, two_tenants["b"]["tenant_id"], two_tenants["b"]["device_id"], "secret_of_b", "critical"
    )

    # Mismatched payload: A's tenant_id (with an endpoint) + B's alarm_id.
    state = WebhooksState()
    await _dispatch_alarm_event(pool, state, str(two_tenants["a"]["tenant_id"]), str(alarm_id))

    resp = await client.get(f"/webhook-endpoints/{created['id']}/deliveries", headers=auth_header(admin_token))
    assert resp.json()["total"] == 0


async def test_claim_skips_deliveries_for_inactive_or_unapproved_tenant(two_tenants, pool):
    """MEDIUM-3: _claim_due_deliveries() used to look only at
    webhook_endpoints.enabled -- a suspended/cancelled tenant, or one whose
    webhooks_enabled was revoked AFTER deliveries were queued (up to ~34h of
    backoff in flight), kept receiving real outgoing POSTs. Rows stay pending
    (never lost) and resume on their own when the tenant is reactivated."""
    from app import db as db_module
    from app.webhooks import _claim_due_deliveries

    endpoint_id, _delivery_id = await _seed_endpoint_and_delivery(
        pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"]
    )

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("UPDATE tenants SET status = 'cancelled' WHERE id = %s", (two_tenants["a"]["tenant_id"],))
    assert await _claim_due_deliveries(pool) == []

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "UPDATE tenants SET status = 'active', webhooks_enabled = false WHERE id = %s",
            (two_tenants["a"]["tenant_id"],),
        )
    assert await _claim_due_deliveries(pool) == []

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("UPDATE tenants SET webhooks_enabled = true WHERE id = %s", (two_tenants["a"]["tenant_id"],))
    claimed = await _claim_due_deliveries(pool)
    assert len(claimed) == 1
    assert claimed[0][1] == endpoint_id


async def test_alarm_with_all_recipients_silenced_still_generates_webhook_delivery(
    client, two_tenants, platform_users, pool
):
    """MEDIUM-1, the highest-impact finding: insert_alarm() only emitted
    pg_notify('notifications', ...) when the in-app mailbox fan-out produced
    >=1 recipient -- muting in-app/email notifications for ALL the tenant's
    users also turned off outgoing webhooks, completely silently, with no
    error or trace. Fixed in 0037 (the NOTIFY is now emitted whenever the
    alarm was inserted). Unlike the rest of the dispatch tests in this file,
    this one DOES start the real listener (instead of calling
    _dispatch_alarm_event directly) -- the bug lived precisely in the
    insert_alarm -> NOTIFY -> listener path the other tests skip on purpose."""
    import asyncio

    from app import db as db_module
    from app.config import get_settings
    from app.webhooks import WebhooksState, run_webhook_dispatch_listener

    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_endpoint(client, admin_token, two_tenants["a"]["tenant_id"])

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            """INSERT INTO user_notification_settings (user_id, tenant_id, in_app_enabled, email_enabled)
               SELECT id, tenant_id, false, false FROM users WHERE tenant_id = %s""",
            (two_tenants["a"]["tenant_id"],),
        )

    state = WebhooksState()
    stop_event = asyncio.Event()
    listener_task = asyncio.create_task(run_webhook_dispatch_listener(get_settings(), pool, state, stop_event))
    try:
        for _ in range(30):
            if state.dispatch_listener_connected:
                break
            await asyncio.sleep(0.1)
        assert state.dispatch_listener_connected

        await _insert_alarm(
            pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"], "silenced_probe", "critical"
        )

        resp = None
        for _ in range(50):
            resp = await client.get(
                f"/webhook-endpoints/{created['id']}/deliveries", headers=auth_header(admin_token)
            )
            if resp.json()["total"] >= 1:
                break
            await asyncio.sleep(0.1)
        assert resp.json()["total"] == 1
    finally:
        stop_event.set()
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass


# --- Delivery worker: signature, retries, exhaustion, circuit breaker ------


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    calls = []
    responses = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, content=None, headers=None, extensions=None):
        _FakeAsyncClient.calls.append({"url": url, "content": content, "headers": headers, "extensions": extensions})
        item = _FakeAsyncClient.responses.pop(0) if _FakeAsyncClient.responses else _FakeResponse(200)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_delivery_http(monkeypatch):
    from app import webhooks

    _FakeAsyncClient.calls = []
    _FakeAsyncClient.responses = []
    import httpx as real_httpx

    # deliver_webhook resolves and pins the IP itself via _resolve_safe_ip --
    # that resolution is simulated, not the old is_webhook_url_safe check
    # (no longer on the delivery path).
    monkeypatch.setattr(
        webhooks,
        "httpx",
        type(
            "_M",
            (),
            {
                "AsyncClient": _FakeAsyncClient,
                "TimeoutException": real_httpx.TimeoutException,
                "ConnectError": real_httpx.ConnectError,
            },
        ),
    )
    monkeypatch.setattr(webhooks, "_resolve_safe_ip", _public_ip)
    return _FakeAsyncClient


async def _public_ip(hostname: str) -> str:
    return "93.184.216.34"


async def _seed_endpoint_and_delivery(pool, tenant_id, user_id, **overrides):
    from app import db as db_module

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("UPDATE tenants SET webhooks_enabled = true WHERE id = %s", (tenant_id,))
        endpoint_row = await (
            await conn.execute(
                """INSERT INTO webhook_endpoints (tenant_id, url, event_types, secret, created_by)
                   VALUES (%s, 'https://example.com/hook', %s, 'topsecret', %s) RETURNING id""",
                (tenant_id, ["device_alarm"], user_id),
            )
        ).fetchone()
        endpoint_id = endpoint_row[0]
        delivery_row = await (
            await conn.execute(
                """INSERT INTO webhook_deliveries (webhook_endpoint_id, tenant_id, event_type, payload, dedupe_key)
                   VALUES (%s, %s, 'device_alarm', %s::jsonb, gen_random_uuid()::text) RETURNING id""",
                (endpoint_id, tenant_id, '{"event": "device_alarm", "data": {}}'),
            )
        ).fetchone()
    return endpoint_id, delivery_row[0]


async def test_successful_delivery_signs_payload_and_resets_failures(client, two_tenants, pool, fake_delivery_http):
    from app import db as db_module
    from app.webhooks import _claim_due_deliveries, _attempt_delivery, sign_payload

    endpoint_id, delivery_id = await _seed_endpoint_and_delivery(
        pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"]
    )
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("UPDATE webhook_endpoints SET consecutive_failures = 3 WHERE id = %s", (endpoint_id,))

    claimed = await _claim_due_deliveries(pool)
    assert len(claimed) == 1
    import asyncio

    await _attempt_delivery(pool, asyncio.Semaphore(1), claimed[0])

    call = fake_delivery_http.calls[0]
    # Pinned IP (no DNS rebinding) + the original domain's Host/SNI.
    assert call["url"] == "https://93.184.216.34/hook"
    assert call["headers"]["Host"] == "example.com"
    assert call["extensions"] == {"sni_hostname": "example.com"}
    sig_header = call["headers"]["X-OpenMDVR-Signature"]
    assert sig_header.startswith("sha256=")
    timestamp = call["headers"]["X-OpenMDVR-Timestamp"]
    expected = "sha256=" + sign_payload("topsecret", timestamp, call["content"])
    assert sig_header == expected

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        d_row = await (await conn.execute("SELECT status, delivered_at FROM webhook_deliveries WHERE id = %s", (delivery_id,))).fetchone()
        e_row = await (await conn.execute("SELECT consecutive_failures, last_success_at FROM webhook_endpoints WHERE id = %s", (endpoint_id,))).fetchone()
    assert d_row[0] == "success"
    assert d_row[1] is not None
    assert e_row[0] == 0
    assert e_row[1] is not None


async def test_failed_delivery_schedules_retry_and_increments_failures(client, two_tenants, pool, fake_delivery_http):
    from app import db as db_module
    from app.webhooks import _claim_due_deliveries, _attempt_delivery

    fake_delivery_http.responses = [_FakeResponse(500, "boom")]
    endpoint_id, delivery_id = await _seed_endpoint_and_delivery(
        pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"]
    )

    claimed = await _claim_due_deliveries(pool)
    import asyncio

    await _attempt_delivery(pool, asyncio.Semaphore(1), claimed[0])

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        d_row = await (
            await conn.execute(
                "SELECT status, attempt_count, response_status_code, next_attempt_at > now() AS retry_in_future FROM webhook_deliveries WHERE id = %s",
                (delivery_id,),
            )
        ).fetchone()
        e_row = await (await conn.execute("SELECT consecutive_failures FROM webhook_endpoints WHERE id = %s", (endpoint_id,))).fetchone()
    assert d_row[0] == "pending"
    assert d_row[1] == 1
    assert d_row[2] == 500
    assert d_row[3] is True  # the next attempt was scheduled in the future (backoff)
    assert e_row[0] == 1


async def test_delivery_exhausts_after_max_attempts(client, two_tenants, pool, fake_delivery_http):
    from app import db as db_module
    from app.webhooks import _MAX_ATTEMPTS, _attempt_delivery

    endpoint_id, delivery_id = await _seed_endpoint_and_delivery(
        pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"]
    )
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute(
            "UPDATE webhook_deliveries SET attempt_count = %s WHERE id = %s", (_MAX_ATTEMPTS - 1, delivery_id)
        )
    fake_delivery_http.responses = [_FakeResponse(500)]

    import asyncio

    claimed_row = (delivery_id, endpoint_id, "device_alarm", {"event": "device_alarm", "data": {}}, _MAX_ATTEMPTS - 1, "https://example.com/hook", "topsecret")
    await _attempt_delivery(pool, asyncio.Semaphore(1), claimed_row)

    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (await conn.execute("SELECT status, attempt_count FROM webhook_deliveries WHERE id = %s", (delivery_id,))).fetchone()
    assert row[0] == "exhausted"
    assert row[1] == _MAX_ATTEMPTS


async def test_circuit_breaker_disables_endpoint_after_repeated_failures(client, two_tenants, pool, fake_delivery_http):
    """10 consecutive failures (the migration's trigger) -> the endpoint
    disables itself, without webhooks.py having to know the threshold."""
    from app import db as db_module
    from app.webhooks import _record_delivery_result

    endpoint_id, delivery_id = await _seed_endpoint_and_delivery(
        pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"]
    )
    for _ in range(10):
        await _record_delivery_result(
            pool, delivery_id=delivery_id, endpoint_id=endpoint_id, attempt_count=0,
            success=False, status_code=500, error_message="boom",
        )
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (
            await conn.execute("SELECT enabled, consecutive_failures FROM webhook_endpoints WHERE id = %s", (endpoint_id,))
        ).fetchone()
    assert row[0] is False
    assert row[1] >= 10


async def test_claim_skips_disabled_endpoint(client, two_tenants, pool):
    from app import db as db_module
    from app.webhooks import _claim_due_deliveries

    endpoint_id, _delivery_id = await _seed_endpoint_and_delivery(
        pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"]
    )
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        await conn.execute("UPDATE webhook_endpoints SET enabled = false WHERE id = %s", (endpoint_id,))

    claimed = await _claim_due_deliveries(pool)
    assert claimed == []


async def test_claim_applies_a_lease_avoiding_double_claim(client, two_tenants, pool):
    """FOR UPDATE SKIP LOCKED + the lease (next_attempt_at pushed into the
    future) -- a second immediate claim must not bring back the same row
    (protection against double delivery in a job queue)."""
    from app.webhooks import _claim_due_deliveries

    await _seed_endpoint_and_delivery(pool, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])

    first = await _claim_due_deliveries(pool)
    assert len(first) == 1
    second = await _claim_due_deliveries(pool)
    assert second == []


# --- "Send test" button + pinned-IP delivery --------------------------------


async def test_test_endpoint_delivers_signed_ping(client, two_tenants, platform_users, monkeypatch):
    """The ping is signed exactly like a real delivery, without being queued."""
    import hashlib
    import hmac

    from app.routers import webhook_endpoints as router_module
    from app.webhooks import DeliveryResult

    captured = {}

    async def fake_deliver(url, body, headers):
        captured.update(url=url, body=body, headers=headers)
        return DeliveryResult(True, 200, None, 12)

    monkeypatch.setattr(router_module, "deliver_webhook", fake_deliver)
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_token = await login(client, two_tenants["a"]["email"])
    created = await _create_endpoint(client, admin_token, two_tenants["a"]["tenant_id"])

    resp = await client.post(f"/webhook-endpoints/{created['id']}/test", headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True, "status_code": 200, "error": None, "elapsed_ms": 12}
    headers = captured["headers"]
    assert headers["X-OpenMDVR-Event"] == "ping"
    expected = hmac.new(
        created["secret"].encode(), f"{headers['X-OpenMDVR-Timestamp']}.".encode() + captured["body"], hashlib.sha256
    ).hexdigest()
    assert headers["X-OpenMDVR-Signature"] == f"sha256={expected}"

    deliveries = await client.get(f"/webhook-endpoints/{created['id']}/deliveries", headers=auth_header(admin_token))
    assert deliveries.json()["total"] == 0


async def test_test_endpoint_isolated_between_tenants(client, two_tenants, platform_users):
    super_admin_token = await login(client, platform_users["super_admin"]["email"])
    await _enable_webhooks(client, super_admin_token, two_tenants["a"]["tenant_id"])
    admin_a = await login(client, two_tenants["a"]["email"])
    created = await _create_endpoint(client, admin_a, two_tenants["a"]["tenant_id"])
    admin_b = await login(client, two_tenants["b"]["email"])
    resp = await client.post(f"/webhook-endpoints/{created['id']}/test", headers=auth_header(admin_b))
    assert resp.status_code == 404


async def test_deliver_webhook_rejects_private_and_explains_redirects(monkeypatch):
    """deliver_webhook resolves ONCE and connects to the validated IP (no DNS
    rebinding); a redirect yields an actionable error, not a silent failure."""
    import httpx

    from app import webhooks

    async def fake_resolve(host):
        return None if host == "internal.example" else "93.184.216.34"

    monkeypatch.setattr(webhooks, "_resolve_safe_ip", fake_resolve)
    r = await webhooks.deliver_webhook("https://internal.example/hook", b"{}", {})
    assert not r.success and "not allowed" in r.error

    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen["follow_redirects"] = kwargs.get("follow_redirects")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content, headers, extensions):
            seen.update(url=url, host=headers["Host"], ext=extensions)
            return httpx.Response(308, headers={"location": "https://hooks.example.com/hook/"})

    monkeypatch.setattr(webhooks.httpx, "AsyncClient", FakeClient)
    r = await webhooks.deliver_webhook("https://hooks.example.com/hook", b"{}", {})
    assert seen["url"] == "https://93.184.216.34/hook"
    assert seen["host"] == "hooks.example.com" and seen["ext"] == {"sni_hostname": "hooks.example.com"}
    assert seen["follow_redirects"] is False
    assert not r.success and r.status_code == 308 and "https://hooks.example.com/hook/" in r.error
