-- Geofences with enter/exit/dwell events: map boundaries that raise events
-- according to their configuration, safe for multi-tenant/multi-user use,
-- reportable and agnostic to the device protocol or model.
--
-- DESIGN:
--
-- 1. PROTOCOL AGNOSTIC: evaluation lives inside insert_gps_position(), the
--    single entry point already used by jt808server and gt06server (and
--    that already hosts the max-speed check, 0050). Any future protocol
--    writing positions through it inherits geofences for free.
--
-- 2. NO PostGIS (not installed in the timescale/timescaledb image): circle =
--    haversine; polygon = ray casting in plpgsql. Metric precision for
--    operational-size geofences (warehouses, yards, customers, cities).
--    Known limitation: polygons crossing the antimeridian (+-180) are not
--    supported.
--
-- 3. EFFICIENCY (evaluation runs on EVERY GPS position of the whole
--    platform, the hottest path of the system):
--      - bounding box precomputed by trigger (never by the client) + a
--        per-tenant index: a geofence whose bbox does not contain the point
--        is never evaluated geometrically, unless the device is currently
--        INSIDE according to its state (needed to detect the exit).
--      - a tenant with no enabled geofences pays ONE empty indexed query per
--        position, nothing more.
--      - geometry is evaluated over native lat/lon arrays, never over the
--        jsonb (see geo_point_in_polygon: ~1000x measured difference).
--      - limits enforced in the database (trigger + CHECK, not only the
--        API): 500 enabled geofences, 500 vertices per polygon and 25000
--        total enabled polygon vertices per tenant, so no tenant can make
--        position INSERTs expensive for the whole platform.
--
-- 4. TRANSITIONS ONLY (edge-triggered), like ignition/max speed:
--    geofence_device_state records whether each device is inside each
--    geofence; an event is only created when the state CHANGES, never once
--    per ping while the unit is parked inside. Also:
--      - hysteresis (hysteresis_m, default 20 m): an exit only counts if the
--        point is outside the boundary by more than that margin, so GPS
--        jitter of a unit parked NEXT to the edge does not cause an
--        enter/exit storm.
--      - out-of-order positions (device buffer after signal loss) never move
--        the state backwards (last_evaluated_at).
--      - creating/redrawing a geofence SILENTLY seeds the state from each
--        unit's last known position, so a geofence drawn over a yard with
--        100 parked units does not fire 100 "entered" notifications.
--
-- 5. REPORTING AND NOTIFICATION ARE SEPARATE: EVERY state change is stored
--    in geofence_events (the reporting source, with a snapshot of the
--    geofence name that survives deletion). Only when the geofence asks for
--    it (notify_on_enter/exit/dwell) is insert_alarm() also called, the same
--    function that feeds the in-app inbox (respecting who sees which
--    device, app_device_recipients) and outbound webhooks.
--
-- 6. MULTI-TENANT SECURITY: FORCE RLS on all 4 tables; writing geofences
--    requires tenant_admin (or platform) IN RLS too, not only in the API
--    (same app_is_tenant_admin() pattern as device_groups, 0031); events
--    and state are readable only if the session can VIEW the device
--    (app_can_view_device, 0032); state and events have NO write GRANT for
--    app_user, only the SECURITY DEFINER functions below write them, so no
--    user can fabricate a geofence event for a report.

-- ---------------------------------------------------------------------------
-- Performance index (not geofence-specific): gps_positions only had
-- (tenant_id, time DESC), so EVERY per-unit query (route history, distance,
-- engine hours, last-hour trail, map's latest position, and the geofence
-- state seeding below) scanned the tenant's ENTIRE fleet within the window
-- and filtered by device_id afterwards. On a hypertable, CREATE INDEX
-- propagates to every existing and future chunk.
--
-- transaction_per_chunk: a regular CREATE INDEX blocks position INSERTs for
-- ALL tenants while it scans the whole hypertable; this builds it chunk by
-- chunk, locking only the current chunk. It must run OUTSIDE a transaction
-- block; apply_migrations.sh uses `psql -f` without --single-transaction, so
-- this statement runs alone, before the BEGIN below.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS gps_positions_device_time_idx ON gps_positions (device_id, "time" DESC)
    WITH (timescaledb.transaction_per_chunk);

-- Everything else in ONE transaction: if something fails halfway nothing is
-- left half-applied and the apply_migrations.sh retry starts clean (otherwise
-- a failure after CREATE TABLE made the migration impossible to retry and
-- add_job() was duplicated on every attempt).
BEGIN;

-- ---------------------------------------------------------------------------
-- Pure geometry (IMMUTABLE, no table access)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION geo_distance_m(lat1 DOUBLE PRECISION, lon1 DOUBLE PRECISION,
                                          lat2 DOUBLE PRECISION, lon2 DOUBLE PRECISION)
RETURNS DOUBLE PRECISION
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $$
    SELECT 2 * 6371008.8 * asin(least(1.0, sqrt(
        power(sin(radians(lat2 - lat1) / 2), 2)
        + cos(radians(lat1)) * cos(radians(lat2)) * power(sin(radians(lon2 - lon1) / 2), 2)
    )))
$$;

-- Ray casting (even/odd) over native lat/lon arrays; closing (last -> first)
-- is implicit.
--
-- Why arrays and NOT the `polygon` jsonb (measured while building this
-- feature): reading vertices with `poly -> i` inside a plpgsql loop
-- detoasts the WHOLE jsonb on every access; 500 geofences of 500 vertices
-- took ~65 s PER POSITION, enough for a single tenant to stall GPS ingestion
-- for the whole platform. With arrays copied into local variables (plpgsql
-- expands them once, O(1) access) the same worst case takes ~55 ms.
CREATE OR REPLACE FUNCTION geo_point_in_polygon(lats DOUBLE PRECISION[], lons DOUBLE PRECISION[],
                                                p_lat DOUBLE PRECISION, p_lon DOUBLE PRECISION)
