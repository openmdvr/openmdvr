package db

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/openmdvr/openmdvr/jt808-server/internal/gpsfilter"
)

// GPSPosition holds the fields extracted from a location report (e.g. JT808
// message 0x0200) for persistence.
type GPSPosition struct {
	Time     time.Time
	Lat      float64
	Lon      float64
	SpeedKmh *float32
	Heading  *float32
	Altitude *float32
	// Raw goes to gps_positions.raw -- the original reading and the GPS
	// quality filter's decision (see internal/gpsfilter) when the stored
	// position differs from the reported one. nil = stored as reported.
	Raw map[string]any
}

// InsertGPSPosition calls the SECURITY DEFINER function insert_gps_position()
// (infra/postgres/migrations/0009_timeseries_access.sql) instead of a direct
// INSERT: gps_positions is a TimescaleDB hypertable and app_user has (and
// must never have) any direct table privilege on it -- see that migration
// for why.
func InsertGPSPosition(ctx context.Context, tx pgx.Tx, tenantID, deviceID uuid.UUID, p GPSPosition) error {
	var raw []byte
	if p.Raw != nil {
		b, err := json.Marshal(p.Raw)
		if err != nil {
			return fmt.Errorf("db: marshaling position raw: %w", err)
		}
		raw = b
	}
	_, err := tx.Exec(ctx,
		`SELECT insert_gps_position($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)`,
		tenantID, deviceID, p.Time, p.Lat, p.Lon, p.SpeedKmh, p.Heading, p.Altitude, raw,
	)
	if err != nil {
		return fmt.Errorf("db: insert_gps_position: %w", err)
	}
	return nil
}

// InsertGPSPositionFiltered runs the reading through the shared GPS quality
// filter (gpsfilter.Default: parked-unit drift, incoherent jumps, noise
// speed) and stores what it decides. It is the ONLY path through which JT808
// and GT06 write positions, so any future protocol inherits the filter. It
// returns the reason (for the caller's log) when the reading was not stored
// as reported; empty in the normal case.
func InsertGPSPositionFiltered(ctx context.Context, tx pgx.Tx, tenantID, deviceID uuid.UUID, s gpsfilter.Sample, altitude *float32) (string, error) {
	if err := relaxCommitForTelemetry(ctx, tx); err != nil {
		return "", err
	}
	if !gpsfilter.Default.Known(deviceID) {
		seedGPSFilter(ctx, tx, deviceID)
	}
	d := gpsfilter.Default.Process(deviceID, s)
	for _, rp := range d.Released {
		if err := InsertGPSPosition(ctx, tx, tenantID, deviceID, fromFiltered(rp, altitude)); err != nil {
			return d.Reason, err
		}
	}
	if d.Action == gpsfilter.Store {
		if err := InsertGPSPosition(ctx, tx, tenantID, deviceID, fromFiltered(d.Position, altitude)); err != nil {
			return d.Reason, err
		}
	}
	return d.Reason, nil
}

// relaxCommitForTelemetry lets a transaction that stores GPS positions commit
// without waiting for the WAL flush (synchronous_commit = off, transaction
// scope only).
//
// Why: insert_gps_position() issues pg_notify for the live map, and
// PostgreSQL serializes the commit of every notifying transaction behind one
// database-wide lock that is held across the WAL flush. With a synchronous
// commit, ingestion throughput is therefore capped at 1 / fsync latency for
// the whole platform, no matter how many connections or cores there are
// (measured: about 550 positions/s on a slow disk, with logins timing out
// behind the queue). With an asynchronous commit the lock is held only for
// the in-memory part of the commit. See docs/scalability.md.
//
// Trade-off: if PostgreSQL itself crashes, positions acknowledged in roughly
// the last 0.6 s (3 x wal_writer_delay) can be lost. The database is never
// corrupted, and a crash of this process loses nothing extra. Alarms are not
// relaxed: InsertAlarm forces a durable commit for its whole transaction.
func relaxCommitForTelemetry(ctx context.Context, tx pgx.Tx) error {
	if _, err := tx.Exec(ctx, `
		SELECT set_config('synchronous_commit', 'off', true)
		WHERE current_setting('openmdvr.durable_commit', true) IS DISTINCT FROM 'on'`); err != nil {
		return fmt.Errorf("db: relax commit: %w", err)
	}
	return nil
}

