"""
Shared fixtures for the RLS isolation test suite.

These tests NEVER connect as superuser to validate isolation: a Postgres
superuser always ignores Row Level Security regardless of FORCE ROW LEVEL
SECURITY (a hard Postgres rule), so testing isolation with that role would give
false positives. Every isolation assertion connects as `app_user` (the same
restricted role the API uses) and sets the `app.tenant_id` / `app.bypass_rls`
session variables exactly as the real middleware does. The superuser is only
used to get a "ground truth" count to compare against a bypass session.
"""
import os
import uuid
from pathlib import Path

import psycopg
import pytest


def _load_dotenv_if_needed():
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv_if_needed()

PG_HOST = os.environ.get("PGHOST", "127.0.0.1")
PG_PORT = os.environ.get("PGPORT", "55432")
PG_DB = os.environ.get("PGDATABASE", "openmdvr")
SUPERUSER_PASSWORD = os.environ["POSTGRES_SUPERUSER_PASSWORD"]
APP_USER_PASSWORD = os.environ["APP_USER_PASSWORD"]

SUPERUSER_DSN = (
    f"host={PG_HOST} port={PG_PORT} dbname={PG_DB} "
    f"user=postgres password={SUPERUSER_PASSWORD}"
)
APP_USER_DSN = (
    f"host={PG_HOST} port={PG_PORT} dbname={PG_DB} "
    f"user=app_user password={APP_USER_PASSWORD}"
)


@pytest.fixture(scope="session")
def superuser_conn():
    conn = psycopg.connect(SUPERUSER_DSN, autocommit=True)
    yield conn
    conn.close()


class _Dsn:
    app_user = APP_USER_DSN
    superuser = SUPERUSER_DSN


@pytest.fixture
def db_dsn():
    """Raw DSNs, for tests that need to control COMMIT/ROLLBACK boundaries
    themselves (e.g. simulating connection reuse from a pool)."""
    return _Dsn()


@pytest.fixture
def scoped_conn():
    """
    Factory fixture. Each call opens a NEW connection as app_user (simulating a
    "fresh" connection taken from the pool) and returns (conn, cur) with the
    session variables already set via set_config(..., true), exactly the
    contract the API must honor in production. All opened connections are
    rolled back and closed at the end of the test, so no test pollutes the next.
    """
    opened = []

    def _make(
        tenant_id: str | None = None,
        bypass: bool = False,
        local: bool = True,
        user_id: str | None = None,
        api_key_device_filter: list[str] | None = None,
    ):
        """
        local=True (default) reproduces EXACTLY the production contract:
        set_config(..., is_local=true) inside an explicit transaction, as the
        API middleware does on every request.

        local=False is only a convenience shortcut for seeding test data with
        separate autocommit statements (it does not simulate the production
        connection pool, so no isolation test should use local=False).

        user_id (same mechanism as tenant_id, GUC app.user_id; see
        0031_device_groups_and_assignments.sql/0032_device_visibility_rls.sql):
        production ALWAYS sets it (every authenticated session has a user_id),
        so a per-TENANT isolation test that omits it no longer reproduces the
        real contract: app_can_view_device() would fail CLOSED (no
        device/alarm/position visible) regardless of tenant_id. Tests that need
        to see devices/alarms/gps_positions as a real tenant_admin must pass
        the user_id seeded by two_tenants (e.g. two_tenants["a"]["user_id"]).
        """
        conn = psycopg.connect(APP_USER_DSN)
        if not local:
            conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT set_config('app.bypass_rls', %s, %s)", ("true" if bypass else "false", local))
        cur.execute(
            "SELECT set_config('app.tenant_id', %s, %s)",
            (str(tenant_id) if tenant_id else "", local),
        )
        cur.execute(
            "SELECT set_config('app.user_id', %s, %s)",
            (str(user_id) if user_id else "", local),
        )
        # api_key_device_filter (0034_api_keys.sql, same encoding as
        # api/app/db.py::tenant_scoped_connection): None = "__unset__" (no
        # extra restriction), [] = empty string (scoped to NO device, which is
        # DIFFERENT from None; never conflate them, see
        # app_api_key_device_filter() in the migration).
        if api_key_device_filter is None:
            device_filter_value = "__unset__"
        else:
            device_filter_value = ",".join(str(d) for d in api_key_device_filter)
        cur.execute(
            "SELECT set_config('app.api_key_device_filter', %s, %s)",
            (device_filter_value, local),
        )
        opened.append(conn)
        return conn, cur

    yield _make

    for c in opened:
        try:
            c.rollback()
        except Exception:
            pass
        c.close()


