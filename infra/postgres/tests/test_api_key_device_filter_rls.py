"""Database-level RLS coverage for the extra scoping of an API key
(0034_api_keys.sql, app_api_key_device_filter()). Complements
test_device_visibility_rls.py (per-user isolation) with a new dimension: a
session that could ALREADY see a device through a real assignment can be
narrowed FURTHER by the app.api_key_device_filter GUC, without touching any
devices/alarms_v/gps_positions_v/notifications policy directly (they all go
through app_can_view_device() or their own explicit condition)."""
import uuid

import pytest


def _make_operator(cur, tenant_id, role="tenant_operator"):
    email = f"{role}-{uuid.uuid4().hex[:8]}@example.com"
    cur.execute(
        "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (%s, %s, 'x', %s) RETURNING id",
        (tenant_id, email, role),
    )
    return cur.fetchone()[0]


def _make_device(cur, tenant_id, label):
    terminal_id = str(int(uuid.uuid4().hex[:8], 16))
    cur.execute(
        "INSERT INTO devices (tenant_id, jt808_terminal_id, label, status) VALUES (%s, %s, %s, 'active') RETURNING id",
        (tenant_id, terminal_id, label),
    )
    return cur.fetchone()[0]


def test_admin_without_filter_sees_all_devices(two_tenants, scoped_conn):
    """Baseline: without an API key (filter __unset__), nothing changes
    compared to the behavior already covered in test_rls_isolation.py."""
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    second_device = _make_device(cur, two_tenants["a"]["tenant_id"], "second")

    _, admin_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    admin_cur.execute("SELECT id FROM devices")
    ids = {r[0] for r in admin_cur.fetchall()}
    assert ids == {two_tenants["a"]["device_id"], second_device}


