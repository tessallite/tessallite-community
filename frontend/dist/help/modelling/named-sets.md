---
title: "Named Lists (Named Sets)"
audience: modeller
area: modelling
updated: 2026-08-11
---

## What this covers

A named list (also called a named set) is a reusable collection of dimension members stored within a semantic model. Named lists let modellers pre-define member selections — top customers, active product categories, regional groupings — that BI tool users can apply as filters, row sets, or column sets without rebuilding the selection each time. This article explains the two kinds of named lists (MDX Named Sets and Tessallite Named Lists), how to create, preview, and refresh them, and how they surface in BI tools and SQL queries.

![The Named Sets panel for the acme demo ModelX. Four named lists are listed as cards, each with its type chip (Dynamic for the Top/Bottom-N sets, Filtered for the rule-based one), a Session scope chip, Edit and Delete actions, a plain-language description, and a display folder. The sets are Top 5 and Bottom 5 Countries by Revenue (Geography), Top 10 Channels by Revenue (Channels), and High-Revenue Account Types (Accounts). Buttons at the top offer "From template" and "Add set".](../assets/screencaps/named-sets-panel.png)

Each card shows the list's type and scope at a glance, a description for the people who will use it, and the display folder it is grouped under.

---

## Two kinds of named lists

The Named Sets panel has two tabs at the top: **MDX Named Sets** and **Tessallite Named Lists**. Each kind serves a different connection path.

| Kind | Connection path | How members are resolved |
|---|---|---|
| **MDX Named Sets** | XMLA (Excel PivotTable, Power BI) | The MDX expression evaluates at query time inside the XMLA engine. |
| **Tessallite Named Lists** | SQL / JDBC / REST | Stored member values expand into an `IN (...)` clause before the SQL is parsed. |

---

## MDX Named Sets

### Types

| Type | Description | When to use |
|---|---|---|
| **Fixed members** | A hand-picked list of specific dimension members. | When the members are stable and known in advance — e.g., a list of strategic accounts. |
| **Top N / Bottom N** | A dynamic list ranking members by a measure and returning the top or bottom N. | When the list should update automatically as data changes — e.g., top 10 products by revenue. |
| **Filtered** | Members matching one or more conditions on dimension attributes. | When membership is defined by business rules — e.g., customers in a specific region with orders above a threshold. |
| **Advanced MDX** | A raw MDX set expression. | When the other builder types cannot express the required logic. Preview is not available for raw MDX; deploy the model and use a BI tool to see results. |

### Creating an MDX Named Set

1. Open the Named Sets panel and make sure the **MDX Named Sets** tab is selected.
2. Click **Add Set**. The dialog opens with a blank definition form.
3. Enter a **Name** (internal identifier, must be unique within the model).
4. Optionally enter a **Display name** (label shown in BI tools) and **Description**.
5. Go to the **List Rule** tab and select the list type — Fixed, Top N, Filtered, or Advanced MDX.
6. Configure the type-specific fields.
7. Click **Preview** to see the resolved members (not available for Advanced MDX).
8. Click **Create**. The named set is available to BI tools after model deployment.

### Using MDX Named Sets in BI tools

After model deployment, MDX named sets appear in the XMLA metadata catalogue. In Excel, use **CUBESET** and **CUBERANKEDMEMBER** formulas to reference them. The Tessallite Excel plugin provides one-click insertion of these formulas from the Report Builder panel.

---

## Tessallite Named Lists

Tessallite Named Lists are designed for the SQL path (JDBC, REST, and DBeaver connections). They store a list of member values that expand into `IN (...)` clauses when referenced in SQL queries using the `@ListName` syntax.

### Definition types

Each Tessallite Named List has a definition type that controls how its members are produced:

| Definition type | What it does | When to use |
|---|---|---|
| **Fixed Members** | You enter values manually, one at a time or by pasting CSV. | When the values are known and stable — e.g., a fixed set of country codes. |
| **Top N** | Queries the source database for the top or bottom N dimension members ranked by a measure. | When the list should reflect the current data ranking — e.g., top 10 accounts by revenue. |
| **Filtered** | Queries the source database for dimension members matching filter conditions. | When membership is defined by attribute rules — e.g., products in category "Electronics" with stock above 100. |
| **Free-hand SQL** | You write a SQL query that returns a single column of values. | When no built-in builder can express the logic — e.g., a complex subquery joining multiple tables. |

