# Solidatus Integration

Status: active. Updated 2026-06-13.

## What it is

The Solidatus integration builds a Solidatus-shaped lineage graph of your Tessallite semantic model — showing where every metric, dimension, KPI, and downstream report comes from — so you can preview exactly what would land in Solidatus, the enterprise lineage and governance graph platform.

In this build the integration is a **preview / dry-run** tool. It reads your model and computes the graph locally; it does not send anything to Solidatus. Live push (writing the nodes and edges into your Solidatus instance) is not yet available — see [Not available yet: live connector](#not-available-yet-live-connector).

## Who it is for

- **Modellers** who want their semantic models visible in the enterprise governance tool
- **Tenant Admins** who configure the connection to the Solidatus instance
- **Governance teams** who browse lineage in Solidatus

## What gets exported

When you preview or dry-run a model export, Tessallite maps these objects into Solidatus graph nodes and edges (computed locally — nothing is written to Solidatus yet):

| Tessallite object | Solidatus representation |
|---|---|
| Project | Business Domain |
| Model | Semantic Model |
| Data Source | Source System |
| Tables + Columns | Table + Column nodes |
| Dimensions | Dimension nodes |
| Measures | Metric nodes |
| KPIs | KPI nodes |
| Glossary terms | Business Term nodes |
| Downstream assets | Consumer Asset nodes (dashboards, reports, APIs) |
| Aggregates | Materialized Aggregate nodes |
| Data tags | Governance Tag nodes |

Relationships (edges) connect these nodes — for example, a Source System "contains" a Table, which "contains" Columns, which "define" Dimensions and Measures.

## Step-by-step: Set up and sync

### Step 1 — Get your Solidatus details

Before you start, ask your Solidatus administrator for:

- **Base URL** — the web address of your Solidatus instance (e.g., `https://customer.solidatus.example.com`)
- **API Token** — a service account token that can import models
- **Workspace ID** — the Solidatus workspace or model where Tessallite objects will land
- **Model Ref** — a name for the Tessallite model inside Solidatus (e.g., `tessallite-sales-domain`)

### Step 2 — Open Model Settings

1. In Tessallite, open the project and model you want to sync.
2. Click the **gear icon** (⚙) in the top-right corner of the Model Builder.
3. The Model Configuration drawer opens.

### Step 3 — Navigate to the Solidatus tab

1. In the settings drawer, scroll the tabs at the top until you see **Solidatus**.
2. Click the Solidatus tab.

If you don't see the Solidatus tab, make sure you are inside a model (not just a project). The tab only appears when a model is selected.

### Step 4 — Add a connection

1. Click the **Add Connection** button.
2. Fill in the dialog:
   - **Display Name** — a friendly name like "Solidatus Production"
   - **Base URL** — the URL from Step 1
   - **API Token** — the token from Step 1 (kept encrypted — never shown again)
   - **Workspace ID** — the workspace ID from Step 1 (optional)
   - **Model Ref** — a reference name for this model in Solidatus (optional)
3. Click **Save**.

### Step 5 — Test the connection

1. Click the **Test Connection** button.
2. In this build the connection check is **simulated** — Tessallite does not yet contact your Solidatus instance. Your configuration is saved, but it is not verified against a live Solidatus API.
3. You will see an informational notice that the validation was simulated. This is not a green pass: a wrong URL or expired token will not be caught until live validation is available.

### Step 6 — Preview what will be exported

1. Click the **Preview Export** button.
2. Tessallite builds the governance graph from your model and shows you:
   - How many **nodes** will be exported (tables, columns, measures, etc.)
   - How many **edges** will be exported (relationships between them)
3. Review the counts to make sure everything looks right.

### Step 7 — Dry run

1. Click the **Dry Run** button.
2. Tessallite runs the full sync pipeline — builds the graph, maps it to Solidatus format, and calculates what would change — but does NOT push anything to Solidatus.
3. The run history shows what WOULD be created or updated.

### Step 8 — Live push (not yet available)

Live push to Solidatus is not implemented in this build, so the **Sync** button is disabled. The dry run in Step 7 lets you confirm exactly what would be created. When a live Solidatus client is wired in, this button will push the previewed nodes and edges and record the result in run history.

## Available now: preview and dry run

Everything in this section works today. It reads your model and computes the
governance graph locally — nothing is sent to Solidatus.

Each time you preview or dry-run, Tessallite:

1. **Builds a governance graph** — reads every table, column, dimension, measure, KPI, glossary term, downstream asset, aggregate, and data tag from your model.
2. **Maps to Solidatus format** — converts each Tessallite object into a Solidatus node or edge.
3. **Hashes every object** — creates a SHA256 fingerprint of each node and edge, ready to drive an incremental diff once live push exists.
4. **Records the run** — saves the run in history with the exact model snapshot it was built from, the node/edge counts, and any governance warnings (for example, a KPI whose expression references a measure that is missing).

The dry run computes what a push *would* do; it does not contact Solidatus and
does not create, update, or deprecate anything remotely.

**Incremental diff is not available yet.** Live push is not implemented in this
build, so no remote mapping is ever saved. With no saved baseline to compare
against, every dry run reports all objects as *new* — it cannot yet show which
nodes or edges changed or stayed the same since a previous sync. When live push
lands, the fingerprints above will drive an incremental create/update/deprecate
diff.

## Not available yet: live connector

Live validation and live push are **not implemented** in this build. Until a
tenant-specific Solidatus client is wired in, the following do NOT happen:

- **Live push** — the Sync button is disabled and the API returns "not
  implemented". No node or edge is created or updated in Solidatus.
- **Remote deprecation** — when you remove objects from your model, the dry run
  records them as removed locally, but nothing is deprecated inside Solidatus,
  because no remote call is made.
- **Live connection checks** — Test Connection is simulated (see Step 5); a
  wrong URL or expired token is not caught until live validation exists.

When the live client lands, this page will be updated with the remote push,
deprecation, and troubleshooting behaviour it introduces.

## Tips and best practices

- **Export the deployed version** — Preview and dry run use the deployed version of your model by default. If you make changes but haven't deployed yet, they won't appear unless you enable "export draft".
- **Run a preview first** — Always preview to check that the counts make sense.
- **Use dry run before a big change** — See what would change before it changes.
- **Read the governance warnings** — A dry run flags objects missing a description or owner, and KPIs / calculated measures whose expression could not be fully resolved. Fixing these makes the future export complete.
- **Check the recorded snapshot** — Each run records the exact model snapshot it was built from, so you can prove which model version a preview came from.
- **Token security** — Your Solidatus API token is encrypted before storage and never returned by the API. If you need to update it, use the Edit button and enter a new token.

## Troubleshooting (preview / dry run)

These cover the preview and dry-run surfaces that work today. There is no live
troubleshooting yet because no remote call is made.

| Problem | Likely cause | Solution |
|---|---|---|
| "Connection not found" | No connection configured | Click Add Connection first |
| "Model not found" | Not inside a model | Open a model in Model Builder |
| Preview shows 0 nodes | Model has no objects yet | Add tables, measures, dimensions first |
| Preview warns about unresolved KPI/measure lineage | A KPI or calculated-measure expression references a measure that is missing or misspelled | Open the measure/KPI and fix the reference |
| "Model not deployed" warning | Model has no deployed version | Deploy the model first, or enable export_draft |

## Related

- [Collibra Integration](collibra-integration.md) — The equivalent integration for Collibra governance platform
- [Model Configuration](../admin/model-configuration.md) — All model-level settings
- [View Model Lineage](../modelling/view-model-lineage.md) — View lineage within Tessallite
- [API Reference](api-reference.md) — REST API documentation

---

← [MCP Server](mcp-server.md) | [Home](../index.md) | [Collibra Integration →](collibra-integration.md)
