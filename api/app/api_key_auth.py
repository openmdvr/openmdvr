"""API key authentication resolution (0034_api_keys.sql) -- separate from
security.py (pure crypto primitives, no data access) and from deps.py (FastAPI
orchestration, decides WHAT to do with these claims): this module is the only
one that goes to the database to validate a key.

Same "alive NOW, not when it was issued" rule that assert_session_active
applies to a JWT (deps.py) -- here it is resolved in the SAME query, since the
database has to be queried anyway to find the key by its hash."""
from __future__ import annotations

import datetime as dt
import logging

from psycopg_pool import AsyncConnectionPool

from . import db as db_module
from .config import Settings
from .security import TokenClaims, hash_api_key

logger = logging.getLogger(__name__)

_LOOKUP_SQL = """
    SELECT ak.id, ak.tenant_id, ak.user_id, ak.can_write, ak.allowed_device_ids,
           ak.revoked_at, ak.expires_at,
           u.role, u.is_platform_bypass, u.status, u.driver_id,
           t.status
    FROM api_keys ak
    JOIN users u ON u.id = ak.user_id
    LEFT JOIN tenants t ON t.id = ak.tenant_id
    WHERE ak.key_hash = %s
"""


async def resolve_api_key(pool: AsyncConnectionPool, settings: Settings, raw_key: str) -> TokenClaims | None:
    """None if the key does not exist, is revoked/expired, or the account/
    tenant it represents is no longer active -- a generic 401 in every case
    (deps.py), never revealing which (neither confirm nor deny)."""
    key_hash = hash_api_key(settings, raw_key)
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        row = await (await conn.execute(_LOOKUP_SQL, (key_hash,))).fetchone()
        if row is None:
            return None
        (
            api_key_id, tenant_id, user_id, can_write, allowed_device_ids,
            revoked_at, expires_at, role, is_platform_bypass, user_status, driver_id,
            tenant_status,
        ) = row

        now = dt.datetime.now(dt.timezone.utc)
        if revoked_at is not None or expires_at <= now:
            return None
        if user_status != "active":
            return None
        if tenant_status is not None and tenant_status != "active":
            return None

        # Best-effort -- a failure here must never block actual authentication
        # (a secondary problem must not block the primary action).
        try:
            await conn.execute("UPDATE api_keys SET last_used_at = now() WHERE id = %s", (api_key_id,))
        except Exception:
            logger.exception("api_key_auth: could not update last_used_at for %s", api_key_id)

    return TokenClaims(
        user_id=str(user_id),
        tenant_id=str(tenant_id),
        role=role,
        is_platform_bypass=bool(is_platform_bypass),
        driver_id=str(driver_id) if driver_id else None,
        auth_method="api_key",
        can_write=bool(can_write),
        # `is not None`, NEVER a truthiness check -- an empty list
        # (allowed_device_ids=[] in the DB) is a real value DISTINCT from None:
        # "if allowed_device_ids else None" would turn [] into None, losing a
        # key deliberately scoped to no device at all.
        allowed_device_ids=tuple(str(d) for d in allowed_device_ids) if allowed_device_ids is not None else None,
        api_key_id=str(api_key_id),
    )


async def log_api_key_usage(
    pool: AsyncConnectionPool,
    *,
    api_key_id: str,
    tenant_id: str,
    method: str,
    path: str,
    status_code: int,
    ip_address: str | None,
) -> None:
    """Audit log of every API-key-authenticated request -- best-effort, a
    problem writing the log must never take down a real request."""
    # A \x00 in the path (Postgres rejects NUL in text columns) used to make
    # THIS audit row disappear silently (the try/except below swallows it on
    # purpose) -- exactly the request most worth auditing (someone sending odd
    # bytes) became invisible. Sanitized before the INSERT instead of relying
    # on it never failing.
    clean_path = path.replace("\x00", "")
    try:
        async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
            await conn.execute(
                """INSERT INTO api_key_usage_log (api_key_id, tenant_id, method, path, status_code, ip_address)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (api_key_id, tenant_id, method, clean_path[:2000], status_code, ip_address),
            )
    except Exception:
        logger.exception("api_key_auth: could not record usage for key %s", api_key_id)
