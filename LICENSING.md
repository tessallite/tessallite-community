# Tessallite Licensing Map

Last updated: 2026-06-23

Tessallite is a product of Tessallite Ltd, a company registered in England and
Wales (company number 17243379), registered office 8 Lambert Road, Barking,
England, United Kingdom, IG11 0QW ("Tessallite Ltd" or the "Licensor").

Tessallite is distributed as a source-available Community product with optional
closed Community and Enterprise components. Different parts of the workspace and
release artifacts are licensed under different terms.

## 1. Source-Available Community Source

The files intentionally published to the public Tessallite Community repository
are source-available under the root `LICENSE` file. They are not Apache-2.0,
MIT, GPL, AGPL, BSD, or OSI open-source code.

`LICENSE-SOURCE-AVAILABLE.md` provides scope notes for the source-available
files. The source-available grant applies only to files intentionally published
to the public Community repository. A file being present in this private
workspace does not mean it is source-available.

## 2. Community Edition Runtime

Tessallite Community Edition is a free, self-hosted edition governed by the
Community terms in `LICENSE-COMMUNITY.md` and by a signed local license file.

Community limits are control-plane limits only:

- one own tenant, in addition to the built-in demo tenant;
- unlimited own projects;
- two own models total across the own tenant;
- two users, unless the signed license states otherwise.

Community Edition does not impose query-volume, row-count, data-volume, credit,
session, acceleration, or optimizer-use caps.

## 3. Enterprise Edition Runtime

Tessallite Enterprise Edition, Enterprise bundles, commercial closed components,
support, production rights, and customer-specific entitlements are governed by
`LICENSE-ENTERPRISE.md` and the applicable written order or agreement.

## 4. Closed Components

The following components are proprietary unless a separate written agreement says
otherwise:

- license manager implementations;
- gateway guard/admission modules;
- internal request-token issuer/verifier modules when shipped closed;
- model-service policy-enforcement modules or compiled model-service builds when
  shipped as closed artifacts;
- license issuer/signing systems;
- private release/signing automation;
- optional optimizer, acceleration planner, aggregate/pocket matcher, exactness
  validator, aggregate/pocket rewrite, and related private IP modules;
- any compiled object code, container image layer, appliance image, or bundle
  explicitly marked as a closed or proprietary Tessallite component.

Closed Components are licensed, not sold. They are not covered by the
source-available Community source license.

## 5. Third-Party Components

Third-party dependencies retain their own licenses. See
`THIRD_PARTY_NOTICES.md`. If a third-party notice conflicts with this licensing
map for that third-party component, the third-party license controls that
component only.

## 6. Trademarks

This licensing map does not grant trademark rights in Tessallite names, logos,
domain names, release marks, or product branding.

## 7. Legal Review

These documents are product license drafts for review. Final customer-facing
terms should be reviewed by qualified counsel before public release or customer
signature.
