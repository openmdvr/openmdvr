"""
Multi-tenant isolation test suite for Row Level Security.

Covers four layers:
  1. Basic isolation (each tenant only sees/modifies its own rows).
  2. Adversarial cases: IDOR (requesting another tenant's resource by id),
     cross-tenant writes, session context "leaking" across transactions on a
     recycled (pooled) connection, and injection via a session variable value.
  3. Schema-level defense in depth (device/tenant and user/tenant consistency
     triggers, append-only tables, ON DELETE RESTRICT) that protects even a
     bypass_rls=true session against "logic chaining" bugs.
  4. Regressions for the independent security review findings: direct access
     to TimescaleDB chunks, referential integrity that ignored RLS, and column
     scope of UPDATE on alarms. See the header of
     infra/postgres/migrations/0009_timeseries_access.sql for why
     gps_positions/alarms/usage_events are accessed through views (_v) and
     functions instead of the base tables.

All error assertions compare the exact SQLSTATE code instead of the psycopg
exception class name, to avoid depending on a particular library version.

How to run: see infra/postgres/README.md.
"""
import psycopg
import pytest

CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"
INSUFFICIENT_PRIVILEGE = "42501"  # RLS policy violation AND missing table/function privilege
INVALID_TEXT_REPRESENTATION = "22P02"
RAISE_EXCEPTION = "P0001"


# ---------------------------------------------------------------------------
# Basic isolation
# ---------------------------------------------------------------------------

