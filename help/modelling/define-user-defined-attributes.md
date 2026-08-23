---
title: "User-Defined Attributes"
audience: modeller
area: modelling
updated: 2026-08-18
---

## What a user-defined attribute is

A **user-defined attribute (UDA)** is a new column you compute from the columns a
table already has, without changing the source database. You write a small
expression — for example, join a region code and a branch code into one label, or
turn a raw amount into a rounded figure — and Tessallite stores that expression on
the model. From then on the computed value behaves like any other column on that
table: you can build a dimension or a measure on top of it.

Think of it as a spreadsheet formula column that lives inside the model instead of
inside one analyst's workbook. Define it once, and every dimension, measure, KPI,
and BI tool that uses it reads the same definition.

## Where UDAs live (this trips people up)

UDAs are **not** in the left Toolbelt next to Measures and Dimensions. You author
them **on the table itself**:

1. On the canvas, click a table node, or find the table in the **Sources** panel.
2. Click the **Edit** (pencil) button to open the table editor.
3. Open the **Attributes** tab.

The Attributes tab lists every column on the table and marks each one as **physical**
(it exists in the source) or **computed** (a UDA you added). This is the only place
UDAs are created and edited.

## UDA vs calculated measure vs plain column

Pick the right tool so the model stays simple:

| You want… | Use | Why |
|---|---|---|
| A value that lives in a source column | A plain column / [plain measure](define-measures.md) | Nothing to compute |
| A new value computed **row by row** from other columns on the **same table** | A **user-defined attribute** | The result is a column; you can group or aggregate by it |
| A number derived from **other measures** (e.g. gross-margin % from two measures) | A [calculated measure](calculated-measures.md) | The result is an aggregate, not a row-level column |
| The same measure shifted across time (YTD, prior year) | A [time variant](configure-time-variants.md) | Time intelligence, generated per query |

The rule of thumb: if the value makes sense on a single row **before** any grouping,
it is a UDA. If it only makes sense **after** aggregation, it is a calculated measure.

## Before you start

- The table must already have its **physical columns** synced from the source. A
  UDA references those columns, so if the table has none yet, sync from the source
  first (the Attributes tab tells you when this is the case).
- Decide whether the result is text, a number, or a date — you will set an
  **output type**.

## Step-by-step: add a user-defined attribute

1. Open the table editor and go to the **Attributes** tab (see above).
2. In **Add computed attribute**, enter a **Name**. Use a business-readable,
   lowercase identifier — for example `region_branch` — because BI tools surface
   this name to end users.
3. Enter the **Expression**. It is a row-level formula over the table's physical
   columns, for example:
   `CONCAT(region_code, '-', branch_code)`.
   Use the **Function** picker and **Insert** to drop a supported function into the
   expression if you are unsure of the exact name.
4. Set the **Output type** (for example text, number, or date) to match what the
   expression returns.
5. Optionally add a **Description**. It is surfaced in the glossary, so a one-line
   explanation here reaches analysts later.
6. Click **Validate**. Tessallite checks the expression against the real source and
   returns *Validation passed* or a specific error. Fix any error before saving —
   an invalid UDA is a wrong-numbers risk downstream.
7. Click **Add**. The new attribute appears in the list marked **computed**.

## Use a UDA as a dimension or a measure

A UDA is only a column definition — on its own it does not appear in queries. To
put it to work:

- Build a **dimension** on it (in the Dimensions area) to group or filter by the
  computed value.
- Build a **measure** on it (in the Measures area) to aggregate the computed value.

Both pick the UDA from the same table column list a physical column would appear in.

## Tips and pitfalls

- **Row-level only.** A UDA is evaluated per row before grouping. It cannot
  reference a measure or an aggregate; that is what a
  [calculated measure](calculated-measures.md) is for.
- **Validate before you trust it.** The Validate button runs the expression against
  the source. A UDA that saves but was never validated can silently produce wrong
  values.
- **Removing a UDA invalidates its dependents.** If a dimension or measure
  references the attribute, deleting the attribute makes them invalid. Repoint or
  remove those first.
- **Hierarchy-generated attributes are read-only.** Some computed attributes are
  created automatically by a date hierarchy. Their expression is managed for you —
  you can rename them, change the description, or adjust the output type, but you
  cannot edit the formula here.
- **Names reach end users.** The attribute name flows through to Excel, JDBC, and
  XMLA, so keep it stable and business-readable.

## Related

- [Define Measures](define-measures.md) — aggregate a column or a UDA
- [Calculated Measures](calculated-measures.md) — derive a number from other measures
- [Define Dimensions](define-dimensions.md) — group and filter by a column or a UDA
- [Add Tables to a Model](add-tables-to-a-model.md) — the table editor these live in
- [Business Glossary](business-glossary.md) — where UDA descriptions surface

---

← [Calculated Measures](calculated-measures.md) | [Home](../index.md) | [Configure Time Variants →](configure-time-variants.md)
