# Collibra Integration

Status: active. Updated 2026-06-13.

## What it is

The Collibra integration builds a Collibra-shaped picture of your Tessallite semantic model — your metrics, dimensions, KPIs, glossary terms, and downstream reports, complete with ownership, classifications, and relationships — so you can preview exactly what would land in **Collibra**, the enterprise data governance and catalog platform.

In this build the integration is a **preview / dry-run** tool. It reads your model and computes the Collibra payload locally; it does not send anything to Collibra. Live push (writing the assets into your Collibra instance) is not yet available — see [Not available yet: live connector](#not-available-yet-live-connector).

## Who it is for

- **Modellers** who want their models governed in the enterprise data catalog
- **Tenant Admins** who configure the connection to the Collibra instance
- **Data Stewards** who manage ownership, classifications, and governance workflows in Collibra

## What gets exported

When you preview or dry-run a model export, Tessallite maps these objects into governed Collibra assets with attributes, relations, and responsibilities (computed locally — nothing is written to Collibra yet):

| Tessallite object | Collibra asset type |
|---|---|
| Project | Data Domain |
| Model | Semantic Model |
| Data Source | System |
| Tables + Columns | Table + Column assets |
| Dimensions | Data Attribute assets |
| Measures | Metric assets |
| KPIs | KPI assets |
| Glossary terms | Business Term assets |
| Downstream assets | Report / Dashboard / Application assets |
| Aggregates | Table assets (materialized) |
| Data tags | Data Classification assets |

Each asset carries its Tessallite attributes (descriptions, types, formulas, visibility). Relationships link assets together — for example, a Metric "is based on" a Measure, which "is source of" a Column.

Ownership information becomes Collibra responsibilities, so stewards can see who owns each governed asset. Ownership is exported for objects that carry an explicit owner: **KPIs** (their owner) and **downstream assets** (reports, dashboards, applications). Measures and dimensions do not carry an owner field, so they are exported without a responsibility.

Lifecycle statuses are mapped automatically:
- Deployed models → **Accepted**
- Draft/unpublished → **Candidate**
- Hidden objects → **Candidate** (technical metadata)
- Deprecated/removed → **Deprecated**

## Key difference from Solidatus

Solidatus is a **lineage graph** — the main deliverable is a visual map of nodes and edges.

Collibra is a **governance operating system** — the main deliverable is a governed catalog with assets, attributes, relations, responsibilities, statuses, and classifications.

Because of this, the Collibra integration exports richer governance metadata: ownership, stewardship, classifications, and lifecycle states.

## Step-by-step: Set up and sync

### Step 1 — Get your Collibra details

Before you start, ask your Collibra administrator for:

- **Base URL** — the web address of your Collibra instance (e.g., `https://your-company.collibra.com`)
- **API Token** — a service account token with import permissions
- **Community ID** — the Collibra Community where assets will be placed
- **Domain ID** — the Collibra Domain within that Community

### Step 2 — Open Model Settings

1. In Tessallite, open the project and model you want to sync.
2. Click the **gear icon** (⚙) in the top-right corner of the Model Builder.
3. The Model Configuration drawer opens.

### Step 3 — Navigate to the Collibra tab

1. In the settings drawer, scroll the tabs until you see **Collibra**.
2. Click the Collibra tab.

### Step 4 — Add a connection

1. Click the **Add Connection** button.
2. Fill in the dialog:
   - **Display Name** — a friendly name like "Collibra Production"
   - **Base URL** — the URL from Step 1
   - **API Token** — the token from Step 1 (kept encrypted)
   - **Community ID** — the Collibra Community ID from Step 1
   - **Domain ID** — the Collibra Domain ID from Step 1
3. Click **Save**.

### Step 5 — Test the connection

1. Click the **Test Connection** button.
2. In this build the connection check is **simulated** — Tessallite does not yet contact your Collibra instance. Your configuration is saved, but it is not verified against a live Collibra API.
3. You will see an informational notice that the validation was simulated. This is not a green pass: a wrong URL or expired token will not be caught until live validation is available.

### Step 6 — Preview what will be exported

1. Click the **Preview Export** button.
2. Tessallite builds the governance graph and shows you:
   - How many **assets** will be created (metrics, dimensions, tables, etc.)
   - How many **relations** will be created (relationships between them)
   - How many **responsibilities** will be assigned (ownership mappings)
3. The preview breaks down counts by asset type so you can verify coverage.

### Step 7 — Dry run

1. Click the **Dry Run** button.
2. Tessallite runs the full sync pipeline — builds the graph, maps to Collibra assets/relations/responsibilities, calculates what would change — but does NOT push to Collibra.

### Step 8 — Live push (not yet available)

Live push to Collibra is not implemented in this build, so the **Sync** button is disabled. The dry run in Step 7 lets you confirm exactly what would be created. When a live Collibra client is wired in, this button will push the previewed assets, relations, and responsibilities and record the result in run history.

## Available now: preview and dry run

Everything here works today. It reads your model and computes the governance
payload locally — nothing is sent to Collibra.

1. **Build governance graph** — reads all model objects from the snapshot.
2. **Map to Collibra format** — converts to assets (with attributes + status), relations, and responsibilities.
3. **Hash every object** — SHA256 fingerprint, ready to drive an incremental diff once live push exists.
4. **Record the run** — saves the run in history with the exact model snapshot it was built from, the asset/relation counts, and any governance warnings.

The dry run computes what a push *would* do; it does not contact Collibra and
does not create, update, or deprecate anything remotely.

**Incremental diff is not available yet.** Live push is not implemented in this
build, so no remote mapping is ever saved. With no saved baseline to compare
against, every dry run reports all objects as *new* — it cannot yet show which
objects changed or stayed the same since a previous sync. When live push lands,
the fingerprints above will drive an incremental create/update/deprecate diff.

## Not available yet: live connector

Live validation and live push are **not implemented** in this build. Until a
tenant-specific Collibra client is wired in, the following do NOT happen:

- **Live push** — the Sync button is disabled and the API returns "not
  implemented". No asset, relation, or responsibility is created or updated in
  Collibra.
- **Remote deprecation** — when you remove objects from your model, the dry run
  records them as removed locally, but nothing is marked Deprecated inside
  Collibra, because no remote call is made.
- **Live connection checks** — Test Connection is simulated (see Step 5); a
  wrong URL, expired token, or missing Community/Domain is not caught until live
  validation exists.

When the live client lands, this page will be updated with the remote push,
deprecation, and troubleshooting behaviour it introduces.

## Tips and best practices

- **Export the deployed version** — Preview and dry run use the deployed model state by default. Draft changes won't appear until deployed (or unless you enable "export draft").
- **Ownership flows through** — KPIs with an owner and downstream assets with an owner become Collibra responsibilities in the payload. Measures and dimensions have no owner field, so they export without a responsibility.
- **Read the governance warnings** — A dry run flags objects missing a description or owner, and KPIs / calculated measures whose expression could not be fully resolved.
- **Check the recorded snapshot** — Each run records the exact model snapshot it was built from.
- **Business vs Technical** — The export supports separate business and technical views. Hidden columns are included in technical exports with `is_hidden = true` metadata.
- **Asset type mappings** — The default asset type names work with standard Collibra installations. If your organization uses custom asset types, configure the mapping in the connection settings.

## Troubleshooting (preview / dry run)

These cover the preview and dry-run surfaces that work today. There is no live
troubleshooting yet because no remote call is made.

| Problem | Likely cause | Solution |
|---|---|---|
| "Connection not found" | No connection configured | Click Add Connection first |
| "Model not found" | Not inside a model | Open a model in Model Builder |
| Preview shows 0 assets | Model has no objects yet | Add tables, measures, dimensions first |
| Preview warns about unresolved KPI/measure lineage | A KPI or calculated-measure expression references a measure that is missing or misspelled | Open the measure/KPI and fix the reference |
| "Model not deployed" warning | Model has no deployed version | Deploy the model first |
| Missing asset types | Custom Collibra setup | Use the asset type mapping config to match your Collibra operating model |

## Related

- [Solidatus Integration](solidatus-integration.md) — The equivalent integration for Solidatus lineage platform
- [Model Configuration](../admin/model-configuration.md) — All model-level settings
- [Business Glossary](../modelling/business-glossary.md) — Manage business terms
- [Usage & Downstream Assets](../modelling/usage-downstream-assets.md) — Tag dashboards, reports, and APIs
- [API Reference](api-reference.md) — REST API documentation

---

← [Solidatus Integration](solidatus-integration.md) | [Home](../index.md) | [Excel Connection Problems →](../troubleshooting/excel-connection-problems.md)
