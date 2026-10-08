# Postgres + RLS — local development

## Start the database

```bash
cd infra
cp .env.example .env   # fill in POSTGRES_SUPERUSER_PASSWORD and APP_USER_PASSWORD
docker compose up -d
```

Migrations in `postgres/migrations/` (`*.sql` plus `0010_set_role_passwords.sh`)
are applied by the `migrate` service (`apply_migrations.sh`) on every
`docker compose up`. Each file is applied exactly once and recorded in the
`schema_migrations` table. To rebuild the database from scratch:

```bash
docker compose down -v && docker compose up -d
```

The container listens on `127.0.0.1:55432` (not 5432, to avoid clashing with a
native Postgres on the host). Health check: `docker compose ps`.

## Run the RLS test suite

```bash
cd infra/postgres/tests
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # .venv/bin/python on Linux/macOS
.venv/Scripts/python -m pytest -v
```

The tests read `infra/.env` automatically (nothing to export by hand). They
cover:

- Basic per-tenant isolation on the core tables (tenants, users, devices,
  gps_positions, alarms, usage_events).
- IDOR: requesting another tenant's resource by id always returns zero rows,
  never an error.
- Cross-tenant INSERT/UPDATE/DELETE blocked (RLS `WITH CHECK` / `USING`).
- Session hygiene: tenant context set with `set_config(..., true)` must not
  survive a `COMMIT` on a reused connection (pool safety).
- A SQL injection payload as the value of `app.tenant_id` fails the cast to
  `uuid` and never alters the query.
- `app_user` cannot read/write the physical hypertable chunks directly
  (`_timescaledb_internal.*`), not even with a bypass session (see below).
- device↔tenant and user↔tenant consistency triggers, which protect even a
  `bypass_rls=true` session against bugs that mix ids from two tenants.
- `ON DELETE RESTRICT`: a device or user cannot be deleted while it has
  history in gps_positions/alarms/usage_events (referential actions ignore
  RLS, so otherwise a normal tenant could alter or destroy those protected
  tables as a side effect of deleting its own resources).
- Append-only tables (`usage_events`, `gps_positions`): not even bypass can
  update/delete the usage ledger directly; the only way to delete
  `gps_positions`/`alarms` rows is through bypass-only functions.
- `acknowledge_alarm()` can only touch `acknowledged_at`/`acknowledged_by`,
  never `video_evidence_key`/`alarm_type`/`severity`/`"time"`.
- Integrity invariants (`users` CHECKs, per-tenant vs. global email
  uniqueness, global uniqueness of `jt808_terminal_id`, `app_user` role
  attributes, `FORCE ROW LEVEL SECURITY` on every business table).

## Why gps_positions/alarms/usage_events are accessed through views and functions

They are TimescaleDB hypertables. TimescaleDB propagates a hypertable's
`GRANT`s to every physical chunk (including future ones) but does **not**
propagate `FORCE ROW LEVEL SECURITY` (unsupported on chunks). A role with a
direct `GRANT` on the hypertable can therefore read/write its chunks by name
(`_timescaledb_internal."_hyper_1_1_chunk"`) without any RLS policy ever being
evaluated, which lets any tenant read, alter, and delete other tenants' data.
The only durable mitigation is to never grant `app_user` a direct table
privilege on these tables. Instead, `app_user` reads through `security_barrier`
views (`gps_positions_v`, `alarms_v`, `usage_events_v`, owned by the migration
role) and writes through `SECURITY DEFINER` functions (`insert_gps_position`,
`insert_alarm`, `insert_usage_event`, `acknowledge_alarm`,
`delete_gps_positions_before`, `delete_alarm`), all defined in
`migrations/0009_timeseries_access.sql`. See that file's header for details.

## Session contract the API must honor

See the header of `migrations/0003_rls_helpers.sql`. In short: at the start of
every transaction, before any business query, the API runs
`SELECT set_config('app.tenant_id', $1, true)` and
`SELECT set_config('app.bypass_rls', $2, true)`, both with the third argument
`true` (transaction scope, not session/connection scope) and with the value
ALWAYS bound as a parameter, never interpolated into the SQL text.
