---
title: "Projects and Models"
audience: modeller
area: concepts
updated: 2026-04-17
---

## What this covers

This article explains what projects and models are, how they differ, and how the Model Builder is organised.

---

## Projects

A project is a container for one or more models. Projects exist within a workspace and group related models — for example, all models related to sales in one project, all models related to inventory in another.

A project has a name, a creation date, and a list of members. Each member has a role within the project: Modeller or Viewer.

Projects are created by Modellers or Tenant Admins.

---

## Models

A model is the core object in Tessallite. It defines a single business fact — the central event or transaction being analysed. A model is built by a Modeller using the Model Builder.

A model contains:

- **Tables** — the source data tables drawn from a connected data source. One table must be of type `fact`. Additional tables of type `dim_aggregate` or `dim_detail` may be added.
- **Joins** — the relationships between tables. A join connects the fact table to a dimension table, or connects dimension tables to each other.
- **Dimensions** — the attributes by which the fact can be sliced and filtered. Each dimension maps to a column in one of the model's tables.
- **Measures** — the numeric values computed from the fact. Each measure defines an aggregation function (SUM, COUNT, AVG, and others) applied to a column.
- **Aggregates** — pre-computed summaries. Tessallite builds these automatically and uses them to answer recurring queries without scanning raw data.
- **Targets** — the data destination where aggregates are stored.

---

## The one-fact rule

Each model must have exactly one fact table. Multi-fact analysis is handled by creating one model per fact within the same project.

---

## The Model Builder

Selecting a model opens the Model Builder. It has five distinct areas.

**Canvas** — a visual diagram of the tables and joins in the model. Nodes are colour-coded: blue for fact tables, green for aggregate dimensions, yellow for detail dimensions. Drawing a line between two nodes creates a join.

**Toolbelt** — a vertical icon strip on the left side of the screen. Each icon opens a panel in the Drawer for managing one aspect of the model: Sources, Joins, Dimensions, Measures, Targets, Aggregates, Refresh, Lineage, Diagnostics.

**Drawer** — the detail panel on the right, showing the content of whichever Toolbelt panel is currently open.

**Summary Bar** — a bar below the page header showing the current count of tables, joins, dimensions, measures, and aggregates, and the target status. The Summary Bar is always visible.

**Model Health tab** — a dedicated view showing structural alerts, recent aggregate refresh runs, recent Optimiser runs, and any invalid objects.

---

## Table types

Tessallite classifies each table in a model into one of three types.

| Type | Description | When to use |
|---|---|---|
| `fact` | The central event or transaction table | One per model — the primary data being analysed |
| `dim_aggregate` | A dimension with low to moderate cardinality | Country, category, status — data that groups well |
| `dim_detail` | A high-cardinality dimension | Customer, product catalogue, address — data for lookup and filtering |

The table type affects how Tessallite builds aggregates and how the query router selects pre-computed summaries.

---

## Related

- [Workspaces and tenants](workspaces-and-tenants.md)
- [Add a data source](../modelling/add-a-data-source.md)
- [Define dimensions](../modelling/define-dimensions.md)
- [Define measures](../modelling/define-measures.md)
- [Create a project](../modelling/create-a-project.md)

---

← [Workspaces and Tenants](workspaces-and-tenants.md) | [Home](../index.md) | [Sources, Tables, and Joins →](sources-tables-and-joins.md)
