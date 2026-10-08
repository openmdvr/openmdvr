"""DB-level RLS coverage for notifications
(infra/postgres/migrations/0033_notifications.sql), complementing
api/tests/test_notifications.py (which already covers the full fan-out and
HTTP isolation). This file focuses on what can only be tested with direct
SQL: the real scope of the GRANT.

two_tenants (conftest.py) already seeds an alarm_id via insert_alarm(); with
the fan-out, that same call also creates a notifications row for the seeded
tenant_admin (user_id), reused here."""
import psycopg
import pytest

INSUFFICIENT_PRIVILEGE = "42501"


def test_update_grant_is_scoped_to_read_at_column(two_tenants, scoped_conn):
    """Security finding F3: the original GRANT covered the whole table, so any
    session could rewrite tenant_id/email_status/title/severity of its OWN
    notification via direct SQL. GRANT UPDATE (read_at) (migration 0033) must
    reject any UPDATE touching another column, even when combined with
    read_at in the same statement."""
    _, cur = scoped_conn(
        tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"]
    )
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            "UPDATE notifications SET read_at = now(), title = 'ARBITRARY' WHERE recipient_user_id = %s",
            (two_tenants["a"]["user_id"],),
        )
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE


def test_update_read_at_alone_is_allowed(two_tenants, scoped_conn):
    """Positive case: the only real path (mark as read) still works after
    narrowing the GRANT."""
    conn, cur = scoped_conn(tenant_id=two_tenants["a"]["tenant_id"], user_id=two_tenants["a"]["user_id"])
    cur.execute(
        "UPDATE notifications SET read_at = now() WHERE recipient_user_id = %s",
        (two_tenants["a"]["user_id"],),
    )
    assert cur.rowcount == 1
    conn.rollback()


def test_insert_denied_even_with_bypass(two_tenants, scoped_conn):
    """app_user has no INSERT grant at all; not even bypass can insert
    directly. The only real write path is insert_alarm() (SECURITY DEFINER,
    runs as the function owner)."""
    conn, cur = scoped_conn(tenant_id=None, bypass=True)
    with pytest.raises(psycopg.Error) as exc_info:
        cur.execute(
            """INSERT INTO notifications (tenant_id, recipient_user_id, event_type, title)
               VALUES (%s, %s, 'device_alarm', 'x')""",
            (two_tenants["a"]["tenant_id"], two_tenants["a"]["user_id"]),
        )
    assert exc_info.value.sqlstate == INSUFFICIENT_PRIVILEGE
    conn.rollback()
