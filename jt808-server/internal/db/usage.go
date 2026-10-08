package db

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// InsertUsageEvent calls the SECURITY DEFINER function insert_usage_event()
// (infra/postgres/migrations/0009_timeseries_access.sql) -- the ledger of
// bytes served to clients, mandatory at every video delivery point. user_id
// is always NULL here: the browser plays the stream directly from
// ZLMediaKit (never proxied through the API), so by the time ZLMediaKit
// reports bytes served there is no way to know which authenticated user
// consumed them -- only which device/tenant. See videobridge/dispatcher.go.
func InsertUsageEvent(ctx context.Context, tx pgx.Tx, tenantID, deviceID uuid.UUID, eventType string, bytesTransferred int64, metadata map[string]any) error {
	var metaJSON json.RawMessage
	if metadata != nil {
		b, err := json.Marshal(metadata)
		if err != nil {
			return fmt.Errorf("db: marshaling usage_event metadata: %w", err)
		}
		metaJSON = b
	}
	_, err := tx.Exec(ctx,
		`SELECT insert_usage_event($1, $2, now(), NULL, $3, $4, $5)`,
		tenantID, deviceID, eventType, bytesTransferred, metaJSON,
	)
	if err != nil {
		return fmt.Errorf("db: insert_usage_event: %w", err)
	}
	return nil
}
