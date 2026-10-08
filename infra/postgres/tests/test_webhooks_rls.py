"""Database-level RLS coverage for outbound webhooks (0035_webhooks.sql):
platform approval (tenants.webhooks_enabled) enforced by trigger on INSERT,
the role dimension in RLS (same criterion as api_keys, finding F10), the
automatic circuit breaker, and retention."""
import uuid

import pytest


def _enable_webhooks(cur, tenant_id):
    cur.execute("UPDATE tenants SET webhooks_enabled = true WHERE id = %s", (tenant_id,))


def _make_viewer(cur, tenant_id):
    email = f"tenant_viewer-{uuid.uuid4().hex[:8]}@example.com"
    cur.execute(
        "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (%s, %s, 'x', 'tenant_viewer') RETURNING id",
        (tenant_id, email),
    )
    return cur.fetchone()[0]


def _insert_endpoint(cur, tenant_id, user_id, suffix="a"):
    cur.execute(
        """INSERT INTO webhook_endpoints (tenant_id, url, event_types, secret, created_by)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (tenant_id, f"https://example.com/hook-{suffix}", ["device_alarm"], f"secret-{suffix}", user_id),
    )
    return cur.fetchone()[0]


# --- Platform approval enforced by trigger, not only by the API


def test_cannot_create_endpoint_without_tenant_approval(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    with pytest.raises(Exception, match="does not have the webhooks feature enabled"):
        _insert_endpoint(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])


def test_can_create_endpoint_once_tenant_approved(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    _enable_webhooks(cur, two_tenants["a"]["tenant_id"])
    endpoint_id = _insert_endpoint(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])
    cur.execute("SELECT enabled FROM webhook_endpoints WHERE id = %s", (endpoint_id,))
    assert cur.fetchone()[0] is True


def test_revoking_approval_does_not_delete_existing_endpoints(two_tenants, scoped_conn):
    """Only INSERT requires tenants.webhooks_enabled=true; revoking it later
    must not delete or break existing rows (the dispatcher, not this trigger,
    pauses actual delivery)."""
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    _enable_webhooks(cur, two_tenants["a"]["tenant_id"])
    endpoint_id = _insert_endpoint(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])

    cur.execute("UPDATE tenants SET webhooks_enabled = false WHERE id = %s", (two_tenants["a"]["tenant_id"],))
    cur.execute("UPDATE webhook_endpoints SET enabled = false WHERE id = %s", (endpoint_id,))
    cur.execute("SELECT id FROM webhook_endpoints WHERE id = %s", (endpoint_id,))
    assert cur.fetchone() is not None


# --- Role dimension in RLS (same criterion as api_keys F10) ---------------


def test_tenant_viewer_cannot_select_webhook_endpoints(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    _enable_webhooks(cur, two_tenants["a"]["tenant_id"])
    _insert_endpoint(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])
    viewer_id = _make_viewer(cur, two_tenants["a"]["tenant_id"])

    _, viewer_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=viewer_id)
    viewer_cur.execute("SELECT id FROM webhook_endpoints")
    assert viewer_cur.fetchall() == []


def test_tenant_admin_of_same_tenant_can_select(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    _enable_webhooks(cur, two_tenants["a"]["tenant_id"])
    endpoint_id = _insert_endpoint(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])

    _, admin_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    admin_cur.execute("SELECT id FROM webhook_endpoints WHERE id = %s", (endpoint_id,))
    assert admin_cur.fetchall() == [(endpoint_id,)]


def test_endpoints_isolated_between_tenants(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    _enable_webhooks(cur, two_tenants["a"]["tenant_id"])
    endpoint_id = _insert_endpoint(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])

    _, admin_b_cur = scoped_conn(tenant_id=two_tenants["b"]["tenant_id"], user_id=two_tenants["b"]["user_id"])
    admin_b_cur.execute("SELECT id FROM webhook_endpoints WHERE id = %s", (endpoint_id,))
    assert admin_b_cur.fetchall() == []


# --- Circuit breaker ---------------------------------------------------


def test_circuit_breaker_disables_endpoint_at_threshold(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    _enable_webhooks(cur, two_tenants["a"]["tenant_id"])
    endpoint_id = _insert_endpoint(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])

    cur.execute("UPDATE webhook_endpoints SET consecutive_failures = 9 WHERE id = %s", (endpoint_id,))
    cur.execute("SELECT enabled FROM webhook_endpoints WHERE id = %s", (endpoint_id,))
    assert cur.fetchone()[0] is True  # not yet

    cur.execute("UPDATE webhook_endpoints SET consecutive_failures = 10 WHERE id = %s", (endpoint_id,))
    cur.execute("SELECT enabled, disabled_at, disabled_reason FROM webhook_endpoints WHERE id = %s", (endpoint_id,))
    enabled, disabled_at, disabled_reason = cur.fetchone()
    assert enabled is False
    assert disabled_at is not None
    assert "consecutive delivery failures" in disabled_reason


def test_circuit_breaker_resets_when_failures_reset_to_zero(two_tenants, scoped_conn):
    """A successful delivery resets consecutive_failures to 0 (webhooks.py);
    confirms the trigger does not interfere with that normal path (it only
    acts when the counter RISES to 10+, never when it drops)."""
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    _enable_webhooks(cur, two_tenants["a"]["tenant_id"])
    endpoint_id = _insert_endpoint(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])

    cur.execute("UPDATE webhook_endpoints SET consecutive_failures = 5 WHERE id = %s", (endpoint_id,))
    cur.execute("UPDATE webhook_endpoints SET consecutive_failures = 0 WHERE id = %s", (endpoint_id,))
    cur.execute("SELECT enabled, consecutive_failures FROM webhook_endpoints WHERE id = %s", (endpoint_id,))
    enabled, failures = cur.fetchone()
    assert enabled is True
    assert failures == 0


# --- Only the worker (bypass connection) may freely change delivery-state
# columns. A normal tenant_admin session (same app_user role, no bypass) may
# RESET them to their healthy defaults (needed for the manual re-enable in
# PATCH /webhook-endpoints/{id}) but never FORGE an arbitrary value. Without
# this trigger a tenant_admin session could set any value via direct SQL.


def test_tenant_admin_can_reset_breaker_fields_to_default(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    _enable_webhooks(cur, two_tenants["a"]["tenant_id"])
    endpoint_id = _insert_endpoint(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])
    cur.execute(
        """UPDATE webhook_endpoints
           SET consecutive_failures = 10, enabled = false, disabled_at = now(), disabled_reason = 'x'
           WHERE id = %s""",
        (endpoint_id,),
    )

    # local=True (default) opens an explicit transaction, like the real API
    # contract. The UPDATE holds a row lock that would block the two_tenants
    # teardown DELETE CASCADE if this connection stayed "idle in transaction"
    # (reproduced: the full suite hung). An explicit commit() closes the
    # transaction before the test ends.
    admin_conn, admin_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    admin_cur.execute(
        """UPDATE webhook_endpoints
           SET consecutive_failures = 0, enabled = true, disabled_at = NULL, disabled_reason = NULL
           WHERE id = %s""",
        (endpoint_id,),
    )
    admin_cur.execute(
        "SELECT consecutive_failures, enabled, disabled_at, disabled_reason FROM webhook_endpoints WHERE id = %s",
        (endpoint_id,),
    )
    assert admin_cur.fetchone() == (0, True, None, None)
    admin_conn.commit()


def test_tenant_admin_cannot_forge_arbitrary_consecutive_failures(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    _enable_webhooks(cur, two_tenants["a"]["tenant_id"])
    endpoint_id = _insert_endpoint(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])

    # Explicit rollback(): after the exception the transaction is aborted but
    # any row lock it took is still held until resolved (same reason as the
    # commit() above).
    admin_conn, admin_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    with pytest.raises(Exception, match="can only reset consecutive_failures to 0"):
        admin_cur.execute("UPDATE webhook_endpoints SET consecutive_failures = 5 WHERE id = %s", (endpoint_id,))
    admin_conn.rollback()


def test_tenant_admin_cannot_forge_disabled_reason(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    _enable_webhooks(cur, two_tenants["a"]["tenant_id"])
    endpoint_id = _insert_endpoint(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])

    admin_conn, admin_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    with pytest.raises(Exception, match="can only clear disabled_at/disabled_reason"):
        admin_cur.execute("UPDATE webhook_endpoints SET disabled_reason = 'forged' WHERE id = %s", (endpoint_id,))
    admin_conn.rollback()


def test_tenant_admin_cannot_touch_worker_timestamps(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    _enable_webhooks(cur, two_tenants["a"]["tenant_id"])
    endpoint_id = _insert_endpoint(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])

    admin_conn, admin_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    with pytest.raises(Exception, match="last_attempt_at/last_success_at"):
        admin_cur.execute("UPDATE webhook_endpoints SET last_attempt_at = now() WHERE id = %s", (endpoint_id,))
    admin_conn.rollback()


# --- webhook_deliveries: isolation + no write GRANT outside bypass


def test_deliveries_isolated_between_tenants(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    _enable_webhooks(cur, two_tenants["a"]["tenant_id"])
    endpoint_id = _insert_endpoint(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])
    cur.execute(
        """INSERT INTO webhook_deliveries (webhook_endpoint_id, tenant_id, event_type, payload, dedupe_key)
           VALUES (%s, %s, 'device_alarm', '{}'::jsonb, gen_random_uuid()::text) RETURNING id""",
        (endpoint_id, two_tenants["a"]["tenant_id"]),
    )
    delivery_id = cur.fetchone()[0]

    _, admin_b_cur = scoped_conn(tenant_id=two_tenants["b"]["tenant_id"], user_id=two_tenants["b"]["user_id"])
    admin_b_cur.execute("SELECT id FROM webhook_deliveries WHERE id = %s", (delivery_id,))
    assert admin_b_cur.fetchall() == []


def test_deliveries_insert_requires_bypass(two_tenants, scoped_conn):
    """The only real writer is the dispatcher/worker (webhooks.py) over a
    bypass connection; a normal tenant session (even tenant_admin) must never
    be able to insert a fake delivery."""
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    _enable_webhooks(cur, two_tenants["a"]["tenant_id"])
    endpoint_id = _insert_endpoint(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])

    _, admin_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    with pytest.raises(Exception, match="row-level security policy"):
        admin_cur.execute(
            """INSERT INTO webhook_deliveries (webhook_endpoint_id, tenant_id, event_type, payload, dedupe_key)
               VALUES (%s, %s, 'device_alarm', '{}'::jsonb, gen_random_uuid()::text)""",
            (endpoint_id, two_tenants["a"]["tenant_id"]),
        )


# --- Retention -----------------------------------------------------------


def test_retention_deletes_only_old_deliveries(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    _enable_webhooks(cur, two_tenants["a"]["tenant_id"])
    endpoint_id = _insert_endpoint(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"])

    cur.execute(
        """INSERT INTO webhook_deliveries (webhook_endpoint_id, tenant_id, event_type, payload, dedupe_key, created_at)
           VALUES (%s, %s, 'device_alarm', '{}'::jsonb, gen_random_uuid()::text, now() - interval '100 days')""",
        (endpoint_id, two_tenants["a"]["tenant_id"]),
    )
    cur.execute(
        """INSERT INTO webhook_deliveries (webhook_endpoint_id, tenant_id, event_type, payload, dedupe_key, created_at)
           VALUES (%s, %s, 'device_alarm', '{}'::jsonb, gen_random_uuid()::text, now() - interval '1 day')""",
        (endpoint_id, two_tenants["a"]["tenant_id"]),
    )

    cur.execute("CALL enforce_webhook_delivery_retention(0, '{}'::jsonb)")

    cur.execute("SELECT count(*) FROM webhook_deliveries WHERE webhook_endpoint_id = %s", (endpoint_id,))
    assert cur.fetchone()[0] == 1
