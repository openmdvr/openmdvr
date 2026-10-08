#!/bin/bash
# Applies pending migrations from ./migrations against an already running
# database. Runs as its own docker-compose service (`migrate`, see
# infra/docker-compose.yml) on EVERY `docker compose up`, both in local dev and
# on every production deploy.
#
# Why it exists: applying migrations by hand is error-prone. If application
# code is deployed automatically but its migrations are not applied, the live
# schema falls behind the running code and endpoints that select new columns
# fail with 500 for every session.
#
# Why `docker-entrypoint-initdb.d` is NOT enough: those scripts ONLY run the
# FIRST time a completely empty data volume starts, never on a redeploy over
# an already initialized database (exactly the production case and any
# persistent dev environment). This script replaces that bind mount entirely
# (see docker-compose.yml) and is the SINGLE source of truth for applying the
# schema, both on a brand-new database and on one with many migrations
# already applied: each file is applied exactly once.
#
# How it knows what was applied: a `schema_migrations` table (file name ->
# applied at) that this script creates if missing. Older migrations
# (0001-0038) are mostly idempotent (CREATE ... IF NOT EXISTS, etc.), but the
# tracking table means new migrations (0039 onward) do not have to rely on
# idempotency: a plain CREATE TABLE is fine because it only runs once.
#
# .sh migrations (currently only 0010_set_role_passwords.sh) run with bash
# instead of psql -f, inheriting this script's environment
# (POSTGRES_USER/POSTGRES_DB/APP_USER_PASSWORD), the same contract they had
# under docker-entrypoint-initdb.d. Once applied and recorded they NEVER run
# again; rotating APP_USER_PASSWORD requires a NEW numbered migration, not an
# automatic re-run of this one.
set -euo pipefail

psql -v ON_ERROR_STOP=1 <<'EOSQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
EOSQL

for file in /migrations/*; do
    name=$(basename "$file")
    # psql -c does NOT interpolate :'var' variables (only when reading from a
    # script/stdin), so each query using :'name' goes through a heredoc, as
    # 0010 does.
    already=$(psql -tA -v name="$name" <<'EOSQL'
SELECT 1 FROM schema_migrations WHERE filename = :'name';
EOSQL
    )
    if [ "$already" = "1" ]; then
        continue
    fi
    echo "apply_migrations: applying $name..."
    case "$file" in
        *.sql)
            psql -v ON_ERROR_STOP=1 -f "$file"
            ;;
        *.sh)
            bash "$file"
            ;;
        *)
            echo "apply_migrations: unknown extension for $name, skipping" >&2
            continue
            ;;
    esac
    psql -v ON_ERROR_STOP=1 -v name="$name" <<'EOSQL'
INSERT INTO schema_migrations (filename) VALUES (:'name');
EOSQL
    echo "apply_migrations: $name applied"
done

echo "apply_migrations: schema up to date"
