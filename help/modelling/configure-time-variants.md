---
title: "Configure Time Variants"
audience: modeller
area: modelling
updated: 2026-05-06
---

## What this covers

A time variant is a derived form of a base measure that answers a time-intelligence question without forcing the modeller to write a separate measure. Examples are year-to-date revenue, prior-quarter order count, and 12-month trailing average. In Tessallite each variant you tick on a base measure becomes its own first-class measure row in the catalog, named `<base>_<variant>` (for example `revenue_ytd`). The variant row inherits the base measure's source column, format, data type, default aggregation, and additivity at creation. This article explains which variants exist, when they are admissible, and how to create them.

---

## Before you start — the prerequisite chain

Creating a time variant requires a chain of configuration steps to be completed first. If any step is missing, the variant will appear as "not eligible" with a reason explaining which prerequisite is absent. The full chain is:

### 1. Define the base measure

The measure you want to extend must already exist. See [Define Measures](define-measures.md).

### 2. Create a time hierarchy with a calendar type

A time hierarchy tells the system which time levels (year, quarter, month, week, day) are available, which time calculations are enabled at each level, and what calendar system governs period boundaries.

To create one:

1. Open the **Hierarchies** panel in the Toolbelt.
2. Create a new hierarchy. Set its dimension kind to **time**.
3. Set the **calendar type** to one of:
   - **standard** — Gregorian calendar, year starts January 1. (Expression-based, no calendar table required.)
   - **fiscal** — Gregorian calendar with a shifted year start. Select the **fiscal year start month** (e.g. April for a UK fiscal year, July for Australian). (Expression-based, no calendar table required.)
   - **iso** — ISO 8601 week-based year. (Expression-based, no calendar table required.)
   - **hijri** — Islamic calendar, year starts 1 Muharram. (Requires a calendar table due to lunar month complexity.)
   - **retail_445** — Retail 4-4-5 calendar pattern. (Requires a calendar table due to irregular week patterns.)
   - **thai_buddhist** — Thai Buddhist calendar. (Requires a calendar table due to era offset handling.)
4. Add levels for each time granularity you need (e.g. year, quarter, month). On each level, set the **time unit** and enable the **allowed time calculations** for the variants you plan to use.

Period boundaries (where a year, quarter, or month starts and ends) are computed automatically from the calendar type. For **standard**, **fiscal**, and **iso** calendar types, a calendar table is not required — the system derives boundaries using date expressions. For **hijri**, **retail_445**, and **thai_buddhist** calendar types, a physical calendar table is required due to the complexity of their date calculations.

If a calendar table IS present in the model, the system uses it for backward compatibility. The system automatically associates measures with the time hierarchy that shares the same table as the measure's source column — no manual linking step is needed.

### Prerequisite summary

| Variant group | Base measure | Time hierarchy with calendar type | Level capabilities |
|---|---|---|---|
| Period-to-date (`ytd`, `qtd`, `mtd`, `wtd`) | Required | Required | `period_to_date` |
| Parallel period (`prior_year`, `prior_quarter`, etc.) | Required | Required | `parallel_period` |
| Year-over-year (`yoy_growth`, `yoy_growth_pct`) | Required | Required | `parallel_period` |
| Window (`lag`, `trailing_n`, `moving_avg_n`) | Required | Not required | `lag` or `moving_window` |

---

## Available variants

| Variant | Family | Required time unit | Calendar type needed |
|---|---|---|---|
| `lag` | lag | (any) | No |
| `prior_year` | parallel_period | year | Yes |
| `prior_quarter` | parallel_period | quarter | Yes |
| `prior_month` | parallel_period | month | Yes |
| `prior_week` | parallel_period | week | Yes |
| `ytd` | period_to_date | year | Yes |
| `qtd` | period_to_date | quarter | Yes |
| `mtd` | period_to_date | month | Yes |
| `wtd` | period_to_date | week | Yes |
| `ytd_prior_year` | period_to_date | year | Yes |
| `yoy_growth` | parallel_period | year | Yes |
| `yoy_growth_pct` | parallel_period | year | Yes |
| `trailing_n` | moving_window | (any) | No |
| `moving_avg_n` | moving_window | (any) | No |

Default `n` for `trailing_n` is 12; default `n` for `moving_avg_n` is 30. Both can be overridden per measure.

---

## Admission rules

A variant can be ticked on a base measure only when **all** of the following are true for the measure's associated time hierarchy:

1. The variant's family appears in at least one level's `allowed_time_calcs`.
2. The variant's required time unit (if any) is present as a level `time_unit` in the same hierarchy.
3. If the variant is period-aware, the associated hierarchy has a calendar type configured.

Variants that fail any rule are not offered for selection in the drawer.

---

