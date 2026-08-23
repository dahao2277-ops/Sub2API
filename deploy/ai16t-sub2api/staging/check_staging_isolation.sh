#!/usr/bin/env bash
set -euo pipefail

staging_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
compose_file="$staging_root/compose.staging.yml"
runtime_env="$staging_root/.runtime/staging.env"

required=(
  "name: sub2api-ai16t-mac1-staging"
  "sub2api-ai16t-mac1-staging-private"
  "sub2api-ai16t-mac1-staging-loopback"
  "127.0.0.1:\${STAGING_BLUE_PORT:-18181}:8080"
  "127.0.0.1:\${STAGING_GREEN_PORT:-18182}:8080"
  "AI16T_ISOLATED_TEST_MODE: \"false\""
  "AI16T_ISOLATED_TEST_HOOKS_ENABLED: \"false\""
  "POSTGRES_PASSWORD_FILE: /run/ai16t/database_password"
  "internal: true"
)

for marker in "${required[@]}"; do
  grep -Fq "$marker" "$compose_file" || {
    echo "STAGING_ISOLATION=FAIL missing=$marker" >&2
    exit 1
  }
done

for forbidden in "ai16t.com" "token-platform-v1" "platform_a" ".runtime/hybrid.env"; do
  if grep -Fiq "$forbidden" "$compose_file"; then
    echo "STAGING_ISOLATION=FAIL forbidden=$forbidden" >&2
    exit 1
  fi
done

if [ -f "$runtime_env" ]; then
  docker compose --env-file "$runtime_env" -f "$compose_file" config --quiet
fi

echo "STAGING_ISOLATION=PASS"
