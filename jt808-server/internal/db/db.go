// Package db is the only layer of the device server that talks to Postgres.
// All other code goes through here -- SQL is never built by hand in another
// package, and device data is never concatenated into a query.
package db

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/openmdvr/openmdvr/jt808-server/internal/config"
)

// NewPool creates the connection pool. It connects with app_user credentials
// set field by field (not an fmt.Sprintf-interpolated DSN) so it does not
// depend on how libpq escapes spaces/quotes inside the password.
func NewPool(ctx context.Context, cfg config.Config) (*pgxpool.Pool, error) {
	poolCfg, err := pgxpool.ParseConfig("")
	if err != nil {
		return nil, fmt.Errorf("db: parsing base config: %w", err)
	}
	poolCfg.ConnConfig.Host = cfg.PGHost
	poolCfg.ConnConfig.Port = mustParsePort(cfg.PGPort)
	poolCfg.ConnConfig.Database = cfg.PGDatabase
	poolCfg.ConnConfig.User = cfg.PGUser
	poolCfg.ConnConfig.Password = cfg.PGPassword
	if cfg.PGMaxConns > 0 {
		poolCfg.MaxConns = int32(cfg.PGMaxConns)
	}

	pool, err := pgxpool.NewWithConfig(ctx, poolCfg)
	if err != nil {
		return nil, fmt.Errorf("db: creating pool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("db: initial ping failed: %w", err)
	}
	return pool, nil
}

// WithBypass runs fn inside a transaction with app.bypass_rls='true'.
//
// The device server is a trusted service (not a user session with a JWT): it
// resolves which tenant each message belongs to by looking up
// devices.tenant_id from the identifier of the connected device, a value
// never signed or verified outside our own database. So every operation uses
// bypass instead of setting app.tenant_id up front -- it would be the wrong
// tenant until the devices lookup resolved it. Tenant/device consistency
// triggers (0007_timeseries_tables.sql) and database CHECKs still apply:
// bypass skips RLS policies, not schema integrity rules.
//
// set_config is ALWAYS called with is_local=true (third parameter),
// transaction scope -- as required by the contract documented in
// infra/postgres/migrations/0003_rls_helpers.sql for any client using the
// app_user connection pool.
func WithBypass(ctx context.Context, pool *pgxpool.Pool, fn func(context.Context, pgx.Tx) error) error {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("db: begin: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	if _, err := tx.Exec(ctx, "SELECT set_config('app.bypass_rls', 'true', true)"); err != nil {
		return fmt.Errorf("db: setting bypass_rls: %w", err)
	}
	if err := fn(ctx, tx); err != nil {
		return err
	}
	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("db: commit: %w", err)
	}
	return nil
}

func mustParsePort(s string) uint16 {
	var port uint16
	_, err := fmt.Sscanf(s, "%d", &port)
	if err != nil || port == 0 {
		return 5432
	}
	return port
}
