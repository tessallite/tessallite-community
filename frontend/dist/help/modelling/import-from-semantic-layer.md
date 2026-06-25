---
title: "Import from a Semantic Layer Tool"
audience: modeller
area: modelling
updated: 2026-05-21
---

## What this covers

Tessallite can import semantic model definitions from three external platforms: **dbt** (MetricFlow v1.7+), **AtScale** (SML), and **Cube.dev**. The importer reads your existing YAML definitions and produces a Tessallite model with dimensions, measures, hierarchies, and a default persona. This article covers what each importer supports, what gets converted, and what needs manual setup after import.

---

## Supported formats

| Platform | Input format | Key construct |
|---|---|---|
| dbt MetricFlow | YAML with `semantic_models:` and `metrics:` keys | Semantic models, metrics, saved queries |
| AtScale SML | Multi-file YAML project (models, datasets, dimensions, metrics, calculations) | SML object types with `object_type:` key |
| Cube.dev | YAML with `cubes:` key | Cubes with measures, dimensions, joins |

---

## What gets imported

All three importers produce the same Tessallite bundle format. The table below shows what transfers and what needs manual attention.

| Concept | dbt | AtScale | Cube.dev |
|---|---|---|---|
| Tables | From `model` ref | From datasets | From `sql_table` |
| Columns | From measure/dimension `expr` | From dataset columns | From measure/dimension `sql` |
| Measures | Mapped (sum, count, count_distinct, average, min, max, median, percentile) | Mapped (20 calculation methods including sum, count, average, min, max, median, percentile, stddev) | Mapped (count, count_distinct, sum, avg, min, max, number, running_total) |
| Dimensions | categorical and time | standard, time, degenerate | string, number, time, boolean, geo |
| Hierarchies | Auto-generated from time dimensions | From dimension hierarchy/level definitions | Explicit + auto-generated from time dimensions |
| Joins | Warning only (foreign entities signal relationships) | From model relationships | From cube join definitions |
| Format strings | Not available | Preserved from metric format field | Preserved from measure format field |
| Personas | Default "Everyone" created | Default "Everyone" created | Default "Everyone" created; private cubes skipped |

---

## What needs manual setup after import

Every import produces a warnings list that tells you exactly what needs attention. Common post-import tasks:

1. **Connections** -- The imported model has no database connections. Bind each table to a Tessallite connection pointing to your database.
2. **Joins** -- dbt imports produce join warnings from foreign entities. AtScale and Cube imports map joins automatically, but review them for correctness.
3. **Calculated measures** -- dbt derived/ratio/cumulative metrics, AtScale MDX calculations, and Cube rolling_window/multi_stage measures all produce warnings. Create equivalent calculated measures in Tessallite.
4. **Row security** -- AtScale row_security objects produce a security warning. Configure personas and row-level security rules in Tessallite.
5. **Deploy** -- Save and deploy the model to make it available via XMLA and JDBC.

---

## How to import

### Via the API

```
POST /api/v1/projects/{project_id}/import/{format}
```

Where `{format}` is `dbt`, `atscale`, or `cube`. Send the YAML file (or a zip of the project directory for multi-file formats) as a file upload.

The response includes:
- `models_parsed` -- number of models found
- `warnings` -- items that need manual attention
- `bundle` -- the full Tessallite project bundle (JSON)

### Via the UI

1. Open your project in the Explorer.
2. Click **Import** next to **Add Model**.
3. Select the import format (dbt, AtScale, or Cube).
4. Upload your YAML file or project zip.
5. Review the parsed models and warnings.
6. Click **Import** to create the model.

---

## Tips

- **Multi-file projects**: dbt and Cube importers scan all `.yml` and `.yaml` files in a zip. AtScale imports expect the standard SML directory layout (models/, datasets/, dimensions/, metrics/, calculations/).
- **Private/hidden items**: Cube cubes, dimensions, and measures marked `public: false` are excluded from import. AtScale metrics with `is_hidden: true` are imported but may warrant review.
- **Labels**: All three importers use label/title fields as display names when available, falling back to humanized versions of the slug.
- **Unsupported aggregations**: Any aggregation type not in the mapping table defaults to `sum` with a warning.

---

## Import from a data catalog

Besides YAML files, Tessallite can pull table and column metadata straight from a running **data catalog** — **DataHub**, **OpenMetadata**, or **Alation** — and build a starter model from it. Use this when your tables are already catalogued and you would rather not hand-write a model from scratch. This is a *live API* import, not a file upload: Tessallite calls the catalog's API at import time and reads what it returns.

**How it works.** You give Tessallite the catalog's API URL and an API token, and optionally a dataset filter to limit which tables come across. Tessallite reads the catalogued tables and columns and creates a model where **numeric columns become measures** and the rest become **dimensions**. The model is created **undeployed** and bound to a **placeholder connection** — you point it at the real source database and review the model before you deploy and query.

**To run it:**

1. Open the Model Builder and choose **Import**.
2. Pick **Data catalog (DataHub / OpenMetadata / Alation)** as the format.
3. Choose the **Catalog system**, then paste the **Catalog API URL** and an **API token**.
4. Optionally set a **Dataset filter** to import only datasets whose name matches a pattern. Leave it blank to import everything the catalog returns.
5. Optionally set a **Model name**, then run the import. Tessallite reports how many models, tables, measures, and dimensions it created, plus any warnings.

**What needs your attention afterwards:**

- The starter model is a *first draft*. Numeric-vs-dimension is a guess based on column type, so re-classify anything that came across the wrong way (an ID column that is numeric but is really a dimension, for example).
- Configure the **real source connection** before you preview or deploy — the import uses a placeholder that cannot run queries.
- **Joins and hierarchies are not inferred** from the catalog. Add them by hand once the tables are in, the same as any other model.

**Tip:** give the API token a *read-only* catalog role. Tessallite only reads metadata; it never needs write access to your catalog.

---

## Related articles

- [Export and Import a Model](export-and-import-a-model.md)
- [Export and Import a Project](export-and-import-a-project.md)
- [Define Measures](define-measures.md)
- [Define Dimensions](define-dimensions.md)

---

<- [Export and Import a Project](export-and-import-a-project.md) | [Home](../index.md) | [Impact Analysis ->](impact-analysis.md)
