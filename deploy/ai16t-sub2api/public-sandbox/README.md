# AI99T Platform B Public Sandbox / APIYI Canary

This deployment is the isolated public sandbox for `Sub2API + AI16T Commercial Core` on `ai99t.com`.
It starts in Mock mode. APIYI Canary mode is available only after the encrypted
SecretProvider, verified model/pricing config, user allowlist, backup, and rollback gates pass.
Real payment remains disabled.

## Fixed safety boundary

- Compose project: `ai99t-platform-b-public-sandbox`.
- Public edge: Caddy on ports 80/443; Blue and Green are also reachable only from host loopback on 18181/18182.
- PostgreSQL, Redis, Commercial Core, and Mock Provider have no host ports and use an internal Docker network.
- Sub2API isolated-test request hooks are disabled. Mock mode keeps Commercial Core in isolated-test; APIYI mode forces normal runtime with all test hooks disabled.
- Public email/password registration is enabled after bootstrap. Email verification, password reset, promotions, invitation/affiliate sales, and subscription purchase remain disabled.
- No Platform A database, Redis, volume, secret, domain, container, or release is referenced.
- APIYI credentials are accepted only through the mode-0600 Unix-socket SecretProvider. PostgreSQL stores metadata (`secret_ref`, fingerprint, last4) only. Payment credentials are not accepted.

## First deployment

Run all commands from this directory on the target server:

```sh
./prepare_runtime.sh
./check_isolation.sh
docker compose --env-file .runtime/public.env -f compose.public-sandbox.yml config --quiet
docker compose --env-file .runtime/public.env -f compose.public-sandbox.yml up -d --build --wait postgres redis mock-provider secret-provider commercial-core sub2api-blue
./post_bootstrap.sh
docker compose --env-file .runtime/public.env -f compose.public-sandbox.yml up -d --build --wait sub2api-green
python3 monitor.py
python3 backup_restore_verify.py
docker compose --env-file .runtime/public.env -f compose.public-sandbox.yml up -d --wait edge
```

DNS must point `ai99t.com` and `www.ai99t.com` at the server before Caddy can complete ACME TLS issuance.

## Blue/Green switching

Commercial Core Blue and Green are different release artifacts:

- Blue is the already-tested stable image referenced by `AI16T_CORE_BLUE_IMAGE`.
  It must be resolved to an immutable digest before a Green build and must never
  be rebuilt from, or receive file overlays from, the candidate checkout.
- Green is the candidate image built with `core-runtime.Dockerfile`.
- Reject the release if Blue and Green have the same image ID, or if the Green
  revision label does not equal `AI16T_BRIDGE_RELEASE_COMMIT` exposed by
  `/version`. Keep both image IDs and revisions in the deployment evidence.

Before changing `AI16T_ACTIVE_CORE_URL`, exercise the inactive Green slot with
Mock Provider only. The mandatory gate uses the shared Ledger volume and must
show all of the following: a request is `PROCESSING` with `reserved_micro > 0`;
Green is force-stopped; Blue takes over only after lease expiry; the request is
settled once or enters `RECONCILIATION_REQUIRED`; the reservation returns to
zero; the balance invariant holds; the old owner cannot finish; and two
simultaneous requests with the same idempotency key dispatch at most once.
An idle healthcheck-only switch is not sufficient for this gate.

Safe Core switch sequence:

```sh
# Inspect both artifacts and refuse an identical image ID.
docker image inspect "$AI16T_CORE_BLUE_IMAGE" \
  --format 'blue {{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
docker image inspect "$AI16T_CORE_GREEN_IMAGE" \
  --format 'green {{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}'

# Start and verify Green without routing public traffic to it.
docker compose --env-file .runtime/public.env -f compose.public-sandbox.yml \
  up -d --no-deps --wait commercial-core-green
docker compose --env-file .runtime/public.env -f compose.public-sandbox.yml \
  exec -T commercial-core-green python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8787/version', timeout=2).read().decode())"

# After the active-reservation Mock gate passes, set the inactive Sub2API slot
# to http://commercial-core-green:8787 and recreate only that inactive slot.
```

Rollback is the reverse operation: point the inactive Sub2API slot to
`http://commercial-core:8787`, recreate only that slot, verify `/ready`, then
switch the edge only if required. Do not restart PostgreSQL, Redis, the Ledger
volume, or the active public Sub2API slot. If any request remains active or a
reservation is non-zero after takeover, stop the switch and enter manual
reconciliation; never force a settlement with unknown usage.

Sub2API edge switching is independent of the Core selection.

Change only `AI99T_ACTIVE_UPSTREAM` inside the mode-0600 `.runtime/public.env` file, then recreate only the edge service:

```sh
docker compose --env-file .runtime/public.env -f compose.public-sandbox.yml up -d --force-recreate --no-deps edge
```

Verify the inactive slot through its loopback port before switching. Never edit application code inside a running container.

## Backup

`backup_restore_verify.py` creates a PostgreSQL custom-format dump, a consistent SQLite Commercial Core backup, and (when present) an encrypted SecretProvider snapshot. It restores PostgreSQL into a disposable verification database, verifies Core integrity, authenticates the encrypted secret snapshot with the production master key, and rejects plaintext `sk-` markers. Evidence and backup files stay in `.runtime/backups` with mode 0600.

The APIYI master key stays outside release directories under `/srv/ai99t/shared`; it is never copied into Git, reports, or the application database. Infrastructure snapshots protect that root-only key separately from the application-level ciphertext backup.

On the public server, set `AI99T_BACKUP_DIR=/srv/ai99t/backups` so backups survive release-directory switches. The included `ai99t-public-sandbox-backup.{service,timer}` runs a verified backup daily at 03:17 Asia/Kuala_Lumpur, while `ai99t-public-sandbox-monitor.{service,timer}` checks both loopback Blue/Green instances every five minutes.

A server-side daily timer may call the same script. DigitalOcean weekly Droplet backups are a separate infrastructure layer and do not replace application-level restore verification.
