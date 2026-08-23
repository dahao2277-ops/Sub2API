# AI16T Dual Platform Boundaries

Status: Accepted for SUB2API_INDEPENDENT_PLATFORM_BUILD.

## Platforms

PLATFORM_A is New API + AI16T Commercial Core.

PLATFORM_B is Sub2API + AI16T Commercial Core.

Both platforms continue. This repository represents PLATFORM_B only.

## Isolation Rules

PLATFORM_B must not share these production resources with PLATFORM_A:

- PostgreSQL
- Redis
- session store
- API key database
- provider/account database
- Docker namespace
- runtime
- backup set
- production domain
- production secrets

AI16T Commercial Core code can be reused, but deployed instances and ledger data stay separate until an explicit migration decision is made.

## Domain And Hosting

`ai16t.com` remains reserved for PLATFORM_A. PLATFORM_B must use localhost or isolated staging until the owner chooses a separate domain.

Current state: OWNER_DOMAIN_SELECTION_REQUIRED after local/staging gates pass.

## Financial Authority

AI16T Ledger is the only financial authority.

Sub2API balance, usage, quota, recharge, and subscription data are control-plane or display projections. If Sub2API and Ledger disagree:

- LEDGER_WINS
- emit or record PROJECTION_DRIFT
- fail closed for new financial writes until reconciliation is complete

## Production Gate

Do not connect real provider secrets, payment providers, customer imports, production billing, or bulk real API keys before independent staging passes health, ready, E2E, security, billing, backup, and restore gates.