def test_tenant_a_sees_only_its_own_device(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute("SELECT id FROM devices")
    assert {r[0] for r in cur.fetchall()} == {two_tenants["a"]["device_id"]}


def test_tenant_b_sees_only_its_own_device(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["b"]["tenant_id"], user_id=two_tenants["b"]["user_id"])
    cur.execute("SELECT id FROM devices")
    assert {r[0] for r in cur.fetchall()} == {two_tenants["b"]["device_id"]}


def test_tenant_isolation_on_users(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute("SELECT id FROM users")
    assert {r[0] for r in cur.fetchall()} == {two_tenants["a"]["user_id"]}


def test_tenant_isolation_on_gps_positions(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute("SELECT device_id FROM gps_positions_v")
    assert {r[0] for r in cur.fetchall()} == {two_tenants["a"]["device_id"]}


def test_tenant_isolation_on_alarms(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute("SELECT id FROM alarms_v")
    assert {r[0] for r in cur.fetchall()} == {two_tenants["a"]["alarm_id"]}


def test_tenant_isolation_on_usage_events(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute("SELECT id FROM usage_events_v")
    assert {r[0] for r in cur.fetchall()} == {two_tenants["a"]["usage_event_id"]}


def test_tenant_sees_exactly_its_own_tenant_row(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute("SELECT id FROM tenants")
    assert {r[0] for r in cur.fetchall()} == {two_tenants["a"]["tenant_id"]}


def test_bypass_session_sees_rows_from_both_tenants(two_tenants, scoped_conn):
    _, cur = scoped_conn(bypass=True)
    cur.execute(
        "SELECT id FROM devices WHERE id IN (%s, %s)",
        (two_tenants["a"]["device_id"], two_tenants["b"]["device_id"]),
    )
    assert {r[0] for r in cur.fetchall()} == {
        two_tenants["a"]["device_id"],
        two_tenants["b"]["device_id"],
    }


def test_bypass_matches_superuser_ground_truth_count(two_tenants, scoped_conn, superuser_conn):
    _, cur = scoped_conn(bypass=True)
    cur.execute("SELECT count(*) FROM devices")
    bypass_count = cur.fetchone()[0]

    su_cur = superuser_conn.cursor()
    su_cur.execute("SELECT count(*) FROM devices")
    su_count = su_cur.fetchone()[0]

    assert bypass_count == su_count


@pytest.mark.parametrize("value", ["TRUE", "True", "1", "on", "yes", "t", ""])
def test_bypass_flag_only_accepts_exact_lowercase_true(two_tenants, scoped_conn, value):
    """Pins app_bypass_rls(): only the literal 'true' enables bypass. Any
    other "truthy" variant from other contexts (Postgres booleans, HTTP, etc.)
    must stay fail-closed here."""
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute("SELECT set_config('app.bypass_rls', %s, true)", (value,))
    cur.execute("SELECT id FROM devices")
    ids = {r[0] for r in cur.fetchall()}
    assert ids == {two_tenants["a"]["device_id"]}, f"bypass_rls={value!r} must not grant bypass"
    conn.rollback()


# ---------------------------------------------------------------------------
# IDOR: requesting another tenant's resource by id must never return data,
# and must never distinguish "does not exist" from "exists but is not yours"
# (same result: zero rows, no error).
# ---------------------------------------------------------------------------

def test_idor_cannot_fetch_other_tenant_device_by_id(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute("SELECT * FROM devices WHERE id = %s", (two_tenants["b"]["device_id"],))
    assert cur.fetchone() is None


def test_idor_cannot_fetch_other_tenant_alarm_by_id(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute("SELECT * FROM alarms_v WHERE id = %s", (two_tenants["b"]["alarm_id"],))
    assert cur.fetchone() is None


def test_idor_cannot_fetch_other_tenant_user_by_id(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute("SELECT * FROM users WHERE id = %s", (two_tenants["b"]["user_id"],))
    assert cur.fetchone() is None


def test_idor_cannot_fetch_other_tenant_usage_event_by_id(two_tenants, scoped_conn):
    _, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute("SELECT * FROM usage_events_v WHERE id = %s", (two_tenants["b"]["usage_event_id"],))
    assert cur.fetchone() is None


# ---------------------------------------------------------------------------
# Cross-tenant writes
# ---------------------------------------------------------------------------

def test_cross_tenant_device_insert_blocked(two_tenants, scoped_conn):
    """Device creation is bypass-only (see 0008); a tenant cannot insert
    either for itself or for another tenant."""
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "INSERT INTO devices (tenant_id, jt808_terminal_id, label) VALUES (%s, 'x-inj', 'y')",
            (two_tenants["b"]["tenant_id"],),
        )
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
    conn.rollback()


def test_own_tenant_device_insert_also_requires_bypass(two_tenants, scoped_conn):
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "INSERT INTO devices (tenant_id, jt808_terminal_id, label) VALUES (%s, 'x-own', 'y')",
            (two_tenants["a"]["tenant_id"],),
        )
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
    conn.rollback()


def test_cross_tenant_update_matches_zero_rows(two_tenants, scoped_conn):
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute("UPDATE devices SET label = 'hacked' WHERE id = %s", (two_tenants["b"]["device_id"],))
    assert cur.rowcount == 0
    conn.rollback()


def test_cross_tenant_delete_matches_zero_rows(two_tenants, scoped_conn):
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute("DELETE FROM devices WHERE id = %s", (two_tenants["b"]["device_id"],))
    assert cur.rowcount == 0
    conn.rollback()


def test_cannot_reparent_own_device_to_other_tenant(two_tenants, scoped_conn):
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "UPDATE devices SET tenant_id = %s WHERE id = %s",
            (two_tenants["b"]["tenant_id"], two_tenants["a"]["device_id"]),
        )
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
    conn.rollback()


def test_tenant_can_update_own_tenant_row_but_not_others(two_tenants, scoped_conn):
    """tenants_update was deliberately widened in 0017_tenant_self_service.sql
    to `USING (app_bypass_rls() OR id = app_current_tenant_id())`, so a
    tenant_admin CAN update its OWN tenants row at the RLS level (which
    columns it may touch is enforced by the API, TenantSettingsUpdate, not by
    RLS). It still cannot update another tenant's row.

    try/finally: if an assert fails before conn.rollback(), the connection
    stays open holding the row lock, and the two_tenants fixture teardown
    (which runs BEFORE scoped_conn's) blocks on that lock for its cleanup
    DELETE, hanging the whole suite without any visible error.
    """
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    try:
        cur.execute("UPDATE tenants SET status = 'cancelled' WHERE id = %s", (two_tenants["a"]["tenant_id"],))
        assert cur.rowcount == 1

        cur.execute("UPDATE tenants SET status = 'cancelled' WHERE id = %s", (two_tenants["b"]["tenant_id"],))
        assert cur.rowcount == 0
    finally:
        conn.rollback()


def test_tenant_cannot_delete_own_tenant_row(two_tenants, scoped_conn):
    # DELETE on tenants is still bypass-only (0008_rls_policies.sql;
    # tenants_delete was never widened like tenants_update).
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    try:
        cur.execute("DELETE FROM tenants WHERE id = %s", (two_tenants["a"]["tenant_id"],))
        assert cur.rowcount == 0
    finally:
        conn.rollback()
    conn.rollback()


# ---------------------------------------------------------------------------
# Session hygiene / connection pooling
# ---------------------------------------------------------------------------

def test_fail_closed_when_no_session_context_set(two_tenants, db_dsn):
    conn = psycopg.connect(db_dsn.app_user)
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM devices WHERE id IN (%s, %s)",
        (two_tenants["a"]["device_id"], two_tenants["b"]["device_id"]),
    )
    assert cur.fetchall() == []
    conn.rollback()
    conn.close()


def test_fail_closed_on_write_when_no_session_context_set(two_tenants, db_dsn):
    conn = psycopg.connect(db_dsn.app_user)
    cur = conn.cursor()
    cur.execute("UPDATE devices SET label = 'x' WHERE id = %s", (two_tenants["a"]["device_id"],))
    assert cur.rowcount == 0
    conn.rollback()
    conn.close()


def test_session_var_does_not_leak_across_transactions_on_reused_connection(two_tenants, db_dsn):
    """
    Simulates a pooled connection reused between two requests from different
    tenants. set_config(..., true) is transaction-local: on COMMIT it must be
    "forgotten", never carrying over the previous request's tenant.
    """
    conn = psycopg.connect(db_dsn.app_user)
    cur = conn.cursor()

    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (str(two_tenants["a"]["tenant_id"]),))
    cur.execute("SELECT set_config('app.bypass_rls', 'false', true)")
    # app.user_id: same contract as tenant_id/bypass_rls
    # (0031_device_groups_and_assignments.sql). Without it,
    # app_can_view_device() fails closed and this SELECT would never see the
    # device regardless of tenant_id, so the leak test would prove nothing.
    cur.execute("SELECT set_config('app.user_id', %s, true)", (str(two_tenants["a"]["user_id"]),))
    cur.execute("SELECT id FROM devices WHERE id = %s", (two_tenants["a"]["device_id"],))
    assert cur.fetchone() is not None
    conn.commit()

    # New implicit transaction on the SAME connection, without setting anything again.
    cur.execute("SELECT id FROM devices WHERE id = %s", (two_tenants["a"]["device_id"],))
    assert cur.fetchone() is None, "the previous transaction's tenant_id/user_id must not survive COMMIT"

    conn.rollback()
    conn.close()


def test_malicious_tenant_id_value_is_inert_data_not_sql(two_tenants, scoped_conn):
    """
    A classic SQL injection payload passed as the VALUE of the session
    variable (never concatenated into the SQL text) must fail the uuid cast,
    not alter the query or leak another tenant's data.
    """
    conn, cur = scoped_conn()
    payload = "' OR '1'='1"
    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (payload,))
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute("SELECT id FROM devices")
    assert exc_info.value.sqlstate == INVALID_TEXT_REPRESENTATION
    conn.rollback()


# ---------------------------------------------------------------------------
# CRITICAL: regression for the main security review finding: direct access
# to a hypertable's physical chunks, bypassing RLS entirely. See the header
# of 0009_timeseries_access.sql.
# ---------------------------------------------------------------------------

def test_app_user_cannot_query_hypertable_chunks_directly(two_tenants, scoped_conn, superuser_conn):
    su_cur = superuser_conn.cursor()
    su_cur.execute(
        """SELECT chunk_schema, chunk_name FROM timescaledb_information.chunks
           WHERE hypertable_name IN ('gps_positions', 'alarms', 'usage_events')"""
    )
    chunks = su_cur.fetchall()
    assert chunks, "no chunks to test; check that two_tenants seeded data"

    conn, cur = scoped_conn(bypass=True)
    for schema, name in chunks:
        with pytest.raises(psycopg.Error) as exc_info:
            cur.execute(f'SELECT 1 FROM "{schema}"."{name}" LIMIT 1')
        assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE, (
            f"{schema}.{name} should be inaccessible to app_user"
        )
        conn.rollback()


def test_app_user_has_no_direct_privilege_on_timeseries_hypertables(two_tenants, superuser_conn):
    su_cur = superuser_conn.cursor()
    for table in ("gps_positions", "alarms", "usage_events"):
        for priv in ("select", "insert", "update", "delete"):
            su_cur.execute("SELECT has_table_privilege('app_user', %s, %s)", (table, priv))
            assert su_cur.fetchone()[0] is False, f"app_user must not have direct {priv} on {table}"


def test_app_user_role_has_no_elevated_attributes(superuser_conn):
    su_cur = superuser_conn.cursor()
    su_cur.execute(
        "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = 'app_user'"
    )
    row = su_cur.fetchone()
    assert row is not None, "the app_user role must exist"
    assert not any(row), f"app_user must not have any elevated attribute, has: {row}"


def test_all_tenant_tables_force_rls(superuser_conn):
    su_cur = superuser_conn.cursor()
    su_cur.execute(
        """SELECT relname FROM pg_class
           WHERE relname IN ('tenants','users','devices','gps_positions','alarms','usage_events')
             AND relnamespace = 'public'::regnamespace
             AND NOT (relrowsecurity AND relforcerowsecurity)"""
    )
    offenders = [r[0] for r in su_cur.fetchall()]
    assert offenders == [], f"tables without ENABLE+FORCE ROW LEVEL SECURITY: {offenders}"


# ---------------------------------------------------------------------------
# ON DELETE RESTRICT: regression for the referential integrity finding that
# ignored RLS (deleting a device/user silently destroyed or mutated
# gps_positions/alarms/usage_events via CASCADE/SET NULL).
# ---------------------------------------------------------------------------

def test_device_with_history_cannot_be_deleted(two_tenants, scoped_conn):
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute("DELETE FROM devices WHERE id = %s", (two_tenants["a"]["device_id"],))
    assert exc_info.value.sqlstate == FOREIGN_KEY_VIOLATION
    conn.rollback()


def test_user_with_usage_history_cannot_be_deleted(two_tenants, scoped_conn):
    conn, cur = scoped_conn(bypass=True)
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute("DELETE FROM users WHERE id = %s", (two_tenants["a"]["user_id"],))
    assert exc_info.value.sqlstate == FOREIGN_KEY_VIOLATION
    conn.rollback()


# ---------------------------------------------------------------------------
# Schema-level defense in depth (protects even bypass_rls=true against bugs
# mixing tenant_id/device_id/user_id from different tenants), exercised
# through the real SECURITY DEFINER functions.
# ---------------------------------------------------------------------------

def test_insert_gps_position_rejects_tenant_id_mismatch(two_tenants, scoped_conn):
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "SELECT insert_gps_position(%s, %s, now(), 0, 0)",
            (two_tenants["b"]["tenant_id"], two_tenants["a"]["device_id"]),
        )
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
    conn.rollback()


def test_device_tenant_mismatch_blocked_even_under_bypass(two_tenants, scoped_conn):
    conn, cur = scoped_conn(bypass=True)
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "SELECT insert_gps_position(%s, %s, now(), 0, 0)",
            (two_tenants["a"]["tenant_id"], two_tenants["b"]["device_id"]),
        )
    assert exc_info.value.sqlstate == RAISE_EXCEPTION
    conn.rollback()


def test_acknowledge_alarm_cross_tenant_denied(two_tenants, scoped_conn):
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "SELECT acknowledge_alarm(%s, %s)",
            (two_tenants["b"]["alarm_id"], two_tenants["a"]["user_id"]),
        )
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
    conn.rollback()


