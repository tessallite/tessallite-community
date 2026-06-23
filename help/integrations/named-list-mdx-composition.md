---
title: "Named List MDX Composition"
audience: analyst
area: Integrations
updated: 2026-05-23
---

## What this covers

Named lists (called named sets in the underlying OLAP model) define reusable groups of dimension members. The Report Builder offers four builder types — Fixed Members, Top N, Filter, and Advanced MDX. This page explains the MDX expression that each builder type produces, how to read and hand-edit it, and how the resulting set is consumed by Excel CUBE formulas.

---

## How named lists become MDX

Every named list ultimately resolves to an MDX set expression stored in the `expression` field. The visual builders generate this expression automatically; Advanced MDX lets you type it directly.

| Builder type | What it does | Generated MDX pattern |
|---|---|---|
| Fixed Members | Enumerates specific members by key | `{[Dimension].[Hierarchy].[Member1], [Dimension].[Hierarchy].[Member2], ...}` |
| Top N | Picks the top or bottom N members ranked by a measure | `TopCount([Dimension].[Hierarchy].Members, N, [Measures].[Measure Name])` or `BottomCount(...)` |
| Filter | Keeps members matching one or more conditions | `Filter([Dimension].[Hierarchy].Members, [Measures].[Revenue] > 1000)` |
| Advanced MDX | Freeform expression | Whatever you type — the engine validates it at save time |

---

## Fixed Members

Fixed Members is the simplest builder. You pick dimension members from a list, and Tessallite wraps them in an MDX set literal.

**Example:** A named list called "Top Regions" containing three specific regions:

```
{[Geography].[Region].[North America],
 [Geography].[Region].[Europe],
 [Geography].[Region].[Asia Pacific]}
```

This expression is static — it always returns the same three members regardless of underlying data changes. Use Fixed Members when the list is curated by a human and should not change automatically.

---

## Top N (Dynamic)

Top N selects the N highest- or lowest-ranked members by a measure. The result changes when the data changes — if a new region overtakes an existing one, it appears in the list on the next query.

**Example:** Top 5 products by revenue:

```
TopCount(
  [Product].[Category].Members,
  5,
  [Measures].[Total Revenue]
)
```

For bottom-ranking (e.g. worst-performing stores), the builder uses `BottomCount`:

```
BottomCount(
  [Store].[Name].Members,
  10,
  [Measures].[Profit Margin]
)
```

---

## Filter

Filter keeps every member that satisfies one or more conditions. Use it when "top N" is too rigid — for example, "all products with revenue above $10,000" regardless of how many there are.

**Example:** Customers with order count above 50:

```
Filter(
  [Customer].[Name].Members,
  [Measures].[Order Count] > 50
)
```

Multiple conditions are combined with `AND`:

```
Filter(
  [Customer].[Name].Members,
  [Measures].[Order Count] > 50
    AND [Measures].[Total Revenue] > 10000
)
```

---

## Advanced MDX

Advanced MDX accepts any valid MDX set expression. Use it when the visual builders cannot express what you need — for example, a CrossJoin, an Except, or a time-relative calculation.

**Common patterns:**

Cross-join two dimensions:

```
CrossJoin(
  {[Geography].[Region].[North America], [Geography].[Region].[Europe]},
  [Product].[Category].Members
)
```

Exclude specific members:

```
Except(
  [Product].[Category].Members,
  {[Product].[Category].[Discontinued]}
)
```

Last 12 months:

```
LastPeriods(12, [Date].[Calendar].[Month].&[202605])
```

---

## How named lists appear in CUBE formulas

When you insert a named list from the Report Builder, the add-in generates a `CUBESET` formula followed by one `CUBERANKEDMEMBER` per visible row:

| Cell | Formula |
|---|---|
| A1 | `=CUBESET("Tessallite","{[Geography].[Region].[North America],[Geography].[Region].[Europe],[Geography].[Region].[Asia Pacific]}","Top Regions")` |
| A2 | `=CUBERANKEDMEMBER("Tessallite",A1,1)` |
| A3 | `=CUBERANKEDMEMBER("Tessallite",A1,2)` |
| A4 | `=CUBERANKEDMEMBER("Tessallite",A1,3)` |

To add a measure column, use `CUBEVALUE` referencing the same dimension member:

| Cell | Formula |
|---|---|
| B2 | `=CUBEVALUE("Tessallite","[Measures].[Total Revenue]",A2)` |
| B3 | `=CUBEVALUE("Tessallite","[Measures].[Total Revenue]",A3)` |

The `CUBESET` recalculates on workbook refresh. For dynamic lists (Top N, Filter), the members returned may change each time.

---

## Validation

The engine validates the MDX expression when you save a named list:

- **Syntax check:** The expression must parse as a valid MDX set expression.
- **Member resolution:** Referenced dimensions and hierarchies must exist in the deployed model.
- **Evaluation:** A preview query runs the expression and returns the first 100 members so you can confirm the result before saving.

If validation fails, the error message identifies the problematic portion of the expression.

---

## Related

- [Tessallite Excel Add-in](excel-add-in.md)
- [Named List Parameterisation](named-list-parameterisation.md)
- [Define Dimensions](../modelling/define-dimensions.md)
- [Define Measures](../modelling/define-measures.md)

---

← [Excel Add-in](excel-add-in.md) | [Home](../index.md) | [Named List Parameterisation →](named-list-parameterisation.md)
