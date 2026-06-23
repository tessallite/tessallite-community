# Solidatus Integration

Status: active. Updated 2026-06-13.

## What it is

The Solidatus integration lets you push your Tessallite semantic model metadata into Solidatus, the enterprise lineage and governance graph platform. When connected, Solidatus becomes a visual map of your data landscape — showing where every metric, dimension, KPI, and downstream report comes from.

## Who it is for

- **Modellers** who want their semantic models visible in the enterprise governance tool
- **Tenant Admins** who configure the connection to the Solidatus instance
- **Governance teams** who browse lineage in Solidatus

## What gets exported

When you sync a model to Solidatus, Tessallite exports these objects as graph nodes and edges:

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

## How sync works (under the hood)

Each time you sync, Tessallite:

1. **Builds a governance graph** — reads every table, column, dimension, measure, KPI, glossary term, downstream asset, aggregate, and data tag from your model.
2. **Maps to Solidatus format** — converts each Tessallite object into a Solidatus node or edge.
3. **Hashes every object** — creates a SHA256 fingerprint of each node and edge.
4. **Compares with previous sync** — checks which objects are new, changed, or unchanged since the last sync.
5. **Pushes only what changed** — sends only new and updated objects to Solidatus. Unchanged objects are skipped.
6. **Saves the mapping** — records which Tessallite objects map to which Solidatus objects, so the next sync can be incremental.

This means the first sync sends everything, but subsequent syncs only send what changed — making it fast and efficient.

## Tips and best practices

- **Sync after deploy** — The sync exports the deployed version of your model. If you make changes but haven't deployed yet, the sync won't see them (unless you enable "export draft" in the sync options).
- **Run a preview first** — Always preview before syncing to check that the counts make sense.
- **Use dry run for changes** — Before a big model update, run a dry run to see what would change.
- **Check run history** — The run history table shows every sync attempt, including failures. If something goes wrong, check the error message.
- **Token security** — Your Solidatus API token is encrypted before storage and never returned by the API. If you need to update it, use the Edit button and enter a new token.
- **Deprecate, don't delete** — When you remove objects from your Tessallite model, the sync marks them as deprecated in Solidatus rather than deleting them. This preserves lineage history.

## Troubleshooting

| Problem | Likely cause | Solution |
|---|---|---|
| "Connection not found" | No connection configured | Click Add Connection first |
| "Model not found" | Not inside a model | Open a model in Model Builder |
| Sync fails with error | Invalid token or URL | Edit the connection and fix the URL/token |
| Preview shows 0 nodes | Model has no objects yet | Add tables, measures, dimensions first |
| Solidatus doesn't show new objects | Workspace ID is wrong | Check the Workspace ID in your connection settings |
| "Model not deployed" warning | Model has no deployed version | Deploy the model first, or enable export_draft |

## Related

- [Collibra Integration](collibra-integration.html) — The equivalent integration for Collibra governance platform
- [Model Configuration](../modelling/model-settings.html) — All model-level settings
- [Lineage Panel](../modelling/lineage.html) — View lineage within Tessallite
- [API Reference](api-reference.html) — REST API documentation

---

← [Embed Agent Chat](embed-agent-chat.md) | [Home](../index.md) | [Collibra Integration →](collibra-integration.md)
