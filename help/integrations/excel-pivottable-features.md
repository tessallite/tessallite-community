---
title: "Excel PivotTable Features"
audience: analyst
area: Integrations
updated: 2026-09-10
---

## What this covers

Once Excel is connected to Tessallite over XMLA, the PivotTable behaves like any Analysis Services cube. This page covers the advanced PivotTable features Tessallite supports — value and label filters, Show Values As, timelines, calculated fields, drill-through, and `GETPIVOTDATA` — and how each maps onto the semantic model. To connect in the first place, see [Connect Excel via XMLA](../getting-started/connect-excel.md).

**Why this matters.** Every one of these features is evaluated *in the source database against the published model*, not in your workbook. That is the whole point of connecting Excel to Tessallite rather than pasting an extract: a "Top 10 customers" filter or a "% of grand total" calculation runs over the full data set the model can see, respects row security and your persona, and stays correct when someone refreshes the workbook tomorrow. The sections below first walk one example end to end, then describe each feature as a reference.

---

## A worked example: top products by margin, this year

Imagine you are a category manager and you want the ten products contributing the most gross margin so far this year, each shown as a share of the whole category. Here is the whole journey, and what Tessallite does underneath at each step.

1. **Build the base layout.** Drag the `Product` dimension to Rows and the `Gross Margin` measure to Values. Excel shows every product and its margin. Underneath, Tessallite issues a `SELECT product, SUM(gross_margin) ... GROUP BY product` against the source — no data leaves the database except the grouped result.
2. **Keep only this year.** Insert a **Timeline** on the `Order Date` hierarchy and drag it to cover the current year. The PivotTable narrows instantly. Tessallite turns the timeline range into a date filter (`WHERE order_date >= ...`) on the same query, so the margin numbers are recomputed in the database, not trimmed in the sheet.
3. **Keep only the top ten.** Open **Value Filters → Top 10** on the Product field, by `Gross Margin`. Tessallite translates this to `ORDER BY SUM(gross_margin) DESC LIMIT 10`. Crucially, the ranking is decided over *all* products in the database first, then the top ten are returned — so you get the genuine leaders, not the top ten of whatever happened to be on screen.
4. **Show each as a share.** Right-click the value column → **Show Values As → % of Grand Total**. Each product now reads as a percentage. Because this is computed server-side as a calculated member, the percentages reconcile exactly with the subtotal and grand total rows, even after the Top 10 filter.
5. **Check the detail behind a number.** Double-click the leading product's cell to **drill through**. Tessallite returns the contributing fact rows on a new sheet, limited to the model's curated drill-through columns and filtered by your persona and row security — so the detail always reconciles to the cell, and never exposes a column you are not allowed to see.

The result is a live, governed report: refresh it next week and every step re-runs against current data, with the same security and the same definitions everyone else uses.

**Good habits this example shows.** Filter to the period *first* so later steps work on less data; prefer a server-side **Value Filter** over manually deleting rows (deleting rows breaks subtotals and is not refreshable); and reach for **drill-through** rather than rebuilding a detail query by hand, so the rows you see are exactly the rows behind the cell.

**A common trap.** "Show Values As" and Excel **Calculated Fields** are presentation conveniences layered on the query result. They cannot invent a number the model does not expose — if you need a brand-new business metric (say a blended margin across two fact tables), define it as a **measure** in the model so it is governed, reusable, and available to every tool, not just this workbook.

---

## Show Values As

Right-click a value and choose **Show Values As** to display a measure as a percentage, running total, rank, or difference instead of the raw number. Tessallite supports % of Grand Total, % of Parent Row/Column, Difference From, % Difference From, Running Total, and Rank (largest or smallest). These are evaluated server-side as calculated members, so subtotals and grand totals stay consistent.

---

## Value filters and Top 10

