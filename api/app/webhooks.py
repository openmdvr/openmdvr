"""Outgoing webhooks -- the "push" counterpart of API keys (api_key_auth.py,
"pull": a third party queries us). See infra/postgres/migrations/
0035_webhooks.sql for the data design (why the secret is stored in plain
text, the role dimension in RLS, the circuit breaker).

Two deliberately separate pieces:
  1. run_webhook_dispatch_listener() -- listens on the SAME Postgres channel
     the in-app mailbox uses (notifications.py, 'notifications') and, per
     alarm, runs ONE cheap indexed query: "does this tenant have the feature
     enabled AND any endpoint subscribed to this event?". If not, it stops
     there -- zero extra work. If so, it enqueues one webhook_deliveries row
     per matching endpoint.
  2. run_webhook_delivery_worker() -- the part that spends time/network:
     claims pending deliveries (FOR UPDATE SKIP LOCKED + a 60 s "lease" via
     next_attempt_at, a standard job-queue pattern -- if the process dies
     mid-delivery, the row becomes available again when the lease expires,
     no cleanup job needed), signs each payload (Stripe-style HMAC-SHA256
     over "timestamp.body", not just the body, so the receiver can reject
     old replays), delivers with exponential retry, and disables the
     endpoint (circuit breaker, via a migration trigger) after too many
     consecutive failures -- never retries forever against a dead endpoint."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
import psycopg

from . import db as db_module
from .config import Settings
from .db import build_conninfo
from . import limits

logger = logging.getLogger(__name__)

_LISTEN_CHANNEL = "notifications"  # same channel as notifications.py -- several sessions can LISTEN on it independently.
_LISTENER_APPLICATION_NAME = "openmdvr_webhook_dispatcher"

_RECONNECT_BACKOFF_INITIAL = 1.0
_RECONNECT_BACKOFF_MAX = 30.0

_DELIVERY_TIMEOUT_SECONDS = limits.WEBHOOK_DELIVERY_TIMEOUT_SECONDS
_MAX_ATTEMPTS = limits.WEBHOOK_MAX_ATTEMPTS
# 1min, 5min, 30min, 2h, 6h, 24h -- exponential backoff with a generous
# ceiling: gives a receiver time to recover from a transient incident
# (deploy, restart) without hammering it, and never retries forever (see
# _MAX_ATTEMPTS + the migration's circuit breaker).
_RETRY_DELAYS_SECONDS = [60, 300, 1800, 7200, 21600, 86400]
_LEASE_SECONDS = 60
_WORKER_POLL_SECONDS = 5.0
_WORKER_BATCH_SIZE = 50
_MAX_CONCURRENT_DELIVERIES = limits.WEBHOOK_MAX_CONCURRENT_DELIVERIES
_MAX_ERROR_SNIPPET = 500


def generate_webhook_secret() -> str:
    return secrets.token_urlsafe(32)


def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    """Stripe-style signature: HMAC-SHA256 over "timestamp.body" (not just
    the body) -- binds the timestamp to the signature so a receiver can
    reject a replayed old delivery by checking the timestamp is recent
    BEFORE trusting the signature."""
    signed_payload = f"{timestamp}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()


async def _resolve_safe_ip(hostname: str) -> str | None:
    """Resolves the hostname and returns ONE validated (public) address, or
    None if any resolved address is private/reserved or it does not
    resolve. This is the basis of "pinning": deliver_webhook() connects to
    THIS exact IP and never resolves DNS again."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, None)
    except OSError:
        return None
    if not infos:
        return None
    chosen: str | None = None
    for info in infos:
        raw_ip = info[4][0].split("%")[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            return None
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return None
        if chosen is None:
            chosen = raw_ip
    return chosen


@dataclass
class DeliveryResult:
    success: bool
    status_code: int | None
    error: str | None
    elapsed_ms: int


async def deliver_webhook(url: str, body: bytes, headers: dict[str, str]) -> DeliveryResult:
    """The single place that performs the outgoing POST (delivery worker AND
    the "Send test" button), with two guarantees:

    1. DNS rebinding is closed: validating by resolving DNS and then letting
       httpx resolve again on connect would let a domain answer a public IP
       at validation time and a private one milliseconds later, reaching the
       internal network. Now DNS is resolved ONCE, validated, and the
       connection goes to that literal IP; the original Host goes in the
       header and in SNI (httpcore `sni_hostname` extension), so TLS still
       verifies the certificate against the real domain.
    2. Redirects are not followed (a redirect could point at the internal
       network), but the error states exactly where it redirects -- a common
       silent cause of "the webhook doesn't work" (http->https, missing
       trailing "/", etc.)."""
    started = time.monotonic()

    def _done(success: bool, status_code: int | None, error: str | None) -> DeliveryResult:
        return DeliveryResult(success, status_code, error, int((time.monotonic() - started) * 1000))

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return _done(False, None, "invalid URL (must be http:// or https://)")
    ip = await _resolve_safe_ip(parsed.hostname)
    if ip is None:
        return _done(False, None, "destination URL not allowed or does not resolve (private/reserved network or nonexistent DNS)")

    host_literal = f"[{ip}]" if ":" in ip else ip
    netloc = host_literal + (f":{parsed.port}" if parsed.port else "")
    pinned_url = parsed._replace(netloc=netloc).geturl()
    host_header = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    request_headers = {**headers, "Host": host_header, "User-Agent": "OpenMDVR-Webhooks/1.0"}
    extensions = {"sni_hostname": parsed.hostname} if parsed.scheme == "https" else {}

    try:
        async with httpx.AsyncClient(timeout=_DELIVERY_TIMEOUT_SECONDS, follow_redirects=False) as client:
            resp = await client.post(pinned_url, content=body, headers=request_headers, extensions=extensions)
    except httpx.TimeoutException:
        return _done(False, None, f"destination did not respond within {int(_DELIVERY_TIMEOUT_SECONDS)} s")
    except httpx.ConnectError as exc:
        return _done(False, None, f"could not connect to destination: {str(exc)[:_MAX_ERROR_SNIPPET]}")
    except Exception as exc:
        return _done(False, None, str(exc)[:_MAX_ERROR_SNIPPET] or type(exc).__name__)

    if 200 <= resp.status_code < 300:
        return _done(True, resp.status_code, None)
    if 300 <= resp.status_code < 400:
        location = resp.headers.get("location", "?")
        return _done(
            False, resp.status_code,
            f"HTTP {resp.status_code}: destination redirects to {location[:300]} -- use that final URL (redirects are not followed)",
        )
    return _done(False, resp.status_code, f"HTTP {resp.status_code}: {resp.text[:_MAX_ERROR_SNIPPET]}")


def build_signed_headers(secret: str, event_type: str, delivery_id: str, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = sign_payload(secret, timestamp, body)
    return {
        "Content-Type": "application/json",
        "X-OpenMDVR-Event": event_type,
        "X-OpenMDVR-Delivery-Id": delivery_id,
        "X-OpenMDVR-Timestamp": timestamp,
        "X-OpenMDVR-Signature": f"sha256={signature}",
    }


async def _resolve_and_check_safe(hostname: str) -> bool:
    """SSRF protection: a tenant_admin controls the destination URL and this
    server makes the outgoing HTTP request -- without this, the URL could
    target internal infrastructure (localhost, the Docker network, the cloud
    metadata endpoint at 169.254.169.254, etc.). Resolves the hostname
    asynchronously (never synchronous socket.getaddrinfo, which would block
    the process's only event loop) and rejects if ANY resolved address is
    private/loopback/link-local/reserved/multicast.

    Used when CREATING/EDITING an endpoint (early validation, clean 422).
    Actual delivery does not rely on this: deliver_webhook() resolves,
    validates and connects to the SAME IP (pinning), closing the DNS
    rebinding gap this function alone cannot close."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        raw_ip = info[4][0].split("%")[0]  # drop the IPv6 link-local zone id if present (fe80::1%eth0)
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


async def is_webhook_url_safe(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    return await _resolve_and_check_safe(parsed.hostname)


@dataclass
class WebhooksState:
    """Single container stored in app.state.webhooks (see main.py lifespan)
    -- same pattern as NotificationsState/LivePositionsState.
    delivery_wake_event wakes the delivery worker IMMEDIATELY when the
    dispatcher enqueues something, instead of waiting for the next poll:
    near-instant delivery in the common case without a short poll interval
    wasting cycles when nothing is pending."""
    delivery_wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    dispatch_listener_connected: bool = False


_MATCHING_ENDPOINTS_SQL = """
    SELECT we.id
    FROM webhook_endpoints we
    JOIN tenants t ON t.id = we.tenant_id
    WHERE we.tenant_id = %s AND we.enabled AND t.webhooks_enabled
      AND we.event_types @> ARRAY[%s]::text[]
"""

# alarms.details keys that ARE sent in the webhook, by alarm_type prefix.
# Adding a new type is one deliberately reviewed line here.
_WEBHOOK_DETAIL_KEYS: tuple[tuple[str, frozenset[str]], ...] = (
    ("geofence_", frozenset({"geofence_id", "geofence_name", "geofence_event_id", "duration_s", "lat", "lon", "summary"})),
    ("overspeed_limit", frozenset({"speed_kmh", "max_speed_kmh"})),
)


def _webhook_details(alarm_type: str, details) -> dict | None:
    if not isinstance(details, dict):
        return None
    for prefix, keys in _WEBHOOK_DETAIL_KEYS:
        if alarm_type.startswith(prefix):
            return {k: v for k, v in details.items() if k in keys}
    return None


_ALARM_SELECT_SQL = """
    SELECT a.device_id, d.label, a.alarm_type, a.severity, a.time, a.details
    FROM alarms_v a JOIN devices d ON d.id = a.device_id
    WHERE a.id = %s AND a.tenant_id = %s
"""


async def run_webhook_dispatch_listener(
    settings: Settings, pool, state: WebhooksState, stop_event: asyncio.Event
) -> None:
    """Same backoff reconnection mechanism as run_listener/
    run_notification_listener -- LISTEN on the 'notifications' channel that
    insert_alarm() already uses (0033), without touching that migration:
    several sessions can LISTEN on the same Postgres channel independently."""
    conninfo = build_conninfo(settings)
    backoff = _RECONNECT_BACKOFF_INITIAL
    while not stop_event.is_set():
        try:
            async with await psycopg.AsyncConnection.connect(
                conninfo, autocommit=True, application_name=_LISTENER_APPLICATION_NAME,
            ) as conn:
                await conn.execute(f"LISTEN {_LISTEN_CHANNEL}")
                backoff = _RECONNECT_BACKOFF_INITIAL
                state.dispatch_listener_connected = True
                async for notify in conn.notifies():
                    if stop_event.is_set():
                        break
                    try:
                        payload = json.loads(notify.payload)
                        if not isinstance(payload, dict):
                            raise ValueError("NOTIFY payload is not a JSON object")
                        alarm_id = payload.get("alarm_id")
                        tenant_id = payload.get("tenant_id")
                        if not alarm_id or not tenant_id:
                            raise ValueError("NOTIFY payload is missing alarm_id/tenant_id")
                        await _dispatch_alarm_event(pool, state, tenant_id, alarm_id)
                    except Exception:
                        logger.warning(
                            "webhooks: notification dropped, could not be processed", exc_info=True
                        )
                        continue
        except asyncio.CancelledError:
            raise
        except Exception:
            state.dispatch_listener_connected = False
            logger.exception(
                "webhooks: dispatcher lost its connection, retrying in %.0fs", backoff
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)


async def _dispatch_alarm_event(pool, state: WebhooksState, tenant_id: str, alarm_id: str) -> None:
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        # Cheap query that ALWAYS runs (the minimum cost of knowing whether
        # there is anything to do) -- it stops here, with no real work, if
        # nobody is subscribed. The common case (no tenant with webhooks
        # enabled) costs one indexed query per alarm, never an HTTP call.
        endpoint_rows = await (
            await conn.execute(_MATCHING_ENDPOINTS_SQL, (tenant_id, "device_alarm"))
        ).fetchall()
        if not endpoint_rows:
            return

        # Security review finding: without AND a.tenant_id = %s, a
        # mismatched notification (the 'notifications' channel is shared and
        # not webhook-exclusive) could deliver ANOTHER tenant's alarm payload
        # under the matching endpoint's tenant_id -- a structural
        # cross-tenant leak, even though not reachable over HTTP today (the
        # only real emitter, insert_alarm(), always pairs both values).
        alarm_row = await (await conn.execute(_ALARM_SELECT_SQL, (alarm_id, tenant_id))).fetchone()
        if alarm_row is None:
            return
        device_id, device_label, alarm_type, severity, alarm_time, alarm_details = alarm_row
        event_payload = {
            "event": "device_alarm",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "data": {
                "alarm_id": str(alarm_id),
                "device_id": str(device_id),
                "device_label": device_label,
                "alarm_type": alarm_type,
                "severity": severity,
                "time": alarm_time.isoformat(),
                # Structured event context -- additive field, existing
                # consumers ignore it. ALLOWLIST per alarm type: never a full
                # passthrough of alarms.details, so a future alarm type with
                # internal data cannot leak out without review.
                "details": _webhook_details(alarm_type, alarm_details),
            },
        }
        # The dedupe key includes the event_type prefix
        # ("device_alarm:<alarm_id>"), not just the alarm id -- a future
        # second event type derived from the SAME alarm (e.g.
        # "alarm_acknowledged") would otherwise silently collide on
        # ON CONFLICT DO NOTHING.
        dedupe_key = f"device_alarm:{alarm_id}"
        for (endpoint_id,) in endpoint_rows:
            # ON CONFLICT DO NOTHING on (webhook_endpoint_id, dedupe_key) --
            # EVERY API replica running this process listens on the SAME
            # LISTEN/NOTIFY channel (see 0036); without this, N replicas
            # would enqueue N copies of the same delivery for one alarm.
            await conn.execute(
                """INSERT INTO webhook_deliveries (webhook_endpoint_id, tenant_id, event_type, payload, dedupe_key)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (webhook_endpoint_id, dedupe_key) DO NOTHING""",
                (endpoint_id, tenant_id, "device_alarm", json.dumps(event_payload), dedupe_key),
            )
    state.delivery_wake_event.set()


_CLAIM_DUE_DELIVERIES_SQL = """
    WITH claimed AS (
        SELECT d.id
        FROM webhook_deliveries d
        JOIN webhook_endpoints e ON e.id = d.webhook_endpoint_id
        JOIN tenants t ON t.id = d.tenant_id
        WHERE d.status = 'pending' AND d.next_attempt_at <= now() AND e.enabled
          -- Security review finding: without this JOIN, a suspended/
          -- cancelled tenant (or one whose webhooks_enabled was revoked
          -- AFTER deliveries were queued, up to ~34h of backoff) kept
          -- receiving real outgoing POSTs, bypassing the service cut-off
          -- (deps.py::assert_session_active), which only covers INCOMING
          -- traffic. Rows of an inactive tenant simply stay pending (never
          -- lost) and resume when the tenant is reactivated.
          AND t.status = 'active' AND t.webhooks_enabled
        ORDER BY d.next_attempt_at
        LIMIT %s
        FOR UPDATE OF d SKIP LOCKED
    )
    UPDATE webhook_deliveries d
    SET next_attempt_at = now() + (%s || ' seconds')::interval
    FROM claimed
    WHERE d.id = claimed.id
    RETURNING d.id, d.webhook_endpoint_id, d.event_type, d.payload, d.attempt_count
"""


async def _claim_due_deliveries(pool) -> list[tuple]:
    """FOR UPDATE SKIP LOCKED + a "lease" (next_attempt_at pushed a few
    seconds into the future) -- standard job-queue pattern (like an SQS
    "visibility timeout"): if the process dies mid-delivery, the row becomes
    available again when the lease expires, no cleanup job needed. SKIP
    LOCKED is free and correct once this service runs with more than one
    replica."""
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        rows = await (
            await conn.execute(_CLAIM_DUE_DELIVERIES_SQL, (_WORKER_BATCH_SIZE, _LEASE_SECONDS))
        ).fetchall()
        if not rows:
            return []
        endpoint_ids = list({r[1] for r in rows})
        endpoint_rows = await (
            await conn.execute(
                "SELECT id, url, secret, enabled FROM webhook_endpoints WHERE id = ANY(%s)", (endpoint_ids,)
            )
        ).fetchall()
    endpoints_by_id = {r[0]: {"url": r[1], "secret": r[2], "enabled": r[3]} for r in endpoint_rows}

    claimed = []
    for delivery_id, endpoint_id, event_type, payload, attempt_count in rows:
        endpoint = endpoints_by_id.get(endpoint_id)
        # Recheck: the endpoint may have been disabled between the JOIN above
        # and this point -- cheap defense, not the real guarantee (that is
        # the WHERE e.enabled of the claim query).
        if endpoint is None or not endpoint["enabled"]:
            continue
        claimed.append(
            (delivery_id, endpoint_id, event_type, payload, attempt_count, endpoint["url"], endpoint["secret"])
        )
    return claimed


async def _attempt_delivery(pool, semaphore: asyncio.Semaphore, claimed_row: tuple) -> None:
    delivery_id, endpoint_id, event_type, payload, attempt_count, url, secret = claimed_row
    async with semaphore:
        body = json.dumps(payload).encode("utf-8")
        headers = build_signed_headers(secret, event_type, str(delivery_id), body)
        # deliver_webhook revalidates the URL on EVERY attempt (DNS may change
        # between saving the endpoint and delivery) and connects to the
        # validated IP.
        result = await deliver_webhook(url, body, headers)
        await _record_delivery_result(
            pool,
            delivery_id=delivery_id,
            endpoint_id=endpoint_id,
            attempt_count=attempt_count,
            success=result.success,
            status_code=result.status_code,
            error_message=result.error,
        )


async def _record_delivery_result(
    pool,
    *,
    delivery_id: str,
    endpoint_id: str,
    attempt_count: int,
    success: bool,
    status_code: int | None,
    error_message: str | None,
) -> None:
    new_attempt_count = attempt_count + 1
    async with db_module.tenant_scoped_connection(pool, tenant_id=None, bypass=True) as conn:
        if success:
            await conn.execute(
                """UPDATE webhook_deliveries
                   SET status = 'success', attempt_count = %s, response_status_code = %s,
                       last_error = NULL, delivered_at = now(), next_attempt_at = now()
                   WHERE id = %s""",
                (new_attempt_count, status_code, delivery_id),
            )
            await conn.execute(
                """UPDATE webhook_endpoints
                   SET consecutive_failures = 0, last_attempt_at = now(), last_success_at = now()
                   WHERE id = %s""",
                (endpoint_id,),
            )
            return

        # consecutive_failures increments on EVERY failed attempt (not only
        # when a whole retry chain is exhausted) -- a more responsive circuit
        # breaker. The enforce_webhook_endpoint_failure_threshold trigger
        # (0035) decides the threshold, not this code.
        await conn.execute(
            "UPDATE webhook_endpoints SET consecutive_failures = consecutive_failures + 1, last_attempt_at = now() WHERE id = %s",
            (endpoint_id,),
        )
        if new_attempt_count >= _MAX_ATTEMPTS:
            await conn.execute(
                """UPDATE webhook_deliveries
                   SET status = 'exhausted', attempt_count = %s, response_status_code = %s, last_error = %s,
                       next_attempt_at = now()
                   WHERE id = %s""",
                (new_attempt_count, status_code, error_message, delivery_id),
            )
        else:
            delay = _RETRY_DELAYS_SECONDS[min(attempt_count, len(_RETRY_DELAYS_SECONDS) - 1)]
            await conn.execute(
                """UPDATE webhook_deliveries
                   SET attempt_count = %s, response_status_code = %s, last_error = %s,
                       next_attempt_at = now() + (%s || ' seconds')::interval
                   WHERE id = %s""",
                (new_attempt_count, status_code, error_message, delay, delivery_id),
            )


async def run_webhook_delivery_worker(pool, state: WebhooksState, stop_event: asyncio.Event) -> None:
    """Sleeps until there is work -- woken IMMEDIATELY by
    delivery_wake_event when the dispatcher enqueues something, with a
    fallback poll (_WORKER_POLL_SECONDS) that only matters for retries whose
    next_attempt_at arrives with nothing new enqueued meanwhile. Never makes
    an HTTP call if _claim_due_deliveries returned nothing."""
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DELIVERIES)
    while not stop_event.is_set():
        state.delivery_wake_event.clear()
        try:
            claimed = await _claim_due_deliveries(pool)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("webhooks: failed to query pending deliveries")
            claimed = []

        if claimed:
            await asyncio.gather(*(_attempt_delivery(pool, semaphore, row) for row in claimed))
            continue  # check right away for more work, without waiting for wake/poll

        try:
            await asyncio.wait_for(state.delivery_wake_event.wait(), timeout=_WORKER_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
