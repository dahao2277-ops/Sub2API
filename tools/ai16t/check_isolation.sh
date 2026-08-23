#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
compose_file="$repo_root/deploy/ai16t-sub2api/compose.sandbox.yml"

required_markers=(
  "name: sub2api-ai16t-mac1"
  "  sub2api:"
  "  commercial-core:"
  "  mock-provider:"
  "  postgres:"
  "  redis:"
  "127.0.0.1:"
  "sub2api-ai16t-mac1-private"
  "sub2api-ai16t-mac1-loopback-ingress"
  "sub2api-ai16t-mac1-app-data"
  "sub2api-ai16t-mac1-postgres-data"
  "sub2api-ai16t-mac1-redis-data"
  "sub2api-ai16t-mac1-postgres-backup"
  "sub2api-ai16t-mac1-core-data"
  "internal: true"
  "POSTGRES_PASSWORD_FILE: /run/ai16t/database_password"
  "AI16T_ISOLATED_TEST_HOOKS_ENABLED: \"true\""
)

for marker in "${required_markers[@]}"; do
  if ! grep -Fq "$marker" "$compose_file"; then
    echo "ISOLATION_CHECK=FAIL missing=$marker" >&2
    exit 1
  fi
done

if grep -Fq -- "- .runtime/hybrid.env" "$compose_file"; then
  echo "ISOLATION_CHECK=FAIL shared_runtime_env_exposed_to_service" >&2
  exit 1
fi

if [ "$(grep -Fc -- '- "127.0.0.1:${AI16T_SUB2API_PORT:-18080}:8080"' "$compose_file")" -ne 1 ]; then
  echo "ISOLATION_CHECK=FAIL unexpected_host_port_count" >&2
  exit 1
fi

for forbidden in "ai16t.com" "token-platform-v1" "platform_a"; do
  if grep -Fiq "$forbidden" "$compose_file"; then
    echo "ISOLATION_CHECK=FAIL forbidden=$forbidden" >&2
    exit 1
  fi
done

echo "ISOLATION_CHECK=PASS"
