# Secret scanning and finding classification

This repository uses Gitleaks as a fail-closed secret gate. The reviewed,
machine-readable inventory is `security/gitleaks-classifications.json`; it
contains metadata and cryptographic fingerprints only. It must never contain a
raw match, credential, authorization header, token, cookie, or private key.

## Required scopes

Run both scans for every release candidate:

1. Current tracked and non-ignored worktree files.
2. Full reachable Git history. History scanning must not be disabled.

The deployment owner must additionally scan the exact Docker build context,
final image filesystem, running container environment and logs, Nginx logs,
frontend static assets and browser responses, and the backup file inventory.
Those runtime scopes are deliberately not emulated by this repository-only
gate and remain a release blocker until their evidence is attached.

## Exact allowlist policy

`.gitleaksignore` contains only exact Gitleaks fingerprints. A fingerprint is
accepted only when the inventory also records its rule, path, line or commit,
value SHA-256 fingerprint, line SHA-256 hash, classification, owner, action and
evidence. Repository-wide, directory-wide, rule-wide, regex, and history
exclusions are prohibited.

The only classifications eligible for an exact allowlist are reviewed test
fixtures, examples, documentation, false positives, hashes, generated files,
revoked credentials with independent revocation evidence, and public OAuth
client credentials. `REAL_ACTIVE_SECRET` and `UNKNOWN` always fail closed.

Google documents that an installed application's client credential may be
embedded and that, in this context, its client secret is not treated as a
confidential secret:

<https://developers.google.com/identity/protocols/oauth2#installed>

This exception does not apply to web-server OAuth client secrets, access or
refresh tokens, API keys, service-account material, or user credentials.

## Verification

```bash
python3 tools/verify_gitleaks_classifications.py \
  --gitleaks /opt/homebrew/bin/gitleaks
python3 -m unittest tools/test_verify_gitleaks_classifications.py
```

The verifier bypasses the checked-in ignore file while collecting evidence,
uses full redaction, writes only into a mode-0700 temporary directory, and
deletes the reports on exit. It compares every live finding to the exact
reviewed metadata and fails if any new, missing, or changed fingerprint exists.

For the ordinary release gates, run Gitleaks again with the checked-in exact
ignore file enabled:

```bash
gitleaks dir --redact=100 --no-banner --exit-code 1 .
gitleaks detect --redact=100 --no-banner --exit-code 1 --source .
```

Do not paste scanner matches into tickets or logs. Report only finding IDs,
rules, paths, commit IDs and fingerprints.

## Release-owner runtime surface handoff

The following commands are auditable entry points for the release owner. They
are intentionally not run by the repository-classification worker because
Docker, production containers, logs, backups and browser state are shared or
production resources. Create every temporary directory with mode `0700`, keep
raw artifacts out of Git, run Gitleaks with `--redact=100`, and destroy the
temporary data through the approved secure-cleanup process after review.

```bash
# Exact Docker build context: materialize the same context and .dockerignore
# selection used by the release build into $RELEASE_CONTEXT_DIR first.
gitleaks dir --redact=100 --no-banner --exit-code 1 "$RELEASE_CONTEXT_DIR"

# Export the final image filesystem without starting it, then scan the export.
docker create --name "$AUDIT_CONTAINER_NAME" "$RELEASE_IMAGE_REF"
docker export "$AUDIT_CONTAINER_NAME" -o "$AUDIT_TMP_DIR/image.tar"
tar -xf "$AUDIT_TMP_DIR/image.tar" -C "$AUDIT_TMP_DIR/image-root"
gitleaks dir --redact=100 --no-banner --exit-code 1 "$AUDIT_TMP_DIR/image-root"

# Environment and log surfaces are streamed; never echo or save their content.
docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
  "$RUNNING_CONTAINER" | gitleaks detect --pipe --redact=100 --no-banner --exit-code 1
docker logs "$RUNNING_CONTAINER" 2>&1 | \
  gitleaks detect --pipe --redact=100 --no-banner --exit-code 1
gitleaks detect --pipe --redact=100 --no-banner --exit-code 1 < "$NGINX_LOG"

# Static frontend assets and an already-sanitized HAR export.
gitleaks dir --redact=100 --no-banner --exit-code 1 "$STATIC_ASSET_DIR"
gitleaks detect --pipe --redact=100 --no-banner --exit-code 1 < "$SANITIZED_HAR"
```

The release owner must separately attest that the backup inventory contains
only the expected backup objects, permissions and retention class. Do not feed
encrypted backup payloads to a scanner that would require decrypting production
secrets outside the restore-verification boundary. If any runtime surface
finds a credential, stop the release; do not copy the match into the report.