def test_alarm_acknowledged_by_user_of_other_tenant_blocked_under_bypass(two_tenants, scoped_conn):
    """The acknowledgement itself may be done in a bypass session (e.g.
    support acknowledging on behalf of a tenant), but the user recorded as
    acknowledged_by must belong to the alarm's tenant."""
    conn, cur = scoped_conn(bypass=True)
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "SELECT acknowledge_alarm(%s, %s)",
            (two_tenants["a"]["alarm_id"], two_tenants["b"]["user_id"]),
        )
    assert exc_info.value.sqlstate == RAISE_EXCEPTION
    conn.rollback()


def test_usage_event_user_id_from_other_tenant_blocked_on_insert(two_tenants, scoped_conn):
    conn, cur = scoped_conn(bypass=True)
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "SELECT insert_usage_event(%s, %s, now(), %s, 'download', 10)",
            (two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"], two_tenants["b"]["user_id"]),
        )
    assert exc_info.value.sqlstate == RAISE_EXCEPTION
    conn.rollback()


# ---------------------------------------------------------------------------
# acknowledge_alarm(): the only write path on an existing alarm, and it must
# only touch acknowledged_at/acknowledged_by. Regression for the finding that
# a table-wide GRANT UPDATE allowed rewriting video_evidence_key, alarm_type,
# severity, etc. (evidence tampering).
# ---------------------------------------------------------------------------