Use **Value Filters** on a row or column field to keep only the members whose measure passes a test — for example *greater than*, *between*, or **Top 10**. Tessallite translates value tests to a `HAVING` clause and Top/Bottom-N to an `ORDER BY` with a `LIMIT`, so the filter runs in the database rather than in the workbook.

---

## Label filters

**Label Filters** (begins with, contains, ends with, and their negations) filter members by their caption. Tessallite maps these to SQL `LIKE` / `NOT LIKE` patterns with the wildcards escaped, so a search for a literal percent sign matches that character rather than everything.

One condition per field is supported — which is what Excel's Label Filter dialog offers. To narrow further, apply a label filter to a second field, or combine a label filter with a value filter. If a workbook or a hand-written query ever sends two label conditions joined by *and* / *or* inside a single filter, Tessallite refuses the query with a clear message rather than guessing which rows you meant; a query that would return the wrong number of rows is never run silently.

---

## Timeline slicers

Insert a **Timeline** on a date hierarchy to filter the PivotTable to a date range with a drag handle. Tessallite exposes date dimensions with the metadata Excel needs to offer Year, Quarter, Month, and Day granularities, and translates the selected range into a date filter on the query. Combine a timeline with ordinary slicers to filter by date and by another dimension at the same time.

---

## Calculated fields

Excel's **Calculated Field** dialog lets you define a new measure as an arithmetic expression over existing measures (for example margin divided by revenue). Excel sends this as a session-scoped `WITH MEMBER` definition; Tessallite evaluates it after the query and preserves number formatting. The calculated field lives only in your workbook session — it does not change the published model.

---

## Drill-through to detail

Double-click a value cell to drill through to the fact rows behind it. Excel issues an XMLA `DRILLTHROUGH` statement and Tessallite returns the contributing rows on a new sheet, honouring the model's curated drill-through columns, row security, and your persona scope. The detail rows always reconcile to the cell you drilled from.

---

## GETPIVOTDATA

Reference a single PivotTable value from elsewhere in the workbook with `GETPIVOTDATA`. Tessallite resolves the function as a point query against the model, so a dashboard cell that uses `GETPIVOTDATA` stays correct when the PivotTable refreshes.

---

## Hierarchies and subtotals

