---
title: "Multi-Calendar Best Practices"
audience: modeller
area: modelling
updated: 2026-05-06
---

## What this covers

This article collects planning, naming, and operational guidelines for models that use more than one calendar type. Following these practices avoids the most common multi-calendar mistakes: ambiguous joins, runaway aggregate counts, and NULL period values from insufficient date coverage.

---

## Planning

Decide which calendar types you need before creating hierarchies. Questions to answer:

1. **Who consumes the reports?** Finance uses fiscal, retail ops uses 4-4-5, logistics uses ISO weeks, legal may need Hijri or Thai Buddhist. Map each audience to a calendar type.
2. **How many date columns does your fact table have?** Each date column that needs time-intelligence variants needs a time hierarchy with the appropriate calendar type. Two date columns using the same calendar type need separate hierarchies.
3. **Do you need cross-calendar comparison?** Comparing fiscal YTD to standard YTD on the same measure requires two hierarchies with different calendar types linked to the same base measure — this creates two variant measures (e.g. `revenue_ytd` and `revenue_ytd_fiscal`).
4. **Do you need a physical calendar table?** For standard, fiscal, ISO, and Hijri calendars, the hierarchy's calendar type is sufficient — period boundaries are computed from SQL expressions. Calendar tables are needed for retail 4-4-5 (irregular period patterns), dense date enumeration, or custom period definitions.

---

## Naming conventions

| Object | Pattern | Example |
|---|---|---|
| Calendar table | `calendar_<type>` | `calendar_fiscal`, `calendar_445` |
| Dimension alias | `<fact_column>_<type>` | `order_date_fiscal` |
| Hierarchy | `<type> Calendar` | `Fiscal Calendar`, `ISO Week Calendar` |
| Time variant | `<base>_<variant>` (auto) | `revenue_ytd`, `revenue_ytd_fiscal` |

Consistent naming across the model makes it immediately clear which calendar system any given object belongs to.

---

## One calendar per date column

Do not reuse the same calendar alias for two different date columns. The query router resolves the join path from the dimension's associated calendar. If two dimensions share an alias, the router cannot determine which join to use, and the query may silently produce wrong results or fail.

If two date columns need the same calendar type, create separate aliases:
- `order_date` → alias `order_date_standard`
- `ship_date` → alias `ship_date_standard`

---

## Calendar coverage

The calendar table must cover your entire fact data range plus headroom:

- **Past:** At least as far back as your oldest fact row. If your data starts in 2018, the calendar must start no later than 2018-01-01.
- **Future:** At least 3 years ahead. Users running YTD or rolling-12-month variants need future dates in the calendar to avoid NULL boundaries.
- **Check coverage periodically.** If your fact data grows beyond the calendar range, new dates silently lose time-intelligence capability.

---

## Aggregate routing impact

Each distinct calendar type × grain combination creates a different query pattern. The aggregate matcher and predictive aggregate builder treat `(revenue, standard_hierarchy.year)` and `(revenue, fiscal_hierarchy.fiscal_year)` as separate grains. This means:

- More calendar types → more distinct query patterns → more aggregates needed for full coverage.
- The predictive builder accounts for this — it scores by ROI, not by raw count — but storage budgets may need to increase.
- If a calendar type is rarely queried, the aggregate for it may never pass the ROI threshold and those queries will always route to source.
- Expression-based variants (those without a calendar table) are not materialised in pre-aggregates — they always rewrite over the base measure at query time. This reduces aggregate storage but means these variants do not benefit from pre-aggregation.

---

## When NOT to use multiple calendar types

If all your reporting is standard Gregorian, one time hierarchy with calendar type `standard` is sufficient. Signs that you don't need a second calendar type:

- Nobody asks for fiscal, retail, or non-Gregorian dates.
- Your company's fiscal year starts in January (standard and fiscal are identical).
- You only have one date column per fact table.

Adding calendar types you don't query adds complexity to the variant resolver. If you don't need a physical calendar table (retail 4-4-5, dense enumeration), don't create one — set the calendar type directly on the hierarchy.

---

## Common mistakes

| Mistake | Symptom | Fix |
|---|---|---|
| Two standard calendars on one source | Ambiguous dropdown in dimension dialog | Unbind the duplicate |
| Calendar range too short (when using calendar table) | YTD returns NULL for recent dates | Extend the calendar range |
| Forgot to bind after manual DDL | Calendar table exists on source but system doesn't know about it | Bind the existing table |
| Mixing calendar types in one pivot | Nonsensical cross-calendar numbers | Filter to one calendar type per query |
| Aliases not created for shared calendar | Wrong join path selected | Create aliases per date column |
| No calendar type on hierarchy | Period-boundary variants show as "not eligible" | Set a calendar type on the time hierarchy |

---

## Related

- [Calendar Types](../concepts/calendar-types.md)
- [Configure Calendar Table](configure-calendar-table.md)
- [Associate Calendar with Dimensions](associate-calendar-with-dimensions.md)
- [Configure Time Variants](configure-time-variants.md)

---

← [Associate Calendar with Dimensions](associate-calendar-with-dimensions.md) | [Home](../index.md) | [Measure Query Panel →](measure-query-panel.md)
