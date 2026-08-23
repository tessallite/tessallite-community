---
title: "Column-Level Security"
audience: modeller
area: modelling
updated: 2026-06-12
---

## What this covers

Column-level security restricts which columns a persona can see. It works through data tag restrictions: you tag sensitive columns, then configure a persona to be restricted from specific tags. At query time, the persona gate either blocks the query (if a restricted column is explicitly requested) or silently excludes the column (if it would have been auto-included via SELECT *). This article explains how the mechanism works, how to configure it, and how it composes with row-level security.

---

## How it works

Column-level security is a two-layer system:

1. **Data tags** label columns by sensitivity (e.g. `PII`, `Financial`). See [Data Tags](data-tags.md).
2. **Persona tag restrictions** define which tags a persona cannot access.

When a query arrives under a persona:

- If the query explicitly requests a column tagged with a restricted tag → the query is rejected with a generic `OBJECT_NOT_AVAILABLE` 403 (the error does not name the column, tag, or persona).
- If the query uses SELECT * and a restricted column would be included → the column is silently dropped from the result set. If **every** column the star resolves to is restricted, the query is rejected — there is nothing left to return.
- If the query uses advanced SQL shapes (subqueries, CTEs, set operations, window functions, and similar) → it is rejected with a 403, because such shapes cannot be checked column-by-column. Rewrite it as a plain `SELECT` over the model.

**Hidden is not column security.** A measure or column marked hidden (`is_hidden`) is dropped from browse lists and from `SELECT *`. Anyone who types the name can still query it. That is curation. To stop a Sales persona from reading cost, put the measure on a persona allow-list or restrict its column with a data tag. Then both `SELECT fee_amount` and `SELECT SUM(fee_amount)` return `OBJECT_NOT_AVAILABLE`. See [Configure Personas](configure-personas.md).

Restrictions follow a column wherever it is used, not just when it is named directly:

- A **calculated measure** whose formula reads a restricted column is blocked too.
- A **time-variant measure** (YTD, prior year, ...) of a measure built on a restricted column is blocked too.
- A **computed attribute** (user-defined attribute) that reads a restricted column is blocked too.

---

## Relationship to row-level security

Row security and column security are independent axes that compose:

| Axis | Controls | Mechanism |
|---|---|---|
| Row security | Which rows a user sees | WHERE clause filters injected by the security rule compiler |
| Column security | Which columns a user sees | Column exclusion based on persona tag restrictions |

A persona can have both row rules and column restrictions. They apply simultaneously: first the column gate narrows the visible columns, then the row security filter narrows the visible rows. Neither depends on the other.

---

## Setting restrictions

1. Open the persona in the **Personas** panel (Toolbelt → Personas → click the persona name).
2. Scroll to the **Column Restrictions** section.
3. Toggle the data tags that this persona should NOT see. Each toggled tag restricts all columns assigned to it.
4. Click **Save**.

---

## What happens at query time

### Explicit column request

If a persona restricted from `PII` runs:

```sql
SELECT customer_name, email, revenue FROM sales
```

And `email` is tagged `PII`, the query is rejected with:

```
403 OBJECT_NOT_AVAILABLE
```

The error does not name the column, the tag, or the persona.

### SELECT * (auto-inclusion)

If the same persona runs:

```sql
SELECT * FROM sales
```

The `email` column is silently excluded from the result. The persona sees `customer_name`, `revenue`, and all other non-restricted columns. No error is raised.

---

## Worked example

**Scenario:** An "External Partner" persona should not see PII data.

1. **Create the PII tag:** In Data Tags, create a tag named `PII`.
2. **Assign columns:** Tag `customers.email`, `customers.phone`, `orders.shipping_address` as `PII`.
3. **Create the persona:** In Personas, create "External Partner".
4. **Set restrictions:** In the persona's Column Restrictions, toggle `PII` to restricted.
5. **Test:** Query as the persona. `SELECT *` returns all columns except email, phone, and shipping_address. `SELECT email FROM ...` returns `OBJECT_NOT_AVAILABLE`.

---

## Pitfalls

- **New columns are visible by default.** When you add a column to a table that has tagged columns, the new column is untagged and therefore visible to all personas. Always review new columns after schema changes.
- **Calculated measures that depend on restricted columns are blocked.** If a calculated measure (or a time variant, or a computed attribute) reads a restricted column anywhere in its formula, restricted personas get `OBJECT_NOT_AVAILABLE`. Either remove the dependency or don't restrict the column.
- **Tag assignment is model-scoped.** Restricting `PII` in one model doesn't affect another model, even if they share the same source tables. Each model's tags and restrictions are independent.
- **Restrictions travel with exports.** Data tags and persona restrictions are part of the model snapshot, so project export/import and version restore keep your column security intact.

---

## Related

- [Data Tags](data-tags.md)
- [Configure Personas](configure-personas.md)
- [Configure Row Security](configure-row-security.md)

---

← [Data Tags](data-tags.md) | [Home](../index.md) | [Configure Personas →](configure-personas.md)