RETURNS BOOLEAN
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT AS $$
DECLARE
    ys DOUBLE PRECISION[] := lats;
    xs DOUBLE PRECISION[] := lons;
    n INT := cardinality(lats);
    i INT;
    j INT := cardinality(lats);
    inside BOOLEAN := false;
BEGIN
    FOR i IN 1 .. n LOOP
        IF ((ys[i] > p_lat) <> (ys[j] > p_lat))
           AND (p_lon < (xs[j] - xs[i]) * (p_lat - ys[i]) / (ys[j] - ys[i]) + xs[i]) THEN
            inside := NOT inside;
        END IF;
        j := i;
    END LOOP;
    RETURN inside;
END;
$$;

-- Minimum distance (m) from the point to the polygon boundary, using a local
-- equirectangular projection centered on the point (negligible error at
-- geofence scale). Only used for exit hysteresis, i.e. almost never (a
-- device that was inside and that ray casting says has left).
CREATE OR REPLACE FUNCTION geo_polygon_edge_distance_m(lats DOUBLE PRECISION[], lons DOUBLE PRECISION[],
                                                       p_lat DOUBLE PRECISION, p_lon DOUBLE PRECISION)
RETURNS DOUBLE PRECISION
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT AS $$
DECLARE
    ys DOUBLE PRECISION[] := lats;
    xs DOUBLE PRECISION[] := lons;
    n INT := cardinality(lats);
    kx DOUBLE PRECISION := 111320.0 * cos(radians(p_lat));
    ky CONSTANT DOUBLE PRECISION := 110574.0;
    i INT; j INT := cardinality(lats);
    ax DOUBLE PRECISION; ay DOUBLE PRECISION; bx DOUBLE PRECISION; by_ DOUBLE PRECISION;
    dx DOUBLE PRECISION; dy DOUBLE PRECISION; t DOUBLE PRECISION; d DOUBLE PRECISION;
    best DOUBLE PRECISION := 'Infinity';
BEGIN
    FOR i IN 1 .. n LOOP
        ax := (xs[j] - p_lon) * kx;
        ay := (ys[j] - p_lat) * ky;
        bx := (xs[i] - p_lon) * kx;
        by_ := (ys[i] - p_lat) * ky;
        dx := bx - ax; dy := by_ - ay;
        IF dx = 0 AND dy = 0 THEN
            t := 0;
        ELSE
            t := greatest(0.0, least(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)));
        END IF;
        d := sqrt(power(ax + t * dx, 2) + power(ay + t * dy, 2));
        IF d < best THEN best := d; END IF;
        j := i;
    END LOOP;
    RETURN best;
END;
$$;

-- ---------------------------------------------------------------------------
-- geofences
-- ---------------------------------------------------------------------------
CREATE TABLE geofences (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name                    TEXT NOT NULL CHECK (btrim(name) <> '' AND length(name) <= 120),
    description             TEXT CHECK (description IS NULL OR length(description) <= 1000),
    color                   TEXT NOT NULL DEFAULT '#037dfe' CHECK (color ~ '^#[0-9a-fA-F]{6}$'),
    shape                   TEXT NOT NULL CHECK (shape IN ('circle', 'polygon')),
    center_lat              DOUBLE PRECISION CHECK (center_lat BETWEEN -90 AND 90),
    center_lon              DOUBLE PRECISION CHECK (center_lon BETWEEN -180 AND 180),
    radius_m                DOUBLE PRECISION CHECK (radius_m BETWEEN 10 AND 100000),
    polygon                 JSONB CHECK (
                                polygon IS NULL
                                OR (jsonb_typeof(polygon) = 'array' AND jsonb_array_length(polygon) BETWEEN 3 AND 500)
                            ),
    -- poly_lat/poly_lon: the same vertices as `polygon` as native arrays,
    -- derived by the trigger (never accepted from the client). These are
    -- what the evaluation engine uses; see geo_point_in_polygon.
    poly_lat                DOUBLE PRECISION[],
    poly_lon                DOUBLE PRECISION[],
    -- bbox_*: ALWAYS computed by the geofences_prepare trigger, never
    -- accepted from the client (a fake "shrunk" bbox would make the geofence
    -- never be evaluated).
    bbox_min_lat            DOUBLE PRECISION NOT NULL,
    bbox_max_lat            DOUBLE PRECISION NOT NULL,
    bbox_min_lon            DOUBLE PRECISION NOT NULL,
    bbox_max_lon            DOUBLE PRECISION NOT NULL,
    enabled                 BOOLEAN NOT NULL DEFAULT true,
    notify_on_enter         BOOLEAN NOT NULL DEFAULT true,
    notify_on_exit          BOOLEAN NOT NULL DEFAULT true,
    -- NULL = no dwell event. When set, a 'dwell' event fires ONCE per visit
    -- when the unit has been inside for that long.
    dwell_minutes           INT CHECK (dwell_minutes IS NULL OR dwell_minutes BETWEEN 1 AND 10080),
    severity                alarm_severity NOT NULL DEFAULT 'info',
    hysteresis_m            INT NOT NULL DEFAULT 20 CHECK (hysteresis_m BETWEEN 0 AND 500),
    -- true = applies to ALL of the tenant's units (including ones added
    -- later); false = only to those in geofence_devices.
    applies_to_all_devices  BOOLEAN NOT NULL DEFAULT true,
    created_by              UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT geofences_tenant_name_unique UNIQUE (tenant_id, name),
    CONSTRAINT geofences_shape_geometry CHECK (
        (shape = 'circle' AND center_lat IS NOT NULL AND center_lon IS NOT NULL AND radius_m IS NOT NULL AND polygon IS NULL)
        OR (shape = 'polygon' AND polygon IS NOT NULL AND center_lat IS NULL AND center_lon IS NULL AND radius_m IS NULL)
    )
);

