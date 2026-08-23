# SUB2API_PRODUCTION_HARDENING

Status: Required before public production.

## Priority Items

| Priority | Area | Required result |
| --- | --- | --- |
| P1 | Idempotency | Same account and idempotency key produce one authoritative financial result through AI16T Core |
| P2 | Concurrent balance | Reserve, settle, and refund prevent double spend, negative balance, and double settlement |
| P3 | Timeout billing | Timeout, client disconnect, fallback, and retry have explicit billing states and no duplicate charge |
| P4 | Provider secret at rest | Use SecretProvider with Vault/KMS/envelope encryption in production |

## Security Review Areas

- path traversal
- OAuth pending exchange
- token leakage
- XSS
- SSRF
- API key storage
- privilege escalation
- quota bypass
- payment callback
- URL allowlist

## Advisory Classification

- OFFICIAL_ADVISORY
- CONFIRMED_REPRODUCIBLE
- OPEN_REPORT_UNCONFIRMED
- FALSE_POSITIVE

Production blocking requires either an official advisory affecting the deployed version or a confirmed reproducible unmitigated risk.

## Current Official Advisory Check

Official GitHub advisories checked during this task:

- GHSA-vrxq-qm4h-6hgg / CVE-2026-73079: high, affected >= v0.1.135 <= v0.1.168, fixed v0.1.169. Current baseline v0.1.179 is above fixed version.
- GHSA-vc2q-289v-74g3 / CVE-2026-27812: high, affected < v0.1.85, fixed v0.1.85. Current baseline v0.1.179 is above fixed version.

This does not replace dynamic verification on the deployed artifact.