def test_alarms_raw_update_denied_for_everyone(two_tenants, scoped_conn):
    conn, cur = scoped_conn(bypass=True)
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "UPDATE alarms SET video_evidence_key = 'stolen', alarm_type = 'benign' WHERE id = %s",
            (two_tenants["a"]["alarm_id"],),
        )
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
    conn.rollback()


def test_acknowledge_alarm_only_touches_acknowledgement_columns(two_tenants, scoped_conn):
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute(
        "SELECT acknowledge_alarm(%s, %s)",
        (two_tenants["a"]["alarm_id"], two_tenants["a"]["user_id"]),
    )
    cur.execute(
        "SELECT alarm_type, video_evidence_key, acknowledged_by FROM alarms_v WHERE id = %s",
        (two_tenants["a"]["alarm_id"],),
    )
    alarm_type, video_evidence_key, acknowledged_by = cur.fetchone()
    assert alarm_type == "fatigue_driving"
    assert video_evidence_key is None
    assert acknowledged_by == two_tenants["a"]["user_id"]
    conn.rollback()


# ---------------------------------------------------------------------------
# Regressions from the SECOND security review round (which audited the
# views + SECURITY DEFINER functions redesign itself).
# ---------------------------------------------------------------------------

def test_pg_temp_shadowing_cannot_bypass_device_tenant_trigger(two_tenants, scoped_conn):
    """
    Without pg_temp pinned at the end of search_path in insert_gps_position()
    and enforce_device_tenant_match(), a session able to run arbitrary SQL
    could create a temp table named "devices" and make the trigger read it
    instead of the real table, defeating the tenant/device validation even in
    a bypass session.
    """
    conn, cur = scoped_conn(bypass=True)
    cur.execute("CREATE TEMP TABLE devices (id uuid, tenant_id uuid)")
    cur.execute(
        "INSERT INTO pg_temp.devices VALUES (%s, %s)",
        (two_tenants["b"]["device_id"], two_tenants["a"]["tenant_id"]),
    )
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "SELECT insert_gps_position(%s, %s, now(), 0, 0)",
            (two_tenants["a"]["tenant_id"], two_tenants["b"]["device_id"]),
        )
    assert exc_info.value.sqlstate == RAISE_EXCEPTION
    conn.rollback()


