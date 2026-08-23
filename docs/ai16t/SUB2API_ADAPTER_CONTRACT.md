# Sub2APIAdapter Contract

Status: Proposed implementation boundary.

## Placement

The first Sub2APIAdapter code lives under:

`backend/internal/ai16t/coreadapter`

This keeps AI16T-specific integration outside upstream-heavy gateway, account, billing, and UI modules until the Commercial Core source is present on this machine.

## Request Flow

Target flow:

Sub2API User -> Sub2API API Key -> Sub2APIAdapter -> AI16T Commercial Core -> Dynamic Router -> Mock Provider -> Usage -> Ledger -> Provider Cost -> Customer Charge -> Minimum Margin -> Balance Projection -> Sub2API Admin/User UI.

## Adapter Operations

- Quote: ask Core pricing/margin for an expected charge.
- Reserve: reserve balance with idempotency before dispatching upstream.
- Settle: settle final usage once provider result is known.
- Refund: refund or release reservation after timeout, client disconnect, both providers fail, or retry-safe failure.
- CheckProjection: compare Sub2API display balance with Ledger authority and return PROJECTION_DRIFT when outside tolerance.

## Fail-Closed Rules

If the Core is unavailable, Sub2API must not create an authoritative usage charge, deduct balance, or mark a paid request as settled. The current adapter skeleton returns `AI16T_CORE_UNAVAILABLE` for all financial operations until a real Core client is connected.

## Secrets

Provider credentials must be represented through `SecretProvider` references. Production implementations should be Vault, KMS, or envelope encryption backed. The current default provider refuses read/write/rotate so test code cannot accidentally persist real provider secrets.

## Known Blocker

The referenced AI16T Commercial Core commit `56108397f31a97926f33d28420e9b44a52547bd2` is documented as READY in local reports, but the source checkout is not present on this Mac. Real API binding, generated client code, and full Hybrid E2E require the M2 source or an exported Core contract package.
