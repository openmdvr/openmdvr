"""Per-user device visibility (see
infra/postgres/migrations/0031_device_groups_and_assignments.sql and
0032_device_visibility_rls.sql): database-level RLS coverage for
app_current_user_id()/app_can_view_device()/app_is_tenant_admin() and the 5
assignment tables. test_rls_isolation.py covers isolation per TENANT; this
file covers the per-USER dimension within the same tenant."""
import psycopg
import pytest

INSUFFICIENT_PRIVILEGE = "42501"
RAISE_EXCEPTION = "P0001"


def _make_operator(cur, tenant_id, role="tenant_operator"):
    import uuid

    email = f"{role}-{uuid.uuid4().hex[:8]}@example.com"
    cur.execute(
        "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (%s, %s, 'x', %s) RETURNING id",
        (tenant_id, email, role),
    )
    return cur.fetchone()[0]


def test_session_without_app_user_id_is_fail_closed(two_tenants, scoped_conn):
    """Production contract: EVERY authenticated session sets app.user_id (see
    tenant_scoped_connection in api/app/db.py). A session with tenant_id but
    WITHOUT user_id (an issuance bug or a malformed test) must see NOTHING by
    default: fail closed, never "sees the whole tenant"."""
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"])  # no user_id
    cur.execute("SELECT id FROM devices")
    assert cur.fetchall() == []


def test_operator_without_assignment_sees_no_devices(two_tenants, scoped_conn):
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    operator_id = _make_operator(cur, two_tenants["a"]["tenant_id"])

    _, op_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=operator_id)
    op_cur.execute("SELECT id FROM devices")
    assert op_cur.fetchall() == []


def test_operator_with_direct_assignment_sees_only_that_device(two_tenants, scoped_conn):
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    operator_id = _make_operator(cur, two_tenants["a"]["tenant_id"])
    cur.execute(
        "INSERT INTO user_device_assignments (user_id, device_id, tenant_id) VALUES (%s, %s, %s)",
        (operator_id, two_tenants["a"]["device_id"], two_tenants["a"]["tenant_id"]),
    )

    _, op_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=operator_id)
    op_cur.execute("SELECT id FROM devices")
    assert {r[0] for r in op_cur.fetchall()} == {two_tenants["a"]["device_id"]}


def test_app_can_view_device_never_true_across_tenants_for_admin(two_tenants, scoped_conn):
    """Regression for F3: before the fix, app_can_view_device() returned TRUE
    for a tenant_admin regardless of the device's tenant (exploitable at the
    function level, although the 3 RLS call sites compensated with a tenant
    AND). Tests the function directly, not only its masked effect in
    devices_select."""
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute("SELECT app_can_view_device(%s)", (two_tenants["b"]["device_id"],))
    assert cur.fetchone()[0] is False

    cur.execute("SELECT app_can_view_device(%s)", ("00000000-0000-0000-0000-000000000000",))
    assert cur.fetchone()[0] is False


def test_operator_cannot_self_assign_device_group_via_direct_sql(two_tenants, scoped_conn):
    """Regression for F4: before the fix, RLS on user_device_group_assignments
    was tenant-wide for INSERT, so any tenant session (not only tenant_admin)
    could add itself to another group via direct SQL and widen its own
    visibility, bypassing the API's require_tenant_admin (this tests the
    database, not the API). It now requires app_is_tenant_admin()."""
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    operator_id = _make_operator(cur, two_tenants["a"]["tenant_id"])
    cur.execute(
        "INSERT INTO device_groups (tenant_id, name) VALUES (%s, 'Admin Group') RETURNING id",
        (two_tenants["a"]["tenant_id"],),
    )
    group_id = cur.fetchone()[0]

    op_conn, op_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=operator_id, local=True)
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc_info:
        op_cur.execute(
            "INSERT INTO user_device_group_assignments (user_id, device_group_id, tenant_id) VALUES (%s, %s, %s)",
            (operator_id, group_id, two_tenants["a"]["tenant_id"]),
        )
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
    op_conn.rollback()


def test_operator_cannot_self_assign_device_directly_via_sql(two_tenants, scoped_conn):
    """Same F4 finding, DIRECT assignment path (not via group): confirms the
    fix is symmetric across the 4 tables.

    Note on the expected error: enforce_user_device_assignment_tenant_match()
    (the BEFORE INSERT trigger) runs BEFORE the RLS WITH CHECK policy is
    evaluated, and its own SELECT on `devices` is subject to the inserting
    session's RLS. An operator without that device assigned cannot see it, so
    the trigger fires first with its generic "device_id is not valid" message
    instead of reaching user_device_assignments_insert. The security outcome
    is the same (the INSERT never completes); only the layer that stops it
    first differs."""
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    operator_id = _make_operator(cur, two_tenants["a"]["tenant_id"])

    op_conn, op_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=operator_id, local=True)
    with pytest.raises(psycopg.Error) as exc_info:
        op_cur.execute(
            "INSERT INTO user_device_assignments (user_id, device_id, tenant_id) VALUES (%s, %s, %s)",
            (operator_id, two_tenants["a"]["device_id"], two_tenants["a"]["tenant_id"]),
        )
    assert exc_info.value.sqlstate in (INSUFFICIENT_PRIVILEGE, RAISE_EXCEPTION)
    op_conn.rollback()


def test_tenant_admin_can_assign_devices_via_sql(two_tenants, scoped_conn):
    """Positive case for the F4 fix: tenant_admin CAN write these tables; the
    fix is not over-restrictive."""
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    operator_id = _make_operator(cur, two_tenants["a"]["tenant_id"])

    admin_conn, admin_cur = scoped_conn(
        tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"], local=True
    )
    admin_cur.execute(
        "INSERT INTO user_device_assignments (user_id, device_id, tenant_id) VALUES (%s, %s, %s)",
        (operator_id, two_tenants["a"]["device_id"], two_tenants["a"]["tenant_id"]),
    )
    admin_conn.commit()

    _, op_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=operator_id)
    op_cur.execute("SELECT id FROM devices")
    assert {r[0] for r in op_cur.fetchall()} == {two_tenants["a"]["device_id"]}


def test_acknowledge_alarm_requires_device_visibility(two_tenants, scoped_conn):
    """Regression for F5: acknowledge_alarm() now also requires
    app_can_view_device(), not only the tenant; same ERRCODE 42501 it already
    used for "another tenant" (see the explicit USING ERRCODE in
    0032_device_visibility_rls.sql)."""
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], bypass=True, local=False)
    operator_id = _make_operator(cur, two_tenants["a"]["tenant_id"])

    _, op_cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=operator_id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as exc_info:
        op_cur.execute("SELECT acknowledge_alarm(%s, %s)", (two_tenants["a"]["alarm_id"], operator_id))
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
