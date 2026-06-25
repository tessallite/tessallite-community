---
title: "Define Hierarchies"
audience: modeller
area: modelling
updated: 2026-05-15
---

## What this covers

The **Hierarchies** panel defines ordered drill paths such as Year > Quarter > Month, Country > Region > Store, or Segment > Category > Product. Hierarchies give Tessallite enough structure to generate time variants, guide drill-through, and expose business navigation paths to downstream consumers.

This is different from the Dimensions panel. Dimensions expose individual attributes; hierarchies declare how attributes roll up.

---

## Hierarchy types

| Type | Use it when |
|---|---|
| Explicit | You already know each level and want to add them manually. |
| Date embedded | A date column can generate levels such as year, quarter, month, week, or day. |
| Segment | A code or text field contains multiple levels that can be split by delimiter or position. |

---

## Time hierarchy fields

For time hierarchies, set the dimension kind to **time** and choose the calendar type when period calculations matter. Fiscal and ISO calendars change the meaning of year, week, quarter, and period-to-date variants.

Time units on levels are what let measures support variants such as YTD, prior year, moving windows, and period-to-date calculations.

---

## Generated attributes are managed for you

When a date hierarchy generates levels such as year, quarter, month, or week, Tessallite also creates the matching attributes on the table for you. These are **generated attributes**: their formula is written and kept up to date by the hierarchy itself, so you do not edit it by hand.

If you open such an attribute in the table editor, the **expression box is locked** and the formula-building tools are hidden, with a note explaining that the attribute came from a date hierarchy. You can still rename it, change its description, or adjust its output type — only the *formula* is protected. This is deliberate: it stops a later change to the hierarchy from colliding with a hand edit, which would otherwise silently drift the two apart. To change what the formula does, edit the **hierarchy**, not the attribute, and the generated attributes update with it.

---

## Health checks

The panel checks whether hierarchy levels still point to valid attributes, whether required time metadata is present, and whether generated levels can be interpreted by the query compiler. Resolve hierarchy warnings before relying on time variants or drill paths.

---

## Related

- [Define Dimensions](define-dimensions.md)
- [Configure Time Variants](configure-time-variants.md)
- [Calendar Types](../concepts/calendar-types.md)

---

← [Define Joins](define-joins.md) | [Home](../index.md) | [Define Dimensions →](define-dimensions.md)
