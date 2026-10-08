"""Database-level RLS coverage for api_keys/api_key_usage_log
(0034_api_keys.sql). Complements test_api_key_device_filter_rls.py (which
covers per-device scoping) with: the ROLE dimension inside RLS (security
finding F10), monotonic revoked_at (F11), and the retention job (F6)."""
import uuid

import pytest


def _make_viewer(cur, tenant_id):
    email = f"tenant_viewer-{uuid.uuid4().hex[:8]}@example.com"
    cur.execute(
        "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (%s, %s, 'x', 'tenant_viewer') RETURNING id",
        (tenant_id, email),
    )
    return cur.fetchone()[0]


def _make_api_key_row(cur, tenant_id, user_id, name_suffix):
    cur.execute(
        """INSERT INTO api_keys (tenant_id, user_id, name, key_prefix, key_hash, created_by, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s, now() + interval '1 year') RETURNING id""",
        (tenant_id, user_id, f"key {name_suffix}", f"omd_{name_suffix}", f"hash-{name_suffix}", user_id),
    )
    return cur.fetchone()[0]


# --- F10: the role dimension lives INSIDE RLS, not only in require_tenant_admin


def test_tenant_viewer_cannot_select_api_keys_of_own_tenant(two_tenants, scoped_conn):
    """Regression for F10: the original policy only required tenant_id = ...
    (no role dimension), so the only real barrier was require_tenant_admin in
    the API, a single layer of defense. app_is_tenant_admin() (0031) is now
    also required inside the policy."""
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    viewer_id = _make_viewer(cur, two_tenants["a"]["tenant_id"])
    _make_api_key_row(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"], "f10sel")

    _, viewer_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=viewer_id)
    viewer_cur.execute("SELECT id FROM api_keys")
    assert viewer_cur.fetchall() == []


def test_tenant_admin_of_same_tenant_can_select(two_tenants, scoped_conn):
    """Baseline: the real tenant_admin must still see its own keys."""
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    key_id = _make_api_key_row(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"], "f10baseline")

    _, admin_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    admin_cur.execute("SELECT id FROM api_keys WHERE id = %s", (key_id,))
    assert admin_cur.fetchall() == [(key_id,)]


def test_tenant_viewer_cannot_revoke_api_keys(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    viewer_id = _make_viewer(cur, two_tenants["a"]["tenant_id"])
    key_id = _make_api_key_row(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"], "f10upd")

    _, viewer_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=viewer_id)
    viewer_cur.execute("UPDATE api_keys SET revoked_at = now() WHERE id = %s", (key_id,))
    # RLS silently affects no rows (USING does not match): 0 rows affected,
    # not an exception, consistent with "neither confirm nor deny".
    assert viewer_cur.rowcount == 0

    _, bypass_cur = scoped_conn(tenant_id=None, bypass=True)
    bypass_cur.execute("SELECT revoked_at FROM api_keys WHERE id = %s", (key_id,))
    assert bypass_cur.fetchone()[0] is None


# --- F11: revoked_at is monotonic -----------------------------------------


def test_revoked_at_cannot_be_unset(two_tenants, scoped_conn):
    """Regression for F11: GRANT UPDATE(revoked_at) allowed un-revoking with a
    direct UPDATE; "a revoked key stays revoked" was only enforced by the
    COALESCE(revoked_at, now()) of a single endpoint. The trigger now
    rejects it."""
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    key_id = _make_api_key_row(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"], "f11")
    cur.execute("UPDATE api_keys SET revoked_at = now() WHERE id = %s", (key_id,))

    _, admin_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    with pytest.raises(Exception, match="cannot be un-revoked"):
        admin_cur.execute("UPDATE api_keys SET revoked_at = NULL WHERE id = %s", (key_id,))


def test_revoke_is_idempotent_at_the_trigger_level(two_tenants, scoped_conn):
    """Setting revoked_at again to a non-NULL value (the endpoint's real
    idempotency, COALESCE) must never trigger it; only non-NULL -> NULL is
    forbidden."""
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    key_id = _make_api_key_row(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"], "f11idem")
    cur.execute("UPDATE api_keys SET revoked_at = now() WHERE id = %s RETURNING revoked_at", (key_id,))
    first_revoked_at = cur.fetchone()[0]

    cur.execute("UPDATE api_keys SET revoked_at = %s WHERE id = %s", (first_revoked_at, key_id))
    cur.execute("SELECT revoked_at FROM api_keys WHERE id = %s", (key_id,))
    assert cur.fetchone()[0] == first_revoked_at


# --- F6: api_key_usage_log retention --------------------------------------


def test_usage_log_retention_deletes_only_old_rows(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    key_id = _make_api_key_row(cur, two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"], "f6")

    cur.execute(
        """INSERT INTO api_key_usage_log (api_key_id, tenant_id, occurred_at, method, path, status_code)
           VALUES (%s, %s, now() - interval '100 days', 'GET', '/devices', 200)""",
        (key_id, two_tenants["a"]["tenant_id"]),
    )
    cur.execute(
        """INSERT INTO api_key_usage_log (api_key_id, tenant_id, occurred_at, method, path, status_code)
           VALUES (%s, %s, now() - interval '1 day', 'GET', '/devices', 200)""",
        (key_id, two_tenants["a"]["tenant_id"]),
    )

    cur.execute("CALL enforce_api_key_usage_log_retention(0, '{}'::jsonb)")

    cur.execute("SELECT count(*) FROM api_key_usage_log WHERE api_key_id = %s", (key_id,))
    assert cur.fetchone()[0] == 1
