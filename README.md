# Tessallite Community Edition

Tessallite is a self-hosted semantic layer and analytics platform: define each
business metric once, govern who can see it, and serve the same trusted answer to
Excel, BI tools, dashboards, applications, and AI data assistants.

This repository is the **source-available Community Edition**. It is for
**inspection and review** of the public source. It is **not** an installer and
it is **not** a runnable product checkout.

## Install (supported): the signed bundle

Do not build this tree expecting a working stack. Download the pre-built, signed
Community bundle and run it on your own server:

- Download: https://tessallite.io/download.html
- Get a free Community licence: https://tessallite.io/register.html
- Install guide: https://tessallite.io/help/install-local.html

The bundle is self-contained (images included), installs offline, and is
verified by a detached checksum and Ed25519 signature **before** extraction.
See the download page for the exact `sha256sum -c` and signature-verification
steps.

## What this source tree contains

Public source for the React frontend (including `shared-ui`), the gateway shell
and open routes, model-service APIs, the query-router parse/bind/security/
source-rewrite path, agent-service, shared schemas, help, and Excel plugin
source. Closed acceleration, optimizer, and licensing-guard components are
**not** included.

This checkout does **not** ship Compose or Helm installers, the scheduler, or
the signed images. Those live in the signed bundle. A source-only tree cannot
start Tessallite; unavailable closed components are not a “degraded but running”
mode.

## Source-only Docker builds (inspection, not the advertised install)

Closed modules are stripped from this tree. For review or CI you can `docker
build` the generated source-only Dockerfiles (no Cython compile step). That is
**not** the supported customer install — use the signed bundle above.

## Licence

Community Edition is governed by the **Tessallite Community Licence**
([`LICENSE-COMMUNITY.md`](LICENSE-COMMUNITY.md)). See the licence map
([`LICENSING.md`](LICENSING.md)) for which terms apply to which component, the
open/closed split ([`OPEN-CLOSED-SPLIT.md`](OPEN-CLOSED-SPLIT.md)), and
third-party attributions ([`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)).

## Security

Report security issues privately to info@tessallite.io. Advisories are
published with their fixes on https://tessallite.io.

---

(c) Tessallite Ltd. Tessallite and the Tessallite logo are trademarks of
Tessallite Ltd.
