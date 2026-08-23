---
title: "Export and Import a Model"
audience: modeller
area: modelling
updated: 2026-05-28
---

![Explorer page showing Add Model and Import buttons.](../assets/screencaps/explorer-import-button.png)

## What this covers

You can export any model as a single JSON file and import it into the same project, a different project in the same tenant, or a different tenant entirely. Connections never travel — credentials stay in the source database — so on import you pick local connections to rebind the model to. This article covers the export download, the import dialog, and how connections are remapped.

---

## Before you start

- To export, you need access to the source model.
- To import, you need access to the target project and at least one connection of each type referenced by the bundle (one Postgres connection per Postgres source, one BigQuery target per BigQuery target, etc.).
- Connections must already exist in the target project. If you do not have them, create them via **Connections** before importing.

---

## Opening the dialog

Both model export and import are accessed from a single **Import / Export** button (swap-arrows icon) in the project toolbar in the Explorer, or in the Model Builder toolbar. Clicking this button opens a dialog with two tabs: **Import** and **Export**.

---

## Exporting a model

1. Click the **Import / Export** button in the project toolbar (Explorer) or Model Builder toolbar.
2. Switch to the **Export** tab.
3. Select a format from the dropdown: **Tessallite Model (.json)**, **Tessallite YAML (.zip)**, or **LookML (.zip)**.
4. For Tessallite Model format, click the model you want to export from the list.
5. Tessallite downloads a file named `{model_slug}.tessallite.json` to your browser's download folder.
6. The Tessallite Model (.json) file is a single JSON document that carries the model's full definition: tables, columns, joins, dimensions, measures (including time variants), hierarchies, calendars, user-defined attributes, personas, row security, column/data tags, KPIs, named sets, named queries, glossary, drill-through sets, parameters, aggregates and pockets (as rebuild-pending), refresh and AI-scheduler config, model settings, and the canvas layout. (The authoritative, machine-checked list is the snapshot coverage matrix, `docs/architecture/architecture_snapshot-coverage-matrix.md`.)

The file does **not** contain credentials, query history, miss logs, alerts, or anything stored at tenant, project, or system scope. The **YAML** and **LookML** exports are deliberately partial: each declares what it drops (YAML in an in-file `not_exported` block; LookML skips calculated/variant measures with a warning), so use Tessallite Model (.json) when you need a complete, re-importable copy.

---

## Importing a model

1. Click the **Import / Export** button in the project toolbar (Explorer) or Model Builder toolbar.
2. The dialog opens on the **Import** tab by default.
3. Select a format from the dropdown: **Tessallite Model (.json)**, **Tessallite YAML (.zip)**, **dbt (.yml / .zip)**, **Cube (.yml / .zip)**, **AtScale SML (.zip)**, or **Catalog** (pull metadata live from DataHub / OpenMetadata / Alation into a starter model).
4. For Tessallite Model format, click **Choose .tessallite.json file** and pick the export file.
5. The dialog reads the bundle and shows:
   - The original model's slug and display name (you can override either).
   - Every connection referenced by the bundle, with the source's display name and connection type.
6. For each referenced connection, pick a local connection from the dropdown. Only connections of the matching type appear.
7. Optionally tick **Deploy immediately after import** to save v1 and deploy in one click.
8. Click **Import**. Tessallite creates a new model in the target project, rewrites every internal UUID, rebinds the connections, and (if you ticked Deploy) saves and deploys v1.
9. The Explorer navigates to the new model.

If the source slug already exists in the target project, Tessallite auto-suffixes (`sales`, `sales-2`, `sales-3`).

---

## What is and is not in an export

| In the export | Not in the export |
|---|---|
| Tables, columns, joins, hierarchies, UDAs | Connection credentials |
| Dimensions, measures | Source data, target data |
| Aggregate definitions and refresh policies | Query logs, miss logs, alerts |
| Per-model AI scheduler config and model settings | System / tenant / project settings |
| Canvas layout (table positions, viewport) | Deployed-version pointer (the import always starts undeployed) |

---

## Tips

- Export is the supported way to move a model between environments (dev → staging → prod). The bundle is stable JSON, safe to commit to git.
- Connection rebinding is mandatory. The importer refuses to proceed if any source or target has no mapping.
- Re-imports do not merge — every import creates a brand-new model with a fresh slug and fresh internal UUIDs.

---

## Related articles

- [Save and Version a Model](save-and-version-a-model.md)
- [Deploy a Model](deploy-a-model.md)
- [Add a Data Source](add-a-data-source.md)

---

← [Deploy a Model](deploy-a-model.md) | [Home](../index.md) | [Export and Import a Project →](export-and-import-a-project.md)