CREATE INDEX geofences_tenant_enabled_idx ON geofences (tenant_id, bbox_min_lat, bbox_max_lat) WHERE enabled;

CREATE TRIGGER geofences_set_updated_at
    BEFORE UPDATE ON geofences
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE geofences IS
    'Tenant geofence (circle or polygon). Evaluated in insert_gps_position() for every position of any protocol.';

-- Validates the geometry (each vertex: numeric [lat, lon] array in range)
-- and computes the bbox. An invalid polygon never reaches
-- geo_point_in_polygon (which assumes validated data).
CREATE OR REPLACE FUNCTION geofences_prepare() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v JSONB;
    vlat DOUBLE PRECISION;
    vlon DOUBLE PRECISION;
    dlat DOUBLE PRECISION;
    dlon DOUBLE PRECISION;
    enabled_count INT;
    vertex_budget_used INT;
BEGIN
    NEW.poly_lat := NULL;
    NEW.poly_lon := NULL;
    IF NEW.shape = 'circle' THEN
        dlat := NEW.radius_m / 110574.0;
        dlon := NEW.radius_m / (111320.0 * greatest(cos(radians(NEW.center_lat)), 0.01));
        NEW.bbox_min_lat := greatest(-90, NEW.center_lat - dlat);
        NEW.bbox_max_lat := least(90, NEW.center_lat + dlat);
        NEW.bbox_min_lon := greatest(-180, NEW.center_lon - dlon);
        NEW.bbox_max_lon := least(180, NEW.center_lon + dlon);
    ELSE
        NEW.bbox_min_lat := 90; NEW.bbox_max_lat := -90;
        NEW.bbox_min_lon := 180; NEW.bbox_max_lon := -180;
        FOR v IN SELECT value FROM jsonb_array_elements(NEW.polygon) LOOP
            IF jsonb_typeof(v) <> 'array' OR jsonb_array_length(v) <> 2
               OR jsonb_typeof(v -> 0) <> 'number' OR jsonb_typeof(v -> 1) <> 'number' THEN
                RAISE EXCEPTION 'invalid polygon vertex: %', v USING ERRCODE = '22023';
            END IF;
            vlat := (v ->> 0)::double precision;
            vlon := (v ->> 1)::double precision;
            IF vlat NOT BETWEEN -90 AND 90 OR vlon NOT BETWEEN -180 AND 180 THEN
                RAISE EXCEPTION 'polygon vertex out of range: %', v USING ERRCODE = '22023';
            END IF;
            NEW.bbox_min_lat := least(NEW.bbox_min_lat, vlat);
            NEW.bbox_max_lat := greatest(NEW.bbox_max_lat, vlat);
            NEW.bbox_min_lon := least(NEW.bbox_min_lon, vlon);
            NEW.bbox_max_lon := greatest(NEW.bbox_max_lon, vlon);
            NEW.poly_lat := array_append(NEW.poly_lat, vlat);
            NEW.poly_lon := array_append(NEW.poly_lon, vlon);
        END LOOP;
    END IF;

    -- Serializes the per-tenant limit checks (security finding: without it,
    -- N concurrent requests exceeded the limits by roughly the API's
    -- concurrency). Transaction lock, released automatically.
    PERFORM pg_advisory_xact_lock(hashtext('geofences:' || NEW.tenant_id::text));

    -- Total limit (enabled or not): bounded storage growth.
    IF TG_OP = 'INSERT' THEN
        SELECT count(*) INTO enabled_count FROM geofences WHERE tenant_id = NEW.tenant_id;
        IF enabled_count >= 1000 THEN
            RAISE EXCEPTION 'limit of 1000 geofences per tenant reached' USING ERRCODE = '54000';
        END IF;
    END IF;

    -- Per-tenant limit enforced in the database (not only the API): every
    -- enabled geofence adds cost to that tenant's position INSERTs.
    IF NEW.enabled AND (TG_OP = 'INSERT' OR NOT OLD.enabled OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id) THEN
        SELECT count(*) INTO enabled_count FROM geofences
        WHERE tenant_id = NEW.tenant_id AND enabled AND id <> NEW.id;
        IF enabled_count >= 500 THEN
            RAISE EXCEPTION 'limit of 500 enabled geofences per tenant reached' USING ERRCODE = '54000';
        END IF;
    END IF;

    -- TOTAL vertex budget for enabled polygons per tenant: the worst-case
    -- cost per position is (geofences whose bbox contains the point) x
    -- (vertices of each). Measured: 500 polygons of 500 vertices covering
    -- the world = ~55 ms per position; with this limit (equivalent to 50
    -- maximum-detail polygons) the worst case is ~5 ms. Circles do not count
    -- (constant cost).
    IF NEW.enabled AND NEW.shape = 'polygon' THEN
        SELECT COALESCE(sum(cardinality(poly_lat)), 0) INTO vertex_budget_used FROM geofences
        WHERE tenant_id = NEW.tenant_id AND enabled AND shape = 'polygon' AND id <> NEW.id;
        IF vertex_budget_used + cardinality(NEW.poly_lat) > 25000 THEN
            RAISE EXCEPTION 'limit of 25000 enabled polygon geofence vertices per tenant reached'
                USING ERRCODE = '54000';
        END IF;
    END IF;

    IF TG_OP = 'UPDATE' AND NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
        RAISE EXCEPTION 'a geofence cannot change tenant' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION geofences_prepare() FROM PUBLIC;

