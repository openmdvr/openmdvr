package db

import (
	"context"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// GetTenantLiveViewLimit returns the per-stream-session live view limit in
// seconds for a tenant (infra/postgres/migrations/0011_tenant_live_view_limit.sql).
// The video bridge uses it to cut the stream server-side when it expires,
// regardless of what the client does.
func GetTenantLiveViewLimit(ctx context.Context, tx pgx.Tx, tenantID uuid.UUID) (int, error) {
	var seconds int
	err := tx.QueryRow(ctx, `SELECT max_live_view_seconds FROM tenants WHERE id = $1`, tenantID).Scan(&seconds)
	if err != nil {
		return 0, fmt.Errorf("db: querying max_live_view_seconds: %w", err)
	}
	return seconds, nil
}

// GetTenantLiveViewQuotaSeconds returns a tenant's MONTHLY (cumulative) live
// view quota in seconds (infra/postgres/migrations/0012_tenant_live_view_quota.sql)
// -- distinct from GetTenantLiveViewLimit, which is a per-session cap.
func GetTenantLiveViewQuotaSeconds(ctx context.Context, tx pgx.Tx, tenantID uuid.UUID) (int, error) {
	var seconds int
	err := tx.QueryRow(ctx, `SELECT live_view_monthly_quota_seconds FROM tenants WHERE id = $1`, tenantID).Scan(&seconds)
	if err != nil {
		return 0, fmt.Errorf("db: querying live_view_monthly_quota_seconds: %w", err)
	}
	return seconds, nil
}

// GetTenantStatus returns tenants.status. It is used by the on_publish hook
// for GT06 video (Jimi IoT JC261/JC400, see gt06videobridge/protocol.go)
// because that path goes through no user session/JWT: the device pushes the
// stream on its own, fully asynchronously from any API request (seconds, or
// minutes if the device reconnects by itself after a network drop, after
// "request_video" was sent). The rest of the video pipeline (0x9101/
// gt06-video) inherits the active-tenant check from assert_session_active
// (api/app/deps.py) BEFORE calling this process. != "active" covers both
// suspended and cancelled, matching the API-side rule.
func GetTenantStatus(ctx context.Context, tx pgx.Tx, tenantID uuid.UUID) (string, error) {
	var status string
	err := tx.QueryRow(ctx, `SELECT status::text FROM tenants WHERE id = $1`, tenantID).Scan(&status)
	if err != nil {
		return "", fmt.Errorf("db: querying tenant status: %w", err)
	}
	return status, nil
}

// GetTenantLiveViewSecondsConsumedThisMonth sums the real duration
// (metadata->>'duration_s', see videobridge/dispatcher.go handleFlowReport)
// of the tenant's live_view sessions since day 1 of the current calendar
// month in UTC. It reads usage_events_v (the security_barrier view), NEVER
// the usage_events hypertable directly: a direct GRANT on a hypertable
// bypasses RLS via its physical chunks (see
// infra/postgres/migrations/0009_timeseries_access.sql).
func GetTenantLiveViewSecondsConsumedThisMonth(ctx context.Context, tx pgx.Tx, tenantID uuid.UUID) (int, error) {
	now := time.Now().UTC()
	monthStart := time.Date(now.Year(), now.Month(), 1, 0, 0, 0, 0, time.UTC)

	var seconds int
	err := tx.QueryRow(ctx, `
		SELECT COALESCE(SUM(NULLIF(metadata->>'duration_s', '')::int), 0)
		FROM usage_events_v
		WHERE tenant_id = $1 AND event_type = 'live_view' AND "time" >= $2
	`, tenantID, monthStart).Scan(&seconds)
	if err != nil {
		return 0, fmt.Errorf("db: summing monthly live view usage: %w", err)
	}
	return seconds, nil
}
