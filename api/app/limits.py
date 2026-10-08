"""Operational limits, configurable per deployment through environment variables.

Every value here is a capacity or abuse-protection limit that an operator may
need to tune for their hardware and fleet size, so none of them is hardcoded in
the routers. Defaults are safe for a small single-VM deployment. See
infra/.env.example for the full list.

Limits tied to the database schema or to a device protocol (NUMERIC column
precision, GT06 command length, polygon vertex budget enforced in SQL) are NOT
here on purpose: changing them without changing the schema or the protocol
would break something.

Values are read once at import time. An invalid value (not a number, or below
its minimum) stops the API at startup instead of being silently ignored.
"""

from __future__ import annotations

import os


def _env_number(name: str, default: float, minimum: float, cast: type) -> float:
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return cast(default)
    try:
        value = cast(raw)
    except ValueError:
        raise RuntimeError(f"limits: {name}={raw!r} is not a valid number") from None
    if value < minimum:
        raise RuntimeError(f"limits: {name}={raw!r} must be >= {minimum}")
    return value


def env_int(name: str, default: int, minimum: int = 1) -> int:
    return int(_env_number(name, default, minimum, int))


def env_float(name: str, default: float, minimum: float = 0.001) -> float:
    return float(_env_number(name, default, minimum, float))


# --- Database -----------------------------------------------------------------
# Connections per API process. Keep (API processes x this) + device server
# PG_MAX_CONNS below PostgreSQL's max_connections.
API_DB_POOL_MAX_SIZE = env_int("API_DB_POOL_MAX_SIZE", 10)

# --- Rate limits (requests per minute) ----------------------------------------
API_KEY_RATE_LIMIT_PER_MINUTE = env_int("API_KEY_RATE_LIMIT_PER_MINUTE", 120)
API_KEY_FAIL_RATE_LIMIT_PER_MINUTE = env_int("API_KEY_FAIL_RATE_LIMIT_PER_MINUTE", 200)
ROUTE_HISTORY_RATE_LIMIT_PER_MINUTE = env_int("ROUTE_HISTORY_RATE_LIMIT_PER_MINUTE", 30)
SNAPSHOT_RATE_LIMIT_PER_MINUTE = env_int("SNAPSHOT_RATE_LIMIT_PER_MINUTE", 12)
WEBHOOK_TEST_RATE_LIMIT_PER_MINUTE = env_int("WEBHOOK_TEST_RATE_LIMIT_PER_MINUTE", 10)

# --- Reports and heavy queries --------------------------------------------------
REPORT_QUERY_TIMEOUT_MS = env_int("REPORT_QUERY_TIMEOUT_MS", 8000, minimum=100)
ROUTE_HISTORY_MAX_DAYS = env_int("ROUTE_HISTORY_MAX_DAYS", 31)
ROUTE_HISTORY_MAX_POINTS = env_int("ROUTE_HISTORY_MAX_POINTS", 3000, minimum=100)
ROUTE_HISTORY_MAX_EVENTS = env_int("ROUTE_HISTORY_MAX_EVENTS", 500, minimum=10)
DISTANCE_REPORT_MAX_DAYS = env_int("DISTANCE_REPORT_MAX_DAYS", 31)
HOURS_REPORT_MAX_DAYS = env_int("HOURS_REPORT_MAX_DAYS", 31)
GEOFENCE_REPORT_MAX_DAYS = env_int("GEOFENCE_REPORT_MAX_DAYS", 93)
GEOFENCE_REPORT_MAX_VISITS = env_int("GEOFENCE_REPORT_MAX_VISITS", 2000)

# --- Outbound webhooks ----------------------------------------------------------
WEBHOOK_MAX_ENDPOINTS_PER_TENANT = env_int("WEBHOOK_MAX_ENDPOINTS_PER_TENANT", 10)
WEBHOOK_DELIVERY_TIMEOUT_SECONDS = env_float("WEBHOOK_DELIVERY_TIMEOUT_SECONDS", 10.0, minimum=1.0)
WEBHOOK_MAX_ATTEMPTS = env_int("WEBHOOK_MAX_ATTEMPTS", 6)
WEBHOOK_MAX_CONCURRENT_DELIVERIES = env_int("WEBHOOK_MAX_CONCURRENT_DELIVERIES", 10)
