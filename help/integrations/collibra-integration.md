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
| Downstream assets (dashboards, reports, APIs) | Report assets |
| Aggregates | Table assets (materialized) |
| Data tags | Data Classification assets |
| Materialisation targets (technical export only) | Data Store assets |

Every downstream asset uses the same default asset type (`Report`), whatever kind of consumer it represents; if your Collibra operating model separates dashboards from reports or applications, override the asset type as described in Step 4.

Each asset carries its Tessallite attributes (descriptions, types, formulas, visibility). Relationships link assets together — for example, a Metric "is based on" a Measure, which "is source of" a Column.

Ownership information becomes Collibra responsibilities, so stewards can see who owns each governed asset. Ownership is exported for objects that carry an explicit owner: **KPIs** (their owner) and **downstream assets** (reports, dashboards, applications). Measures and dimensions do not carry an owner field, so they are exported without a responsibility. The responsibility carries the owner value exactly as it is stored on the object in Tessallite — for a KPI that is the internal Tessallite user identifier — so plan how those identifiers map to Collibra users or groups before a live push is available.

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

The tab appears only for users who may author this model. Creating, editing,
deleting, activating and deactivating a connection require a tenant admin;
Test Connection and Dry Run require a modeller; the preview, run history and
mapping lists are readable by any user who can open the model.

### Step 4 — Add a connection

1. Click the **Add Connection** button.
2. Fill in the dialog:
   - **Display Name** — a friendly name like "Collibra Production"
   - **Base URL** — the URL from Step 1
   - **API Token** — the token from Step 1 (kept encrypted)
   - **Community ID** — the Collibra Community ID from Step 1
   - **Domain ID** — the Collibra Domain ID from Step 1
3. Click **Save**.

The dialog collects those five values and nothing else. A connection can also
carry custom asset-type, relation-type and responsibility-role names for a
Collibra operating model that does not use the defaults, but those overrides
are not editable in this dialog: set them with the Collibra configuration API
(`PUT /api/v1/projects/{project_id}/models/{model_id}/collibra/config/{connection_id}`,
fields `asset_type_mapping`, `relation_type_mapping`,
`responsibility_mapping`). The effective mapping — defaults merged with your
overrides — is readable at
`GET /api/v1/projects/{project_id}/models/{model_id}/collibra/asset-types?connection_id={connection_id}`.

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

A preview is a calculation only: it does not create a run in history and it
contacts nothing outside Tessallite.

### Step 7 — Dry run

1. Click the **Dry Run** button.
2. Tessallite runs the full sync pipeline — builds the graph, maps to Collibra assets/relations/responsibilities, calculates what would change — but does NOT push to Collibra.
3. A row appears in **Run History** with mode `Dry run`, status `Succeeded`, the asset and relation counts, the model snapshot the run was built from, and any governance warnings.

The run row is the only thing the dry run writes. No object mapping, no
deprecation record, and no remote change is stored.

### Step 8 — Live push (not yet available)

Live push to Collibra is not implemented in this build. In the Collibra tab the
push button is disabled and reads **Sync Unavailable**; hovering it explains
that live push is not implemented and that Dry Run is the way to validate the
export payload. Pressing it does nothing, because it cannot be pressed.

Calling the API directly does not get further. A sync request with
`dry_run: false` is refused at the boundary with HTTP 501 and the code
`collibra_push_not_implemented`, before any graph is built, and the refused
attempt is recorded as a `collibra.sync.rejected` audit event naming the
connection and the reason.

The dry run in Step 7 lets you confirm exactly what would be created. When a
live Collibra client is wired in, this button will push the previewed assets,
relations, and responsibilities and record the result in run history.

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

- **Live push** — the push button reads **Sync Unavailable** and is disabled,
  and the API refuses a non-dry-run request with "not implemented". No asset,
  relation, or responsibility is created or updated in Collibra.
- **Remote deprecation** — removing an object from your model deprecates
  nothing, in Collibra or in Tessallite. The deprecation step runs only in push
  mode, which is refused, so a dry run neither marks the object Deprecated
  remotely nor records the removal locally.
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
- **Asset type mappings** — The default asset type names work with standard Collibra installations. If your organization uses custom asset types, set the per-connection overrides through the configuration API shown in Step 4; there is no mapping editor in the Collibra tab yet.

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
| Preview shows a Collibra asset type your operating model does not have | Your Collibra setup renames or omits the default types | Set the per-connection type overrides through the configuration API in Step 4. Tessallite cannot detect the mismatch for you: the missing-type check belongs to live validation, which is not implemented, so Test Connection always reports no missing types |

## Related

- [Solidatus Integration](solidatus-integration.md) — The equivalent integration for Solidatus lineage platform
- [Model Configuration](../admin/model-configuration.md) — All model-level settings
- [Business Glossary](../modelling/business-glossary.md) — Manage business terms
- [Usage & Downstream Assets](../modelling/usage-downstream-assets.md) — Tag dashboards, reports, and APIs
- [API Reference](api-reference.md) — REST API documentation

---

← [Solidatus Integration](solidatus-integration.md) | [Home](../index.md) | [Excel Connection Problems →](../troubleshooting/excel-connection-problems.md)
