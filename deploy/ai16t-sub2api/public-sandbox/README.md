# AI99T Platform B Public Mock Sandbox

This deployment is the isolated public sandbox for `Sub2API + AI16T Commercial Core` on `ai99t.com`.
It is not a live-provider or live-payment production environment.

## Fixed safety boundary

- Compose project: `ai99t-platform-b-public-sandbox`.
- Public edge: Caddy on ports 80/443; Blue and Green are also reachable only from host loopback on 18181/18182.
- PostgreSQL, Redis, Commercial Core, and Mock Provider have no host ports and use an internal Docker network.
- Sub2API isolated-test request hooks are disabled. Commercial Core remains mock-only internally so test credit can never become real money.
- Registration, password reset, promotions, invitation/affiliate sales, and subscription purchase are disabled after bootstrap.
- No Platform A database, Redis, volume, secret, domain, container, or release is referenced.
- No real provider secret and no payment credential is accepted by this deployment package.

## First deployment

Run all commands from this directory on the target server:

```sh
./prepare_runtime.sh
./check_isolation.sh
docker compose --env-file .runtime/public.env -f compose.public-sandbox.yml config --quiet
docker compose --env-file .runtime/public.env -f compose.public-sandbox.yml up -d --build --wait postgres redis mock-provider commercial-core sub2api-blue
./post_bootstrap.sh
docker compose --env-file .runtime/public.env -f compose.public-sandbox.yml up -d --build --wait sub2api-green
python3 monitor.py
python3 backup_restore_verify.py
docker compose --env-file .runtime/public.env -f compose.public-sandbox.yml up -d --wait edge
```

DNS must point `ai99t.com` and `www.ai99t.com` at the server before Caddy can complete ACME TLS issuance.

## Blue/Green switching

Change only `AI99T_ACTIVE_UPSTREAM` inside the mode-0600 `.runtime/public.env` file, then recreate only the edge service:

```sh
docker compose --env-file .runtime/public.env -f compose.public-sandbox.yml up -d --force-recreate --no-deps edge
```

Verify the inactive slot through its loopback port before switching. Never edit application code inside a running container.

## Backup

`backup_restore_verify.py` creates a PostgreSQL custom-format dump and a consistent SQLite Commercial Core backup, restores PostgreSQL into a disposable verification database, compares table/user counts, and verifies Core integrity. Evidence and backup files stay in `.runtime/backups` with mode 0600.

A server-side daily timer may call the same script. DigitalOcean weekly Droplet backups are a separate infrastructure layer and do not replace application-level restore verification.