CREATE TRIGGER geofences_prepare
    BEFORE INSERT OR UPDATE ON geofences
    FOR EACH ROW EXECUTE FUNCTION geofences_prepare();

-- ---------------------------------------------------------------------------
-- geofence_devices: explicit scope (only when applies_to_all_devices=false)
-- ---------------------------------------------------------------------------
CREATE TABLE geofence_devices (
    geofence_id UUID NOT NULL REFERENCES geofences(id) ON DELETE CASCADE,
    device_id   UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (geofence_id, device_id)
);

CREATE INDEX geofence_devices_device_idx ON geofence_devices (device_id);
CREATE INDEX geofence_devices_tenant_idx ON geofence_devices (tenant_id);

-- Same pattern as enforce_device_group_member_tenant_match (0031): both FKs
-- must belong to the row's tenant, regardless of who writes.
CREATE OR REPLACE FUNCTION enforce_geofence_device_tenant_match() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    g_tenant UUID;
    d_tenant UUID;
BEGIN
    SELECT tenant_id INTO g_tenant FROM geofences WHERE id = NEW.geofence_id;
    IF g_tenant IS NULL OR g_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'geofence_id % is not valid for tenant_id %', NEW.geofence_id, NEW.tenant_id;
    END IF;
    SELECT tenant_id INTO d_tenant FROM devices WHERE id = NEW.device_id;
    IF d_tenant IS NULL OR d_tenant <> NEW.tenant_id THEN
        RAISE EXCEPTION 'device_id % is not valid for tenant_id %', NEW.device_id, NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION enforce_geofence_device_tenant_match() FROM PUBLIC;

CREATE TRIGGER geofence_devices_enforce_tenant
    BEFORE INSERT OR UPDATE ON geofence_devices
    FOR EACH ROW EXECUTE FUNCTION enforce_geofence_device_tenant_match();

-- ---------------------------------------------------------------------------
-- geofence_device_state: is this device inside this geofence? Rows exist only
-- for (geofence, device) pairs that were ever within the bbox, never a full
-- cartesian product. No row = outside.
-- ---------------------------------------------------------------------------
CREATE TABLE geofence_device_state (
    geofence_id         UUID NOT NULL REFERENCES geofences(id) ON DELETE CASCADE,
    device_id           UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    inside              BOOLEAN NOT NULL,
    entered_at          TIMESTAMPTZ,
    -- true if entered_at comes from seeding (the unit was already inside when
    -- the geofence was created/redrawn): that visit's duration is a lower
    -- bound, not exact, and the report says so.
    entry_estimated     BOOLEAN NOT NULL DEFAULT false,
    dwell_notified      BOOLEAN NOT NULL DEFAULT false,
    -- Last evaluated position: guards against out-of-order positions. Updated
    -- on every transition and, without a transition, at most every 5 min
    -- (security finding: one UPDATE per ping per geofence the unit is in was
    -- write amplification on the shared ingestion path).
    last_evaluated_at   TIMESTAMPTZ NOT NULL,
    -- Last enter/exit: debounce (see evaluate_geofences_for_position).
    last_transition_at  TIMESTAMPTZ,
    PRIMARY KEY (device_id, geofence_id)
);

CREATE INDEX geofence_device_state_inside_idx ON geofence_device_state (geofence_id) WHERE inside;
CREATE INDEX geofence_device_state_tenant_idx ON geofence_device_state (tenant_id);

-- ---------------------------------------------------------------------------
-- geofence_events: the reporting source. Plain table (not a hypertable): an
-- event is only created on a TRANSITION, orders of magnitude fewer rows than
-- gps_positions, which also avoids the known hypertable + RLS issues (see
-- 0009/0019).
-- ---------------------------------------------------------------------------
CREATE TABLE geofence_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    geofence_id     UUID REFERENCES geofences(id) ON DELETE SET NULL,
    -- snapshot: the report stays readable if the geofence is later deleted
    -- or renamed.
    geofence_name   TEXT NOT NULL,
    device_id       UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL CHECK (event_type IN ('enter', 'exit', 'dwell')),
    "time"          TIMESTAMPTZ NOT NULL,
    lat             DOUBLE PRECISION NOT NULL,
    lon             DOUBLE PRECISION NOT NULL,
    speed_kmh       REAL,
    -- Only on 'exit'/'dwell': when the visit started and how long it lasted
    -- up to this event, so the visits report needs no enter/exit pairing at
    -- query time.
    entered_at      TIMESTAMPTZ,
    duration_s      INT,
    entry_estimated BOOLEAN NOT NULL DEFAULT false,
    -- Generated alarm (if the geofence asked to notify this type). alarms is
    -- a hypertable (composite PK), hence no formal FK.
    alarm_id        UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX geofence_events_tenant_time_idx ON geofence_events (tenant_id, "time" DESC);
CREATE INDEX geofence_events_device_time_idx ON geofence_events (device_id, "time" DESC);
CREATE INDEX geofence_events_geofence_time_idx ON geofence_events (geofence_id, "time" DESC);

COMMENT ON TABLE geofence_events IS
    'Actual enter/exit/dwell transitions. Written only by evaluate_geofences_for_position() (SECURITY DEFINER), never by app_user.';

-- ---------------------------------------------------------------------------
-- Silent state seeding (see point 4 of the header)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION geofence_contains(g geofences, p_lat DOUBLE PRECISION, p_lon DOUBLE PRECISION)
RETURNS BOOLEAN
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN p_lat < g.bbox_min_lat OR p_lat > g.bbox_max_lat
          OR p_lon < g.bbox_min_lon OR p_lon > g.bbox_max_lon THEN false
        WHEN g.shape = 'circle' THEN geo_distance_m(g.center_lat, g.center_lon, p_lat, p_lon) <= g.radius_m
        ELSE geo_point_in_polygon(g.poly_lat, g.poly_lon, p_lat, p_lon)
    END
