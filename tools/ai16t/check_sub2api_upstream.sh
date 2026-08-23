#!/usr/bin/env bash
set -euo pipefail

repo="${1:-Wei-Shaw/sub2api}"
baseline="${2:-75f88be5f75c27771836b586f7de1503afa0e3bc}"

echo "repo=$repo"
echo "baseline=$baseline"

echo "latest_release:"
gh release list --repo "$repo" --limit 1

echo "security_advisories:"
gh api "repos/$repo/security-advisories" --paginate \
  --jq '.[] | [.ghsa_id, .severity, .vulnerabilities[0].vulnerable_version_range, .vulnerabilities[0].patched_versions, .html_url] | @tsv'

echo "upstream_main:"
git fetch upstream --tags --prune >/dev/null
git rev-parse upstream/main

echo "main_commits_after_baseline:"
git log --oneline "$baseline..upstream/main" | sed -n '1,40p'

echo "migration_files_after_baseline:"
git diff --name-only "$baseline..upstream/main" -- backend/migrations
