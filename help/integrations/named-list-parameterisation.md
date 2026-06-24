---
title: "Named List Parameterisation"
audience: analyst
area: Integrations
updated: 2026-05-23
---

## What this covers

Named lists can be made dynamic by using parameterised expressions — MDX patterns that change their result based on external inputs like the current date, a user-selected slicer value, or a cell reference. This page explains three parameterisation strategies: time-relative expressions, measure-driven filters, and Excel cell references that flow into CUBE formulas.

---

## Strategy 1: Time-relative MDX functions

MDX provides built-in functions that compute sets relative to a point in time. Because these evaluate at query time, the list stays current without manual updates.

### Last N periods

Return the last 12 months from the current month:

```
LastPeriods(12, [Date].[Calendar].[Month].CurrentMember)
```

If the date hierarchy has a default member set to the current month, `CurrentMember` resolves automatically. Otherwise, specify the anchor:

```
LastPeriods(12, [Date].[Calendar].[Month].&[202605])
```

### Year-to-date

Return all months from January to the current month:

```
YTD([Date].[Calendar].[Month].CurrentMember)
```

### Periods-to-date at a higher grain

Return all days in the current quarter:

```
PeriodsToDate(
  [Date].[Calendar].[Quarter],
  [Date].[Calendar].[Day].CurrentMember
)
```

### Parallel period comparison

Return the same month last year:

```
ParallelPeriod(
  [Date].[Calendar].[Year], 1,
  [Date].[Calendar].[Month].CurrentMember
)
```

---

## Strategy 2: Measure-driven thresholds

Use `Filter` with a measure threshold to create lists that adapt as data changes. The threshold value is baked into the expression but can be updated by editing the named list.

### Members above a threshold

All products with inventory below the reorder point:

```
Filter(
  [Product].[SKU].Members,
  [Measures].[Current Inventory] < [Measures].[Reorder Point]
)
```

### Percentage-based filtering

Top contributors that account for 80% of revenue (Pareto):

```
TopPercent(
  [Product].[Category].Members,
  80,
  [Measures].[Total Revenue]
)
```

### Combining time and measure filters

Products with growing revenue (this year vs. last year):

```
Filter(
  [Product].[Category].Members,
  ([Measures].[Total Revenue], [Date].[Calendar].[Year].&[2026])
    > ([Measures].[Total Revenue], [Date].[Calendar].[Year].&[2025])
)
```

---

## Strategy 3: Excel cell references in CUBE formulas

When a named list is inserted into a workbook as `CUBESET` + `CUBERANKEDMEMBER` formulas, you can make the set expression reference other cells. This lets end users change parameters without editing MDX.

### Parameterising a Top N count

Instead of a fixed count, reference a cell:

| Cell | Content |
|---|---|
| B1 | `10` (user-editable count) |
| A1 | `=CUBESET("Tessallite","TopCount([Product].[Category].Members,"&B1&",[Measures].[Total Revenue])","Top Products")` |
| A2 | `=CUBERANKEDMEMBER("Tessallite",A1,1)` |

When the user changes B1 from `10` to `5`, the set recalculates to show only 5 members.

### Parameterising a threshold

| Cell | Content |
|---|---|
| B1 | `10000` (minimum revenue threshold) |
| A1 | `=CUBESET("Tessallite","Filter([Customer].[Name].Members,[Measures].[Total Revenue] > "&B1&")","High-Value Customers")` |

### Parameterising a time period

| Cell | Content |
|---|---|
| B1 | `202605` (year-month key, user-editable) |
| A1 | `=CUBESET("Tessallite","LastPeriods(12,[Date].[Calendar].[Month].&["&B1&"])","Rolling 12 Months")` |

### Using CUBEMEMBER as a slicer

A `CUBEMEMBER` cell can act as a slicer that feeds into `CUBEVALUE`:

| Cell | Formula |
|---|---|
| D1 | `=CUBEMEMBER("Tessallite","[Geography].[Region].[North America]")` |
| D2 | `=CUBEVALUE("Tessallite","[Measures].[Total Revenue]",D1)` |

Change D1 to a different region, and D2 recalculates. You can wire a data-validation dropdown to D1's input to build a user-friendly slicer.

---

## Refresh behaviour

- CUBE formulas recalculate when the workbook refreshes (**Data → Refresh All** or Ctrl+Alt+F5).
- Time-relative expressions (`CurrentMember`, `YTD`, `LastPeriods`) re-evaluate against the server's current date at refresh time.
- Cell-referenced parameters update immediately when the referenced cell changes — the formula triggers a recalculation.
- The underlying XMLA connection caches metadata for the session. If the model is redeployed while the workbook is open, close and reopen the workbook or re-establish the connection to pick up schema changes.

---

## Limitations

- **No user prompts at refresh.** Unlike parameterised SQL queries in some tools, CUBE formulas do not prompt the user for input. Use cell references (Strategy 3) to achieve the same effect.
- **String escaping.** Member keys containing special characters (`]`, `"`) must be escaped in the MDX expression. The named list builder handles this automatically; if writing Advanced MDX by hand, double the closing bracket (`]]`) inside member references.
- **Set size.** Very large sets (thousands of members) may cause slow formula evaluation. Use `TopCount` or `Filter` to keep sets to a practical size.

---

## Related

- [Named List MDX Composition](named-list-mdx-composition.md)
- [Tessallite Excel Add-in](excel-add-in.md)
- [Excel PivotTable Features](excel-pivottable-features.md)
- [Define Hierarchies](../modelling/define-hierarchies.md)

---

← [Named List MDX Composition](named-list-mdx-composition.md) | [Home](../index.md) | [Excel PivotTable Features →](excel-pivottable-features.md)