def test_pg_temp_shadowing_cannot_suppress_usage_events(two_tenants, scoped_conn, superuser_conn):
    """The same technique must not divert a real insert_usage_event() into a
    shadow temp table, hiding billable usage."""
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute(
        """CREATE TEMP TABLE usage_events (
            id uuid DEFAULT gen_random_uuid(), "time" timestamptz, tenant_id uuid,
            device_id uuid, user_id uuid, event_type usage_event_type,
            bytes_transferred bigint, metadata jsonb
        )"""
    )
    cur.execute(
        "SELECT insert_usage_event(%s, %s, now(), %s, 'download', 999999999)",
        (two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"], two_tenants["a"]["user_id"]),
    )
    conn.commit()

    su_cur = superuser_conn.cursor()
    su_cur.execute(
        "SELECT sum(bytes_transferred) FROM usage_events WHERE tenant_id = %s",
        (two_tenants["a"]["tenant_id"],),
    )
    total = su_cur.fetchone()[0]
    assert total == 1024 + 999999999, "the event must be written to the REAL table, not a temp shadow"


def test_insert_alarm_rejects_video_evidence_key_without_own_tenant_prefix(two_tenants, scoped_conn):
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    foreign_key = f"tenants/{two_tenants['b']['tenant_id']}/evidence/secret.mp4"
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "SELECT insert_alarm(%s, %s, now(), 'forged', 'info', NULL, %s)",
            (two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"], foreign_key),
        )
    assert exc_info.value.sqlstate == CHECK_VIOLATION
    conn.rollback()


def test_insert_alarm_accepts_video_evidence_key_with_own_tenant_prefix(two_tenants, scoped_conn):
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    own_key = f"tenants/{two_tenants['a']['tenant_id']}/evidence/clip.mp4"
    cur.execute(
        "SELECT insert_alarm(%s, %s, now(), 'forged', 'info', NULL, %s)",
        (two_tenants["a"]["tenant_id"], two_tenants["a"]["device_id"], own_key),
    )
    assert cur.fetchone()[0] is not None
    conn.rollback()


def test_alarms_v_security_barrier_prevents_leaky_qual_pushdown(two_tenants, scoped_conn):
    """
    security_barrier=true prevents an extra predicate in the outer query from
    being evaluated BEFORE the tenant filter embedded in the view. If the
    planner pushed this expression below the filter, it would also run
    against tenant B's row and raise a division by zero: a cross-tenant
    existence oracle via the error type, not the data.
    """
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute(
        "SELECT count(*) FROM alarms_v WHERE 1 / (CASE WHEN tenant_id = %s THEN 0 ELSE 1 END) = 1",
        (two_tenants["b"]["tenant_id"],),
    )
    assert cur.fetchone()[0] == 1  # only the own alarm is visible, and it never divides by zero
    conn.rollback()


# ---------------------------------------------------------------------------
# Append-only tables: not even bypass can mutate written history, and the only
# deletion path (gps_positions/alarms) is through bypass-only functions.
# ---------------------------------------------------------------------------

def test_usage_events_cannot_be_updated(two_tenants, scoped_conn):
    conn, cur = scoped_conn(bypass=True)
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "UPDATE usage_events SET bytes_transferred = 0 WHERE id = %s",
            (two_tenants["a"]["usage_event_id"],),
        )
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
    conn.rollback()