@pytest.fixture
def bypass_conn(scoped_conn):
    """bypass=true autocommit connection, to seed test data as a super_admin flow would."""
    return scoped_conn(bypass=True, local=False)


@pytest.fixture
def two_tenants(bypass_conn, superuser_conn):
    """
    Seeds two independent tenants, each with: a tenant_admin user, a device, a
    GPS position, an alarm and a usage_event. Returns a dict with every id the
    tests need.

    Writes to gps_positions/alarms/usage_events go through the SECURITY DEFINER
    functions of 0009_timeseries_access.sql (app_user has no direct GRANT on
    those three hypertables; see the security finding documented there).

    Cleanup: device_id/user_id are ON DELETE RESTRICT from the three time-series
    tables (on purpose, see 0007), so a DELETE CASCADE from tenants is not
    enough and those rows must be deleted first. app_user has no way to delete
    usage_events (a truly append-only ledger, not even with bypass), so cleanup
    uses the superuser connection, exactly like a real data purge outside the
    application role.
    """
    conn, cur = bypass_conn
    data = {}
    for key in ("a", "b"):
        suffix = uuid.uuid4().hex[:8]
        cur.execute(
            "INSERT INTO tenants (name) VALUES (%s) RETURNING id",
            (f"tenant-{key}-{suffix}",),
        )
        tenant_id = cur.fetchone()[0]

        cur.execute(
            """INSERT INTO users (tenant_id, email, password_hash, role)
               VALUES (%s, %s, 'x', 'tenant_admin') RETURNING id""",
            (tenant_id, f"admin-{key}-{suffix}@example.com"),
        )
        user_id = cur.fetchone()[0]

        cur.execute(
            """INSERT INTO devices (tenant_id, jt808_terminal_id, label)
               VALUES (%s, %s, %s) RETURNING id""",
            (tenant_id, f"jt808-{key}-{suffix}", f"device-{key}"),
        )
        device_id = cur.fetchone()[0]

        cur.execute(
            "SELECT insert_gps_position(%s, %s, now(), 19.4, -99.1)",
            (tenant_id, device_id),
        )

        cur.execute(
            "SELECT insert_alarm(%s, %s, now(), 'fatigue_driving')",
            (tenant_id, device_id),
        )
        alarm_id = cur.fetchone()[0]

        cur.execute(
            "SELECT insert_usage_event(%s, %s, now(), %s, 'live_view', 1024)",
            (tenant_id, device_id, user_id),
        )
        usage_event_id = cur.fetchone()[0]

        data[key] = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "device_id": device_id,
            "alarm_id": alarm_id,
            "usage_event_id": usage_event_id,
        }

    yield data

    tenant_ids = (data["a"]["tenant_id"], data["b"]["tenant_id"])
    su_cur = superuser_conn.cursor()
    su_cur.execute("DELETE FROM usage_events WHERE tenant_id IN (%s, %s)", tenant_ids)
    su_cur.execute("DELETE FROM alarms WHERE tenant_id IN (%s, %s)", tenant_ids)
    su_cur.execute("DELETE FROM gps_positions WHERE tenant_id IN (%s, %s)", tenant_ids)
    su_cur.execute("DELETE FROM tenants WHERE id IN (%s, %s)", tenant_ids)
