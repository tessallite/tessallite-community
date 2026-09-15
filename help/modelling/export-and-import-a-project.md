---
title: "Export and Import a Project"
audience: tenant-admin
area: modelling
updated: 2026-08-27
---

## What this covers

Project-level export captures an entire project as a single JSON file: every model, connection, LLM configuration, agent setup, cross-model recipe, and project setting. You can import this bundle to create a brand-new project in the same tenant (or a different one), or to replace an existing project wholesale. This is the recommended way to promote a project between environments (dev, UAT, prod) and to snapshot before destructive testing.

This article covers export options, the import workflow, how connections and credentials are handled, and what happens to materialized objects after import.

---

## When to use project export vs model export

| Scenario | Use |
|---|---|
| Move one model between projects | [Model export](export-and-import-a-model.md) |
| Promote an entire project (all models, config, connections) to another environment | Project export |
| Snapshot a project before running destructive automated tests | Project export |
| Back up a single model before editing | [Model export](export-and-import-a-model.md) |
| Clone a project within the same tenant under a different slug | Project export in create mode |

---

## Before you start

- **Export** requires the Admin role on the project.
- **Import** requires Tenant Admin or System Admin privileges.
- If you plan to include credentials in the export, choose a passphrase at least 8 characters long. You will need the same passphrase on import.
- For a locked hosted-demo tenant, Tessallite rejects credential-bearing exports before loading credential keys or project data. This applies when the tenant is listed in `DEMO_SOURCE_LOCKED_TENANTS` or is classified as a demo by its licence.
- Metadata-only export remains available to an authorized project admin for a locked hosted-demo tenant.

---

## Opening the dialog

Project import and export are accessed from a single **Import / Export** button (swap-arrows icon) in the Explorer sidebar, below the tenant card. This button is visible to admin users only. Clicking it opens a dialog with two tabs: **Import** and **Export**.

---

## Exporting a project

1. In the Explorer sidebar, click the **Import / Export** button.
2. Switch to the **Export** tab.
3. Select the project you want to export from the dropdown.
4. Choose which sections to include under **Included in export**:

| Section | Default | What it contains |
|---|---|---|
| Connections | On | Connection configs (host, port, database, etc.) |
| LLM Configurations | On | AI provider endpoints, model names, parameters |
| Agent Configuration | On | Agent persona, judge rubrics, model contexts, webhook URL |
| Cross-Model Recipes | On | Multi-model calculation recipes |
| Project Settings | On | All key-value project settings |
| Access Bindings | Off | User-role assignments for the project and its models |

5. Optionally check **Include credentials (connections, API keys)**. When enabled, two passphrase fields appear. Enter and confirm a passphrase (minimum 8 characters). Credentials are encrypted with this passphrase using PBKDF2 key derivation and Fernet symmetric encryption. Without the passphrase, the encrypted data is unreadable.
6. Click **Export**. The browser downloads a file named `{slug}-export-{date}.json`.

Models are always included regardless of section selection.

---

## What is and is not in a project export

| In the export | Not in the export |
|---|---|
| Project metadata (slug, display name, active flag) | Source data, target data, query results |
| All models with full snapshots and version history | Query logs, miss logs, alert history |
| Connection configurations | System-level and tenant-level settings |
| LLM provider configurations | Materialized aggregate data |
| Agent configuration, rubrics, model contexts | Agent conversation history |
| Cross-model recipes | Scheduler run history |
| Project settings | Pocket table data |
| Access bindings (optional) | |
| Credentials (optional, encrypted) | |

---

## Importing a project

1. In the Explorer sidebar, click the **Import / Export** button. The dialog opens on the **Import** tab by default.
2. Click **Choose project export file (.json)** and select the bundle.
3. The dialog shows a summary: project name, model count, included sections, and whether credentials are present.
4. If the bundle includes credentials, enter the passphrase used during export.
5. Choose an import mode:

### Create new project

Creates a fresh project. The slug defaults to the exported slug but can be overridden. If the slug already exists in the tenant, the import fails with an error asking you to choose a different slug or use replace mode.

### Replace existing project

Deletes all existing content in the target project (models, connections, LLM configs, agent config, recipes, settings, access bindings) and restores everything from the bundle. The project slug must match an existing project. A confirmation dialog warns that this action cannot be undone.

6. Optionally override slugs:
   - **Project slug** and **display name**: change the target project identity.
   - **Model slug overrides**: expand the model slug section to rename individual models on import.

7. **Connection mapping**: if the bundle includes connections, the dialog shows each exported connection with its type. Connections are resolved in this order:
   - If credentials are included, each connection can be created new or mapped to an existing connection.
   - If credentials are not included, each connection must be mapped to an existing connection of matching type.
   - When creating new connections from a bundle with credentials, the credentials are decrypted with your passphrase and re-encrypted under the target tenant's system key.

8. Optionally check **Override existing connections with exported credentials** to update credentials on mapped connections.

9. **Preview before applying (replace mode).** Before committing a destructive replace, run a **dry run** first. A dry run does not touch the target project at all — it does not decrypt credentials, delete anything, or create anything, and it does not even need the export passphrase. Instead it returns a *plan* showing exactly how many objects in each section (models, connections, personas, and so on) would be deleted and recreated if you went ahead. Review the counts; if they look right, run the real import. This turns "replace wholesale" from a leap of faith into a checked step.

10. Click **Import**.

Before applying either mode, Tessallite validates every cross-model recipe in the bundle. Recipe steps are remapped to the new model IDs, and their measure and combine references must resolve against the imported model definitions. If a recipe is malformed or names a model that is not in the bundle, the whole import stops before creating or deleting project content. The error identifies the recipe and exact JSON path.

---

## After import

After a successful import, the dialog shows a summary with any warnings and required actions.

### What resets on import

- **Aggregates** are set to **pending**. They need a refresh cycle to build their physical tables.
- **Pocket tables** are set to **stale**. They rebuild on the next scheduled or manual refresh.
- **Deployed version** is cleared. Each imported model starts as an undeployed draft. You must explicitly deploy each model before it serves queries through BI tools or the query router.

### Why materialized objects reset

Aggregates and pockets are physical tables in the target database. The exported bundle contains their definitions (which measures, which dimensions, which refresh schedule) but not the data. Resetting their status tells the scheduler to rebuild them from scratch against the target's actual data connections.

### Post-import checklist

1. Review any warnings shown in the import result.
2. For each model: open the Model Builder, verify the data source connections are correct, then Save and Deploy.
3. If the project uses the conversational agent, verify the LLM configuration and re-test the agent.
4. Run a manual refresh to trigger aggregate and pocket rebuilds.

---

## Tips

- Export bundles are stable JSON. You can commit them to version control for auditing or rollback.
- Use create mode for environment promotion (dev to UAT) and replace mode for resetting a project to a known state.
- When credentials are not included, you must have matching connections already set up in the target tenant before importing.
- Access bindings reference users by identity (email). If a user does not exist in the target tenant, the binding is skipped with a warning.
- The import runs in a single database transaction. If any step fails, everything rolls back cleanly.

---

## Related articles

- [Export and Import a Model](export-and-import-a-model.md)
- [Save and Version a Model](save-and-version-a-model.md)
- [Deploy a Model](deploy-a-model.md)
- [Project Settings](../admin/project-settings.md)

---

← [Export and Import a Model](export-and-import-a-model.md) | [Home](../index.md) | [Import from dbt →](import-from-dbt.md)
