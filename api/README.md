# api

FastAPI service with the business API of OpenMDVR: tenants, users and roles,
devices, vehicles and drivers, alarms and GPS positions, live video
authorization, billing, notifications, API keys, outgoing webhooks and
geofences.

Device traffic (JT808/JT1078/GT06) is handled by the Go service in
`jt808-server`; this API is ordinary request/response CRUD, which is why it
is written in Python. Both services share the same PostgreSQL + TimescaleDB
database and the same Row Level Security (RLS) model.

## Running locally

```bash
cp infra/.env.example infra/.env     # fill in the secrets
cd infra
docker compose up -d --build api     # also starts postgres + migrations
```

The API listens on `127.0.0.1:8000`; `GET /health` reports readiness and
`listener_connected` (see "Live positions"). Configuration comes only from
environment variables (`app/config.py`):

| Variable | Purpose |
|---|---|
| `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `APP_USER_PASSWORD` | Connection as `app_user` (the non-owner role RLS applies to) |
| `JWT_SECRET`, `JWT_EXPIRE_MINUTES` (default 480) | Session tokens |
| `API_KEY_PEPPER` | HMAC pepper for API key hashes, deliberately separate from `JWT_SECRET` |
| `JT1078_BRIDGE_BASE_URL` | Internal video bridge in `jt808-server` (Docker network only) |
| `R2_ENDPOINT`, `R2_ACCESS_KEY`, `R2_SECRET_KEY`, `R2_BUCKET` | Optional S3-compatible storage for alarm clips (signed URLs) |

### First super_admin

Users can only be created by an authenticated user, so the first platform
account is created with a script that connects with a bypass session. It is
idempotent:

```bash
docker exec -e APP_USER_PASSWORD=... -e PGHOST=postgres -e PGPORT=5432 \
  openmdvr-api python scripts/bootstrap_admin.py --email admin@example.com