def test_admin_with_filter_sees_only_filtered_device(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    second_device = _make_device(cur, two_tenants["a"]["tenant_id"], "second")

    _, admin_cur = scoped_conn(
        tenant_id=two_tenants["a"]["tenant_id"],
        user_id=two_tenants["a"]["user_id"],
        api_key_device_filter=[str(two_tenants["a"]["device_id"])],
    )
    admin_cur.execute("SELECT id FROM devices")
    ids = {r[0] for r in admin_cur.fetchall()}
    assert ids == {two_tenants["a"]["device_id"]}
    assert second_device not in ids


def test_empty_filter_list_is_deny_all_not_unrestricted(two_tenants, scoped_conn):
    """[] (empty list) MUST behave differently from omitting the parameter.
    Regression: an empty list is falsy in Python, and treating it like None
    turned "scoped to no device" into "unrestricted"."""
    _, admin_cur = scoped_conn(
        tenant_id=two_tenants["a"]["tenant_id"],
        user_id=two_tenants["a"]["user_id"],
        api_key_device_filter=[],
    )
    admin_cur.execute("SELECT id FROM devices")
    assert admin_cur.fetchall() == []


def test_filter_narrows_within_operator_assignment_not_beyond_it(two_tenants, scoped_conn):
    """The API key filter NEVER widens what the user can already see; it can
    only narrow WITHIN the real assignment."""
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    operator_id = _make_operator(cur, two_tenants["a"]["tenant_id"])
    second_device = _make_device(cur, two_tenants["a"]["tenant_id"], "second")
    # The operator is assigned to BOTH devices.
    cur.execute(
        "INSERT INTO user_device_assignments (user_id, device_id, tenant_id) VALUES (%s, %s, %s), (%s, %s, %s)",
        (
            operator_id, two_tenants["a"]["device_id"], two_tenants["a"]["tenant_id"],
            operator_id, second_device, two_tenants["a"]["tenant_id"],
        ),
    )

    # An API key of this operator scoped to a device of ANOTHER tenant (which
    # it could not see anyway): the filter never "adds" access, it only
    # subtracts.
    _, op_cur = scoped_conn(
        tenant_id=two_tenants["a"]["tenant_id"],
        user_id=operator_id,
        api_key_device_filter=[str(two_tenants["b"]["device_id"])],
    )
    op_cur.execute("SELECT id FROM devices")
    assert op_cur.fetchall() == []

    # Scoped to ONE of the two it does see: narrows correctly.
    _, op_cur2 = scoped_conn(
        tenant_id=two_tenants["a"]["tenant_id"],
        user_id=operator_id,
        api_key_device_filter=[str(second_device)],
    )
    op_cur2.execute("SELECT id FROM devices")
    assert {r[0] for r in op_cur2.fetchall()} == {second_device}


def test_filter_narrows_alarms_v_too(two_tenants, scoped_conn):
    """alarms_v (0032) calls app_can_view_device(), so the API key filter
    applies there without alarms.py knowing anything about it."""
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    second_device = _make_device(cur, two_tenants["a"]["tenant_id"], "second")
    cur.execute(
        "SELECT insert_alarm(%s, %s, now(), 'over_speed', 'warning')",
        (two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"]),
    )
    cur.execute(
        "SELECT insert_alarm(%s, %s, now(), 'over_speed', 'warning')",
        (two_tenants["a"]["tenant_id"], second_device),
    )

    _, admin_cur = scoped_conn(
        tenant_id=two_tenants["a"]["tenant_id"],
        user_id=two_tenants["a"]["user_id"],
        api_key_device_filter=[str(two_tenants["a"]["device_id"])],
    )
    admin_cur.execute("SELECT device_id FROM alarms_v")
    assert {r[0] for r in admin_cur.fetchall()} == {two_tenants["a"]["device_id"]}


def test_filter_narrows_notifications_and_excludes_deviceless_rows(two_tenants, scoped_conn, superuser_conn):
    """notifications_select (0033, redefined in 0034) does not go through
    app_can_view_device(); it has its own explicit condition. A notification
    WITHOUT device_id (a future device-less event) must be EXCLUDED while a
    filter is active (fail closed).

    notifications has no INSERT grant for app_user (not even with bypass); its
    only real writer is insert_alarm() (SECURITY DEFINER). The two rows WITH a
    device are seeded through the real fan-out; the row WITHOUT a device (no
    real write path exists today) is seeded with the superuser, which ignores
    RLS/GRANTs entirely. Only to test this condition, never a pattern for
    business code."""
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    second_device = _make_device(cur, two_tenants["a"]["tenant_id"], "second")
    admin_id = two_tenants["a"]["user_id"]

    cur.execute(
        "SELECT insert_alarm(%s, %s, now(), 'over_speed', 'warning')",
        (two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"]),
    )
    cur.execute(
        "SELECT insert_alarm(%s, %s, now(), 'over_speed', 'warning')",
        (two_tenants["a"]["tenant_id"], second_device),
    )
    su_cur = superuser_conn.cursor()
    su_cur.execute(
        """INSERT INTO notifications (tenant_id, recipient_user_id, event_type, device_id, title)
           VALUES (%s, %s, 'future_event_type', NULL, 'no associated device')""",
        (two_tenants["a"]["tenant_id"], admin_id),
    )

    _, admin_cur = scoped_conn(
        tenant_id=two_tenants["a"]["tenant_id"],
        user_id=admin_id,
        api_key_device_filter=[str(two_tenants["a"]["device_id"])],
    )
    admin_cur.execute("SELECT device_id FROM notifications WHERE recipient_user_id = %s", (admin_id,))
    assert {r[0] for r in admin_cur.fetchall()} == {two_tenants["a"]["device_id"]}


# --- Cross-tenant isolation of the api_keys table itself --------------------


def test_api_keys_isolated_between_tenants(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    cur.execute(
        """INSERT INTO api_keys (tenant_id, user_id, name, key_prefix, key_hash, created_by, expires_at)
           VALUES (%s, %s, 'key a', 'omd_aaaa', 'hasha', %s, now() + interval '1 year') RETURNING id""",
        (two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"], two_tenants["a"]["user_id"]),
    )
    key_a = cur.fetchone()[0]

    _, admin_b_cur = scoped_conn(tenant_id=two_tenants["b"]["tenant_id"], user_id=two_tenants["b"]["user_id"])
    admin_b_cur.execute("SELECT id FROM api_keys WHERE id = %s", (key_a,))
    assert admin_b_cur.fetchall() == []


def test_api_keys_insert_rejects_device_outside_tenant(two_tenants, scoped_conn):
    """enforce_api_key_tenant_match() (trigger): defense in depth on top of
    the validation the API already does."""
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    with pytest.raises(Exception, match="does not belong to any tenant|outside tenant"):
        cur.execute(
            """INSERT INTO api_keys (tenant_id, user_id, name, key_prefix, key_hash, created_by,
                                      expires_at, allowed_device_ids)
               VALUES (%s, %s, 'x', 'omd_bbbb', 'hashb', %s, now() + interval '1 year', %s)""",
            (
                two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"], two_tenants["a"]["user_id"],
                [two_tenants["b"]["device_id"]],
            ),
        )


def test_api_key_usage_log_isolated_between_tenants(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    cur.execute(
        """INSERT INTO api_keys (tenant_id, user_id, name, key_prefix, key_hash, created_by, expires_at)
           VALUES (%s, %s, 'key a', 'omd_cccc', 'hashc', %s, now() + interval '1 year') RETURNING id""",
        (two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"], two_tenants["a"]["user_id"]),
    )
    key_a = cur.fetchone()[0]
    cur.execute(
        """INSERT INTO api_key_usage_log (api_key_id, tenant_id, method, path, status_code)
           VALUES (%s, %s, 'GET', '/devices', 200)""",
        (key_a, two_tenants["a"]["tenant_id"]),
    )

    _, admin_b_cur = scoped_conn(tenant_id=two_tenants["b"]["tenant_id"], user_id=two_tenants["b"]["user_id"])
    admin_b_cur.execute("SELECT id FROM api_key_usage_log WHERE api_key_id = %s", (key_a,))
    assert admin_b_cur.fetchall() == []


def test_api_key_usage_log_has_no_insert_grant_outside_bypass(two_tenants, scoped_conn):
    """The only real writer is the auth process over a bypass connection
    (api/app/api_key_auth.py). A normal tenant session (JWT or API key) must
    NEVER be able to insert here, not even a row of its own tenant."""
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    cur.execute(
        """INSERT INTO api_keys (tenant_id, user_id, name, key_prefix, key_hash, created_by, expires_at)
           VALUES (%s, %s, 'key a', 'omd_dddd', 'hashd', %s, now() + interval '1 year') RETURNING id""",
        (two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"], two_tenants["a"]["user_id"]),
    )
    key_a = cur.fetchone()[0]

    _, admin_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    with pytest.raises(Exception, match="new row violates row-level security policy"):
        admin_cur.execute(
            """INSERT INTO api_key_usage_log (api_key_id, tenant_id, method, path, status_code)
               VALUES (%s, %s, 'GET', '/devices', 200)""",
            (key_a, two_tenants["a"]["tenant_id"]),
        )
