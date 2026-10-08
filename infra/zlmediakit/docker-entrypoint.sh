#!/bin/sh
# Replaces the __ZLM_API_SECRET__ placeholder in the mounted config.ini (the
# secret is never versioned; same pattern as
# infra/postgres/migrations/0010_set_role_passwords.sh) and starts MediaServer.
set -eu

if [ -z "${ZLM_API_SECRET:-}" ]; then
    echo "docker-entrypoint.sh: ZLM_API_SECRET is not set, aborting." >&2
    exit 1
fi

# Defaults to 1 (local dev, useful for debugging). Set to 0 in production via
# ZLM_API_DEBUG (docker-compose.yml): apiDebug=1 logs full playback tickets.
ZLM_API_DEBUG="${ZLM_API_DEBUG:-1}"

RUNTIME_CONF=/opt/media/conf/config.runtime.ini
sed -e "s/__ZLM_API_SECRET__/${ZLM_API_SECRET}/" -e "s/__ZLM_API_DEBUG__/${ZLM_API_DEBUG}/" /opt/media/conf/config.ini > "$RUNTIME_CONF"

cd /opt/media/bin
exec ./MediaServer -s default.pem -c "$RUNTIME_CONF" -l 0
