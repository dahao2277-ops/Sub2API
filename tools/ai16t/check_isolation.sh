#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
compose_file="$repo_root/deploy/ai16t-sub2api/compose.sandbox.yml"

required_markers=(
  "name: ai16t-sub2api-sandbox"
  "127.0.0.1:"
  "ai16t-sub2api-sandbox-postgres-data"
  "ai16t-sub2api-sandbox-redis-data"
  "ai16t-sub2api-sandbox-postgres-backup"
  "internal: true"
)

for marker in "${required_markers[@]}"; do
  if ! grep -Fq "$marker" "$compose_file"; then
    echo "ISOLATION_CHECK=FAIL missing=$marker" >&2
    exit 1
  fi
done

for forbidden in "ai16t.com" "token-platform" "platform_a"; do
  if grep -Fiq "$forbidden" "$compose_file"; then
    echo "ISOLATION_CHECK=FAIL forbidden=$forbidden" >&2
    exit 1
  fi
done

echo "ISOLATION_CHECK=PASS"