$$;

-- Meters OUTSIDE the boundary (0 or negative if inside).
CREATE OR REPLACE FUNCTION geofence_outside_distance_m(g geofences, p_lat DOUBLE PRECISION, p_lon DOUBLE PRECISION)
RETURNS DOUBLE PRECISION
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN g.shape = 'circle' THEN geo_distance_m(g.center_lat, g.center_lon, p_lat, p_lon) - g.radius_m
        WHEN geo_point_in_polygon(g.poly_lat, g.poly_lon, p_lat, p_lon) THEN 0
        ELSE geo_polygon_edge_distance_m(g.poly_lat, g.poly_lon, p_lat, p_lon)
    END
$$;

CREATE OR REPLACE FUNCTION seed_geofence_state(p_geofence_id UUID, p_device_id UUID DEFAULT NULL)
RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    g geofences;
BEGIN
    SELECT * INTO g FROM geofences WHERE id = p_geofence_id;
    IF NOT FOUND THEN RETURN; END IF;

    DELETE FROM geofence_device_state
    WHERE geofence_id = p_geofence_id AND (p_device_id IS NULL OR device_id = p_device_id);

    IF NOT g.enabled THEN RETURN; END IF;

    INSERT INTO geofence_device_state
        (geofence_id, device_id, tenant_id, inside, entered_at, entry_estimated, last_evaluated_at)
    SELECT g.id, d.id, g.tenant_id, true, p.time, true, p.time
    FROM devices d
    CROSS JOIN LATERAL (
        SELECT gp.time, gp.lat, gp.lon FROM gps_positions gp
        WHERE gp.device_id = d.id AND gp.time <= now() + interval '5 minutes'
        ORDER BY gp.time DESC LIMIT 1
    ) p
    WHERE d.tenant_id = g.tenant_id
      AND (p_device_id IS NULL OR d.id = p_device_id)
      AND (g.applies_to_all_devices
           OR EXISTS (SELECT 1 FROM geofence_devices gd WHERE gd.geofence_id = g.id AND gd.device_id = d.id))
      AND geofence_contains(g, p.lat, p.lon);
END;
$$;

REVOKE ALL ON FUNCTION seed_geofence_state(UUID, UUID) FROM PUBLIC;

CREATE OR REPLACE FUNCTION geofences_after_change() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP = 'INSERT'
       OR NEW.shape IS DISTINCT FROM OLD.shape
       OR NEW.center_lat IS DISTINCT FROM OLD.center_lat
       OR NEW.center_lon IS DISTINCT FROM OLD.center_lon
       OR NEW.radius_m IS DISTINCT FROM OLD.radius_m
       OR NEW.polygon IS DISTINCT FROM OLD.polygon
       OR NEW.enabled IS DISTINCT FROM OLD.enabled
       OR NEW.applies_to_all_devices IS DISTINCT FROM OLD.applies_to_all_devices
    THEN
        PERFORM seed_geofence_state(NEW.id);
    END IF;
    RETURN NULL;
END;
$$;

REVOKE ALL ON FUNCTION geofences_after_change() FROM PUBLIC;

CREATE TRIGGER geofences_after_change
    AFTER INSERT OR UPDATE ON geofences
    FOR EACH ROW EXECUTE FUNCTION geofences_after_change();

-- Adding a device to the scope seeds ONLY that pair; removing it deletes its
-- state (otherwise a stale "inside" state would produce a false exit if the
-- device were added again later).
CREATE OR REPLACE FUNCTION geofence_devices_after_change() RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM seed_geofence_state(NEW.geofence_id, NEW.device_id);
    ELSE
        DELETE FROM geofence_device_state WHERE geofence_id = OLD.geofence_id AND device_id = OLD.device_id;
    END IF;
    RETURN NULL;
END;
$$;

REVOKE ALL ON FUNCTION geofence_devices_after_change() FROM PUBLIC;

CREATE TRIGGER geofence_devices_after_change
    AFTER INSERT OR DELETE ON geofence_devices
    FOR EACH ROW EXECUTE FUNCTION geofence_devices_after_change();