```

## Tests

Tests run against the **real** Postgres from `infra/` (no database mocks:
tenant isolation is enforced by RLS, which can only be tested against
Postgres). `tests/conftest.py` loads `infra/.env` if present and defaults to
`127.0.0.1:55432`.

```bash
cd api
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest tests/ -v
```

Notes:

- On Windows, `conftest.py` sets `WindowsSelectorEventLoopPolicy`; async
  psycopg does not work with the default `ProactorEventLoop`.
- SSE endpoints cannot be tested through `httpx.ASGITransport` (it blocks
  until the response generator finishes). `test_positions_stream.py` and
  `test_notifications_stream.py` use the `stream_client` fixture, a real
  loopback uvicorn server.
- The shared `two_tenants` fixture has no subscription on purpose; tests
  that need device quota set it explicitly (`_set_device_quota`).
- Time-series rows are written through the same `SECURITY DEFINER`
  functions the device servers use (`insert_gps_position`, `insert_alarm`),
  so triggers (geofences, overspeed, notifications) run exactly as in
  production.

Database-level RLS tests live separately in `infra/postgres/tests/`.

## Module map

| Path | Responsibility |
|---|---|
| `app/main.py` | App, lifespan (background listeners/workers), global exception handlers, API key audit middleware |
| `app/config.py` | Settings from environment |
| `app/db.py` | Connection pool, `tenant_scoped_connection` (sets the RLS session GUCs) |
| `app/deps.py` | `get_current_user`, `get_db`, role dependencies, session revocation, API key gates |
| `app/security.py` | bcrypt, JWT encode/decode and claim shape validation, API key hashing |
| `app/api_key_auth.py` | API key lookup and validation |
| `app/rate_limit.py` | In-process fixed-window rate limiter |
| `app/live_positions.py` | LISTEN/NOTIFY listener, per-tenant fan-out, SSE tickets for positions |
| `app/notifications.py` | Same pattern for the in-app notification mailbox |
| `app/webhooks.py` | Webhook dispatcher, delivery worker, SSRF protection, signing |
| `app/payments.py` | `PaymentProvider` abstraction (`ManualPaymentProvider` today) |
| `app/gt06_config_commands.py` | Catalog of GT06 configuration commands (server-side text builder) |
| `app/storage.py` | Storage abstraction (signed URLs) for S3-compatible buckets |
| `app/routers/*` | One router per resource (see table below) |
| `scripts/bootstrap_admin.py` | First platform account |

Routers and their tags (tags matter for API keys, see below):

| Router | Prefix | Tag |
|---|---|---|
| `auth.py` | `/auth/login` | `auth` |
| `tenants.py` | `/tenants` | `tenants` |
| `users.py` | `/users` (status, password reset, device assignments, notification settings, API keys) | `users` |
| `devices.py` | `/devices` (CRUD, models, positions, route history) | `devices` |
| `video.py` | `/devices/{id}/video`, `/snapshot`, `/live-view-balance` | `video` |
| `device_commands.py` | `/devices/{id}/commands` (engine cut/resume) | `device-commands` |
| `device_config_commands.py` | `/devices/{id}/config-commands` | `device-config-commands` |
| `device_groups.py` | `/device-groups` | `device-groups` |
| `vehicles.py`, `drivers.py`, `routes.py`, `shifts.py`, `driver_shift_alerts.py` | fleet, drivers, routes, shifts and policy alerts | same as prefix |
| `positions.py` | `/positions/latest`, `/positions/stream[/ticket]` | `positions` |
| `alarms.py` | `/alarms` (list, acknowledge, alarm video clips) | `alarms` |
| `notifications.py` | `/notifications` (mailbox + SSE) | `notifications` |
| `billing.py` | `/billing/*` | `billing` |
| `platform.py` | `/platform/*` (monitoring/map settings, device health) | `platform` |
| `webhook_endpoints.py` | `/webhook-endpoints` | `webhooks` |
| `geofences.py` | `/geofences` | `geofences` |

List endpoints return `Page[T]` (`items`, `total`, `limit`, `offset`) with
optional `search` (ILIKE) and, for platform sessions, an optional
`tenant_id` filter. Literal sub-paths (`/devices/models`, `/geofences/events`,
`/geofences/report`, `/drivers/shift-status`) are registered **before** the
`/{id}` route of the same router; Starlette matches in registration order.

## Authentication, roles and RLS

**Roles.** `super_admin` and `support` are platform roles (`tenant_id` NULL,
explicit RLS bypass). `tenant_admin`, `tenant_operator`, `tenant_viewer` and
`driver` are scoped to one tenant. A `driver` account is a `users` row linked
to a `drivers` row (`driver_id` claim).

**Session contract.** The API connects as `app_user`. Every authenticated
request opens a transaction and sets `app.tenant_id`, `app.bypass_rls`,
`app.user_id`, `app.driver_id` and `app.api_key_device_filter` with
`set_config(..., true)` and bound parameters (never string-interpolated,
never a non-local `SET`). The JWT is the only source of tenant and role.
See `infra/postgres/migrations/0003_rls_helpers.sql`.

**Let RLS filter.** Routers do not add `WHERE tenant_id = ...` by hand;
duplicating the rule in two places lets them drift. Optional `tenant_id`
query parameters only narrow what RLS already allows and can never widen it.
Rows of another tenant return 404 (never 403), so existence is not
confirmed.

**Device visibility.** `app_can_view_device()` (migration 0032) is the single
predicate behind `devices`, `gps_positions_v`, `alarms_v`, device commands,
clips, video and the position stream: `tenant_admin` and platform see all
devices of the tenant; operators and viewers only see devices assigned to
them directly or through a device group.

**Role dependencies** (`app/deps.py`), allowlist-style where possible:

| Dependency | Allows |
|---|---|
| `require_super_admin` | super_admin only (create tenants, platform accounts, billing catalog, map override, config commands) |
| `require_bypass` | super_admin or support (operational platform work: devices, quotas, payments) |
| `require_tenant_admin_or_super_admin` | issuing new credentials (API keys, webhook secrets); excludes support |
| `require_tenant_admin` | tenant_admin of the tenant, or platform |
| `require_non_driver` | every role except `driver` (fleet data) |
| `require_driver` | driver only (`/shifts/clock`) |

`support` reads across tenants but cannot create tenants or platform
accounts, and cannot issue credentials: RLS defines what is technically
possible, the app narrows it to what the business allows.

**Time-series tables.** Hypertables (`gps_positions`, `alarms`,
`usage_events`) are never granted directly to `app_user`: TimescaleDB
propagates grants to chunks but not `FORCE ROW LEVEL SECURITY`, so a direct
grant allows bypassing RLS by chunk name. Reads go through
`security_barrier` views, writes through `SECURITY DEFINER` functions
(`0009_timeseries_access.sql`).

## Security notes

Findings from internal security reviews and how each is enforced now:

- **Login enumeration and timing.** Unknown email and wrong password return
  the same body, and the unknown-email path verifies against
  `DUMMY_PASSWORD_HASH` so both paths cost the same. Login is refused when
  the tenant is not `active`. Passwords over 72 UTF-8 bytes are a clean 422.
- **bcrypt off the event loop.** `verify_password` runs through
  `run_in_threadpool`; a burst of logins no longer stalls the whole API.
- **Session revocation.** A signature check alone kept disabled users and
  cancelled tenants working until the JWT expired (up to 8h).
  `assert_session_active` re-checks `users.status`, `tenants.status` and API
  key revocation on every request (inside `get_db`), in
  `POST /positions/stream/ticket`, and periodically inside open SSE streams.
  401 means the account is no longer valid; 402 means the tenant has no
  active service. Cost: one indexed query per authenticated request.
- **JWT claim shape.** `decode_access_token` validates `role` against the
  known roles and `tenant_id` as a UUID, so a malformed but validly signed
  token is a 401, not a 500 from an RLS cast.
- **Drivers do not inherit fleet access.** Adding the `driver` role once
  gave drivers viewer-level access to devices, positions, alarms and video;
  `require_non_driver` now guards every fleet endpoint, and driver-scoped
  tables (`driver_shift_events`, `routes`, `driver_shift_alerts`) carry a
  per-driver RLS dimension as well.
- **Error hygiene.** Global handlers turn `psycopg.errors.DataError` (e.g. a
  NUL byte in free text) into a 422, sanitize non-finite floats before
  echoing validation errors, and strip non-serializable context from
  Pydantic errors. `InsufficientPrivilege`/`CheckViolation`/`ForeignKey`
  violations are mapped to 403/422 instead of raw 500s. Bridge and network
  error text is never forwarded to clients; it is logged server-side.
- **Bounds that match the schema.** Numeric fields carry Pydantic bounds
  calibrated to their actual `NUMERIC(p,s)` columns; overflowing products
  inside jobs are isolated per tenant (see Billing).

## Live positions (SSE)

The map receives GPS positions pushed in real time:

1. `insert_gps_position()` calls `pg_notify('gps_positions', ...)`
   (`0018_gps_position_notify.sql`); it is delivered only on commit. Device
   ignition/power changes are pushed on the separate `device_status` channel
   by a trigger and carried over the same SSE stream (`type: "device_status"`).
2. `run_listener()` keeps **one** `LISTEN` connection per API process,
   outside the request pool, and reconnects with exponential backoff
   (1s to 30s). A malformed notification is dropped without taking the
   listener down.
3. `LISTEN/NOTIFY` has no permission model, so isolation is application
   code: `PositionBroadcaster` keeps per-tenant subscriber queues (plus one
   group for bypass sessions) and filters by device visibility for
   operators/viewers.
4. `EventSource` cannot send an `Authorization` header, so the client first
   calls `POST /positions/stream/ticket` and opens
   `GET /positions/stream?ticket=...`. Tickets are opaque, single-use and
   expire after 30s; the JWT never travels in a URL. The stream re-checks the
   ticket's role and the session status on its own.
5. `GET /positions/latest` seeds the map and is used for periodic
   reconciliation (one `LATERAL ... LIMIT 1` per visible device over the
   `(device_id, time DESC)` index).

The notification mailbox (`/notifications/stream`) uses the same design on
the `notifications` channel, isolated by `recipient_user_id`. Each client
only receives its own row, never the full recipient list.

## Video

`POST /devices/{id}/video` is the trust boundary for video. It resolves the
device through the RLS-scoped connection, then calls the internal bridge
(`JT1078_BRIDGE_BASE_URL`, no auth of its own, never exposed publicly):
`/api/v1/9101` for JT808 cameras or `/api/v1/gt06-video` for GT06 dashcams,
followed by `/api/v1/video-tickets`. The playback URL carries a single-use
ticket bound to tenant, device, app and channel that ZLMediaKit validates in
its `on_play` hook, so a guessed stream URL yields 401 without waking the
camera. The URL is never returned without a ticket.

- Per-session time (`tenants.max_live_view_seconds`) and the monthly quota
  (`live_view_monthly_quota_seconds`) are enforced by the bridge; quota
  exhaustion is surfaced as 402. `GET /devices/{id}/live-view-balance`
  returns the real remaining balance from the bridge's central meter.
- `POST /devices/{id}/snapshot` returns a preview JPEG: shared cache first,
  then a native photo where the protocol supports it, otherwise a short
  video start plus a single frame capture. Rate limited per device.
- `usage_events` records bytes actually served to players (via
  ZLMediaKit `on_flow_report` in the bridge), not estimates.

## Device commands

- `POST /devices/{id}/commands` (`engine_stop`/`engine_resume`, GT06 only,
  `require_tenant_admin`). The audit row (`device_commands`) is written in
  its own short transaction before the call and updated afterwards, so a
  later failure cannot erase the record. The status reflects the device's
  actual reply text, not just transport success; the reply is sanitized
  (NUL/control bytes) before storage.
- `POST /devices/{id}/config-commands`: the command text is always built
  server-side from a `command_key` plus validated parameters
  (`app/gt06_config_commands.py`); clients never send raw text. Sending is
  super_admin only; viewing history is platform (`require_bypass`).
  Commands that can disconnect a device (`server`, `apn`, `rservice`,
  `update_firmware`) are flagged for extra UI friction, and firmware URLs
  are restricted to the vendor's OTA domain.
- Alarm clips (`POST /alarms/{id}/request-clip`, `GET /alarms/{id}/clip`)
  are idempotent per alarm, limited to alarm types the device can record,
  marked failed when stale, and gated by device visibility.

## Billing

All billing tables have RLS; platform-only data never reaches a tenant.

- **Catalog** (`billing_plans`, global, bypass-only SELECT). Creating or
  editing plans is `require_super_admin`; subscription lines
  (`tenant_subscription_items`, with optional `unit_price_override` and a
  `category`) are `require_bypass`. Ending a line sets `ended_at`, never
  deletes.
- **Device quota.** `POST /devices` enforces quota per category: the sum of
  `quantity` of active lines (`camera` for `jt808`/`gt06_video`, `gps` for
  `gt06`) against active devices of that category. No active lines means
  zero quota. `TenantOut` exposes `camera_device_quota`/`gps_device_quota`.
- **Promotions and invoices.** `generate_invoices()` (`SECURITY DEFINER`,
  daily TimescaleDB job) snapshots prices into `invoice_line_items`. Each
  tenant is processed inside its own exception block, so one tenant with
  out-of-range data cannot roll back or block the run for everyone else.
  `GET /billing/invoices` is readable by the tenant's own `tenant_admin`.
- **Payments and suspension.** Payments go through
  `PaymentProvider.record_payment()` (the extension point for a payment
  gateway). It locks the invoice (`FOR UPDATE`), supports partial payments,
  marks it paid and reactivates a suspended tenant immediately.
  `enforce_billing_suspension()` is the daily safety net: marks overdue
  invoices, suspends after a 5-day grace period and reactivates when
  nothing is overdue. A non-active tenant gets 402 on every request.
- **Profitability** (`GET /billing/profitability`, platform only) estimates
  cost from active devices and real bytes, normalizes revenue to a monthly
  equivalent by billing period, and sorts by worst margin.
  `GET /billing/sim-usage` reports per-SIM data usage measured by the device
  servers. `GET /billing/my-subscription` is the narrow tenant view; it
  resolves plan names through `resolve_billing_plan_public()` only for
  lines RLS already returned to that tenant.

## API keys

API keys (`0034_api_keys.sql`) authenticate **as an existing user** with the
same role, tenant and device assignments, plus two immutable restrictions:

1. `can_write`: without it, only `GET`/`HEAD`/`OPTIONS` are allowed.
2. `allowed_device_ids`: further narrows device visibility (`NULL` means
   unscoped, `[]` means no devices). Implemented as the
   `app.api_key_device_filter` GUC read inside `app_can_view_device()`, so
   every device-scoped surface inherits it.

Enforcement lives in the single choke point `deps.py::get_current_user`.
A route is reachable by an API key only if **all** its tags are in
`_API_KEY_ALLOWED_TAGS` (devices, positions, vehicles, drivers, routes,
notifications, alarms, shifts, driver-shift-alerts, geofences). Auth,
tenants, users, billing, platform, device commands, video, device groups
and webhooks are always excluded, as are SSE stream endpoints. New routers
are excluded by default.

- Management: `POST|GET /users/{id}/api-keys`,
  `.../{key_id}/revoke`, `.../{key_id}/usage`. Creating requires
  `tenant_admin` or `super_admin` (never `support`); revoking/listing allows
  platform support. RLS on `api_keys` also requires `app_is_tenant_admin()`.
- The raw key is returned once; only an HMAC-SHA256 hash with
  `API_KEY_PEPPER` is stored. A trigger prevents un-revoking a key, and
  revocation keeps working even after scoped devices are deleted.
- Every API key request is audited in `api_key_usage_log` (including
  rejections and 429s) by a middleware that writes after the response is
  sent. The log has 90-day retention.
- Rate limits: 120 requests/minute per key
  (`API_KEY_RATE_LIMIT_PER_MINUTE`) and a separate per-IP limit for unknown
  keys (`API_KEY_FAIL_RATE_LIMIT_PER_MINUTE`, default 200). Limits are per
  process.
- Known limits: `allowed_device_ids` scopes telemetry only (vehicles,
  drivers, routes and geofence configuration are tenant-wide), and the
  audited IP is the proxy's unless uvicorn runs with `--proxy-headers`.

## Webhooks

Outgoing webhooks (`0035`-`0037`) push events (today `device_alarm`) to
HTTP endpoints registered by a `tenant_admin`, once a **super_admin** has
set `tenants.webhooks_enabled`.

- **Dispatcher** (`run_webhook_dispatch_listener`): listens on the shared
  `notifications` channel. For each alarm it runs one indexed query ("is the
  feature enabled and is any endpoint subscribed?") and stops there in the
  common case. Deliveries are enqueued with
  `ON CONFLICT (webhook_endpoint_id, dedupe_key) DO NOTHING`
  (`dedupe_key = "device_alarm:<alarm_id>"`), so several API processes
  listening to the same channel never duplicate deliveries. The alarm lookup
  matches both id and tenant. `insert_alarm()` always emits the notify,
  independently of whether any mailbox recipient exists.
- **Worker** (`run_webhook_delivery_worker`): claims due deliveries with
  `FOR UPDATE SKIP LOCKED` and a 60s lease, only for active tenants with the
  feature still enabled; retries at 60s, 5m, 30m, 2h, 6h and 24h. A trigger
  disables an endpoint after 10 consecutive failures; worker-owned columns
  cannot be forged through SQL.
- **Signing:** HMAC-SHA256 over `"timestamp.body"`. The payload includes an
  allowlist of alarm detail keys per alarm type, not raw `details`.
- **SSRF:** URLs are validated at create/update time and at delivery.
  `deliver_webhook()` resolves DNS once, rejects private, loopback,
  link-local, reserved and multicast addresses, and connects to the pinned
  IP with the original Host/SNI (no DNS rebinding). Redirects are not
  followed and produce an actionable error.
- **Limits and permissions:** 10 endpoints per tenant; creating an endpoint
  or rotating a secret excludes `support`; a foreign `tenant_id` is
  rejected before touching the database (no oracle).
  `POST /webhook-endpoints/{id}/test` sends a signed ping synchronously
  (rate limited) without enqueuing it.

## Geofences

Circle or polygon zones (`0052_geofences.sql`, `routers/geofences.py`) that
produce **enter**, **exit** and **dwell** events for any device reporting
GPS. Evaluation runs inside `insert_gps_position()`, the single entry point
used by every protocol server, so it is protocol-agnostic. There is no
PostGIS dependency: haversine for circles, ray casting for polygons.

| Method | Path | Permission |
|---|---|---|
| GET | `/geofences` | `require_non_driver` |
| POST/PATCH/DELETE | `/geofences[/{id}]` | `require_tenant_admin` (+ `app_is_tenant_admin()` in RLS) |
| GET | `/geofences/{id}` , `/{id}/occupancy` | `require_non_driver` |
| GET | `/geofences/events` (window up to 93 days), `/geofences/report` | `require_non_driver` |

- Only transitions create events (`geofence_device_state`), with exit
  hysteresis (`hysteresis_m`, default 20 m), a 60s debounce per
  (geofence, unit) and protection against out-of-order positions. Positions
  more than 5 minutes in the future are ignored.
- Creating, redrawing or enabling a geofence seeds state silently from the
  last known positions (marked `entry_estimated`), so a new geofence over a
  yard does not fire one "entered" per parked unit.
- Every change goes to `geofence_events` (report source, keeps a snapshot of
  the name). Notifying is optional per geofence and goes through
  `insert_alarm()` (`geofence_enter|exit|dwell`), capped at 20
  notifications per position.
- `app_user` has no write grant on events/state, and bounding-box/vertex
  columns are computed by triggers only.
- Database-enforced budgets per tenant: 1000 geofences (500 enabled), 500
  vertices per polygon and 25000 enabled polygon vertices in total. Vertices
  are evaluated as native arrays copied to local plpgsql variables; reading
  them from `jsonb` by index inside the loop was orders of magnitude slower.

## Other modules

- **Vehicles and drivers** are tenant_admin self-service entities;
  `driver_vehicle_assignments` keeps a time-ranged history (at most one
  active driver per vehicle, enforced by a partial unique index). Plates and
  license numbers are unique per tenant; names are not.
- **Reports:** `GET /vehicles/{id}/distance` (haversine between consecutive
  points), `GET /vehicles/{id}/engine-hours` (driving / idle below 5 km/h /
  engine off, from ignition alarms and GPS speed) and
  `GET /drivers/{id}/hours` (paired clock-in/out minus meal breaks). Windows
  are capped at 31 days. These are data for reports, not certified
  regulatory formats.
- **Tenant self-service:** `PATCH /tenants/{id}/settings` (branding and
  driver policy) is separate from the bypass-only `PATCH /tenants/{id}`
  (quotas, retention, billing period, webhook approval). Policy violations
  generate informational `driver_shift_alerts` inside a savepoint; they never
  block the real shift event.
- **Retention:** `gps_positions` is pruned per tenant
  (`tenants.gps_retention_days`, default 90) by
  `enforce_gps_position_retention()`; `alarms` uses a fixed 365-day native
  policy; `usage_events` (billing ledger) is never deleted. Native
  compression is not used because TimescaleDB refuses it on tables with RLS.
- **Platform settings:** `/platform/monitoring-settings` (online threshold,
  readable by any session), `/platform/map-settings` (tile provider override,
  super_admin to edit) and `/platform/device-health` (deduplicated
  operational device problems, platform only).
- **Overspeed:** `vehicles.max_speed_kmh` is checked inside
  `insert_gps_position()`; one `overspeed_limit` alarm per rising edge.
