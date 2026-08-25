#!/bin/sh
set -eu

root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
runtime="$root/.runtime"
domain="${AI99T_DOMAIN:-ai99t.com}"
admin_email="${AI99T_ADMIN_EMAIL:-owner@ai99t.com}"
core_source="${AI16T_CORE_SOURCE_DIR:-/srv/ai99t/current/commercial-core}"
secret_root="${AI99T_SECRET_ROOT:-/srv/ai99t/shared/apiyi-secret-provider}"
drift_root="${AI99T_DRIFT_DATA_DIR:-/srv/ai99t/shared/ai16t-drift}"
provider_mode="${AI16T_PROVIDER_MODE:-mock}"

case "$domain" in
  ai99t.com) ;;
  *) echo "Refusing unexpected public domain" >&2; exit 1 ;;
esac

umask 077
mkdir -p "$runtime/backups"
chmod 0700 "$runtime" "$runtime/backups"
mkdir -p "$secret_root/data" "$secret_root/run" "$drift_root"
chmod 0700 "$secret_root" "$secret_root/data" "$secret_root/run" "$drift_root"

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
# Sub2API and Commercial Core run as different non-root UIDs.  Keep separate
# mode-0600 bind-mount files with the same signing value so neither service has
# to receive world-readable key material.
cp "$runtime/core_signing_key" "$runtime/core_signing_key_sub2api"
generate_hex_secret "$runtime/fingerprint_key"
generate_hex_secret "$runtime/mock_provider_key"
generate_hex_secret "$runtime/database_password"
generate_hex_secret "$runtime/redis_password"
generate_hex_secret "$runtime/jwt_secret"
generate_hex_secret "$runtime/totp_encryption_key"
generate_password_secret "$runtime/admin_password"

if [ ! -f "$secret_root/master_key" ]; then
  openssl rand 32 > "$secret_root/master_key"
fi
chmod 0600 "$secret_root/master_key"

if [ ! -f "$runtime/apiyi_model_config_core.json" ]; then
  printf '%s\n' '{"models":[{"model":"gpt-4o-mini","upstream_model":"gpt-4o-mini","input_per_million_micro":10000000,"output_per_million_micro":10000000,"cached_per_million_micro":5000000,"customer_input_per_million_micro":15000000,"customer_output_per_million_micro":15000000,"customer_cached_per_million_micro":7500000}]}' > "$runtime/apiyi_model_config_core.json"
fi
cp "$runtime/apiyi_model_config_core.json" "$runtime/apiyi_model_config_sub2api.json"
chmod 0600 "$runtime/apiyi_model_config_core.json" "$runtime/apiyi_model_config_sub2api.json"

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
core_release_commit="${AI16T_CORE_RELEASE_COMMIT:-}"
if [ -z "$core_release_commit" ]; then
  core_release_commit="$(git -C "$core_source" rev-parse HEAD)"
fi
printf '%s' "$core_release_commit" | grep -Eq '^[0-9a-f]{40}$' || {
  echo "AI16T_CORE_RELEASE_COMMIT must be a full Git commit SHA" >&2
  exit 1
}
core_release_tree="${AI16T_CORE_RELEASE_TREE:-}"
if [ -z "$core_release_tree" ]; then
  core_release_tree="$(git -C "$core_source" rev-parse 'HEAD^{tree}')"
fi
printf '%s' "$core_release_tree" | grep -Eq '^[0-9a-f]{40}$' || {
  echo "AI16T_CORE_RELEASE_TREE must be a full Git tree SHA" >&2
  exit 1
}
core_image_tag="$(printf '%s' "$core_release_commit" | cut -c1-12)"
# Blue must be an already-built immutable stable artifact. Never rebuild or
# overlay candidate bridge files onto it during a Green release.
core_blue_image="${AI16T_CORE_BLUE_IMAGE:-}"
printf '%s' "$core_blue_image" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}$' || {
  echo "AI16T_CORE_BLUE_IMAGE must name the prior stable image by immutable sha256 digest" >&2
  exit 1
}
core_green_image="${AI16T_CORE_GREEN_IMAGE:-ai99t/commercial-core-runtime:green-${core_image_tag}-${release_tag}}"
egress_ip="${AI99T_EXPECTED_EGRESS_IP:-}"
case "$provider_mode" in
  mock)
    core_runtime_mode="isolated-test"
    core_test_mode="mock-only-enabled"
    ;;
  apiyi)
    core_runtime_mode="normal"
    core_test_mode="disabled"
    printf '%s' "$egress_ip" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' || {
      echo "AI99T_EXPECTED_EGRESS_IP must be set for APIYI mode" >&2
      exit 1
    }
    ;;
  *) echo "Unsupported AI16T provider mode" >&2; exit 1 ;;
