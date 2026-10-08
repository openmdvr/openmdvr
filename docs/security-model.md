# Security model

OpenMDVR moves three kinds of sensitive data: **where vehicles are**
(location history), **what is happening inside and around them** (live
video, audio, event clips) and **control over the vehicle** (remote engine
cut-off). This document describes how the platform protects them, what has
been tested, and the known gaps. Report vulnerabilities via
[SECURITY.md](../SECURITY.md).

## Threat model (summary)

| Actor | Goal | Primary controls |
|---|---|---|
| Tenant A user | Read or act on tenant B's vehicles | PostgreSQL Row Level Security (RLS), per-request session GUCs, tests with ≥2 tenants |
| Low-privilege role (viewer, driver, scoped API key) | Escalate inside a tenant | Role allowlists in the API **and** role/device dimensions inside RLS |
| Internet attacker | Take down device ingestion, inject data, steal video | Bounded parsers, connection limits, per-connection panic recovery, one-time video tickets, ZLMediaKit hook authorization |
| Spoofed / buggy device | Corrupt shared data (time series, "last position") | Timestamp sanity windows, GPS quality filter, fix-bit enforcement |
| Stolen credential | Long-lived access | Session revalidation on every request, API key revocation, HMAC-hashed keys |
| Operator mistake | Cut fuel to the wrong vehicle | Typed confirmation, `tenant_admin`-only, full audit trail with the device's real reply |

## 1. Tenant isolation lives in the database

- Shared schema, every business table has `tenant_id`, and **RLS is
  `FORCE`d** on all of them. The API never relies on `WHERE tenant_id = ...`
  alone: the database refuses rows from other tenants even if a query is wrong.
- The API sets `app.tenant_id`, `app.user_id`, `app.role`, `app.driver_id`
  and `app.api_key_device_filter` with parameterized `set_config()` per
  transaction. User input is never concatenated into SQL.
- Platform roles (`super_admin`, `support`) bypass RLS only through an
  **explicit** flag, never implicitly.
- Device visibility inside a tenant (user/group assignments, scoped API keys)
  is enforced by one SQL predicate, `app_can_view_device()`, reused by the
  views and policies of devices, positions, alarms, commands, video and
  geofences — so a new resource inherits it instead of re-implementing it.
- **TimescaleDB caveat**: hypertables propagate `GRANT`s to their chunks but
  not `FORCE ROW LEVEL SECURITY`, so a direct grant would let a role read
  chunks by name and bypass RLS. Time-series tables are therefore read
  through `security_barrier` views and written through `SECURITY DEFINER`
  functions (`insert_gps_position()`, `insert_alarm()`); the application
  role has no direct grant on any hypertable. For the same reason native
  TimescaleDB compression (incompatible with RLS) is deliberately disabled.
- Isolation tests (`infra/postgres/tests`) run against a real PostgreSQL,
  never mocks, with at least two tenants that must not see each other.

### Fan-out paths outside RLS
`LISTEN/NOTIFY` has no permission model. Live positions (SSE), in-app
notifications and webhooks therefore filter by tenant/device **at the
source** in a single per-process listener (never "fetch everything and
filter in the browser"), and the SSE handshake uses a one-time ticket so
the JWT never appears in a URL. Sessions are closed explicitly on logout.

## 2. Authentication and sessions

- JWT (HS256) with `tenant_id` + `role`; algorithm pinned, claims validated
  (unknown role or malformed UUID → 401, never 500).
- **Revocation is real**: every authenticated request re-checks that the
  user and the tenant are still active (401 for a disabled account, 402 for
  a suspended/cancelled tenant), including long-lived SSE streams.
- Login uses constant-time behavior for unknown emails (dummy bcrypt hash),
  rejects passwords > 72 bytes, runs bcrypt off the event loop, and returns
  one generic message for every failure.
- **API keys** authenticate *as* an existing user and can only narrow it:
  read-only flag and an optional device allowlist (enforced in RLS). Keys are
  stored as HMAC-SHA256 with a dedicated pepper, shown once, rate-limited,
  audited (including rejected attempts) and immutable (rotate = revoke +
  issue). Sensitive routers (auth, billing, users, platform, engine commands,
  live video, SSE) are never reachable with an API key.

## 3. Device ingestion (internet-facing TCP)

JT808, JT1078, GT06 and RTMP listeners accept traffic from thousands of
untrusted field devices. Every listener follows the same checklist:

- `recover()` per connection — one malformed packet can never crash the process;
- buffer limits checked on the **remainder** after extracting complete frames;
- maximum concurrent connections and idle timeouts;
- never leave a client without an answer on internal errors (prevents retry storms);
- devices must be provisioned; unknown identifiers are dropped;
- device-reported timestamps are bounded (a fake year 2255 once created
  dozens of hypertable chunks and a "future last position" — now replaced by
  server receive time); positions without a GPS fix are not stored;
- a GPS quality filter suppresses parked drift without discarding real data
  (originals are kept in `gps_positions.raw`).

## 4. Video and evidence

- ZLMediaKit open-source edition has no JT1078 support; the Go bridge
  translates JT1078 → RTP. Playback requires a **one-time ticket bound to
  tenant + device + channel + app**, validated in the `on_play` hook before a
  single byte is served. `on_publish` authorizes every RTMP push (only
  provisioned camera devices may publish). HLS and directory listing are
  disabled so no segment can be fetched without a ticket.
- Every byte served to a client is metered in `usage_events` from
  ZLMediaKit's real flow reports, and live view has per-session and monthly
  quotas enforced server-side.
- Event clips uploaded by devices are accepted only while that device has an
  authenticated protocol session, are correlated to the exact requested
  file, and clips whose embedded timestamp does not match the alarm are
  quarantined instead of being attached as evidence.

## 5. Remote commands

Engine stop/resume is the most dangerous action in the system:
`tenant_admin` (or platform) only, typed confirmation in the UI, a durable
audit row written before the command is sent, the device's literal reply
stored, success only when the reply confirms it, and strict
command↔response correlation so a late reply can never confirm a different
command. Device configuration commands are `super_admin` only and are built
server-side from validated parameters (no free text reaches the socket).

## 6. Webhooks (outbound)

Enabled per tenant by a platform admin. Payloads are signed (HMAC-SHA256
with timestamp), destinations are validated against private/reserved
networks at creation **and** delivery, DNS is resolved once and the
connection is pinned to the validated IP (prevents DNS rebinding),
redirects are not followed, and a circuit breaker disables failing endpoints.

## Process

Changes to auth, RLS, ingestion or remote commands receive an adversarial
review (static review plus live attacks against a running stack) before
merge. Past reviews found and fixed, among others: a driver role inheriting
fleet-wide read access, missing session revocation, a webhook SSRF check
performed only at delivery time (not at creation), an
unauthenticated clip-upload overwrite path, and a JT808 video regression
caused by a new publish hook. Each fix ships with a regression test.

## Known limitations

- No login rate limiting yet (bcrypt cost is the only brake).
- GT06 has no cryptographic device authentication (protocol limitation): a
  party that knows an IMEI can impersonate that tracker. Mitigations are
  provisioning, timestamp/quality checks and anomaly visibility, not crypto.
- Video tickets travel in the query string; keep ZLMediaKit API debug
  logging off in production.
- The RTMP port relies on ZLMediaKit, which has no native per-IP connection
  cap; use a firewall or edge proxy.
- No third-party penetration test has been performed yet.
