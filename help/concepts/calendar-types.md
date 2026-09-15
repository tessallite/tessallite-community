---
title: "Calendar Types"
audience: modeller
area: concepts
updated: 2026-07-04
---

## What this covers

Tessallite supports six calendar types that govern how period boundaries (year start, quarter start, etc.) are computed for time-intelligence variants like YTD, prior year, and year-over-year growth. The calendar type is set on the time hierarchy and determines which SQL expressions the query router uses to derive period boundaries. This article explains the six types, when each applies, and how they differ.

---

## How period boundaries are computed

Tessallite computes period boundaries using SQL expressions derived from the hierarchy's calendar type. For standard and ISO calendars, the system uses `EXTRACT`-based expressions. For fiscal calendars with a shifted year start, it generates CASE expressions that offset the year and quarter. For example, a fiscal year starting in April uses `CASE WHEN EXTRACT(MONTH FROM date) >= 4 THEN EXTRACT(YEAR FROM date) ELSE EXTRACT(YEAR FROM date) - 1 END` to determine the fiscal year.

A physical calendar table is **not required** for most calendar types. The system derives boundaries directly from the date column. Calendar tables remain valuable for:

- **Retail 4-4-5 and Hijri** - these period systems require a physical table because their boundaries cannot be safely inferred from ordinary Gregorian date arithmetic.
- **Dense date enumeration** — queries that need every date in a range, including dates with no fact rows.
- **Custom period definitions** — non-standard boundaries that don't follow a formulaic pattern.

When a calendar table IS present, the system uses its pre-computed columns instead of expressions for backward compatibility.

---

## Calendar table binding rule

Standard, fiscal, ISO Week, and Thai Buddhist calendars are expression-capable: Tessallite can compute their period boundaries from the hierarchy's calendar type and the fact date. Retail 4-4-5 and Hijri calendars are table-bound: they require a physical calendar table with real mapped source columns. A physical table is also required for dense date enumeration, custom business period columns, coverage checks, and calendar dimension aliases.

## The six calendar types

### Standard (Gregorian)

The default. Year starts January 1, months are 1-12, quarters are Q1 (Jan-Mar) through Q4 (Oct-Dec). Use this when all your reporting follows the common calendar.

**Who uses it:** Most organisations that don't have a fiscal offset or specialised reporting calendar.

### Fiscal

Identical structure to standard, but the year starts on a configurable month (e.g., April for many governments and enterprises, July for Australian public sector, October for US federal). A fiscal year starting in April means fiscal Q1 is April-June.

**Who uses it:** Governments, enterprises, universities — any organisation whose financial year does not start in January.

**Year captions:** Fiscal and NRF retail calendars expose `year_no` (the
numeric key) and a caption-only `year_label`. The tenant setting
`calendar.fiscal_year_label_format` accepts `start_year` (default),
`span_short`, `span_long`, `span_fy`, and `end_year`; see [Configure Calendar
Table](../modelling/configure-calendar-table.md) for the examples and the
tenant settings endpoint. January-start fiscal calendars always use the plain
integer, regardless of the selected token.

**Key concept — fiscal offset:** The fiscal start month shifts all period boundaries. "Fiscal year 2026" with an April start covers April 2025 through March 2026. Forgetting this offset is the most common source of wrong YTD numbers in cross-calendar environments.

### ISO week (ISO 8601)

Weeks start on Monday, numbered 1-52 (occasionally 53). Week 1 is the week containing the first Thursday of January. This means January 1 can fall in ISO week 52 or 53 of the previous year — a subtlety that breaks naive `EXTRACT(WEEK FROM date)` in many SQL dialects.

**Who uses it:** Logistics, manufacturing, EU government reporting, any context where "week" must be unambiguous across borders and systems.

### Retail 4-4-5

The NRF (National Retail Federation) calendar divides the year into 4-week and 5-week periods following a 4-4-5 pattern within each quarter. Periods do not align with calendar months: a "retail month" is either 4 or 5 weeks long.

Because this week-and-period grid **cannot** be derived by ordinary date arithmetic, a retail 4-4-5 calendar **requires a physical calendar table** to be bound to the time hierarchy. Tessallite reads the period and year boundaries straight from that table's pre-computed columns rather than computing them from a formula — which also means the exact retail-year start date is whatever your calendar table defines, not a value Tessallite guesses. If you select the retail calendar type but no calendar table is bound, the query **fails loudly with a clear error** rather than silently falling back to ordinary months and returning wrong numbers.

**Who uses it:** Retail chains, CPG companies, any business that needs like-for-like weekly comparisons across years without the distortion of months having different numbers of days.

**Key concept — period vs month:** In a 4-4-5 calendar, "Period 1" is the first 4-week block of Q1, not January. Retail reports labelled "monthly" are actually period reports. Mixing retail periods with standard months in the same pivot query produces meaningless numbers.

Retail year captions use the same tenant setting as fiscal calendars, while
`retail_year` remains the numeric grouping and sort key. A normal rebuild is
required before an existing table gains `year_label`; BI metadata falls back to
the numeric key until then.

### Hijri (Islamic)

A lunar calendar with 12 months of 29 or 30 days, producing a year of 354 or 355 days. Months shift approximately 11 days earlier each Gregorian year. The conversion between Gregorian and Hijri dates is algorithmic but lossy at the edges: a single Gregorian date can span two Hijri months depending on moon sighting conventions.

**Who uses it:** Islamic finance, Middle Eastern government agencies, any reporting context where Hijri dates are the legal or cultural standard.

**Key concept — conversion is approximate:** Different authorities use different sighting rules. Tessallite uses the Umm al-Qura algorithm (the Saudi civil calendar), which is the most widely accepted computational standard. For dates near month boundaries, there may be a one-day discrepancy with locally observed calendars.

### Thai Buddhist

The Thai Buddhist calendar adds 543 years to the Gregorian year (2026 CE = 2569 BE). Months and days are identical to the standard calendar. This is a trivial offset, but it matters in Thai government reporting where the legal year must appear as the Buddhist Era number.

**Who uses it:** Thai government agencies, Thai financial institutions, any organisation required to report in Buddhist Era years.

---

## Multiple calendar types on a single model

A model can use any number of calendar types across its hierarchies. This is common when:

- You report revenue on a fiscal calendar but track logistics on ISO weeks.
- Your sales team uses 4-4-5 periods while finance uses standard months.
- You serve both domestic (Hijri) and international (Gregorian) audiences.

Each time hierarchy carries its own calendar type. When you create a time hierarchy, you set the calendar type on it. Different hierarchies on the same model can use different calendar types. Physical calendar tables are only needed for table-bound calendar types (retail 4-4-5 and Hijri) or physical-table workflows such as dense enumeration, custom period columns, coverage checks, and dimension aliases.

**When one is enough:** If all your time-intelligence variants use the same calendar system, one hierarchy with one calendar type is sufficient. Don't create extra hierarchies "just in case" — each one adds complexity to the variant resolver.

---

## Related

- [Configure Calendar Table](../modelling/configure-calendar-table.md)
- [Associate Calendar with Dimensions](../modelling/associate-calendar-with-dimensions.md)
- [Configure Time Variants](../modelling/configure-time-variants.md)
- [Multi-Calendar Best Practices](../modelling/multi-calendar-best-practices.md)

---

← [Roles and Permissions](roles-and-permissions.md) | [Home](../index.md) | [Create a Project →](../modelling/create-a-project.md)
