# Tessallite Community Edition

Tessallite is a self-hosted semantic layer and analytics platform: define each
business metric once, govern who can see it, and serve the same trusted answer to
Excel, BI tools, dashboards, applications, and AI data assistants.

This repository is the **source-available Community Edition**. It is a developer
source checkout — **not** the recommended way to install the product.

## Install (recommended): the signed bundle

Most users should **not** build from source. Download the pre-built, signed
Community bundle and run it on your own server:

- Download: https://tessallite.io/download.html
- Get a free Community licence: https://tessallite.io/register.html
- Install guide & docs: https://tessallite.io/help/index.html

The bundle is self-contained (all images included), installs offline, and is
verified by checksum and Ed25519 signature. See the download page for the exact
`sha256sum -c` and signature-verification steps.

## What Community includes

The full product — semantic modelling, Excel/XMLA and JDBC/BI access, the
conversational agent (bring your own LLM key), acceleration, and the demo dataset.
The limits are control-plane only:

- 2 tenants (a built-in demo plus 1 of your own)
- unlimited projects
- 2 models total in your own tenant
- 2 users

A licence unlocks more users and models plus enterprise packaging and support.
See https://tessallite.io/pricing.html.

## Developer source checkout

This repo contains the open Community source: the React frontend, the gateway
shell and open routes, model-service APIs, the query-router source/parse/bind/
security/source-rewrite path, agent-service, shared schemas, and deployment
descriptors. The closed acceleration/optimizer and the licensing/guard components
are **not** included; in source-only mode those features report "component
unavailable" and the product still runs against your configured sources.

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