// requireDurableCommit makes the current transaction commit synchronously,
// even if it already stored a GPS position (relaxCommitForTelemetry), and
// marks it so a later position in the same transaction cannot relax it again.
func requireDurableCommit(ctx context.Context, tx pgx.Tx) error {
	if _, err := tx.Exec(ctx, `
		SELECT set_config('openmdvr.durable_commit', 'on', true),
		       set_config('synchronous_commit', 'on', true)`); err != nil {
		return fmt.Errorf("db: durable commit: %w", err)
	}
	return nil
}

// seedGPSFilter loads the unit's last stored position so the filter does not
// judge the first reading without memory (see gpsfilter.Seed). An error here
// never blocks ingestion: the filter falls back to its first-reading rule.
func seedGPSFilter(ctx context.Context, tx pgx.Tx, deviceID uuid.UUID) {
	var (
		t        time.Time
		lat, lon float64
		speed    *float32
	)
	// Subtransaction (SAVEPOINT): an error in this query must never abort
	// the transaction that later stores the position.
	sub, err := tx.Begin(ctx)
	if err != nil {
		return
	}
	defer sub.Rollback(ctx)
	err = sub.QueryRow(ctx, `
		SELECT time, lat, lon, speed_kmh FROM gps_positions_v
		WHERE device_id = $1 AND time <= now() + interval '5 minutes'
		ORDER BY time DESC LIMIT 1`, deviceID).Scan(&t, &lat, &lon, &speed)
	if err != nil {
		return // no history (new unit) or error: no seed
	}
	var sp float64
	if speed != nil {
		sp = float64(*speed)
	}
	gpsfilter.Default.Seed(deviceID, t, lat, lon, sp)
}

func fromFiltered(p gpsfilter.Position, altitude *float32) GPSPosition {
	return GPSPosition{Time: p.Time, Lat: p.Lat, Lon: p.Lon, SpeedKmh: p.SpeedKmh, Heading: p.Heading, Altitude: altitude, Raw: p.Raw}
}

// InsertAlarm calls the SECURITY DEFINER function insert_alarm() and returns
// the id of the created alarm (used, e.g., to attach a video clip request).
func InsertAlarm(ctx context.Context, tx pgx.Tx, tenantID, deviceID uuid.UUID, t time.Time, alarmType, severity string) (uuid.UUID, error) {
	if err := requireDurableCommit(ctx, tx); err != nil {
		return uuid.Nil, err
	}
	var id uuid.UUID
	err := tx.QueryRow(ctx,
		`SELECT insert_alarm($1, $2, $3, $4, $5, NULL, NULL)`,
		tenantID, deviceID, t, alarmType, severity,
	).Scan(&id)
	if err != nil {
		return uuid.Nil, fmt.Errorf("db: insert_alarm (%s): %w", alarmType, err)
	}
	return id, nil
}

// RecordDeviceDataUsage calls the SECURITY DEFINER function
// record_device_data_usage() (0045_device_models_sim_usage.sql) -- the
// device's REAL cellular SIM consumption (TCP-level rx/tx bytes, see
// internal/datausage.CountingConn), accumulated in a monthly rollup. It takes
// the *pgxpool.Pool directly (not an open pgx.Tx, unlike
// InsertAlarm/InsertGPSPosition) because it is called from the periodic and
// connection-close flush, outside any ongoing business transaction -- it
// opens its own short bypass connection.
func RecordDeviceDataUsage(ctx context.Context, pool *pgxpool.Pool, deviceID uuid.UUID, bytesRx, bytesTx int64) error {
	if bytesRx == 0 && bytesTx == 0 {
		return nil
	}
	err := WithBypass(ctx, pool, func(ctx context.Context, tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `SELECT record_device_data_usage($1, $2, $3)`, deviceID, bytesRx, bytesTx)
		return err
	})
	if err != nil {
		return fmt.Errorf("db: record_device_data_usage: %w", err)
	}
	return nil
}
