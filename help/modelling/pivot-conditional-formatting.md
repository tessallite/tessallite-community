---
title: "Pivot Conditional Formatting"
audience: modeller
area: modelling
updated: 2026-05-24
---

## What this covers

The Measure Query Panel (pivot grid) supports three kinds of cell-level conditional formatting: **colour scale**, **data bars**, and **threshold**. This page explains each kind, how to configure it, and what the visual output looks like.

---

## Kinds of conditional formatting

### Colour scale

Cells are shaded on a gradient between two colours based on their value relative to the minimum and maximum in the result set. Low values receive one colour, high values receive the other, and values in between are interpolated.

Use colour scales when you want a heatmap effect across your pivot grid.

### Data bars

Each cell shows a horizontal bar whose width is proportional to the cell value relative to the maximum value in the result set. The bar renders behind the number so both the bar and the value are visible.

Use data bars when you want a quick visual comparison of relative magnitudes.

### Threshold

Cells are coloured based on whether their value is above or below a user-defined threshold. Values above the threshold receive one colour and values at or below receive another.

When threshold mode is selected, a **value input field** appears in the control bar so you can type the threshold number. The field is only visible when the kind is set to threshold.

---

## How to use it

1. Open the **Measure Query Panel** (Pivot tab) in the Model Builder.
2. Run a query so the grid is populated.
3. In the controls above the grid, find the **Conditional Format** dropdown.
4. Select one of: **Color Scale**, **Data Bars**, or **Threshold**.
5. For threshold mode, enter the boundary value in the text field that appears.
6. The grid updates immediately. To remove formatting, select **None** from the dropdown.

---

## Notes

- Conditional formatting is applied per-session and is not saved with the model. Refreshing the page or changing the query resets it.
- Formatting applies to all numeric measure columns in the grid. It does not apply to dimension labels or row/column headers.
- Colour choices use the Tessallite palette by default and are not currently user-customisable.

---

## Related

- [Measure Query Panel](measure-query-panel.md)
- [Query Panel](query-panel.md)
- [Define Measures](define-measures.md)

---

<- [Measure Query Panel](measure-query-panel.md) | [Home](../index.md) | [Query Panel ->](query-panel.md)
