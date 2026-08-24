#!/bin/sh
set -eu

root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
runtime="$root/.runtime"
domain="${AI99T_DOMAIN:-ai99t.com}"
admin_email="${AI99T_ADMIN_EMAIL:-owner@ai99t.com}"
core_source="${AI16T_CORE_SOURCE_DIR:-/srv/ai99t/current/commercial-core}"

case "$domain" in
  ai99t.com) ;;
  *) echo "Refusing unexpected public domain" >&2; exit 1 ;;
esac

umask 077
mkdir -p "$runtime/backups"
chmod 0700 "$runtime" "$runtime/backups"

generate_hex_secret() {
  target="$1"
  if [ ! -f "$target" ]; then
    openssl rand -hex 32 > "$target"
  fi
  chmod 0600 "$target"
}

generate_password_secret() {
  target="$1"
  if [ ! -f "$target" ]; then
    openssl rand -base64 36 | tr -d '\n' | tr '/+' '_-' > "$target"
    printf '\n' >> "$target"
  fi
  chmod 0600 "$target"
}

generate_hex_secret "$runtime/core_signing_key"
generate_hex_secret "$runtime/fingerprint_key"
generate_hex_secret "$runtime/mock_provider_key"
generate_hex_secret "$runtime/database_password"
generate_hex_secret "$runtime/redis_password"
generate_hex_secret "$runtime/jwt_secret"
generate_hex_secret "$runtime/totp_encryption_key"
generate_password_secret "$runtime/admin_password"

database_password="$(tr -d '\n' < "$runtime/database_password")"
redis_password="$(tr -d '\n' < "$runtime/redis_password")"
admin_password="$(tr -d '\n' < "$runtime/admin_password")"
jwt_secret="$(tr -d '\n' < "$runtime/jwt_secret")"
totp_key="$(tr -d '\n' < "$runtime/totp_encryption_key")"

{
  echo "DATABASE_PASSWORD=$database_password"
  echo "REDIS_PASSWORD=$redis_password"
  echo "ADMIN_EMAIL=$admin_email"
  echo "ADMIN_PASSWORD=$admin_password"
  echo "JWT_SECRET=$jwt_secret"
  echo "TOTP_ENCRYPTION_KEY=$totp_key"
} > "$runtime/sub2api.env"

release_commit="${SUB2API_RELEASE_COMMIT:-}"
if [ -z "$release_commit" ]; then
  release_commit="$(git -C "$root/../../.." rev-parse HEAD)"
fi
printf '%s' "$release_commit" | grep -Eq '^[0-9a-f]{40}$' || {
  echo "SUB2API_RELEASE_COMMIT must be a full Git commit SHA" >&2
  exit 1
}
release_tag="$(printf '%s' "$release_commit" | cut -c1-12)"
{
  echo "AI99T_DOMAIN=$domain"
  echo "AI99T_ACME_EMAIL=$admin_email"
  echo "AI99T_ACTIVE_UPSTREAM=sub2api-blue:8080"
  echo "SUB2API_RELEASE_COMMIT=$release_commit"
  echo "SUB2API_IMAGE_TAG=$release_tag"
  echo "AI16T_CORE_SOURCE_DIR=$core_source"
  echo "BLUE_LOOPBACK_PORT=18181"
  echo "GREEN_LOOPBACK_PORT=18182"
} > "$runtime/public.env"

unset database_password redis_password admin_password jwt_secret totp_key
chmod 0600 \
  "$runtime/core_signing_key" \
  "$runtime/fingerprint_key" \
  "$runtime/mock_provider_key" \
  "$runtime/database_password" \
  "$runtime/redis_password" \
  "$runtime/jwt_secret" \
  "$runtime/totp_encryption_key" \
  "$runtime/admin_password" \
  "$runtime/sub2api.env" \
  "$runtime/public.env"
chmod 0700 "$runtime" "$runtime/backups"
echo "PUBLIC_SANDBOX_RUNTIME_READY"
