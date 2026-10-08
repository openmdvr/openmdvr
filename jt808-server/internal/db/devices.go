package db

import (
	"context"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// ErrDeviceNotFound is returned when no provisioned device matches the
// identifier reported by the incoming connection. Provisioning devices is a
// platform/support action (see infra/postgres/migrations/0008_rls_policies.sql),
// never self-service: hardware connecting with an identifier nobody
// provisioned must not be able to write data under any tenant.
var ErrDeviceNotFound = errors.New("db: no device provisioned for this terminal_id")

type Device struct {
	ID       uuid.UUID
	TenantID uuid.UUID
	Status   string
	// Protocol ('jt808'|'gt06'|'gt06_video'). Needed by the GT06 video
	// on_publish hook (see gt06videobridge/protocol.go::AuthorizePublish) to
	// require that the publishing IMEI belongs to a gt06_video device (WITH a
	// camera), not a plain gt06 GPS tracker sharing the same identifier
	// format. Found in a security review: without this check any provisioned
	// GPS-only GT06 tracker could "publish video" (never playable, but it
	// consumed ingest/CPU/disk indefinitely).
	Protocol string
}

// LookupDeviceByTerminalID finds the device by its jt808_terminal_id (BCD
// decoded to text by the protocol library). jt808_terminal_id is globally
// unique (see 0006_devices.sql), so this query does not -- and cannot --
// filter by tenant: what it returns is precisely which tenant the incoming
// connection belongs to.
//
// IMPORTANT when provisioning devices: the protocol library's BCD decoder
// (utils.Bcd2Dec) STRIPS leading zeros from the terminal number -- a
// terminal configured on the hardware as "013800000001" arrives here as
// "13800000001". If devices.jt808_terminal_id is stored with the leading
// zero as typed by a human, the lookup never matches and registration is
// rejected (code 4, "not provisioned") even though the device IS
// provisioned. The provisioning layer must normalize (strip leading zeros)
// before inserting. Confirmed empirically with the test simulator
// (testclient/simulate.py).
func LookupDeviceByTerminalID(ctx context.Context, tx pgx.Tx, terminalID string) (Device, error) {
	var d Device
	err := tx.QueryRow(ctx,
		`SELECT id, tenant_id, status::text, protocol::text FROM devices WHERE jt808_terminal_id = $1`,
		terminalID,
	).Scan(&d.ID, &d.TenantID, &d.Status, &d.Protocol)
	if errors.Is(err, pgx.ErrNoRows) {
		return Device{}, ErrDeviceNotFound
	}
	if err != nil {
		return Device{}, fmt.Errorf("db: looking up device by terminal_id: %w", err)
	}
	return d, nil
}

// LookupDeviceByIMEI finds the device by its gt06_imei (extracted from the
// GT06 login packet -- a direct 8-byte hex dump, without the leading-zero
// gotcha of JT808 BCD, see 0026_gt06_devices.sql). Globally unique for the
// same reason as jt808_terminal_id: the GT06 server routes by IMEI before it
// knows which tenant the connection belongs to.
func LookupDeviceByIMEI(ctx context.Context, tx pgx.Tx, imei string) (Device, error) {
	var d Device
	err := tx.QueryRow(ctx,
		`SELECT id, tenant_id, status::text, protocol::text FROM devices WHERE gt06_imei = $1`,
		imei,
	).Scan(&d.ID, &d.TenantID, &d.Status, &d.Protocol)
	if errors.Is(err, pgx.ErrNoRows) {
		return Device{}, ErrDeviceNotFound
	}
	if err != nil {
		return Device{}, fmt.Errorf("db: looking up device by gt06_imei: %w", err)
	}
	return d, nil
}

// TouchLastSeen updates devices.last_seen_at. Called on every heartbeat and
// position report received from an already authenticated device.
func TouchLastSeen(ctx context.Context, tx pgx.Tx, deviceID uuid.UUID) error {
	_, err := tx.Exec(ctx, `UPDATE devices SET last_seen_at = now() WHERE id = $1`, deviceID)
	if err != nil {
		return fmt.Errorf("db: updating last_seen_at: %w", err)
	}
	return nil
}

// UpdateDeviceStatus updates last_seen_at + ignition_on/power_connected in a
// SINGLE write (avoids an extra round trip of TouchLastSeen plus a separate
// UPDATE per position/alarm). Called from jt808server (every 0x0200 -- the
// STATUS field is mandatory, so ignitionOn/powerConnected are almost always
// non-nil) and gt06server (heartbeats and alarm frames, which carry the
// "Terminal Information" byte -- see parseTerminalInfo).
//
// ignitionOn/powerConnected nil = "this message does not carry that signal"
// -- it NEVER overwrites a known value with NULL (hence COALESCE).
// *_changed_at only advances when the NEW value differs from the row's OLD
// value (IS DISTINCT FROM, evaluated against the column as it was BEFORE
// this UPDATE -- that is how Postgres resolves column references inside the
// same SET). There is no read-then-write race: it is one atomic statement on
// a PK-indexed row.
func UpdateDeviceStatus(ctx context.Context, tx pgx.Tx, deviceID uuid.UUID, ignitionOn, powerConnected *bool) error {
	_, err := tx.Exec(ctx, `
		UPDATE devices SET
			last_seen_at = now(),
			ignition_on = COALESCE($2, ignition_on),
			ignition_changed_at = CASE
				WHEN $2 IS NOT NULL AND $2 IS DISTINCT FROM ignition_on THEN now()
				ELSE ignition_changed_at
			END,
			power_connected = COALESCE($3, power_connected),
			power_changed_at = CASE
				WHEN $3 IS NOT NULL AND $3 IS DISTINCT FROM power_connected THEN now()
				ELSE power_changed_at
			END
		WHERE id = $1`,
		deviceID, ignitionOn, powerConnected,
	)
	if err != nil {
		return fmt.Errorf("db: updating ignition/power status: %w", err)
	}
	return nil
}
