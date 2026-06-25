---
title: "Demo tenant: acme-demo"
audience: all
area: getting-started
updated: 2026-06-24
---

## What this covers

`acme-demo` is the comprehensive demo workspace shipped with Tessallite. It exercises every modelling feature against a self-contained dataset so new operators can explore a working install without authoring anything from scratch.

It supersedes the legacy `acme-test` fixture. `acme-test` scripts remain in `tessallite/scripts/` for backward compatibility, but new code should target `acme-demo`.

---

## What it contains

- One project (`project1`).
- Five models in that project. Two are the primary worked examples used throughout this help and the end-to-end test suite:
  - `ModelX` — small smoke model: one fact, a handful of measures, one persona, one row-security rule. The simplest model to read when you are learning the canvas.
  - `modely` — comprehensive model: two facts, snowflake dimensions, time + geo hierarchies, many measures (sum / avg / calculated / currency + percent formats / time variants), three personas, two row-security rules (mapping + role predicate), pocket tables, predictive aggregates (including a demand-driven one), lifecycle events, drill-through sets, and glossary entries. This is the model that exercises nearly every feature.
  - The remaining three — `Model L`, `onboarding`, and `inventory` — are additional sample models added so the demo tenant shows a realistic multi-model workspace (a fuller catalogue, more dimensions, and varied measure counts) rather than just two. They are good for exploring the Explorer and model-switching, but `modely` remains the reference for feature walkthroughs.
- Source schema `acme_demo_src` and target schema `acme_demo_target` in `tessallite_system`. Both are recreated on every reseed.

---

## How it gets seeded in the Community bundle

The signed Community bundle seeds it during `./install.sh`. After the system schema is ready, the installer loads the demo source data and imports the project bundle so a new user has a working tenant immediately.

1. Source and target schemas are loaded from `deploy/Sample-db/acme-demo/*.sql`.
2. The acme-demo tenant is reset, the project + four connections are created, then all five models are re-imported from the seed bundle `tessallite/seeds/acme-demo/project.json` (a single project export containing every model; older builds used separate per-model JSON files).

The seed is conditional on `SYSTEM_DATABASE_URL` and `CREDENTIAL_ENCRYPTION_KEY` being present in the generated `.env`. If the seed assets are missing, the installer prints a notice and continues so the platform still starts.

---

## Rebuilding the seed locally

To rebuild the JSON bundles from a clean state:

```bash
bash scripts/regenerate-acme-demo-seeds.sh
```

This script orchestrates:

1. `tessallite/scripts/reset_acme_demo_tenant.py` — drops + recreates the tenant.
2. `tessallite/scripts/load_acme_demo_schemas.sh` — recreates `acme_demo_src` + `acme_demo_target`.
3. `tessallite/scripts/seed_acme_demo_project.py` — imports project + connection + models from the project export bundle.

Re-running against an unchanged codebase produces zero diff.

---

## Adding a new feature to modely

1. Modify `tessallite/scripts/bootstrap_acme_demo_models.py` to add the feature.
2. Run `bash scripts/regenerate-acme-demo-seeds.sh`.
3. Commit the changed `tessallite/seeds/acme-demo/project.json` and `MANIFEST.sha256`.

The committed `project.json` is the source of truth. Container deploys re-import from it; they do not re-run the bootstrap step.

---

## Test fixture

`tests/conftest.py` exposes the `acme_demo_seeded` session-scoped pytest fixture. It is opt-in: set `ACME_DEMO_FIXTURE_ENABLED=1` (and `SYSTEM_DATABASE_URL` + `CREDENTIAL_ENCRYPTION_KEY`) to enable. It is checksum-gated via `tessallite/seeds/acme-demo/.last_seeded_checksum` (gitignored), so unchanged seed inputs are a no-op (~0.01 s); a fresh reseed runs in roughly six seconds.

The comprehensive coverage suite is `tests/e2e/test_acme_demo_modely.py` — one or more tests per feature row in the modely matrix. Run it with:

```bash
ACME_DEMO_FIXTURE_ENABLED=1 \
SYSTEM_DATABASE_URL=postgresql+asyncpg://tessallite:<pw>@localhost:5432/tessallite_system \
CREDENTIAL_ENCRYPTION_KEY=<key> \
pytest tests/e2e/test_acme_demo_modely.py
```

---

## Related

- [Install Tessallite Locally](install-local.md)
- [First-time setup](first-time-setup.md)
- [Workspaces and tenants](../concepts/workspaces-and-tenants.md)

---

← [Workspace Explorer](workspace-explorer.md) | [Home](../index.md) | [Install Tessallite Locally →](install-local.md)
