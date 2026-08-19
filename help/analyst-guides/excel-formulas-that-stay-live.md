---
title: "Excel Formulas That Stay Live"
audience: end user
area: analyst-guides
updated: 2026-08-04
---

## What this covers

Sometimes you do not want a whole PivotTable — you want one governed number in one cell. A board pack with revenue in a sentence. A commentary sheet where the figures update themselves. This page covers the three formula families that do that with Tessallite: the `TESSALLITE.*` functions from the Excel add-in, the classic `GETPIVOTDATA`, and the CUBE formulas for the advanced path — and when to reach for each.

---

## The three families at a glance

| Family | Comes from | One-cell example | Best for |
|---|---|---|---|
| `TESSALLITE.VALUE` / `.KPI` / `.MEMBERVALUE` | The Tessallite Excel add-in | `=TESSALLITE.VALUE("modelx","net_sales")` | Hand-built report sheets, commentary cells, small schedules |
| `GETPIVOTDATA` | Any XMLA PivotTable | `=GETPIVOTDATA("net_sales",$A$3,"product_name","Widget")` | Pulling a figure out of a PivotTable you already have |
| `CUBEVALUE` / `CUBEMEMBER` | The add-in's CUBE formula wizard | generated for you | Large formula-driven grids over a live OLAP connection |

All three are live. None of them is a paste. Refresh the workbook and every one of them re-asks Tessallite the question.

---

## TESSALLITE.VALUE — a governed number in a cell

The simplest of the three. With the add-in signed in and a model selected, type:

```
=TESSALLITE.VALUE("modelx", "net_sales")
```

The cell now holds net sales for the whole model — computed by Tessallite and respecting your security. The custom function returns the governed value; apply the Excel number format you want to the cell. Add filters as extra pairs of arguments when you want a slice:

```
=TESSALLITE.VALUE("modelx", "net_sales", "country_code", "US")
```

That is net sales for the US only, still one cell, still live.

A few things worth knowing before you build a whole sheet of these:

- **Calls are batched.** A sheet with fifty `TESSALLITE.VALUE` cells does not send fifty separate requests; calls made during the same recalculation are grouped together. The returned `VALUE` and `MEMBERVALUE` results are not held in a 60-second result cache.
- **KPI and named-list lookups use a 60-second cache.** Click **Refresh** in the Report Builder footer to clear those caches and recalculate the workbook immediately.
- **The formula checks the model.** If you open the workbook while the task pane has a different model selected, the cell tells you so in plain words instead of quietly returning a number from the wrong model. Switch the task pane back to `modelx` and refresh.
- **Switching persona changes the numbers.** That is the security working as designed: a persona that cannot see a measure gets no value from it, in a formula exactly as in a PivotTable.

> **A worked example — the self-updating sentence.** In cell B2 Maya writes `="Net sales this period: "&TEXT(TESSALLITE.VALUE("modelx","net_sales"),"#,##0")`. The sentence in the board pack now carries a real, current figure every time the workbook opens. Nobody re-keys it; nobody pastes the wrong week's number.

---

## TESSALLITE.KPI — the traffic light in a cell

```
=TESSALLITE.KPI("modelx", "Net Sales", "value")
=TESSALLITE.KPI("modelx", "Net Sales", "goal")
=TESSALLITE.KPI("modelx", "Net Sales", "status")
```

Three properties: the headline number, the target, and the governed verdict. Status returns `1` (green), `0` (amber), or `-1` (red) — deliberately numeric, so Excel's **Conditional Formatting > Icon Sets** can turn the cell into an actual traffic light with two clicks.

This is the same verdict the KPI card shows in the add-in, the same one a PivotTable KPI folder shows, and the same one the Tessallite scorecard shows. One definition, four surfaces.

---

## TESSALLITE.MEMBERVALUE — one measure, one member

```
=TESSALLITE.MEMBERVALUE("modelx", "net_sales", "product_name", "Widget")
```

This reads like the filtered `TESSALLITE.VALUE` and that is close to true — it exists for small hand-built schedules where you want, say, five named products down column A and a measure beside each, without a PivotTable's structure. Point each row's formula at the member name in column A (for example `=TESSALLITE.MEMBERVALUE("modelx","net_sales","product_name",A4)`) and the schedule fills itself — and keeps filling itself on every refresh.

---

## GETPIVOTDATA — borrow from a PivotTable you trust

If the number you want is already sitting in a PivotTable on another sheet, `GETPIVOTDATA` reaches in and takes it by name:

```
=GETPIVOTDATA("net_sales", $A$3, "product_name", "Widget")
```

The formula refers to the PivotTable (the `$A$3` corner) and the field names, not to a cell address — so when the PivotTable grows, shrinks, or re-sorts, the formula still finds the right value. Tessallite resolves it as a point query against the model, which means it stays correct through every refresh.

**When this beats TESSALLITE.VALUE:** when the PivotTable already applies the filters you want (a timeline, a Top 10, a slicer selection). The formula inherits all of that context for free, instead of you re-typing it as filter arguments.

---

## CUBE formulas — the advanced path

The add-in's **CUBE formula wizard** generates native Excel OLAP formulas — `CUBEVALUE`, `CUBEMEMBER`, `CUBESET` — over a workbook connection named `Tessallite`. These shine for big formula-driven grids: a hundred members down the side, five measures across the top, every cell a live cube reference.

Reach for them when a PivotTable's layout is too rigid and `TESSALLITE.*` cells would need too much hand-wiring. The wizard writes the formulas for you, so you never have to remember the syntax. One caveat from the add-in guide: local PivotTable inserts only accept additive standard measures — for calculated, time-variant, or semi-additive measures, the CUBE path (or an Insert Table) is the right tool, because Excel must not re-add a value that is only correct at its governed grain.

---

## Which one when

- Building a commentary sheet or board pack with a dozen figures? **`TESSALLITE.VALUE`** and **`TESSALLITE.KPI`**.
- The figure already exists in a PivotTable on another sheet? **`GETPIVOTDATA`**.
- A large grid of members by measures, all formula-driven? **CUBE wizard**.
- Need a whole table, not cells? That is a PivotTable or an add-in Insert Table — see [Build Your First Excel Dashboard](build-your-first-excel-dashboard.md).

> **Common trap: the hard-coded "temporary" number.** Someone types this week's revenue into a cell "just for the meeting", and eighteen months later the board is still reading it. Every formula on this page costs one minute once and is right forever. The paste is never cheaper in the end.

---

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `TESSALLITE.*` cells show an error about the model | Task pane has a different model selected than the formula names | Select the model the formula names, then Refresh |
| Functions return nothing at all | Add-in not signed in, or no model selected | Open the task pane, sign in, pick the model |
| A KPI formula still shows its prior value | KPI evaluations can remain cached for up to 60 seconds, or the workbook has not recalculated | Click **Refresh** in the Report Builder footer for a full fresh pass |
| `GETPIVOTDATA` returns `#REF!` | The PivotTable no longer shows the field the formula asks for | Re-add the field to the PivotTable, or adjust the formula |
| A KPI name is not found | The KPI was renamed in the model | Update the formula to the new name — names are the contract |

---

## Related

- [Tessallite Excel Add-in](../integrations/excel-add-in.md)
- [Excel PivotTable Features](../integrations/excel-pivottable-features.md)
- [Build Your First Excel Dashboard](build-your-first-excel-dashboard.md)
- [KPIs (Key Performance Indicators)](../concepts/kpis.md)

---

← [Build a Report with the Excel Add-in](build-a-report-with-the-excel-add-in.md) | [Home](../index.md) | [Build a Power BI Report →](build-a-power-bi-report.md)
