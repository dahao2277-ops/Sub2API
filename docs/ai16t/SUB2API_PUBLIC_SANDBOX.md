# SUB2API_PUBLIC_SANDBOX

Status: Design record for PLATFORM_B.

The Compose file is a static local-only blueprint and was not started in this task. Its Redis test password is supplied as a Compose secret and materialized only into an ephemeral mode-0600 config inside the container; it is not placed in the `redis-server` argument vector.

## Environment Separation

PLATFORM_B needs independent:

- domain
- server
- PostgreSQL
- Redis
- runtime
- Docker Compose project name
- backup storage
- release slot
- monitoring namespace
- production secrets

## Blue/Green

Use two slots:

- BLUE: current production or LAST_KNOWN_GOOD
- GREEN: next release or INACTIVE_SLOT

Deploy new versions to the inactive slot. Promote only after health, ready, DB, Redis, E2E, security, and billing checks pass. Keep the old slot available for rollback.

## Health And Ready

Required checks:

- HTTP status
- latency
- 5xx
- Postgres
- Redis
- CPU
- RAM
- disk
- billing anomaly
- duplicate usage
- projection drift
- provider failure
- account cooldown

## Backup And Restore

- daily PostgreSQL backup
- pre-deploy backup
- pre-migration backup
- scheduled restore drill

Backup is not PASS until restore has been verified.

## Stop Condition

After code, security, and staging preparation pass, stop with:

- OWNER_DOMAIN_SELECTION_REQUIRED
- OWNER_SERVER_PURCHASE_REQUIRED

Do not buy domains or servers automatically.
