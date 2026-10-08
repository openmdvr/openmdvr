-- Row Level Security policies. One policy per command (SELECT/INSERT/UPDATE/
-- DELETE) rather than a single FOR ALL: a FOR ALL policy with one USING shares
-- that condition with DELETE, which in Postgres has NO WITH CHECK clause.
-- Splitting them prevents, e.g., a table meant to be "read own row only" for a
-- tenant (like tenants) from allowing DELETE just because the row was visible.
--
-- Every table uses FORCE ROW LEVEL SECURITY in addition to ENABLE: FORCE makes
-- the policy apply even to the table owner, in case a migration or a more
-- privileged role ever ends up owning these tables.
--
-- Scope note: RLS here guarantees isolation by tenant_id. It does NOT replace
-- role-based authorization within a tenant (e.g. only tenant_admin may create
-- users); the API enforces that in the business layer using the JWT `role`
-- claim, and tests it there.
--
-- gps_positions, alarms and usage_events are TimescaleDB hypertables: they keep
-- ENABLE+FORCE+policies here for hygiene and defense in depth, but deliberately
-- get NO direct GRANT to app_user (see the comment in 0007_timeseries_tables.sql
-- and the real access layer in 0009_timeseries_access.sql). A direct GRANT on a
-- hypertable propagates to its physical chunks and allows reading/writing them
-- by name (_timescaledb_internal.*) without going through RLS, because FORCE ROW
-- LEVEL SECURITY is not supported on chunks.

GRANT USAGE ON SCHEMA public TO app_user;

-- ---------------------------------------------------------------------------
-- tenants: a visible row is "my own tenant" (or everything, under bypass). No
-- tenant-scoped role can insert/update/delete tenants; bypass only.
-- ---------------------------------------------------------------------------
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;

CREATE POLICY tenants_select ON tenants
    FOR SELECT
    USING (app_bypass_rls() OR id = app_current_tenant_id());

CREATE POLICY tenants_insert ON tenants
    FOR INSERT
    WITH CHECK (app_bypass_rls());

CREATE POLICY tenants_update ON tenants
    FOR UPDATE
    USING (app_bypass_rls())
    WITH CHECK (app_bypass_rls());

CREATE POLICY tenants_delete ON tenants
    FOR DELETE
    USING (app_bypass_rls());

GRANT SELECT, INSERT, UPDATE, DELETE ON tenants TO app_user;

-- ---------------------------------------------------------------------------
-- users: full management within the own tenant (tenant_admin manages its
-- users), or bypass for the platform. WITH CHECK on UPDATE prevents a row from
-- being "reassigned" to another tenant_id via UPDATE.
-- ---------------------------------------------------------------------------
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;

CREATE POLICY users_select ON users
    FOR SELECT
    USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());

CREATE POLICY users_insert ON users
    FOR INSERT
    WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());

CREATE POLICY users_update ON users
    FOR UPDATE
    USING (app_bypass_rls() OR tenant_id = app_current_tenant_id())
    WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());

CREATE POLICY users_delete ON users
    FOR DELETE
    USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON users TO app_user;

-- ---------------------------------------------------------------------------
-- devices: read/update/delete within the own tenant (tenant_admin manages its
-- own devices: label, status, etc.). CREATION (INSERT) is bypass ONLY: adding a
-- device means registering a real jt808_terminal_id (SIM number) of hardware
-- installed by the platform, a provisioning action, not tenant self-service.
-- Also, jt808_terminal_id is GLOBALLY unique (needed for JT808 routing); if a
-- tenant could insert freely, it could use the uniqueness error as an oracle to
-- confirm which SIMs another tenant has, or pre-emptively "squat" a terminal_id
-- another tenant has not provisioned yet (blocking legitimate onboarding).
-- Restricting INSERT to bypass closes both vectors.
-- ---------------------------------------------------------------------------
ALTER TABLE devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE devices FORCE ROW LEVEL SECURITY;

CREATE POLICY devices_select ON devices
    FOR SELECT
    USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());

CREATE POLICY devices_insert ON devices
    FOR INSERT
    WITH CHECK (app_bypass_rls());

CREATE POLICY devices_update ON devices
    FOR UPDATE
    USING (app_bypass_rls() OR tenant_id = app_current_tenant_id())
    WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());

CREATE POLICY devices_delete ON devices
    FOR DELETE
    USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON devices TO app_user;

-- ---------------------------------------------------------------------------
-- gps_positions: immutable telemetry from a tenant's perspective. Readable and
-- insertable within the own tenant; only bypass may delete (retention cleanup),
-- and NOBODY may UPDATE (no such privilege is granted and no policy exists).
--
-- NO GRANT to app_user here; see the note at the top. The application's real
-- access is via gps_positions_v / insert_gps_position() /
-- delete_gps_positions_before() in 0009_timeseries_access.sql.
-- ---------------------------------------------------------------------------
ALTER TABLE gps_positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE gps_positions FORCE ROW LEVEL SECURITY;

CREATE POLICY gps_positions_select ON gps_positions
    FOR SELECT
    USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());

CREATE POLICY gps_positions_insert ON gps_positions
    FOR INSERT
    WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());

CREATE POLICY gps_positions_delete ON gps_positions
    FOR DELETE
    USING (app_bypass_rls());

-- ---------------------------------------------------------------------------
-- alarms: read/insert within the own tenant (or bypass); "acknowledge alarm"
-- only touches acknowledged_at/acknowledged_by, never the whole row; DELETE
-- bypass-only (a tenant may not delete its own history).
--
-- NO GRANT to app_user here. Real access is via alarms_v / insert_alarm() /
-- acknowledge_alarm() / delete_alarm() in 0009_timeseries_access.sql. There is
-- deliberately NO whole-table UPDATE grant, not even within the own tenant: a
-- whole-row UPDATE would let a tenant rewrite video_evidence_key, alarm_type,
-- severity or "time" of its own alarm (evidence tampering), something an RLS
-- policy based only on tenant_id cannot prevent per column.
-- ---------------------------------------------------------------------------
ALTER TABLE alarms ENABLE ROW LEVEL SECURITY;
ALTER TABLE alarms FORCE ROW LEVEL SECURITY;

CREATE POLICY alarms_select ON alarms
    FOR SELECT
    USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());

CREATE POLICY alarms_insert ON alarms
    FOR INSERT
    WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());

CREATE POLICY alarms_update ON alarms
    FOR UPDATE
    USING (app_bypass_rls() OR tenant_id = app_current_tenant_id())
    WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());

CREATE POLICY alarms_delete ON alarms
    FOR DELETE
    USING (app_bypass_rls());

-- ---------------------------------------------------------------------------
-- usage_events: insert-only ledger. Not even bypass has UPDATE/DELETE via
-- app_user; it is the source of truth for billing and must not be alterable
-- from the normal application flow under any session.
--
-- NO GRANT to app_user here. Real access is via usage_events_v /
-- insert_usage_event() in 0009_timeseries_access.sql.
-- ---------------------------------------------------------------------------
ALTER TABLE usage_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_events FORCE ROW LEVEL SECURITY;

CREATE POLICY usage_events_select ON usage_events
    FOR SELECT
    USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());

CREATE POLICY usage_events_insert ON usage_events
    FOR INSERT
    WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant_id());
