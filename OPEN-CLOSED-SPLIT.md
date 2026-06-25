# Tessallite Component Boundary Map

Version: 1.0
Effective date: 2026-06-23

This file summarizes which Tessallite materials are source-available,
proprietary, commercial, or third-party. It is a plain-language map and does
not replace the license documents or a signed written agreement.

| Area | Category | Terms | Notes |
|---|---|---|---|
| Public Community source repository | Source-available | Root `LICENSE`; scope notes in `LICENSE-SOURCE-AVAILABLE.md` | Applies only to files intentionally published to the public Community repository. |
| Published frontend source | Source-available when published | Root `LICENSE` for published source files | User-interface source follows the published source terms unless a file states otherwise. |
| Community runtime services and bundle | Mixed runtime package | `LICENSE-COMMUNITY.md`, root `LICENSE`, `NOTICE`, `LICENSING.md`, third-party notices | Community is free to self-host within the signed licence and release configuration. |
| Licence manager and signing services | Proprietary runtime/service components | Community or Enterprise runtime terms; written agreement where applicable | Used to activate signed local licences and edition entitlements. |
| Gateway/admission controls | Proprietary runtime components where included | Community or Enterprise runtime terms | Used in packaged releases according to edition and release configuration. |
| Optimizer and acceleration modules | Included by edition and release configuration | Community terms, Enterprise terms, or written Order | Availability depends on the installed bundle and licence. |
| Enterprise artifacts | Commercial/proprietary where provided | `LICENSE-ENTERPRISE.md` and the Order | Customer-specific terms can add or change deployment and support rights. |
| Third-party dependencies | Third-party | `THIRD_PARTY_NOTICES.md` and dependency licences | Each dependency keeps its own licence terms. |
| Tessallite marks, logos, names, and domains | Tessallite brand/trademark material | Written brand guidance or written permission | Source-available terms do not grant trademark rights. |

## Community Edition Positioning

Public materials should describe Community Edition as a free, self-hosted
edition for evaluation, proof of value, development, training, and small
internal use, activated by a signed local licence and subject to practical
edition limits.

Avoid using internal implementation details as buyer-facing copy. When a user or
investor asks about the licensing model, the plain answer is:

- Community is free to self-host within the signed licence limits.
- Selected Community source files are source-available under Tessallite's source
  terms.
- Enterprise is the commercial path for broader scale, support, SSO, deployment
  options, and assurance material.
- Hosted, OEM, resale, white-label, or service-provider rights are available
  only by written agreement.
