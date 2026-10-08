#!/usr/bin/env bash
# Creates infra/.env from infra/.env.example and fills every required secret
# with a fresh random value. Refuses to overwrite an existing .env.
#
#   ./infra/init-env.sh
#
# Review the generated file before exposing anything beyond 127.0.0.1
# (PUBLIC_BIND_HOST, public URLs, storage credentials).
set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
example="$dir/.env.example"
target="$dir/.env"

if [ -e "$target" ]; then
  echo "init-env: $target already exists, not overwriting it" >&2
  exit 1
fi

rand() {
  # 32 random bytes as hex: no characters that need quoting in a .env file.
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

required="POSTGRES_SUPERUSER_PASSWORD APP_USER_PASSWORD ZLM_API_SECRET JWT_SECRET API_KEY_PEPPER"

cp "$example" "$target"
chmod 600 "$target" 2>/dev/null || true
for key in $required; do
  value="$(rand)"
  if grep -q "^${key}=$" "$target"; then
    sed -i.bak "s|^${key}=$|${key}=${value}|" "$target"
  elif ! grep -q "^${key}=" "$target"; then
    echo "${key}=${value}" >> "$target"
  fi
done
rm -f "$target.bak"

echo "init-env: wrote $target with random secrets"
