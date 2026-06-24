---
title: "Associate Calendar with Dimensions"
audience: modeller
area: modelling
updated: 2026-05-06
---

## What this covers

When you add a date or timestamp dimension to a model, Tessallite offers to associate it with a calendar table. This association is **optional** — period boundaries for time variants are computed from SQL expressions derived from the hierarchy's calendar type, and a physical calendar table is not required. Calendar association is useful when you have a calendar table for dense date enumeration, retail 4-4-5 periods, or custom period columns.

This article explains the calendar association dialog, when to create aliases, and how the association interacts with time variants.

---

## Before you start

- The dimension must reference a date or timestamp column. Non-date dimensions do not show the calendar association dialog.
- A calendar table is only needed if you require dense date enumeration, retail 4-4-5 periods, or custom period columns. For standard, fiscal, hijri, or ISO time variants, set the calendar type on the time hierarchy instead — no calendar table required. See [Configure Time Variants](configure-time-variants.md).
- If you do want to associate a calendar table, at least one must be registered on the model's data source. See [Configure Calendar Table](configure-calendar-table.md).

---

## When the dialog appears

The calendar association dialog appears when:

1. You add a new dimension whose source column is a date or timestamp type.
2. You edit an existing date dimension and change its source column.

Tessallite detects the data type from the source column metadata and shows the calendar dropdown automatically.

---

## Reading the calendar dropdown

The dropdown lists all calendar tables registered on the data source that owns the dimension's column. Each entry shows the calendar table name and its type (Standard, Fiscal, ISO Week, etc.).

If the dropdown is empty, no calendar tables are bound on that source. Create one first (see [Configure Calendar Table](configure-calendar-table.md)).

---

## When to use an alias vs the table directly

### Direct use

When a calendar table is used by exactly one date column in the model. Example: you have one fact table with one `order_date` column, and one standard calendar. The join from `order_date` to `calendar.date_key` is unambiguous.

### Alias required

When the same calendar table serves multiple date columns. Example: your fact table has both `order_date` and `ship_date`, and both need the standard calendar. If both dimensions join to the same `calendar` table, the query router cannot distinguish which join path belongs to which dimension. Creating an alias — `order_date_standard` and `ship_date_standard` — gives each path a distinct identity.

**Rule of thumb:** If you have N date columns pointing to the same calendar type, you need N aliases (or 1 direct use + N-1 aliases).

---

## Naming aliases

Use the pattern `<fact_column>_<calendar_type>`:

| Fact column | Calendar type | Alias name |
|---|---|---|
| `order_date` | Standard | `order_date_standard` |
| `ship_date` | Standard | `ship_date_standard` |
| `fiscal_close_date` | Fiscal | `fiscal_close_date_fiscal` |

Consistent naming makes the model self-documenting and avoids confusion when reading the lineage graph.

---

## The "Add Hierarchy" button

After selecting a calendar, the dialog offers an **Add Hierarchy** button. Clicking it creates a pre-populated hierarchy whose levels correspond to the calendar's period columns:

| Calendar type | Pre-populated levels |
|---|---|
| Standard | Year > Half > Quarter > Month > Week > Day |
| Fiscal | Fiscal Year > Fiscal Half > Fiscal Quarter > Fiscal Month > Week > Day |
| ISO Week | ISO Year > ISO Week > Day |
| Retail 4-4-5 | Retail Year > Retail Quarter > Retail Period > Retail Week > Day |
| Hijri | Hijri Year > Hijri Month > Hijri Day |
| Thai Buddhist | BE Year > Quarter > Month > Day |

You can customize the levels after creation — remove levels you don't need, rename them for clarity, or reorder them.

---

## Worked example

A sales fact table has two date columns: `order_date` (standard reporting) and `fiscal_close_date` (fiscal reporting). You want YTD revenue by order date and fiscal YTD revenue by fiscal close date.

### Without calendar tables (expression-based)

1. **Create time hierarchies:** In the Hierarchies panel, create two hierarchies:
   - `Standard Calendar` — dimension kind: time, calendar type: standard. Add year, quarter, month levels with `period_to_date` enabled.
   - `Fiscal Calendar` — dimension kind: time, calendar type: fiscal, fiscal year start month: April. Add the same levels.

2. **Enable time variants:** On the `revenue` measure, enable `_ytd`. The system automatically finds time hierarchies on the same table as the measure's source column. Select the standard hierarchy for `revenue_ytd` and the fiscal hierarchy for `revenue_ytd_fiscal`.

Period boundaries are computed from SQL expressions — no physical calendar table needed.

### With calendar tables (for dense enumeration or retail 4-4-5)

1. **Create calendar tables:** On the source, create `calendar_standard` (type: Standard) and `calendar_fiscal` (type: Fiscal, start month: April).

2. **Add `order_date` dimension:** Select source column `order_date`. In the calendar dropdown, select `calendar_standard`. Click **Add Hierarchy** to create the standard Year > Quarter > Month > Day hierarchy. No alias needed — only one column uses the standard calendar.

3. **Add `fiscal_close_date` dimension:** Select source column `fiscal_close_date`. In the calendar dropdown, select `calendar_fiscal`. Click **Add Hierarchy** to create the fiscal hierarchy.

4. **Enable time variants:** On the `revenue` measure, enable `_ytd`. The variant resolver sees two hierarchies with period-to-date capability. Select the standard hierarchy for `revenue_ytd` and the fiscal hierarchy for `revenue_ytd_fiscal`.

When calendar tables are present, the system uses their pre-computed columns instead of expressions for backward compatibility.

---

## Related

- [Configure Calendar Table](configure-calendar-table.md)
- [Calendar Types](../concepts/calendar-types.md)
- [Configure Time Variants](configure-time-variants.md)
- [Dimension Aliases](dimension-aliases.md)

---

← [Configure Calendar Table](configure-calendar-table.md) | [Home](../index.md) | [Multi-Calendar Best Practices →](multi-calendar-best-practices.md)
