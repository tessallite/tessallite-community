# Tessallite Community Edition License Terms

Version: draft 0.1
Last updated: 2026-06-23

These terms are a draft for review. They are intended to describe the free,
self-hosted Tessallite Community Edition under the source-available Community
model.

## 1. Definitions

**Licensor** means Tessallite Ltd, a company registered in England and Wales
(company number 17243379), registered office 8 Lambert Road, Barking, England,
United Kingdom, IG11 0QW. Where these terms use "Tessallite" as the licensor or
an acting party (for example "Tessallite grants" or "Tessallite may terminate"),
that means the Licensor.

**Tessallite** means the Tessallite software, documentation, release bundle,
container images, configuration templates, demo content, and related materials
made available by or on behalf of the Licensor.

**Community Edition** means the free self-hosted edition activated by a signed
Community license file.

**Community Source** means source code intentionally published under the
source-available root `LICENSE` in the Tessallite Community repository.

**Closed Components** means proprietary Tessallite components, including the
license manager, gateway guard, license issuer, closed model-service policy
modules or compiled model-service builds, private release/signing automation,
and optional closed optimizer/acceleration modules.

**Built-in demo tenant** means the seeded demonstration tenant and its hosted or
packaged demo source data.

**Own tenant** means the tenant created or used by you for your own evaluation,
small-team, or internal use.

**Signed license** means the JSON license document issued by Tessallite and
verified locally by the product.

## 2. Grant

Subject to these terms and your signed license, Tessallite grants you a
non-exclusive, non-transferable, revocable license to install and run Community
Edition for internal evaluation, proof-of-value, development, training, and
small-team internal business use.

Community Source remains available under the source-available root `LICENSE`.
Closed Components are licensed only under these Community terms and are not
open-source software.

## 3. Community Limits

Unless your signed license says otherwise, Community Edition permits:

- one built-in demo tenant;
- one own tenant;
- unlimited own projects;
- two own models total across the own tenant;
- two users.

The built-in demo tenant does not count as your own tenant. Projects are not
currently capped in Community Edition; the practical control-plane cap is the
number of own models.

Community limits are control-plane limits only. Community Edition does not cap:

- query volume;
- row count;
- data volume;
- source data size;
- credits;
- acceleration use;
- optimizer use;
- session count, unless a later signed license explicitly adds such a limit.

All product capabilities may be available in Community Edition, subject to the
installed components, your own configuration, normal authentication and
authorization, and the limits above.

## 4. Signed Local License

Community Edition requires a signed local license file to activate protected
control-plane writes. The license manager verifies the license locally and
offline using public verification keys.

The product may refuse to create or activate additional users, projects, models,
or tenants when the signed license limits are reached.

License verification is local. Loss of internet access must not disable an
already-installed Community Edition solely because Tessallite's website or
license registry is unreachable.

## 5. Demo Tenant And Demo Data

The built-in demo tenant and demo source are provided for demonstration and
evaluation. Demo source credentials, if shipped or configured, may be read-only.
You must not attempt to bypass demo source protections, scrape hosted demo data
at scale, or use demo data as a production dataset.

## 6. Restrictions

You must not:

- sell, sublicense, rent, lease, or transfer Community Edition or Closed
  Components to a third party;
- provide Tessallite as a hosted, managed, bureau, resale, OEM, white-label, or
  service-provider offering to third parties without a separate written
  agreement;
- remove copyright, trademark, proprietary, or license notices;
- reverse engineer, decompile, disassemble, or attempt to extract source code
  from Closed Components except to the extent mandatory law permits it;
- bypass or disable the license manager, gateway guard, internal request-token
  controls, or signed-license checks;
- use Community Edition in a way that exceeds the signed license limits;
- use Tessallite names, logos, or marks except as allowed by written brand
  guidance.

## 7. Community Source

Nothing in these Community terms expands the source-available rights granted for
Community Source. If you modify Community Source, your rights and obligations for
those files are governed by the root `LICENSE` and
`LICENSE-SOURCE-AVAILABLE.md`.

These Community terms govern Closed Components, signed bundles, runtime
activation, and edition limits.

## 8. Optional Beacon

Community Edition may include an optional adoption beacon. The beacon is for
awareness and advisory delivery only. It must not act as a remote kill switch or
remote control plane.

Allowed beacon payloads should be limited to license ID, product version,
edition, and anonymous high-level activity counters. Beacon payloads must not
include query text, row data, schemas, credentials, or personal data beyond the
license ID.

If the beacon is unavailable or disabled, the product must continue to operate
subject to its local signed license.

## 9. Enterprise Upgrade

Enterprise Edition removes or changes Community control-plane limits only through
a signed Enterprise license and/or a written order. Enterprise may include more
users, more models, more own tenants, additional deployment options, support,
security assurance material, SSO, onboarding, and commercial terms.

## 10. Ownership

Tessallite and its licensors retain all rights not expressly granted. Community
Edition is licensed, not sold.

## 11. No Warranty

Community Edition is provided "as is" and "as available", without warranty of
any kind to the maximum extent permitted by law.

Tessallite does not guarantee that Community Edition, Community Source,
documentation, examples, demo data, container images, installation scripts,
connectors, integrations, migration scripts, updates, or related materials will
be secure, accurate, complete, uninterrupted, error-free, fit for a particular
purpose, compatible with your environment, or available for any particular
period.

## 12. Limitation Of Liability

To the maximum extent permitted by law, Tessallite's aggregate liability for
Community Edition is limited to the greater of the amount you paid for Community
Edition in the twelve months before the claim or GBP 100.

## 13. Termination

Tessallite may terminate your Community license if you materially breach these
terms and fail to cure the breach after written notice. On termination, you must
stop using Closed Components and destroy copies of them, except where retention
is required by law.

Community Source rights continue only as permitted by the root source-available
`LICENSE`, unless separately terminated under that license.

## 14. Governing Law

These draft terms are intended to use the laws of England and Wales, with courts
of England and Wales having exclusive jurisdiction, unless a final signed
agreement states otherwise.

## 15. Contact

For licensing questions, contact info@tessallite.io.
