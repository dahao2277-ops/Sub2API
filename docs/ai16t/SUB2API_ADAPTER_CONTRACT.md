# Sub2APIAdapter Contract

Status: Foundation implemented; runtime wiring blocked on pinned Core source.

## Placement

The canonical Sub2API-facing execution boundary lives under:

`backend/internal/integration/ai16tadapter`

Supporting Core financial and secret ports live under `backend/internal/ai16t/coreadapter`. The earlier duplicate string-money facade was removed after independent review to prevent accidental wiring. These packages remain isolated from upstream-heavy gateway, account, billing and UI modules until the pinned Commercial Core source is present. They are not wired into production routes.

## Request Flow

Target flow:

Sub2API User -> Sub2API API Key -> Sub2APIAdapter -> AI16T Commercial Core -> Dynamic Router -> Mock Provider -> Usage -> Ledger -> Provider Cost -> Customer Charge -> Minimum Margin -> Balance Projection -> Sub2API Admin/User UI.

## Sub2API execution boundary

- Authenticate a keyed HMAC fingerprint, never pass the raw API Key to a dependency.
- Reject revoked keys, disabled users and missing model mappings before calling Core.
- Forward the exact idempotency key to Core; compute the request hash inside the trusted Adapter boundary from identity, credential, Core model and payload.
- Reject a billable Core result without an authoritative request ID and Ledger reference.
- Publish only a read-only projection; persist `PROJECTION_DRIFT` in a durable gate without repeating the Core call, and block later financial calls until reconciliation.

## Supporting Core operations

- Quote: ask Core pricing/margin for an expected charge.
- Reserve: reserve balance with idempotency before dispatching upstream.
- Settle: settle final usage once provider result is known.
- Refund: refund or release reservation after timeout, client disconnect, both providers fail, or retry-safe failure.
- CheckProjection: compare Sub2API display balance with Ledger authority and return PROJECTION_DRIFT when outside tolerance.

## Fail-Closed Rules

If the Core is unavailable, Sub2API must not create an authoritative usage charge, deduct balance, or mark a paid request as settled. The supporting fail-closed adapter returns `AI16T_CORE_UNAVAILABLE` for all financial operations until a real Core client is connected.

## Secrets

Provider credentials must be represented through `SecretProvider` references. Production implementations should be Vault, KMS, or envelope encryption backed. The current default provider refuses read/write/rotate so test code cannot accidentally persist real provider secrets.

## Known Blocker

The referenced AI16T Commercial Core commit `56108397f31a97926f33d28420e9b44a52547bd2` is documented as READY in local reports, but the source checkout is not present on this Mac. The current packages are therefore contract candidates, not proof of real API binding. Generated/verified client binding and full Hybrid E2E require the M2 source or an exported, pinned Core contract package.