-- ---------------------------------------------------------------------------
-- Evaluation engine: called ONLY from insert_gps_position().
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION geofence_emit_event(
    g geofences, p_device_id UUID, p_event_type TEXT, p_time TIMESTAMPTZ,
    p_lat DOUBLE PRECISION, p_lon DOUBLE PRECISION, p_speed_kmh REAL,
    p_entered_at TIMESTAMPTZ, p_entry_estimated BOOLEAN, p_allow_notify BOOLEAN
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_event_id UUID;
    v_duration INT;
    v_alarm_id UUID;
    v_notify BOOLEAN;
    v_summary TEXT;
BEGIN
    IF p_event_type <> 'enter' AND p_entered_at IS NOT NULL THEN
        v_duration := greatest(0, extract(epoch FROM (p_time - p_entered_at)))::int;
    END IF;

    INSERT INTO geofence_events (tenant_id, geofence_id, geofence_name, device_id, event_type, time,
                                 lat, lon, speed_kmh, entered_at, duration_s, entry_estimated)
    VALUES (g.tenant_id, g.id, g.name, p_device_id, p_event_type, p_time,
            p_lat, p_lon, p_speed_kmh,
            CASE WHEN p_event_type = 'enter' THEN NULL ELSE p_entered_at END,
            v_duration, COALESCE(p_entry_estimated, false) AND p_event_type <> 'enter')
    RETURNING id INTO v_event_id;

    v_notify := CASE p_event_type
        WHEN 'enter' THEN g.notify_on_enter
        WHEN 'exit' THEN g.notify_on_exit
        ELSE g.dwell_minutes IS NOT NULL
    END;
    IF NOT v_notify OR NOT p_allow_notify THEN RETURN false; END IF;

    v_summary := CASE p_event_type
        WHEN 'enter' THEN 'Entered «' || g.name || '»'
        WHEN 'exit' THEN 'Exited «' || g.name || '»'
        ELSE 'In «' || g.name || '» for ' || g.dwell_minutes || ' min'
    END;

    BEGIN
        v_alarm_id := insert_alarm(
            g.tenant_id, p_device_id, p_time, 'geofence_' || p_event_type, g.severity,
            jsonb_build_object(
                'geofence_id', g.id,
                'geofence_name', g.name,
                'geofence_event_id', v_event_id,
                'duration_s', v_duration,
                'lat', p_lat,
                'lon', p_lon,
                'summary', v_summary
            )
        );
        UPDATE geofence_events SET alarm_id = v_alarm_id WHERE id = v_event_id;
    EXCEPTION WHEN OTHERS THEN
        -- The event (reporting source) is already stored; a notification
        -- failure must never remove it.
        RAISE WARNING 'geofence_emit_event: insert_alarm failed for event %: %', v_event_id, SQLERRM;
    END;
    RETURN true;
END;
$$;

REVOKE ALL ON FUNCTION geofence_emit_event(geofences, UUID, TEXT, TIMESTAMPTZ, DOUBLE PRECISION, DOUBLE PRECISION, REAL, TIMESTAMPTZ, BOOLEAN, BOOLEAN) FROM PUBLIC;

-- Guardrails from the security review, in addition to hysteresis and the
-- ordering guard:
--   - Future timestamps: a single position dated in the future (a device
--     with a misconfigured clock, or a spoofed terminal_id; JT808 does not
--     bound the timestamp) left last_evaluated_at in the future and froze
--     that unit's geofence state forever. Positions more than 5 min ahead
--     are ignored for geofences.
--   - Debounce: an enter/exit less than 60 s after the previous transition
--     of the SAME (geofence, unit) does not change the state, so GPS jitter
--     on an edge with hysteresis_m=0 no longer causes an event storm.
--   - Limit of 20 notifications per position: 500 overlapping geofences no
--     longer turn one ping into 500 alarms + fan-out + webhooks (all
--     transitions are still stored in geofence_events for reporting).
CREATE OR REPLACE FUNCTION evaluate_geofences_for_position(
    p_tenant_id UUID, p_device_id UUID, p_time TIMESTAMPTZ,
    p_lat DOUBLE PRECISION, p_lon DOUBLE PRECISION, p_speed_kmh REAL
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    g geofences;
    s geofence_device_state;
    v_in BOOLEAN;
    v_found BOOLEAN;
    v_notified INT := 0;
    c_max_notifications CONSTANT INT := 20;
    c_debounce CONSTANT INTERVAL := interval '60 seconds';
    c_eval_refresh CONSTANT INTERVAL := interval '5 minutes';
BEGIN
    IF p_time > now() + interval '5 minutes' THEN
        RETURN;
    END IF;

    FOR g IN
        SELECT gf.* FROM geofences gf
        WHERE gf.tenant_id = p_tenant_id
          AND gf.enabled
          AND (gf.applies_to_all_devices
               OR EXISTS (SELECT 1 FROM geofence_devices gd WHERE gd.geofence_id = gf.id AND gd.device_id = p_device_id))
          AND (
              (p_lat BETWEEN gf.bbox_min_lat AND gf.bbox_max_lat AND p_lon BETWEEN gf.bbox_min_lon AND gf.bbox_max_lon)
              OR EXISTS (SELECT 1 FROM geofence_device_state st
                         WHERE st.device_id = p_device_id AND st.geofence_id = gf.id AND st.inside)
          )
    LOOP
        SELECT * INTO s FROM geofence_device_state
        WHERE device_id = p_device_id AND geofence_id = g.id
        FOR UPDATE;
        v_found := FOUND;

        v_in := geofence_contains(g, p_lat, p_lon);

        IF NOT v_found THEN
            INSERT INTO geofence_device_state
                (geofence_id, device_id, tenant_id, inside, entered_at, last_evaluated_at, last_transition_at)
            VALUES (g.id, p_device_id, p_tenant_id, v_in, CASE WHEN v_in THEN p_time END, p_time,
                    CASE WHEN v_in THEN p_time END)
            ON CONFLICT (device_id, geofence_id) DO NOTHING;
            IF v_in AND geofence_emit_event(g, p_device_id, 'enter', p_time, p_lat, p_lon, p_speed_kmh, NULL, false,
                                            v_notified < c_max_notifications) THEN
                v_notified := v_notified + 1;
            END IF;
            CONTINUE;
        END IF;

        -- Stale position (device buffer after signal loss): never moves the
        -- state backwards.
        IF p_time < s.last_evaluated_at THEN
            CONTINUE;
        END IF;

        -- Exit hysteresis: within the edge margin it still counts as
        -- inside.
        IF s.inside AND NOT v_in AND geofence_outside_distance_m(g, p_lat, p_lon) <= g.hysteresis_m THEN
            v_in := true;
        END IF;

        -- Debounce: a transition too soon after the previous one is
        -- ignored (state unchanged; the next position re-evaluates it).
        IF v_in <> s.inside AND s.last_transition_at IS NOT NULL AND p_time - s.last_transition_at < c_debounce THEN
            CONTINUE;
        END IF;

        IF v_in AND NOT s.inside THEN
            UPDATE geofence_device_state
            SET inside = true, entered_at = p_time, entry_estimated = false, dwell_notified = false,
                last_evaluated_at = p_time, last_transition_at = p_time
            WHERE device_id = p_device_id AND geofence_id = g.id;
            IF geofence_emit_event(g, p_device_id, 'enter', p_time, p_lat, p_lon, p_speed_kmh, NULL, false,
                                   v_notified < c_max_notifications) THEN
                v_notified := v_notified + 1;
            END IF;
        ELSIF NOT v_in AND s.inside THEN
            UPDATE geofence_device_state
            SET inside = false, entered_at = NULL, entry_estimated = false, dwell_notified = false,
                last_evaluated_at = p_time, last_transition_at = p_time
            WHERE device_id = p_device_id AND geofence_id = g.id;
            IF geofence_emit_event(g, p_device_id, 'exit', p_time, p_lat, p_lon, p_speed_kmh,
                                   s.entered_at, s.entry_estimated, v_notified < c_max_notifications) THEN
                v_notified := v_notified + 1;
            END IF;
        ELSIF v_in AND g.dwell_minutes IS NOT NULL AND NOT s.dwell_notified
              AND s.entered_at IS NOT NULL
              AND p_time - s.entered_at >= make_interval(mins => g.dwell_minutes) THEN
            UPDATE geofence_device_state SET dwell_notified = true, last_evaluated_at = p_time
            WHERE device_id = p_device_id AND geofence_id = g.id;
            IF geofence_emit_event(g, p_device_id, 'dwell', p_time, p_lat, p_lon, p_speed_kmh,
                                   s.entered_at, s.entry_estimated, v_notified < c_max_notifications) THEN
                v_notified := v_notified + 1;
            END IF;
        ELSIF p_time - s.last_evaluated_at >= c_eval_refresh THEN
            UPDATE geofence_device_state SET last_evaluated_at = p_time
            WHERE device_id = p_device_id AND geofence_id = g.id;
        END IF;
    END LOOP;
END;
$$;

REVOKE ALL ON FUNCTION evaluate_geofences_for_position(UUID, UUID, TIMESTAMPTZ, DOUBLE PRECISION, DOUBLE PRECISION, REAL) FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- insert_gps_position(): same as 0050 plus the geofence block at the end, in
-- its own BEGIN/EXCEPTION (it can never roll back the stored position or the
-- speed alarm). CREATE OR REPLACE keeps the existing GRANT/REVOKE
-- (0009/0018).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION insert_gps_position(
    p_tenant_id UUID,
    p_device_id UUID,
    p_time      TIMESTAMPTZ,
    p_lat       DOUBLE PRECISION,
    p_lon       DOUBLE PRECISION,
    p_speed_kmh REAL DEFAULT NULL,
    p_heading   REAL DEFAULT NULL,
    p_altitude  REAL DEFAULT NULL,
    p_raw       JSONB DEFAULT NULL
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_max_speed NUMERIC;
    v_active    BOOLEAN;
BEGIN
    IF NOT (app_bypass_rls() OR p_tenant_id = app_current_tenant_id()) THEN
        RAISE EXCEPTION 'tenant_id % not authorized for this session', p_tenant_id
            USING ERRCODE = '42501';
    END IF;
    INSERT INTO gps_positions (time, tenant_id, device_id, lat, lon, speed_kmh, heading, altitude, raw)
    VALUES (p_time, p_tenant_id, p_device_id, p_lat, p_lon, p_speed_kmh, p_heading, p_altitude, p_raw);

    PERFORM pg_notify(
        'gps_positions',
        json_build_object(
            'tenant_id', p_tenant_id,
            'device_id', p_device_id,
            'lat', p_lat,
            'lon', p_lon,
            'speed_kmh', p_speed_kmh,
            'heading', p_heading,
            'time', p_time
        )::text
    );

    IF p_speed_kmh IS NOT NULL THEN
        BEGIN
            SELECT v.max_speed_kmh, d.overspeed_active INTO v_max_speed, v_active
            FROM devices d LEFT JOIN vehicles v ON v.id = d.vehicle_id
            WHERE d.id = p_device_id;

            IF v_max_speed IS NOT NULL THEN
                IF p_speed_kmh > v_max_speed AND NOT COALESCE(v_active, false) THEN
                    UPDATE devices SET overspeed_active = true WHERE id = p_device_id;
                    PERFORM insert_alarm(
                        p_tenant_id, p_device_id, p_time, 'overspeed_limit', 'warning',
                        jsonb_build_object('speed_kmh', p_speed_kmh, 'max_speed_kmh', v_max_speed)
                    );
                ELSIF p_speed_kmh <= v_max_speed AND COALESCE(v_active, false) THEN
                    UPDATE devices SET overspeed_active = false WHERE id = p_device_id;
                END IF;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'insert_gps_position: max speed check failed for device %: %', p_device_id, SQLERRM;
        END;
    END IF;

    BEGIN
        PERFORM evaluate_geofences_for_position(p_tenant_id, p_device_id, p_time, p_lat, p_lon, p_speed_kmh);
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'insert_gps_position: geofence evaluation failed for device %: %', p_device_id, SQLERRM;
    END;
END;
$$;

-- ---------------------------------------------------------------------------
-- insert_alarm(): identical to 0037 plus the notification `body` taken from
-- p_details->>'summary' when present (e.g. "Entered «North Warehouse»").
-- Additive: any alarm without 'summary' keeps body NULL as before. The
-- 'Alarm: ' title prefix is a stored data format parsed by the frontend.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION insert_alarm(
    p_tenant_id          UUID,
    p_device_id          UUID,
    p_time               TIMESTAMPTZ,
    p_alarm_type         TEXT,
    p_severity           alarm_severity DEFAULT 'warning',
    p_details            JSONB DEFAULT NULL,
    p_video_evidence_key TEXT DEFAULT NULL
) RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    new_id UUID;
    recipient_count INT;
BEGIN
    IF NOT (app_bypass_rls() OR p_tenant_id = app_current_tenant_id()) THEN
        RAISE EXCEPTION 'tenant_id % not authorized for this session', p_tenant_id
            USING ERRCODE = '42501';
    END IF;
    INSERT INTO alarms (time, tenant_id, device_id, alarm_type, severity, details, video_evidence_key)
    VALUES (p_time, p_tenant_id, p_device_id, p_alarm_type, p_severity, p_details, p_video_evidence_key)
    RETURNING id INTO new_id;

    BEGIN
        WITH recipients AS (
            SELECT r.user_id,
                   COALESCE(s.in_app_enabled, true) AS in_app_enabled,
                   COALESCE(s.email_enabled, false) AS email_enabled
            FROM app_device_recipients(p_device_id) AS r(user_id)
            LEFT JOIN user_notification_settings s ON s.user_id = r.user_id
        ), inserted AS (
            INSERT INTO notifications (
                tenant_id, recipient_user_id, event_type, device_id, alarm_id, alarm_time,
                title, body, severity, in_app_enabled, email_status
            )
            SELECT p_tenant_id, user_id, 'device_alarm', p_device_id, new_id, p_time,
                   'Alarm: ' || p_alarm_type, left(p_details ->> 'summary', 500), p_severity, in_app_enabled,
                   (CASE WHEN email_enabled THEN 'pending' ELSE 'not_applicable' END)::notification_email_status
            FROM recipients
            WHERE in_app_enabled OR email_enabled
            RETURNING recipient_user_id
        )
        SELECT count(*) INTO recipient_count FROM inserted;
    EXCEPTION WHEN OTHERS THEN
        recipient_count := 0;
        RAISE WARNING 'insert_alarm: notification fan-out failed for alarm % (device %): %', new_id, p_device_id, SQLERRM;
    END;

    BEGIN
        PERFORM pg_notify(
            'notifications',
            json_build_object('tenant_id', p_tenant_id, 'alarm_id', new_id)::text
        );
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'insert_alarm: pg_notify failed for alarm %: %', new_id, SQLERRM;
    END;

    RETURN new_id;
END;
$$;

REVOKE ALL ON FUNCTION insert_alarm(UUID, UUID, TIMESTAMPTZ, TEXT, alarm_severity, JSONB, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION insert_alarm(UUID, UUID, TIMESTAMPTZ, TEXT, alarm_severity, JSONB, TEXT) TO app_user;

-- ---------------------------------------------------------------------------
-- RLS
-- ---------------------------------------------------------------------------
ALTER TABLE geofences ENABLE ROW LEVEL SECURITY;
ALTER TABLE geofences FORCE ROW LEVEL SECURITY;
CREATE POLICY geofences_select ON geofences
    FOR SELECT USING (app_bypass_rls() OR tenant_id = app_current_tenant_id());
CREATE POLICY geofences_insert ON geofences
    FOR INSERT WITH CHECK (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
CREATE POLICY geofences_update ON geofences
    FOR UPDATE USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()))
    WITH CHECK (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
CREATE POLICY geofences_delete ON geofences
    FOR DELETE USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
GRANT SELECT, INSERT, DELETE ON geofences TO app_user;
-- Column-level UPDATE: bbox_* and tenant_id are never written by the API
-- (the trigger computes the bbox; tenant_id is immutable).
GRANT UPDATE (name, description, color, shape, center_lat, center_lon, radius_m, polygon, enabled,
              notify_on_enter, notify_on_exit, dwell_minutes, severity, hysteresis_m,
              applies_to_all_devices) ON geofences TO app_user;

ALTER TABLE geofence_devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE geofence_devices FORCE ROW LEVEL SECURITY;
CREATE POLICY geofence_devices_select ON geofence_devices
    FOR SELECT USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_can_view_device(device_id)));
-- app_can_view_device on INSERT too (security finding): an API key with
-- allowed_device_ids could add a device outside its own scope to a
-- geofence's scope.
CREATE POLICY geofence_devices_insert ON geofence_devices
    FOR INSERT WITH CHECK (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()
                                                AND app_can_view_device(device_id)));
CREATE POLICY geofence_devices_delete ON geofence_devices
    FOR DELETE USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_is_tenant_admin()));