## Creating variants — two paths

There are two ways to create time variants. Both produce the same result — a new measure row in the catalog.

### Path A: From the Measures panel (Toolbelt)

1. Open the base measure in the Drawer (Toolbelt → Measures → click the measure name).
2. Under **Time variants**, tick the variants you want. For `trailing_n` and `moving_avg_n`, enter an `N` (defaults: 12 and 30).
3. For period-boundary variants, select the **hierarchy** to use. The dropdown shows all hierarchies with matching capabilities. Different hierarchies point to different calendar tables (e.g. standard vs fiscal), so the choice determines which calendar system the variant uses.
4. Click **Save**. The catalog refreshes; each ticked variant appears as its own measure row beside the base, named `<base>_<variant>`.

### Path B: From the Measure Query Panel (pivot table)

This is a shortcut for when you are actively testing a measure and want to quickly add a variant without leaving the pivot context.

1. Open the **Measure Query Panel** from the Toolbelt.
2. Select a measure in the Measure dropdown.
3. Next to the measure dropdown, find the **variant button** — a small icon showing a function symbol (f) with a plus sign (+).
4. Click the button. A popover opens showing all 14 variant kinds.
5. Each variant shows its eligibility status:
   - **Eligible** variants display the suggested name (e.g. `base_amount_ytd`) and can be clicked to create immediately.
   - **Ineligible** variants show the specific reason they cannot be created and what needs to be configured. For example: "This measure has no associated time hierarchy. Create a time hierarchy with the required levels on the same table as the measure's source column."
   - **Already added** variants are greyed out.
6. For parametric variants (`trailing_n`, `moving_avg_n`), a text field appears where you can enter the window size `N`.
7. Click an eligible variant to create it. The new measure is added to the catalog and becomes available in the pivot table's measure dropdown.

### Managing variants

To remove a variant, untick it in the Measures panel Drawer and Save. The variant row is deleted; queries that referenced it will fail with an unknown-measure error.

To rename or re-format a variant row, open the variant directly. Source column, aggregation, and data type are inherited from the base and are not editable on the variant row — change them on the base.

### Multiple variants with different calendars

You can create the same variant type multiple times on the same base measure, each with a different hierarchy:

- `revenue_ytd` — year-to-date using the standard calendar hierarchy
- `revenue_ytd_fiscal` — year-to-date using the fiscal calendar hierarchy

Each produces a separate measure row with distinct period boundaries. BI tools see them as independent measures and can place them side by side in the same pivot table.

### What happens when you delete a hierarchy

If you delete a hierarchy that a variant depends on, the variant loses its period-boundary capability. The base measure still works, but the variant row becomes invalid and is marked with a warning. Either re-create the hierarchy or remove the variant.

---

## How variants are computed

Each variant is rewritten into a window function at query time. Period boundaries are computed using SQL expressions derived from the hierarchy's calendar type — for example, `EXTRACT(YEAR FROM date)` for a standard calendar or a fiscal-year CASE expression for fiscal calendars. If a calendar table is present in the model, the system uses it instead of expressions for backward compatibility. Postgres is the canonical authoring dialect; for BigQuery and Spark, Tessallite transpiles the canonical SQL via sqlglot. The variant row is metadata only — it does not duplicate the value in storage unless the AI optimiser proposes a per-variant aggregate.

> **Note:** If a variant query is slow, run the AI optimiser. The optimiser scores per-variant aggregates the same way it scores base-measure aggregates and proposes pre-aggregated tables when the cost-benefit is positive. See [Use the AI optimiser](use-the-ai-optimiser.md).

---

## Filtering and period-to-date variants (important limitation)

A period-to-date variant (`ytd`, `qtd`, `mtd`, `wtd`, and the prior-period and year-over-year families) is a window calculation that runs **over the rows that survive your filters**. The window cannot see rows you have filtered away.

This means: if you filter a query down to a single sub-period and also ask for a period-to-date measure, the period-to-date value covers **only the rows left after the filter**, not the whole period to that point.

**Worked example.** You query `revenue_ytd` and add a filter `month = '2024-03'`.

- What you might expect: revenue accumulated from January through March 2024.
- What you get today: March 2024's revenue only — because January and February were removed by the filter before the window ran, so there is nothing earlier for the window to accumulate.

To get a correct year-to-date figure, keep the earlier periods in the result set. Two reliable patterns:

- **Plot the trend.** Put the time dimension (e.g. month) on an axis and let every month through. Each row then shows its own correct period-to-date value, and you read off the month you care about. This is the intended way to consume period-to-date variants.
- **Filter to the period, not a point inside it.** Filter by the *whole* period (e.g. `year = 2024`) rather than a single sub-period (`month = '2024-03'`), so all the rows the window needs are present.

