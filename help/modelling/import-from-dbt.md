---
title: "Import from dbt"
audience: modeller
area: Modelling
updated: 2026-05-22
---

## What this covers

If your team already defines metrics in dbt, you can bootstrap a Tessallite model from a dbt project instead of building it by hand. The importer reads dbt's `semantic_models` and `metrics` and maps them onto Tessallite tables, dimensions, and measures, giving you a working model to review, connect, and deploy.

---

## What gets imported

| dbt concept | Tessallite equivalent |
|---|---|
| `semantic_model` | A model table (the entity it is defined on becomes the source table) |
| `entity` (primary / foreign) | Join keys used to relate tables |
| `dimension` (categorical) | A dimension |
| `dimension` (time, with grain) | A time dimension with its grain |
| `measure` | A measure with its aggregation (sum, count, average, and so on) |
| `metric` (simple) | A measure or calculated measure |
| `metric` (ratio / derived / cumulative) | Imported with a warning for manual review |

---

## Run the import

Provide the dbt definitions either as a single `schema.yml` (or the project's `semantic_models` YAML) or as a zip of the dbt project. The importer accepts dbt v1.7 and later.

**From the API:**

```bash
curl -X POST http://localhost:3000/api/v1/projects/<project_id>/import/dbt \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@my_dbt_project.zip"
```

The response reports how many models, dimensions, and measures were created, and lists any metrics that need manual follow-up.

---

## After importing

1. **Bind a connection.** The imported model references table and column names from dbt but has no live credentials. Attach a data source so Tessallite can read the underlying tables — see [Manage Connections](manage-connections.md).
2. **Review classifications.** Confirm the fact and dimension tables, join keys, and measure aggregations are what you expect. Adjust on the canvas as needed.
3. **Resolve metric warnings.** Ratio, derived, and cumulative metrics are imported as a best effort; rebuild them as [Calculated Measures](calculated-measures.md) or [time variants](configure-time-variants.md) where the warning asks you to.
4. **Save and deploy.** Once the model validates, save a version and deploy it so BI tools and the agent can query it — see [Deploy a Model](deploy-a-model.md).

---

## Limitations

- The importer maps structure and definitions, not data. You still bind a live connection to query.
- Complex metric types (ratio, derived, cumulative) generate warnings and may need to be re-expressed as calculated measures or time variants.
- dbt tests and freshness checks are not imported; use [Data Quality Rules](data-quality-rules.md) for equivalent validation in Tessallite.

---

## Related

- [Create a Project](create-a-project.md)
- [Add a Data Source](add-a-data-source.md)
- [Calculated Measures](calculated-measures.md)
- [Export and Import a Model](export-and-import-a-model.md)

---

← [Export and Import a Project](export-and-import-a-project.md) | [Home](../index.md) | [Model Templates →](model-templates.md)