GRANT SELECT, INSERT, DELETE ON geofence_devices TO app_user;

ALTER TABLE geofence_device_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE geofence_device_state FORCE ROW LEVEL SECURITY;
CREATE POLICY geofence_device_state_select ON geofence_device_state
    FOR SELECT USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_can_view_device(device_id)));
GRANT SELECT ON geofence_device_state TO app_user;

ALTER TABLE geofence_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE geofence_events FORCE ROW LEVEL SECURITY;
CREATE POLICY geofence_events_select ON geofence_events
    FOR SELECT USING (app_bypass_rls() OR (tenant_id = app_current_tenant_id() AND app_can_view_device(device_id)));
-- Deletion only for platform cleanup/retention (bypass); a tenant never
-- deletes its own event history.
CREATE POLICY geofence_events_delete ON geofence_events
    FOR DELETE USING (app_bypass_rls());
GRANT SELECT ON geofence_events TO app_user;

-- ---------------------------------------------------------------------------
-- geofence_events retention: 365 days, same horizon as alarms (0019). Same
-- add_job() mechanism as enforce_gps_position_retention.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE enforce_geofence_event_retention(job_id INT, config JSONB)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    DELETE FROM geofence_events WHERE time < now() - interval '365 days';
END;
$$;

REVOKE ALL ON PROCEDURE enforce_geofence_event_retention(INT, JSONB) FROM PUBLIC;

SELECT add_job('enforce_geofence_event_retention', '1 day');

COMMIT;