This is a property of how window functions interact with filters, not a defect specific to one variant — it applies to every period-aware variant in the catalog. If you need a single year-to-date number for one point in time without plotting the trend, use the trend pattern above and pick the row, or define the figure as a base measure with a period filter.

---

## Period-to-date variants reset at the period boundary

A period-to-date variant only accumulates **inside its own period**, and starts again at the next period boundary:

- **Year-to-date (`ytd`)** restarts at the first day of each year.
- **Quarter-to-date (`qtd`)** restarts at the first day of each quarter.
- **Month-to-date (`mtd`)** restarts at the first day of the month.
- **Week-to-date (`wtd`)** restarts at the start of each ISO week.

Numbers never spill from one period into the next. January's `mtd` covers only January — it does not carry December's total forward — and the same month a year earlier is a *separate* bucket. This matters most across year boundaries: a year-to-date figure for early January is small (a few days), not "last year's full total plus a few days". Week-to-date follows the **ISO** calendar, so the week that straddles 1 January belongs to exactly one ISO week-numbering year and is never split across two.

**Prior-year and prior-period families look back a *real* period.** `ytd_prior_year` returns the year-to-date figure for the **same point in the previous year**, so you can place this year and last year side by side and compare like with like. `prior_quarter` compares the same position within the previous quarter. When there is no matching period in the prior year — for example the first year of data — the value comes back **blank rather than a wrong number**, so a missing comparison never silently shows as zero or as the current period repeated.

> If you have used an earlier build where a year-to-date measure appeared to cumulate across year boundaries, or where a "prior year" figure looked identical to the current year, those were the symptoms this behaviour corrects: each period now resets cleanly and prior-period families genuinely look back.

---

## Variants in pre-aggregates (limitation)

The optimiser materialises only **window-based** variants in CTAS pre-aggregate tables: `lag`, `trailing_n`, and `moving_avg_n`. These are computed inside the aggregate using a window function over the per-grain rows.

**Period-aware variants are not materialised.** Variants in this list always rewrite over the base measure's pre-aggregate (or the source) at query time:

- `prior_year`, `prior_quarter`, `prior_month`, `prior_week`
- `ytd`, `qtd`, `mtd`, `wtd`
- `ytd_prior_year`, `yoy_growth`, `yoy_growth_pct`

These variants depend on a calendar-table JOIN whose validity moves with the calendar contents. Baking that JOIN into a CTAS would couple the aggregate to a specific calendar snapshot, so Tessallite refuses the materialisation by design. They still execute correctly via the rewriter — only the *pre-aggregate* path is closed.

A window-based variant additionally requires the aggregate's grain to contain a time dimension (used for `ORDER BY` in the window). If you create an aggregate at a non-time grain, only base measures and non-variant aggregations are materialised.

---

## Worked example — rolling 6-month average NPS score

Suppose you have a **customer onboarding** model built on a fact table that records one row per customer application. Each row has a `nps_score` (0-10 rating collected from the applicant) and a `survey_date` (the date the survey was completed). You already defined a base measure `avg_nps_score` with `default_agg = AVG` on the `nps_score` column.

**Goal:** show a monthly trend where each month's value is the rolling average of the current month and the five months before it, so stakeholders can see how customer satisfaction is trending over a smoothed window rather than bouncing month to month.

### Step 1 — Create a date hierarchy on `survey_date`

1. Open the **Hierarchies** panel and click **Generate Date Hierarchy**.
2. Select the calendar alias for `survey_date` and choose grain **Year > Month > Day** (y_m_d).
3. The system creates a hierarchy with three levels. Each level gets the default allowed time calculations — including `moving_window`, which is the capability that `moving_avg_n` requires.

After this step you have a hierarchy named something like `survey_date Calendar` with levels for year, month, and day. The month-level dimension is `survey_date_calendar_month`.

### Step 2 — Link the base measure to the hierarchy

1. Open the base measure `avg_nps_score` in the Measures panel.
2. Set the **Hierarchy** field to `survey_date Calendar`.
3. Save.

This tells the system which time hierarchy governs time variants on this measure.

### Step 3 — Create the moving-average variant

1. With the base measure still open, click **Add Time Variant**.
2. Select variant type **Moving Average (N)**.
3. Set **N = 6** (six months).
4. The system proposes the name `avg_nps_score_moving_avg_6`. Accept or rename.
5. Save. The variant appears as a new measure row in the catalog.

### Step 4 — Query the variant

Open any BI tool connected through the Tessallite gateway and run:

```sql
SELECT "survey_date_calendar_month",
       "avg_nps_score_moving_avg_6"
FROM   "customer_onboarding"
GROUP BY "survey_date_calendar_month"
```