def test_usage_events_cannot_be_deleted(two_tenants, scoped_conn):
    conn, cur = scoped_conn(bypass=True)
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute("DELETE FROM usage_events WHERE id = %s", (two_tenants["a"]["usage_event_id"],))
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
    conn.rollback()


def test_gps_positions_cannot_be_updated(two_tenants, scoped_conn):
    conn, cur = scoped_conn(bypass=True)
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute("UPDATE gps_positions SET lat = 0 WHERE device_id = %s", (two_tenants["a"]["device_id"],))
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
    conn.rollback()


def test_gps_positions_raw_delete_denied_for_everyone(two_tenants, scoped_conn):
    """There is no direct GRANT DELETE on the hypertable: not even a bypass
    session can run a raw DELETE FROM gps_positions; the only path is
    delete_gps_positions_before()."""
    conn, cur = scoped_conn(bypass=True)
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute("DELETE FROM gps_positions WHERE device_id = %s", (two_tenants["a"]["device_id"],))
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
    conn.rollback()


def test_delete_gps_positions_before_requires_bypass(two_tenants, scoped_conn):
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "SELECT delete_gps_positions_before(%s, now() + interval '1 day')",
            (two_tenants["a"]["device_id"],),
        )
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
    conn.rollback()


def test_delete_gps_positions_before_works_under_bypass(two_tenants, scoped_conn):
    conn, cur = scoped_conn(bypass=True)
    cur.execute(
        "SELECT delete_gps_positions_before(%s, now() + interval '1 day')",
        (two_tenants["a"]["device_id"],),
    )
    assert cur.fetchone()[0] == 1
    conn.rollback()