### How the compute-and-store model works

For dynamic definition types (Top N, Filtered, Free-hand SQL), members are not computed on every query. Instead:

1. You save the list definition (dimension, measure, conditions, or SQL query).
2. You click **Refresh** on the List Rule tab. Tessallite executes the definition against the source database and stores the resulting values.
3. At query time, the stored values are expanded into the SQL — identical to Fixed Members. No source query runs during user queries.

This design keeps query-time behaviour simple and predictable. Refresh also records the member set's `last_refreshed_at` vintage in the named-list `trust_meta` metadata. Model Builder and the named-list API show the live definition's vintage; BI catalogue metadata shows the vintage in the deployed snapshot. If your source data changes, click Refresh again to update the stored members, then redeploy the model so queries use the updated values. Refreshed members are NOT available to queries until the model is redeployed.

### Limits

- Maximum member values per list is controlled by the NAMED_LIST_MEMBER_CAP setting (default 1,000, ceiling 5,000). If a refresh query returns more than the configured cap, the refresh is rejected (never silently truncated).
- Free-hand SQL must return exactly one column. `SELECT *` is rejected.
- Free-hand SQL must not contain DML keywords (INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, GRANT, REVOKE).

### Creating a Tessallite Named List

1. Open the Named Sets panel and switch to the **Tessallite Named Lists** tab.
2. Click **Add Set**. The dialog opens with the Tessallite kind pre-selected.
3. Enter a **Name**.
4. Go to the **List Rule** tab.
5. Select the **Definition type** from the dropdown: Fixed Members, Top N, Filtered, or Free-hand SQL.
6. Select a **Data type** (string or number).
7. Configure the definition:
   - **Fixed Members:** select a dimension, then type or paste member values.
   - **Top N:** select a dimension, choose a measure, enter the count, and pick a direction (top or bottom).
   - **Filtered:** select a dimension and add filter conditions.
   - **Free-hand SQL:** enter a SQL query in the text area.
8. Click **Create**.
9. For dynamic types (Top N, Filtered, Free-hand SQL): open the list for editing and click **Refresh** on the List Rule tab to compute members from the source data. The button shows the last-refreshed timestamp or "Never refreshed" if no refresh has run. This timestamp describes the stored member set; redeploy is still required before queries use a newly refreshed set.

### Using Tessallite Named Lists in SQL queries

Reference a list with `@ListName` inside an `IN` or `NOT IN` clause:

```sql
SELECT product_name, SUM(revenue)
FROM sales
WHERE channel IN (@ActiveChannels)
GROUP BY product_name
```

The query router replaces `@ActiveChannels` with the stored member values before parsing. String values are single-quoted; number values are bare. The replacement happens before the SQL parser sees the query, so downstream routing, security, and aggregation work normally.

Only `IN (...)` and `NOT IN (...)` usage shapes are accepted. Using `@ListName` in an `=`, `LIKE`, `SELECT`, or any other position is rejected with a clear error message.

---

## Certification and governance

Both kinds of named lists support the same certification lifecycle:

| Status | Meaning |
|---|---|
| **Draft** | Work in progress; visible to modellers only. |
| **Shared** | Available to all users but not yet certified. |
| **Certified** | Reviewed and approved for production use. |
| **Deprecated** | Scheduled for removal; a replacement may be specified. |

Version history tracks changes to the definition, and impact analysis shows where each named list is used across workbooks and dashboards.

---

## Before you start

- You must have a model open in Model Builder with at least one dimension defined.
- For Top N lists, the ranking measure must already exist in the model.
- For Filtered lists, the dimension must have queryable attributes.
- Tessallite Named Lists are available on the SQL path after saving and deploying the model. MDX Named Sets also require model deployment. Refreshed members only reach queries after a redeploy.

---

## Related

- [Define Dimensions](define-dimensions.html)
- [Define Measures](define-measures.html)
- [Named List Parameterisation (MDX)](../integrations/named-list-parameterisation.html)
- [Usage & Downstream Assets](usage-downstream-assets.html)
- [KPIs](kpis.html)
