#!/bin/bash
# Sets app_user's password from the APP_USER_PASSWORD environment variable.
# Runs as part of docker-entrypoint-initdb.d, so the container's environment is
# already available (defined in infra/docker-compose.yml from infra/.env, which
# is NEVER committed).
#
# In production the same pattern points at the environment variables injected
# by the deployment platform, never at a value in a versioned file.
#
# IMPORTANT: the password is passed to psql as a variable (-v pw=...) and
# referenced in SQL as :'pw' (psql's safe quoting), NEVER interpolated directly
# into the SQL text. With direct interpolation, a rotated secret containing a
# single quote (') could break init at best, or run arbitrary SQL as superuser
# at worst -- the kind of injection 0003_rls_helpers.sql rules out for any
# external value.
set -euo pipefail

if [ -z "${APP_USER_PASSWORD:-}" ]; then
    echo "0010_set_role_passwords.sh: APP_USER_PASSWORD is not set, aborting init." >&2
    exit 1
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v pw="$APP_USER_PASSWORD" <<-'EOSQL'
    ALTER ROLE app_user WITH PASSWORD :'pw';
EOSQL
