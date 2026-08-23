#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
expected_fetch="https://github.com/Wei-Shaw/sub2api.git"
baseline_tag="v0.1.179"
baseline_commit="75f88be5f75c27771836b586f7de1503afa0e3bc"

cd "$repo_root"

actual_fetch="$(git remote get-url upstream)"
actual_push="$(git remote get-url --push upstream)"
head_commit="$(git rev-parse HEAD)"
tag_commit="$(git rev-list -n 1 "$baseline_tag")"

[[ "$actual_fetch" == "$expected_fetch" ]] || {
  echo "UPSTREAM_CHECK=FAIL unexpected_fetch_url" >&2
  exit 1
}
[[ "$actual_push" == "DISABLED" ]] || {
  echo "UPSTREAM_CHECK=FAIL push_not_disabled" >&2
  exit 1
}
[[ "$tag_commit" == "$baseline_commit" ]] || {
  echo "UPSTREAM_CHECK=FAIL baseline_tag_mismatch" >&2
  exit 1
}

latest_tag="$(gh api repos/Wei-Shaw/sub2api/releases/latest --jq .tag_name)"
latest_release_commit="$(git ls-remote "$expected_fetch" "refs/tags/$latest_tag^{}" | awk 'NR==1 {print $1}')"
if [[ -z "$latest_release_commit" ]]; then
  latest_release_commit="$(git ls-remote "$expected_fetch" "refs/tags/$latest_tag" | awk 'NR==1 {print $1}')"
fi
upstream_main="$(git ls-remote "$expected_fetch" refs/heads/main | awk 'NR==1 {print $1}')"
local_upstream_main="$(git rev-parse upstream/main)"

echo "BASELINE_TAG=$baseline_tag"
echo "BASELINE_COMMIT=$baseline_commit"
echo "WORKTREE_HEAD=$head_commit"
echo "LATEST_RELEASE=$latest_tag"
echo "LATEST_RELEASE_COMMIT=$latest_release_commit"
echo "UPSTREAM_MAIN=$upstream_main"
echo "LOCAL_UPSTREAM_MAIN=$local_upstream_main"
if [[ "$local_upstream_main" != "$upstream_main" ]]; then
  echo "UPSTREAM_CHECK=UNKNOWN_STALE_LOCAL_REF" >&2
  exit 1
fi
if [[ "$latest_tag" == "$baseline_tag" ]]; then
  echo "RELEASE_STATUS=BASELINE_IS_LATEST_RELEASE"
else
  echo "RELEASE_STATUS=UPDATE_AVAILABLE"
fi
if git remote get-url origin >/dev/null 2>&1; then
  echo "ORIGIN_STATUS=CONFIGURED"
else
  echo "ORIGIN_STATUS=OWNER_NAMESPACE_REQUIRED"
fi
echo "SECURITY_ADVISORIES_BEGIN"
gh api repos/Wei-Shaw/sub2api/security-advisories --paginate \
  --jq '.[] | [.ghsa_id, .severity, .vulnerabilities[0].vulnerable_version_range, .vulnerabilities[0].patched_versions, .html_url] | @tsv'
echo "SECURITY_ADVISORIES_END"
echo "MIGRATIONS_AFTER_BASELINE_BEGIN"
git diff --name-only "$baseline_commit..upstream/main" -- backend/migrations
echo "MIGRATIONS_AFTER_BASELINE_END"
echo "UPSTREAM_CHECK=PASS"