The result is one row per month. Each row shows the average NPS score smoothed over a 6-month rolling window:

| survey_date_calendar_month | avg_nps_score_moving_avg_6 |
|---|---|
| 1 | 6.78 |
| 2 | 6.45 |
| 3 | 6.42 |
| 4 | 6.51 |
| 5 | 6.56 |
| 6 | 6.60 |
| 7 | 6.72 |
| 8 | 6.80 |
| 9 | 6.77 |
| 10 | 6.76 |
| 11 | 6.75 |
| 12 | 6.80 |

### What Tessallite does behind the scenes

The query router rewrites the semantic query into a source SQL statement that looks like this (shown here in PostgreSQL form — the system transpiles automatically for BigQuery, Spark, and other connectors):

```sql
SELECT EXTRACT(MONTH FROM cal.full_date)          AS survey_date_calendar_month,
       AVG(AVG(fact.nps_score)) OVER (
           ORDER BY MIN(EXTRACT(MONTH FROM cal.full_date))
           ROWS BETWEEN 5 PRECEDING AND CURRENT ROW
       )                                            AS avg_nps_score_moving_avg_6
FROM   source_schema.fact_onboarding AS fact
  LEFT JOIN source_schema.dim_date AS cal
    ON fact.survey_date = cal.full_date
GROUP BY EXTRACT(MONTH FROM cal.full_date)
```

Key points:

- The inner `AVG(fact.nps_score)` is the base measure aggregation.
- The outer `AVG(...) OVER (... ROWS BETWEEN 5 PRECEDING AND CURRENT ROW)` is the rolling window contributed by the `moving_avg_n` variant with N=6 (current row + 5 preceding = 6 rows).
- The `ORDER BY MIN(...)` ensures the window orders rows chronologically. The `MIN()` wrapper is a technical requirement so that certain databases (notably BigQuery) accept the expression inside an aggregated query.
- The calendar table join (`dim_date`) is used because the month dimension is a date-hierarchy dimension referencing calendar columns. If you used a simple `DATE_TRUNC` dimension instead, no join would be needed.

### When to use this vs. a filtered measure

The rolling-window variant produces a **trend line** — one smoothed value per time period. It does not produce a single number.

If what you need is a single KPI value like "average NPS in the last 6 months", use a regular base measure query with a date filter instead:

```sql
SELECT AVG("nps_score")
FROM   "customer_onboarding"
WHERE  "survey_date" >= DATE_SUB(CURRENT_DATE(), INTERVAL 6 MONTH)
```

Use the rolling variant when you want to **plot** the trend; use a filtered query when you want a **single scorecard number**.

---

## Troubleshooting

| Message | Meaning | What to do |
|---|---|---|
| "This measure has no associated time hierarchy." | The system could not find a time hierarchy on the same table as the measure's source column. | Create a time hierarchy whose levels reference columns on the same table as the measure's source column. |
| "The associated time hierarchy does not declare the '...' capability on any level." | The hierarchy exists but none of its levels have the required time calculation enabled. | Edit the hierarchy. On the appropriate level, enable the missing calculation (e.g. `period_to_date` for YTD, `parallel_period` for prior year). |
| "The associated time hierarchy has no '...'-grain level." | The hierarchy has the right calculation but is missing a level at the required granularity (e.g. no year-level for YTD). | Edit the hierarchy and add a level with the missing time unit. |
| "The associated time hierarchy has no calendar type configured." | Period-aware variants need a calendar type on the hierarchy. | Edit the hierarchy and set a calendar type (standard, fiscal, hijri, or iso). |
| "A measure named '...' already exists in this model." | A variant with this name was previously created, or another measure uses the same name. | Delete or rename the conflicting measure, then try again. |

---

## Pitfalls

- **Creating a period-boundary variant without a calendar type.** The variant will be marked as ineligible. Edit the hierarchy and set a calendar type before enabling period-boundary variants.
- **Mixing calendars in the same pivot query.** Querying `revenue_ytd` (standard) and `revenue_ytd_fiscal` (fiscal) in the same pivot with a single date dimension produces meaningless cross-calendar numbers. Use one calendar system per query context.
- **Assuming all variants need a calendar type.** Window variants (`lag`, `trailing_n`, `moving_avg_n`) work without any calendar configuration — they only need a date dimension in the query grain.

---

## Related

- [Define Measures](define-measures.md)
- [Configure Calendar Table](configure-calendar-table.md)
- [Associate Calendar with Dimensions](associate-calendar-with-dimensions.md)
- [Calendar Types](../concepts/calendar-types.md)
- [Use the AI optimiser](use-the-ai-optimiser.md)

---

← [Calculated Measures](calculated-measures.md) | [Home](../index.md) | [Configure Calendar Table →](configure-calendar-table.md)