def test_alarms_raw_delete_denied_for_everyone(two_tenants, scoped_conn):
    conn, cur = scoped_conn(bypass=True)
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute("DELETE FROM alarms WHERE id = %s", (two_tenants["a"]["alarm_id"],))
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
    conn.rollback()


def test_delete_alarm_requires_bypass(two_tenants, scoped_conn):
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute("SELECT delete_alarm(%s)", (two_tenants["a"]["alarm_id"],))
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
    conn.rollback()


def test_delete_alarm_works_under_bypass(two_tenants, scoped_conn):
    conn, cur = scoped_conn(bypass=True)
    cur.execute("SELECT delete_alarm(%s)", (two_tenants["a"]["alarm_id"],))
    cur.execute("SELECT 1 FROM alarms_v WHERE id = %s", (two_tenants["a"]["alarm_id"],))
    assert cur.fetchone() is None
    conn.rollback()


# ---------------------------------------------------------------------------
# Data integrity invariants (users.tenant_id / role / bypass flag)
# ---------------------------------------------------------------------------

def test_platform_role_with_tenant_id_rejected(two_tenants, bypass_conn):
    _, cur = bypass_conn
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            """INSERT INTO users (tenant_id, email, password_hash, role, is_platform_bypass)
               VALUES (%s, 'fake-admin@example.com', 'x', 'super_admin', true)""",
            (two_tenants["a"]["tenant_id"],),
        )
    assert exc_info.value.sqlstate == CHECK_VIOLATION


def test_tenant_role_without_tenant_id_rejected(bypass_conn):
    _, cur = bypass_conn
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            """INSERT INTO users (tenant_id, email, password_hash, role)
               VALUES (NULL, 'orphan@example.com', 'x', 'tenant_admin')"""
        )
    assert exc_info.value.sqlstate == CHECK_VIOLATION


def test_same_email_allowed_across_different_tenants(two_tenants, bypass_conn):
    _, cur = bypass_conn
    email = "shared-across-tenants@example.com"
    cur.execute(
        "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (%s, %s, 'x', 'tenant_viewer')",
        (two_tenants["a"]["tenant_id"], email),
    )
    cur.execute(
        "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (%s, %s, 'x', 'tenant_viewer')",
        (two_tenants["b"]["tenant_id"], email),
    )  # must not raise


def test_duplicate_email_within_same_tenant_rejected(two_tenants, bypass_conn):
    _, cur = bypass_conn
    email = "dup-within-tenant@example.com"
    cur.execute(
        "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (%s, %s, 'x', 'tenant_viewer')",
        (two_tenants["a"]["tenant_id"], email),
    )
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role) VALUES (%s, %s, 'x', 'tenant_viewer')",
            (two_tenants["a"]["tenant_id"], email),
        )
    assert exc_info.value.sqlstate == UNIQUE_VIOLATION


def test_jt808_terminal_id_unique_across_tenants(two_tenants, bypass_conn):
    _, cur = bypass_conn
    terminal_id = f"dup-terminal-{two_tenants['a']['tenant_id']}"
    cur.execute(
        "INSERT INTO devices (tenant_id, jt808_terminal_id, label) VALUES (%s, %s, 'd1')",
        (two_tenants["a"]["tenant_id"], terminal_id),
    )
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "INSERT INTO devices (tenant_id, jt808_terminal_id, label) VALUES (%s, %s, 'd2')",
            (two_tenants["b"]["tenant_id"], terminal_id),
        )
    assert exc_info.value.sqlstate == UNIQUE_VIOLATION
