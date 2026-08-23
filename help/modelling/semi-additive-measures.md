---
title: "Semi-Additive Measures"
audience: modeller
area: modelling
updated: 2026-05-24
---

## What this covers

Some numbers should never be added up across time. This page explains what semi-additive measures are, why they matter, and how to configure them so that Excel PivotTables, query results, and dashboards show correct subtotals and grand totals.

---

## What "additive" means

Most business measures are **additive**: you can safely sum them across any dimension. Revenue earned in January plus revenue earned in February equals revenue for Q1. Revenue from the US plus revenue from Europe equals global revenue. Adding up works in every direction.

## What "semi-additive" means

Some measures are **semi-additive**: they can be summed across most dimensions, but **not across time**. The most common examples:

| Measure | Why you cannot sum across time |
|---|---|
| **Account balance** | Your bank balance on Jan 31 plus your balance on Feb 28 does not equal your Q1 balance. Your Q1 balance is the Feb 28 number (the last value). |
| **Headcount** | If you had 100 employees in January and 105 in February, your Q1 headcount is 105, not 205. |
| **Inventory on hand** | 500 units on Jan 31 plus 480 on Feb 28 does not equal 980 for Q1. The answer is 480. |
| **Open support tickets** | 42 open tickets on Monday plus 38 on Tuesday does not mean 80 total. The point-in-time count is what matters. |

### What goes wrong when you sum a semi-additive measure

Imagine a simple bank account balance over three months:

| Month | Balance |
|---|---|
| January | $100 |
| February | $150 |
| March | $200 |

If Tessallite sums these to produce a Q1 subtotal, the PivotTable would show **$450**. That is wrong -- the real Q1 ending balance is **$200**. The analyst sees $450, believes it, and makes a decision based on an inflated number. This is the core risk of misconfigured aggregation.

---

## Aggregation types

Tessallite supports six aggregation types. Choose the one that matches the business meaning of your measure:

| Aggregation | Subtotal behaviour | Use for |
|---|---|---|
| **sum** | Add up all values. | Revenue, quantity sold, cost, transactions. |
| **last_non_empty** | Take the last non-null value along the time axis. Sum across non-time dimensions. | Account balance, headcount, inventory, any point-in-time snapshot. |
| **max** | Take the maximum value. | Peak concurrent users, highest temperature, record high. |
| **min** | Take the minimum value. | Lowest temperature, minimum stock level. |
| **count_distinct** | Count unique values (not composable -- re-queries the source). | Unique customers, unique products sold. |
| **average** | Compute the weighted average (not composable -- re-queries the source). | Average order value, average response time. |

> **By Account is not a supported behaviour.** You may have seen a **By Account** option in older versions. It would have let each account in an account dimension carry its own additivity rule (some accounts sum over time, some take the last balance), but the per-account aggregation engine was never built, so the option has been removed from the semi-additive dropdown. Use **last_non_empty** for balance-style accounts instead. If an older model still has a measure set to By Account, that measure is flagged as invalid until you pick a supported behaviour and re-enable it.

---

## How to configure a semi-additive measure

1. Open the model in the **Model Builder**.
2. Select the measure in the **Measures** panel.
3. Change the **Default aggregation** dropdown from `sum` to the correct type (e.g., `last_non_empty` for a balance measure).
4. Save the model.

That is all. No Excel-side configuration is needed. When an Excel user creates a PivotTable with this measure, Tessallite automatically applies the correct aggregation for subtotals and grand totals.

---

## How Excel users see semi-additive measures

Excel users do not need to know about semi-additive aggregation. The PivotTable renders subtotals and grand totals using the aggregation type set by the modeller:

**Wrong (measure configured as sum):**

| Month | Balance |
|---|---|
| January | $100 |
| February | $150 |
| March | $200 |
| **Q1 Subtotal** | **$450** |

**Correct (measure configured as last_non_empty):**

| Month | Balance |
|---|---|
| January | $100 |
| February | $150 |
| March | $200 |
| **Q1 Subtotal** | **$200** |

The only difference is the modeller's aggregation setting. The Excel user sees correct numbers without any manual intervention.

---

## How it works internally

When Tessallite generates subtotals for a PivotTable, the subtotal engine reads each measure's aggregation type from the model metadata. For `sum`, `min`, and `max`, subtotals are computed from the detail rows already fetched. For `last_non_empty`, the engine identifies the time dimension in the query, finds the last non-null value along that axis for each partition, and uses that as the subtotal. For `count_distinct` and `average`, the engine issues a supplementary query to the source database at the subtotal grain, because these aggregations are not composable from pre-aggregated rows.

---

## Common mistakes

| Mistake | Consequence | Fix |
|---|---|---|
| Leaving a balance measure as `sum` | Subtotals show inflated numbers | Change to `last_non_empty` |
| Using `last_non_empty` for a revenue measure | Subtotals show only the last period's revenue, not the total | Change to `sum` |
| Using `average` when you mean `sum / count` | Subtotals may differ from a calculated ratio | Use a calculated measure for ratios |

---

## Related

- [Define Measures](define-measures.md)
- [Calculated Measures](calculated-measures.md)
- [Configure Aggregates](configure-aggregates.md)
- [KPIs](kpis.md) — a KPI can apply the same closing/average reduction per period. See "Choosing the reduction grain" there for the periods it accepts and why a column name is not one of them.

---

<- [Define Measures](define-measures.md) | [Home](../index.md) | [Calculated Measures ->](calculated-measures.md)
