package db

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// RecordDeviceHealthEvent records (or increments an already open) operational
// problem of a device -- data wasted on retries, a native photo that fell
// back to the expensive path, etc. -- for the platform's device health view
// (migration 0053, record_device_health_event). The same open
// (device, kind, dedupeKey) is ONE row with a counter, never one row per
// occurrence.
func RecordDeviceHealthEvent(ctx context.Context, tx pgx.Tx, deviceID uuid.UUID, kind, dedupeKey, severity, title string, detail map[string]any, bytesWasted int64) error {
	var raw []byte
	if detail != nil {
		b, err := json.Marshal(detail)
		if err != nil {
			return fmt.Errorf("db: marshaling health detail: %w", err)
		}
		raw = b
	}
	_, err := tx.Exec(ctx,
		`SELECT record_device_health_event($1, $2, $3, $4, $5, $6::jsonb, $7)`,
		deviceID, kind, dedupeKey, severity, title, raw, bytesWasted,
	)
	if err != nil {
		return fmt.Errorf("db: record_device_health_event (%s): %w", kind, err)
	}
	return nil
}
