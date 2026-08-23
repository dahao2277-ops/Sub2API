#!/usr/bin/env bash
set -euo pipefail

# Backward-compatible entrypoint. The canonical checker deliberately uses
# GitHub API and git ls-remote instead of mutating local refs with git fetch.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$repo_root/tools/ai16t/check_upstream_status.sh"
