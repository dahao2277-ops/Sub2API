# SUB2API Hybrid E2E Matrix

Status: BLOCKED_ON_LOCAL_COMMERCIAL_CORE_SOURCE

## Required Scenarios

| # | Scenario | Status |
| --- | --- | --- |
| 1 | Root login | Pending |
| 2 | Admin login | Pending |
| 3 | User login | Pending |
| 4 | Valid API key | Pending |
| 5 | Revoked API key | Pending |
| 6 | Disabled user | Pending |
| 7 | Model mapping | Pending |
| 8 | Sticky routing | Pending |
| 9 | Primary success | Pending |
| 10 | Primary 500 to fallback success | Pending |
| 11 | 429 cooldown | Pending |
| 12 | Timeout | Pending |
| 13 | Both providers fail | Pending |
| 14 | Usage | Pending |
| 15 | Ledger | Pending |
| 16 | Customer charge | Pending |
| 17 | Provider cost | Pending |
| 18 | Margin | Pending |
| 19 | Duplicate idempotency | Pending |
| 20 | Concurrent duplicate | Pending |
| 21 | Insufficient balance | Pending |
| 22 | Concurrent balance | Pending |
| 23 | Failed request no charge | Pending |
| 24 | Retry no double charge | Pending |
| 25 | Projection drift | Pending |
| 26 | Secret not leaked | Pending |

## Blocker

The referenced AI16T Commercial Core source at branch `integration/ai16t-commercial-core-v1-remediated`, commit `56108397f31a97926f33d28420e9b44a52547bd2`, is not present on this Mac. The adapter contract can compile locally, but full dynamic Hybrid E2E requires that source or a reachable isolated Core service.
