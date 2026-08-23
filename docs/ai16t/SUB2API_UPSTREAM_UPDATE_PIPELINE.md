# SUB2API_UPSTREAM_UPDATE_PIPELINE

Status: Accepted.

## Policy

Official releases must never auto-upgrade production.

## Flow

1. Detect new upstream release.
2. Read release notes.
3. Check official security advisories.
4. Inspect migration diff.
5. Create `upstream-sync/<date>-<version>` branch.
6. Merge or rebase upstream in an isolated worktree.
7. Resolve conflicts.
8. Run unit tests.
9. Run frontend tests.
10. Run security scan.
11. Run Hybrid E2E.
12. Run database clone migration.
13. Deploy to staging.
14. Canary.
15. Blue/Green production.

## Current Baseline

- locked release: v0.1.179
- locked commit: 75f88be5f75c27771836b586f7de1503afa0e3bc
- latest upstream main checked during this task: d45135d87df16d48637f04ccd245727bc955ba54
- state: UPDATE_AVAILABLE on main, no auto-upgrade

## Migration Gate

v0.1.179 release notes include migrations 226, 227, and 228 plus a long-context billing behavior change. Any production adoption must run backup, DB clone migration, compatibility, rollback test, staging, then production migration.

If migration breaks old-version compatibility, report MAINTENANCE_WINDOW_MAY_BE_REQUIRED.
