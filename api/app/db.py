"""Postgres access layer. Single source of truth for how the API fulfills the
RLS session contract documented in
infra/postgres/migrations/0003_rls_helpers.sql: at the start of every
transaction, before any business query, app.tenant_id/app.bypass_rls are set
via set_config(..., true) -- ALWAYS with the third argument true (transaction
scope, so a recycled pool connection never "carries over" a previous
request's tenant) and ALWAYS with the value bound as a parameter, never
interpolated into the SQL text.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from .config import Settings
from . import limits


def build_conninfo(settings: Settings) -> str:
    return (
        f"host={settings.pg_host} port={settings.pg_port} "
        f"dbname={settings.pg_database} user={settings.pg_user} "
        f"password={settings.pg_password}"
    )


def build_pool(settings: Settings) -> AsyncConnectionPool:
    # open=False: opened explicitly in the FastAPI lifespan (see main.py) to
    # control startup/shutdown order.
    return AsyncConnectionPool(build_conninfo(settings), min_size=1, max_size=limits.API_DB_POOL_MAX_SIZE, open=False)


@asynccontextmanager
async def tenant_scoped_connection(
    pool: AsyncConnectionPool,
    *,
    tenant_id: str | None,
    bypass: bool,
    driver_id: str | None = None,
    user_id: str | None = None,
    api_key_device_filter: tuple[str, ...] | None = None,
) -> AsyncIterator[AsyncConnection]:
    """Yields a connection with the RLS context already set for ONE transaction.

    tenant_id/bypass/driver_id/user_id/api_key_device_filter must come from an
    already validated JWT or API key (see security.py/deps.py) -- never from a
    value the client can send directly without that verification. driver_id
    is only non-None for a `driver` role session (see migration 0015 /
    deps.get_db) -- any other session leaves it empty, and
    app_current_driver_id() in RLS returns NULL for those. user_id (same
    mechanism, migration 0031) travels in ANY authenticated session -- used by
    app_can_view_device() (0031_device_groups_and_assignments.sql, wired into
    RLS in 0032_device_visibility_rls.sql) to restrict device/alarm/position
    visibility to what each user is assigned. api_key_device_filter
    (0034_api_keys.sql) is only non-None for an API-key session with
    allowed_device_ids configured -- it narrows that same set FURTHER inside
    app_can_view_device(), without any endpoint/view having to know.
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.bypass_rls', %s, true)",
                ("true" if bypass else "false",),
            )
            await conn.execute(
                "SELECT set_config('app.tenant_id', %s, true)",
                (tenant_id or "",),
            )
            await conn.execute(
                "SELECT set_config('app.driver_id', %s, true)",
                (driver_id or "",),
            )
            await conn.execute(
                "SELECT set_config('app.user_id', %s, true)",
                (user_id or "",),
            )
            # Deliberate encoding (see app_api_key_device_filter() in
            # 0034_api_keys.sql): None (no API key, or no allowed_device_ids)
            # MUST be distinguishable from an empty tuple (API key scoped to NO
            # device) -- "if api_key_device_filter else ''" would treat both
            # the same (() is falsy), losing the real deny-all case.
            if api_key_device_filter is None:
                device_filter_value = "__unset__"
            else:
                device_filter_value = ",".join(api_key_device_filter)
            await conn.execute(
                "SELECT set_config('app.api_key_device_filter', %s, true)",
                (device_filter_value,),
            )
            yield conn