The field list is grouped so you can find things quickly: **Measures**, **KPIs**, **Dimensions** (single-level attributes such as account type), **Time** (every date field together with its calendar hierarchy — Year, Quarter, Month, Day — so a date is one place to look, and Excel's Timeline filter works on it) and **Hierarchies** (the other multi-level hierarchies, such as Country > City > Channel). Date, geography, and entity hierarchies appear with working expand/collapse. When you place more than one hierarchy on an axis, Tessallite computes the cross-product of subtotal levels so each subtotal and grand total is correct for additive measures. Non-additive measures (such as a ratio) show a dash in the total row instead of a misleading sum.

Tessallite uses the same stable member identity in the field-list metadata and in plain, subtotal, and grand-total query results. Captions can stay business-friendly without changing that identity, so Excel can match refreshed members to the PivotCache stored in the workbook.

Row security applies while Excel is building the member list, before you add a
measure. If you may read Web transactions but not Store transactions, a
channel-only list contains Web and leaves out Store. A flat date field also
behaves like any other PivotTable field when it is combined with another field:
the requested field totals and grand total are calculated from the rows you may
see.

### Subtotals: one switch per level

**What a hierarchy is in a PivotTable.** A hierarchy is a ladder of levels, from the widest to the narrowest: Country, then City, then Channel. In the Rows pane the whole ladder appears as one box. Underneath that box, Excel keeps one field for each rung. Each rung has its own settings, and one of those settings is whether it shows a total row.

**What a total row is for.** When you expand a country to see its cities, the country's own number moves off its own line and onto a line called `GB Total`, below its cities. The same happens one rung down: expand London to see its channels and London's number moves to `London Total`. A member you have not expanded keeps its number on its own line. This is how every OLAP PivotTable works, in Excel and in Analysis Services.

**Why a level can look empty.** If a rung's total switch is off, an expanded member on that rung has nowhere to put its number, so its line stays blank. Nothing is missing; the number is simply not shown. Turn the switch on for that rung and the total line appears with the number on it.

**Why one switch is not enough.** The Field Settings dialog only ever changes the rung it was opened for. Open it from the box in the Rows pane, or from a country cell, and it changes Country alone. That gives you `GB Total` but not `London Total`, and London's line stays blank while Manchester, which is not expanded, shows its number. This surprises many people the first time.

**How to turn totals on.**

- **Every rung at once, the simple way:** on the PivotTable **Design** tab choose **Subtotals**, then **Show all Subtotals at Bottom of Group** (or at Top of Group). Every rung of every hierarchy on the axis gets its total row.
- **One rung at a time:** right-click a cell that sits on that rung, for example the London cell, and choose **Field Settings**. The dialog names the rung you are changing next to *Source Name* (here `City`). Choose **Automatic** under Subtotals. Repeat for any other rung.

**How to check it is right.** Each total is one figure calculated by Tessallite, not a number Excel adds up on screen. A country total is exactly the sum of its cities, and a city total is exactly the sum of its channels. If you add the lines yourself you will get the same figure to the last penny.

**Tip.** Set the subtotal choice once, on the Design tab, before you start expanding. Then every member you open later already has its total line, and the table reads the same way at every depth.

---

## KPIs in a PivotTable

The field list groups each published KPI under a KPI folder with up to four members you can tick: **Value**, **Goal**, **Status**, and **Trend**. Value is the headline number, Goal is the target, and Status and Trend are the governed verdicts.

The important idea is that **Status and Trend are governed**. When you tick Status, the cell does not show the raw business number and it is not a colour Excel guessed. It shows the single traffic-light verdict the model owner defined — a green result is `1`, on-watch is `0`, and off-target is `-1` — the exact same verdict you see on the Tessallite scorecard and in the Excel formula functions. Because a real verdict lives behind the checkbox, Excel can draw the traffic-light icon over it correctly. If a KPI has no target or bands to judge against, Status stays blank rather than inventing a verdict.

A worked example: drop the "Net Revenue" KPI's Value and Status onto a pivot. Value shows the revenue figure; Status shows a single `1`, `0`, or `-1` and a red/amber/green light, matching the scorecard.

A few rules keep the number honest:

- **Goal and Status are one figure for the whole model, not per slice.** Only the KPI Value can be broken down by a dimension. You cannot break Goal or Status down by a dimension (for example, Status by Region) or put a dimension on the report filter next to Status. If you try, Tessallite returns a clear message asking you to remove the breakdown, rather than repeating one model-wide target or verdict against every region as if it were sliced. Build the KPI table on its own (Value, Goal, Status with no row or column field), and use the underlying measure and the model owner's regional KPIs to compare regions. The same rule applies to the model information measures (Last Refreshed, Source System, Owner): they are one value for the whole model and are refused with a dimension breakdown.
- **Value and Goal can sit on a pivot with dimensions** in the normal way — they are ordinary numbers.
- Each KPI must have a unique name. If two KPIs share the same caption, Tessallite asks the model owner to rename them rather than guess which verdict you meant.

## Notes and limitations

- These features require an XMLA connection. SQL-based tools over JDBC see a flat relational view without PivotTable semantics.
- Calculated fields are session-scoped: they are not saved to the model and are not shared with other users.
- Power BI connects over the same XMLA endpoint but does not support XMLA drill-through natively.

---

## Related

- [Connect Excel via XMLA](../getting-started/connect-excel.md)
- [Tessallite Excel Add-in](excel-add-in.md)
- [Excel XMLA Connection Guide](excel-xmla-connection-guide.md)
- [Drill-through](../modelling/drill-through.md)

---

← [Excel XMLA Connection Guide](excel-xmla-connection-guide.md) | [Home](../index.md) | [Tessallite Excel Add-in →](excel-add-in.md)
