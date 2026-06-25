# Tessallite Licensing Map

Version: 1.0
Effective date: 2026-06-23

Tessallite is a product of Tessallite Ltd, a company registered in England and
Wales (company number 17243379), registered office 8 Lambert Road, Barking,
England, United Kingdom, IG11 0QW ("Tessallite Ltd" or the "Licensor").

Tessallite is distributed as a source-available Community product with
commercial Enterprise options. Different parts of the workspace and release
artifacts are licensed under different terms.

## 1. Source-Available Community Source

The files intentionally published to the public Tessallite Community repository
are source-available under the root `LICENSE` file.

`LICENSE-SOURCE-AVAILABLE.md` provides scope notes for the source-available
files. The source-available grant applies only to files intentionally published
to the public Community repository. A file being present in this private
workspace does not mean it is source-available.

## 2. Community Edition Runtime

Tessallite Community Edition is a free, self-hosted edition governed by the
Community terms in `LICENSE-COMMUNITY.md` and by a signed local license file.

Community limits are primarily control-plane limits:

- one own tenant, in addition to the built-in demo tenant;
- own projects within the practical limits of the installed release;
- two own models total across the own tenant;
- two users, unless the signed license states otherwise.

The signed license, release notes, or installed configuration may include
reasonable technical, operational, or edition limits that are appropriate for the
Community Edition.

## 3. Enterprise Edition Runtime

Tessallite Enterprise Edition, Enterprise bundles, commercial proprietary components,
support, production rights, and customer-specific entitlements are governed by
`LICENSE-ENTERPRISE.md` and the applicable written order or agreement.

## 4. Proprietary Runtime Components

Some runtime, licensing, release, support, and deployment components are
proprietary unless a separate written agreement says otherwise. These can
include:

- license manager implementations;
- gateway guard/admission modules;
- internal request-token issuer/verifier modules when shipped as proprietary
  runtime components;
- model-service policy modules or compiled model-service builds when shipped as
  proprietary runtime components;
- license issuer/signing systems;
- private release/signing automation;
- optional optimizer, acceleration planner, aggregate/pocket matcher, exactness
  validator, aggregate/pocket rewrite, and related proprietary modules;
- any compiled object code, container image layer, appliance image, or bundle
  explicitly marked as a proprietary Tessallite component.

Proprietary Components are licensed, not sold. They are not covered by the
source-available Community source license.

## 5. Third-Party Components

Third-party dependencies retain their own licenses. See
`THIRD_PARTY_NOTICES.md`. If a third-party notice conflicts with this licensing
map for that third-party component, the third-party license controls that
component only.

## 6. Trademarks

This licensing map does not grant trademark rights in Tessallite names, logos,
domain names, release marks, or product branding.

## 7. Document Status

This map summarizes the Tessallite license set. If this map conflicts with a
specific license file or a written agreement signed by Tessallite, the specific
license file or signed written agreement controls for its subject matter.
