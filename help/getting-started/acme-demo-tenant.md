---
title: "Demo tenant: acme-demo"
audience: all
area: getting-started
updated: 2026-07-22
---

## What this covers

Tessallite ships with a ready-made demo workspace so a new operator can explore a working install without building anything from scratch. There are two closely related versions of it, and it is worth knowing which one you are looking at:

- The **Community demo** is the tenant your installed Community bundle can provision for you. It is named `demo`, uses the project `project-demo`, and contains three models. See "How it gets seeded in the Community bundle" below.
- The **acme-demo dev fixture** is the fuller workspace used by the Tessallite team for development and testing. It is named `acme-demo`, uses the project `project1`, and contains five models. It is not what the Community installer creates; it is described here because most feature walkthroughs and the test suite reference it.

The rest of this page describes the dev `acme-demo` fixture in detail (the reference for feature walkthroughs), then explains exactly what the Community installer does.

`acme-demo` supersedes the legacy `acme-test` fixture. `acme-test` scripts remain in `tessallite/scripts/` for backward compatibility, but new code should target `acme-demo`.

---

## What the dev acme-demo fixture contains

- One project (`project1`).
- Five models in that project. Two are the primary worked examples used throughout this help and the end-to-end test suite:
  - `ModelX` — small smoke model: one fact, a handful of measures, one persona, one row-security rule. The simplest model to read when you are learning the canvas.
  - `modely` — comprehensive model: two facts, snowflake dimensions, time + geo hierarchies, many measures (sum / avg / calculated / currency + percent formats / time variants), three personas, two row-security rules (mapping + role predicate), pocket tables, predictive aggregates (including a demand-driven one), lifecycle events, drill-through sets, and glossary entries. This is the model that exercises nearly every feature.
  - The remaining three — `Model L`, `onboarding`, and `inventory` — are additional sample models added so the demo tenant shows a realistic multi-model workspace (a fuller catalogue, more dimensions, and varied measure counts) rather than just two. They are good for exploring the Explorer and model-switching, but `modely` remains the reference for feature walkthroughs.
- Source schema `acme_demo_src` and target schema `acme_demo_target` in `tessallite_system`. Both are recreated on every reseed.

---

## How it gets seeded in the Community bundle

Two things are important to get right here, because they differ from the dev fixture above:

- The Community installer does **not** create the `acme-demo` tenant with five models. It provisions a smaller demo tenant named **`demo`**, in the project **`project-demo`**, with **three** models: `modely`, `onboarding`, and `inventory`.
- Seeding is **opt-in**, and **off by default**. A fresh Community install starts empty. This is deliberate: the demo tenant ships fixed, publicly known credentials (`admin@demo.com` / `demo`), and the UI (port 3000) and JDBC (port 5433) ports are reachable, so auto-seeding would place a known login on a reachable surface.

### Turning the demo on

To include the demo dataset, set this in your `.env` **before** running `./install.sh`:

```
SEED_DEMO_TENANT=true
```

Then run the installer normally. When the flag is on, `./install.sh` runs the one demo-reseed path (`deploy/demo-tenant/reseed.py`): it loads the demo source data and seeds the `demo` tenant, project `project-demo`, and its three models — the same path the in-app demo-reseed button uses.

You can also leave the flag off at install time and provision the demo later from the in-app demo-reseed control, without re-running the installer.

### The installer checks its own work

When you ask for the demo (`SEED_DEMO_TENANT=true`), the installer does not just start the containers and declare success. It verifies the demo is actually usable, and **fails the install loudly if it is not**:

1. A failed seed is a failed install — the installer stops with an error instead of reporting success on an empty tenant.
2. It signs in as the demo admin and confirms the expected number of models exist.
3. It runs a **real first query** through the gateway (the same JDBC path a BI tool uses) and requires it to return rows. If that query fails or returns nothing, the install fails.

So a "Tessallite Community is running" banner with the demo enabled means a real query already worked, not merely that the containers came up. You can re-run the same style of check any time with `./healthcheck.sh`, which now fails with a non-zero result if any required service (including the scheduler, optimizer, and agent) is down.

The verification thresholds and the demo target (tenant, project, model, and the smoke query) are configurable in `.env` — see the `SEED_DEMO_*` and `DEMO_SMOKE_*` keys in `.env.example` — so you can point the check at a different demo model without editing the installer.

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
