# Tessallite Community Edition License Terms

Version: 1.0
Effective date: 2026-06-23

These terms describe the free, self-hosted Tessallite Community Edition under
the source-available Community model.

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

**Proprietary Components** means Tessallite components licensed as part of the
Community runtime but not published as Community Source, including the license
manager, gateway guard, license issuer, proprietary policy modules, release
signing automation, and optional proprietary optimizer or acceleration modules.

**Built-in demo tenant** means the seeded demonstration tenant and its hosted or
packaged demo source data.

**Own tenant** means the tenant created or used by you for your own evaluation,
small-team, or internal use.

**Signed license** means the JSON license document issued by Tessallite and
verified locally by the product.

## 2. Grant

Subject to these terms and your signed license, Tessallite grants you a
non-exclusive, non-transferable license to install and run Community Edition for
internal evaluation, proof-of-value, development, training, and small-team
internal business use.

Community Source remains available under the source-available root `LICENSE`.
Proprietary Components are licensed under these Community terms.

## 3. Community Limits

Unless your signed license or release notes state otherwise, Community Edition
is generally configured to permit:

- one built-in demo tenant;
- one own tenant;
- own projects within the practical limits of the installed release;
- two own models total across the own tenant;
- two users.

The built-in demo tenant does not count as your own tenant. Projects are not
currently the primary entitlement boundary; the practical control-plane cap is
normally the number of own models, users, and own tenants permitted by the signed
license.

Community limits are primarily control-plane limits. The signed license, release
notes, or installed configuration may include reasonable technical, operational,
or edition limits that are appropriate for the Community Edition.

Product capabilities available in Community Edition depend on the installed
components, signed license, release configuration, your own configuration,
normal authentication and authorization, and the limits above.

## 4. Signed Local License

Community Edition requires a signed local license file to activate protected
control-plane writes. The license manager verifies the license locally and
offline using public verification keys.

The product may refuse to create or activate additional users, projects, models,
or tenants when the signed license limits are reached.

License verification is designed to run locally for Community installations.
Some optional services, updates, registry access, advisory feeds, support
features, or future entitlements may still depend on network availability or
release-specific configuration.

## 5. Demo Tenant And Demo Data

The built-in demo tenant and demo source are provided for demonstration and
evaluation. Demo source credentials, if shipped or configured, may be read-only.
The demo tenant is intended for evaluation use, not as a production dataset or
bulk data extraction source.

## 6. Commercial Boundaries

A separate written agreement with Tessallite is required to:

- sell, sublicense, rent, lease, or transfer Community Edition or Proprietary
  Components to a third party;
- provide Tessallite as a hosted, managed, bureau, resale, OEM, white-label, or
  service-provider offering to third parties;
- use Tessallite names, logos, or marks outside written brand guidance.

You agree to:

- keep copyright, trademark, proprietary, and license notices in place;
- use Community Edition within the signed license limits;
- use signed-license and entitlement controls as provided;
- not reverse engineer, decompile, disassemble, or attempt to extract source
  code from Proprietary Components except to the extent mandatory law permits
  it.

## 7. Community Source

Nothing in these Community terms expands the source-available rights granted for
Community Source. If you modify Community Source, your rights and obligations for
those files are governed by the root `LICENSE` and
`LICENSE-SOURCE-AVAILABLE.md`.

These Community terms govern Proprietary Components, signed bundles, runtime
activation, and edition limits.

## 8. Optional Beacon

Community Edition may include an optional adoption beacon. The beacon is for
awareness and advisory delivery only.

Allowed beacon payloads should be limited to license ID, product version,
edition, and anonymous high-level activity counters. Beacon payloads are not
intended to include query text, row data, schemas, credentials, or personal data
beyond the license ID.

If the beacon is unavailable or turned off, the product is designed to continue
operating subject to its local signed license.

## 9. Enterprise Upgrade

Enterprise Edition removes or changes Community control-plane limits only through
a signed Enterprise license and/or a written order. Enterprise may include more
users, more models, more own tenants, additional deployment options, support,
security assurance material, SSO, onboarding, and commercial terms.

## 10. Ownership

Tessallite and its licensors retain all rights not expressly granted. Community
Edition is licensed, not sold.

## 11. Warranty Disclaimer

Community Edition is provided without a commercial warranty unless Tessallite
agrees otherwise in writing.

To the maximum extent permitted by law, Tessallite does not provide implied
warranties for Community Edition, Community Source, documentation, examples,
demo data, container images, installation scripts, connectors, integrations,
migration scripts, updates, or related materials.

## 12. Limitation Of Liability

To the maximum extent permitted by law, Tessallite's aggregate liability for
Community Edition is limited to the greater of the amount you paid for Community
Edition in the twelve months before the claim or GBP 100.

## 13. Termination

Tessallite may end your Community license if you materially breach these terms
and do not cure the breach after written notice. If that happens, you must stop
using Proprietary Components and destroy copies of them, except where retention
is required by law.

Community Source rights continue only as permitted by the root source-available
`LICENSE`, unless separately terminated under that license.

## 14. Governing Law

These terms are governed by the laws of England and Wales. The courts of England
and Wales have exclusive jurisdiction, unless a separate written agreement signed
by Tessallite states otherwise.

## 15. Contact

For licensing questions, contact info@tessallite.io.
