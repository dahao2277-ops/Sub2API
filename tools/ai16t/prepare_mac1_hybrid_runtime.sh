#!/bin/sh
set -eu

foundation_revision="9e968cb48a8866baa051f75b0d114df78b4b3f0f"
commercial_core_revision="56108397f31a97926f33d28420e9b44a52547bd2"
expected_core_tree="6057218ef7291f5d9fb493e8fb7b02fa059ef049"
repo_root="$(git rev-parse --show-toplevel)"
runtime_dir="$repo_root/deploy/ai16t-sub2api/.runtime"
core_root="${AI16T_CORE_SOURCE_DIR:-/Users/hhhh/Projects/ai16t-commercial-core}"

case "$repo_root" in
  /Users/hhhh/AI-Worktrees/Sub2API/hybrid-v1) ;;
  *) echo "Refusing unexpected worktree: $repo_root" >&2; exit 1 ;;
esac
case "$core_root" in
  /Users/hhhh/Projects/ai16t-commercial-core) ;;
  *) echo "Refusing unexpected Commercial Core path" >&2; exit 1 ;;
esac

if ! git merge-base --is-ancestor "$foundation_revision" HEAD; then
  echo "Hybrid branch is not descended from the exact Foundation authority" >&2
  exit 1
fi
if [ "$(git -C "$core_root" rev-parse HEAD)" != "$commercial_core_revision" ]; then
  echo "Commercial Core commit mismatch" >&2
  exit 1
fi
if [ "$(git -C "$core_root" rev-parse HEAD^{tree})" != "$expected_core_tree" ]; then
  echo "Commercial Core tree mismatch" >&2
  exit 1
fi
if [ -n "$(git -C "$core_root" status --porcelain --untracked-files=no)" ]; then
  echo "Commercial Core tracked worktree is dirty" >&2
  exit 1
fi
if [ "$(git remote get-url origin)" != "https://github.com/dahao2277-ops/Sub2API.git" ]; then
  echo "Personal Fork origin mismatch" >&2
  exit 1
fi
if [ "$(git remote get-url upstream)" != "https://github.com/Wei-Shaw/sub2api.git" ]; then
  echo "Official upstream mismatch" >&2
  exit 1
fi
if [ "$(git remote get-url --push upstream)" != "DISABLED" ]; then
  echo "Official upstream push is not disabled" >&2
  exit 1
fi

umask 077
mkdir -p "$runtime_dir"
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

if [ ! -f "$runtime_dir/hybrid.env" ]; then
  db_password="$(openssl rand -hex 24)"
  redis_password="$(openssl rand -hex 24)"
  admin_password="$(openssl rand -base64 30 | tr -d '\n' | tr '/+' '_-')"
  jwt_secret="$(openssl rand -hex 32)"
  totp_key="$(openssl rand -hex 32)"
  {
    echo "DATABASE_PASSWORD=$db_password"
    echo "REDIS_PASSWORD=$redis_password"
    echo "ADMIN_EMAIL=admin@sub2api.local"
    echo "ADMIN_PASSWORD=$admin_password"
    echo "JWT_SECRET=$jwt_secret"
    echo "TOTP_ENCRYPTION_KEY=$totp_key"
  } > "$runtime_dir/hybrid.env"
  unset db_password redis_password admin_password jwt_secret totp_key
fi
chmod 0600 "$runtime_dir/hybrid.env"

echo "MAC1_HYBRID_RUNTIME_READY"
