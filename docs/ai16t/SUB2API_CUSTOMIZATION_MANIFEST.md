# SUB2API_CUSTOMIZATION_MANIFEST

Purpose: keep ai16t customization small and easy to merge with upstream.

| Upstream area | Current change | Why | Merge conflict risk | Move to extension? | Upstream update handling |
| --- | --- | --- | --- | --- | --- |
| `backend/internal/ai16t/*` | New isolated Sub2APIAdapter boundary | Coordinates reserve, settle, refund, secret resolution, and projection drift on the Sub2API side | Low | Already isolated | Re-run Go tests and compare contract with Core |
| `backend/internal/ai16t/coreadapter/*` | New isolated AI16T Core contract and fail-closed skeleton | Defines Ledger authority, idempotency, secret provider, and projection drift boundary | Low | Already isolated | Re-run Go tests and compare contract with Core |
| `docs/ai16t/*` | New AI16T platform, hardening, pipeline, staging, and report docs | Owner-facing and team-facing delivery control | Low | Not needed | Keep as local fork documentation |
| `tools/ai16t/check_sub2api_upstream.sh` | Read-only upstream release/advisory checker | Prevent auto-upgrade while preserving official update awareness | Low | Not needed | Run before each upstream-sync branch |

## Do Not Modify Directly Without Review

- `LICENSE`
- upstream copyright or notice files
- upstream README license/attribution sections
- production deployment scripts
- payment webhook handlers
- database migrations
- gateway billing path

## Future Manifest Entries

Every future Bot2/Codex change must add:

- upstream file
- what changed
- why
- merge conflict risk
- whether it can move to extension
- official update handling
