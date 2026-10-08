package videobridge

import (
	"context"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
)

// ResolveTenantVideoLimits resolves tenant_id -> per-session limit
// (max_live_view_seconds) and remaining monthly quota
// (live_view_monthly_quota_seconds minus this month's real consumption, see
// db.GetTenantLiveViewSecondsConsumedThisMonth). Protocol-agnostic by
// design: how the tenant_id was obtained is Protocol.LookupDevice's job (see
// TenantVideoLimits below).
func ResolveTenantVideoLimits(ctx context.Context, tx pgx.Tx, tenantID uuid.UUID) (maxSeconds int, quotaRemainingSeconds int, err error) {
	maxSeconds, err = db.GetTenantLiveViewLimit(ctx, tx, tenantID)
	if err != nil {
		return 0, 0, err
	}
	quota, err := db.GetTenantLiveViewQuotaSeconds(ctx, tx, tenantID)
	if err != nil {
		return 0, 0, err
	}
	consumed, err := db.GetTenantLiveViewSecondsConsumedThisMonth(ctx, tx, tenantID)
	if err != nil {
		return 0, 0, err
	}
	return maxSeconds, quota - consumed, nil
}

// TenantVideoLimits resolves deviceKey -> tenant -> limits using the given
// Protocol's LookupDevice (each protocol resolves its own identifier without
// this package knowing the format). The remaining quota also subtracts
// open-session time the central meter has not written yet (see LiveMeter):
// with two cameras open, requesting a third sees the real balance, not the
// one from before they were opened.
func TenantVideoLimits(ctx context.Context, pool *pgxpool.Pool, meter *LiveMeter, proto Protocol, deviceKey string) (maxSeconds int, quotaRemainingSeconds int, err error) {
	var tenantID uuid.UUID
	err = db.WithBypass(ctx, pool, func(ctx context.Context, tx pgx.Tx) error {
		dev, err := proto.LookupDevice(ctx, tx, deviceKey)
		if err != nil {
			return err
		}
		tenantID = dev.TenantID
		maxSeconds, quotaRemainingSeconds, err = ResolveTenantVideoLimits(ctx, tx, dev.TenantID)
		return err
	})
	if err == nil {
		unflushed, _ := meter.Unflushed(tenantID)
		quotaRemainingSeconds -= unflushed
	}
	return maxSeconds, quotaRemainingSeconds, err
}
