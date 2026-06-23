# Tessallite Open / Closed Component Split

Last updated: 2026-06-23

This file is a review aid for legal, security, release, and customer-facing
documentation. It does not replace the license documents.

| Area | Open / Closed | License / Terms | Notes |
|---|---|---|---|
| Public Community source repository | Source-available | Root `LICENSE`; scope notes in `LICENSE-SOURCE-AVAILABLE.md` | Only files intentionally published to `tessallite-community`; not OSI open-source. |
| TSX/React frontend | Source-available when published | Root `LICENSE` | Frontend displays edition state but never enforces limits alone. |
| Gateway protocol shell | Source-available unless a closed guard module is embedded | Root `LICENSE` for source-available files; proprietary for guard | Gateway guard may be closed. |
| Model-service baseline APIs | Source-available APIs + call-site glue | Root `LICENSE` for published source | Glue (`licensing_guard.py`) only *calls* the policy; it holds no cap values. |
| Edition cap policy (the decision) | Closed | Proprietary | `shared/licensing/closed/manager_impl.py` (compiled). Holds the actual user/project/model cap values and `can_create` decision. No cap number lives in Community Source. |
| Query-router source path | Source-available when published | Root `LICENSE` | Query path is not the Community entitlement boundary. |
| License manager | Closed | Community or Enterprise runtime terms | Verifies signed licenses and evaluates limits. |
| License issuer/signing service | Closed/internal | Proprietary/internal | Runs on Tessallite infrastructure only. |
| Gateway guard | Closed or closed plugin | Community or Enterprise runtime terms | Admission, internal token issuance, high-level endpoint guard. |
| Internal request-token issuer/verifier | Shared or closed | Depends on packaging | Must prevent direct backend bypass. |
| Optimizer | Optional closed IP | Proprietary if closed | Not gated by Community license; private for IP if shipped closed. |
| Acceleration planner/matchers/rewriters | Optional closed IP | Proprietary if closed | Not gated by Community license; private for IP if shipped closed. |
| Community signed bundle | Mixed | `LICENSE`, `NOTICE`, `LICENSING.md`, `LICENSE-COMMUNITY.md`, third-party notices | Contains source-available code, closed components, and third-party components. |
| Enterprise artifacts | Mixed/proprietary | `LICENSE-ENTERPRISE.md` and Order | Customer-specific terms can override defaults. |
| Third-party dependencies | Third-party | `THIRD_PARTY_NOTICES.md` | Each dependency keeps its own license. |
| Tessallite marks/logos | Proprietary trademark/brand | Not granted by source-available license | Use requires written permission or published brand rules. |

## Enforcement integrity

Edition caps cannot be removed by editing Community Source. The cap values and the
allow/deny decision live only in the closed, compiled license manager
(`shared/licensing/closed/manager_impl.py`); the source-available model-service
glue merely calls that decision and carries no cap numbers. The closed gateway guard blocks
over-cap control-plane writes independently, so tampering with source-available
model-service code alone does not lift a cap. Any Community build that ships cap
enforcement must therefore include the closed license manager and the closed
gateway guard; a source-only build without those closed components is unmetered
by design and is not a licensed Community Edition.

Community limits currently intended for customer-facing materials:

- built-in demo tenant included and does not count as own tenant;
- one own tenant;
- unlimited own projects;
- two own models total across the own tenant;
- two users;
- all features enabled;
- no query/data/credit/acceleration/optimizer caps.
