# Architecture

Stable design documents, specifications, and reference material for the Excel plugin.

**TL;DR:** Four reference documents define the plugin: a functional spec (SPECS), a visual/UX spec (FRONTEND-DESIGN), a compatibility matrix for Office.js API support, and a UX review that realigns both specs to the correct product purpose.

**Documents**

- `architecture_specs.md` — Functional specification. Features, data flow, API contracts, security model, distribution.
- `architecture_frontend-design.md` — Visual/UX design. Screens, components, interactions, state transitions, theme tokens.
- `architecture_compatibility-matrix.md` — Office.js API compatibility results from `runCompatibilitySpike()`.
- `architecture_specs-and-ux-review.md` — Re-evaluation of SPECS and FRONTEND-DESIGN against the correct product purpose (analytics workbench, not web frontend clone).
- `looker-and-looker-cloud-core-client-specs.md` — Current compatibility spec for Data Studio direct and optional generated LookML export; Cloud Core is not required. Companion plan: `docs/execution/looker-and-looker-cloud-core-support-plan.md`.