esac
{
  echo "AI99T_DOMAIN=$domain"
  echo "AI99T_ACME_EMAIL=$admin_email"
  echo "AI99T_ACTIVE_UPSTREAM=sub2api-blue:8080"
  echo "AI16T_ACTIVE_CORE_URL=http://commercial-core:8787"
  echo "SUB2API_RELEASE_COMMIT=$release_commit"
  echo "SUB2API_IMAGE_TAG=$release_tag"
  echo "AI16T_CORE_SOURCE_DIR=$core_source"
  echo "AI16T_CORE_RELEASE_COMMIT=$core_release_commit"
  echo "AI16T_CORE_RELEASE_TREE=$core_release_tree"
  echo "AI16T_CORE_IMAGE_TAG=$core_image_tag"
  echo "AI16T_CORE_BLUE_IMAGE=$core_blue_image"
  echo "AI16T_CORE_GREEN_IMAGE=$core_green_image"
  echo "AI16T_PROVIDER_MODE=$provider_mode"
  echo "AI16T_CORE_RUNTIME_MODE=$core_runtime_mode"
  echo "AI16T_CORE_TEST_MODE=$core_test_mode"
  echo "AI16T_PROVIDER_SECRET_REF=${AI16T_PROVIDER_SECRET_REF:-apiyi/prod-canary}"
  echo "AI16T_CANARY_USER_REFERENCES=${AI16T_CANARY_USER_REFERENCES:-}"
  echo "AI16T_INITIAL_CREDIT_MICRO=${AI16T_INITIAL_CREDIT_MICRO:-200000}"
  echo "AI99T_EXPECTED_EGRESS_IP=$egress_ip"
  echo "AI99T_SECRET_DATA_DIR=$secret_root/data"
  echo "AI99T_SECRET_RUN_DIR=$secret_root/run"
  echo "AI99T_SECRET_MASTER_KEY_FILE=$secret_root/master_key"
  echo "AI99T_DRIFT_DATA_DIR=$drift_root"
  echo "AI99T_MODEL_CONFIG_CORE_FILE=$runtime/apiyi_model_config_core.json"
  echo "AI99T_MODEL_CONFIG_SUB2API_FILE=$runtime/apiyi_model_config_sub2api.json"
  echo "BLUE_LOOPBACK_PORT=18181"
  echo "GREEN_LOOPBACK_PORT=18182"
} > "$runtime/public.env"

unset database_password redis_password admin_password jwt_secret totp_key
chmod 0600 \
  "$runtime/core_signing_key" \
  "$runtime/core_signing_key_sub2api" \
  "$runtime/fingerprint_key" \
  "$runtime/mock_provider_key" \
  "$runtime/database_password" \
  "$runtime/redis_password" \
  "$runtime/jwt_secret" \
  "$runtime/totp_encryption_key" \
  "$runtime/admin_password" \
  "$runtime/sub2api.env" \
  "$runtime/public.env"
if [ "$(id -u)" -eq 0 ]; then
  chown 65532:65532 "$runtime/core_signing_key" "$runtime/mock_provider_key"
  chown 65532:65532 "$runtime/apiyi_model_config_core.json"
  chown 1000:1000 "$runtime/core_signing_key_sub2api" "$runtime/fingerprint_key"
  chown 1000:1000 "$runtime/apiyi_model_config_sub2api.json"
  chown -R 65532:65532 "$secret_root"
  chown -R 1000:1000 "$drift_root"
fi
chmod 0700 "$runtime" "$runtime/backups"
echo "PUBLIC_SANDBOX_RUNTIME_READY"
