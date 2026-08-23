#!/bin/sh
set -eu

expected_worktree="/Users/hhhh/AI-Worktrees/Sub2API/hybrid-v1"
expected_core="/Users/hhhh/Projects/ai16t-commercial-core"
expected_core_revision="56108397f31a97926f33d28420e9b44a52547bd2"
expected_core_tree="6057218ef7291f5d9fb493e8fb7b02fa059ef049"
repo_root="$(git rev-parse --show-toplevel)"
staging_root="$repo_root/deploy/ai16t-sub2api/staging"
runtime_dir="$staging_root/.runtime"

[ "$repo_root" = "$expected_worktree" ] || {
  echo "Refusing unexpected Worktree" >&2
  exit 1
}
[ "$(git -C "$expected_core" rev-parse HEAD)" = "$expected_core_revision" ] || {
  echo "Commercial Core revision mismatch" >&2
  exit 1
}
[ "$(git -C "$expected_core" rev-parse HEAD^{tree})" = "$expected_core_tree" ] || {
  echo "Commercial Core tree mismatch" >&2
  exit 1
}
[ -z "$(git -C "$expected_core" status --porcelain --untracked-files=no)" ] || {
  echo "Commercial Core tracked worktree is dirty" >&2
  exit 1
}

umask 077
mkdir -p "$runtime_dir/backups"
chmod 0700 "$runtime_dir" "$runtime_dir/backups"

generate_secret() {
  path="$1"
  if [ ! -f "$path" ]; then
    openssl rand -hex 32 > "$path"
  fi
  chmod 0600 "$path"
}

generate_secret "$runtime_dir/core_signing_key"
generate_secret "$runtime_dir/fingerprint_key"
generate_secret "$runtime_dir/mock_provider_key"
generate_secret "$runtime_dir/database_password"
generate_secret "$runtime_dir/redis_password"

if [ ! -f "$runtime_dir/sub2api.env" ]; then
  database_password="$(tr -d '\n' < "$runtime_dir/database_password")"
  redis_password="$(tr -d '\n' < "$runtime_dir/redis_password")"
  admin_password="$(openssl rand -base64 30 | tr -d '\n' | tr '/+' '_-')"
  jwt_secret="$(openssl rand -hex 32)"
  totp_key="$(openssl rand -hex 32)"
  {
    echo "DATABASE_PASSWORD=$database_password"
    echo "REDIS_PASSWORD=$redis_password"
    echo "ADMIN_EMAIL=admin@staging.sub2api.local"
    echo "ADMIN_PASSWORD=$admin_password"
    echo "JWT_SECRET=$jwt_secret"
    echo "TOTP_ENCRYPTION_KEY=$totp_key"
  } > "$runtime_dir/sub2api.env"
  unset database_password redis_password admin_password jwt_secret totp_key
fi

head_revision="$(git rev-parse --short=12 HEAD)"
{
  echo "SUB2API_IMAGE_TAG=$head_revision"
  echo "STAGING_BLUE_PORT=18181"
  echo "STAGING_GREEN_PORT=18182"
} > "$runtime_dir/staging.env"
chmod 0600 \
  "$runtime_dir/core_signing_key" \
  "$runtime_dir/fingerprint_key" \
  "$runtime_dir/mock_provider_key" \
  "$runtime_dir/database_password" \
  "$runtime_dir/redis_password" \
  "$runtime_dir/sub2api.env" \
  "$runtime_dir/staging.env"
chmod 0700 "$runtime_dir" "$runtime_dir/backups"

echo "STAGING_RUNTIME_READY"
