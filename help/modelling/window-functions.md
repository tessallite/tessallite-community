---
title: "Understanding Window Functions"
audience: modeller
area: modelling
updated: 2026-08-14
---

## What this covers

Tessallite's window variants — `trailing_n`, `moving_avg_n`, and `lag` — compute values over ordered sequences. Each variant is a window function applied at query time. When a semantic time grain is resolved, the source and aggregate routes use the same period key and reject sparse frames rather than silently widening them. This article explains what the window sees, what happens when data has gaps, and how partial windows behave.

## Which variants use windows

| Variant | Window type | Window size |
|---|---|---|
| `trailing_n` | Sum over preceding rows | N rows (default 12) |
| `moving_avg_n` | Average over preceding rows | N rows (default 30) |
| `lag` | Single row N places earlier | N (default 1) |

These variants do not require a calendar type or a time hierarchy. They only need a date dimension in the query grain to order rows.

## How windows are measured

Window variants use a `ROWS` frame, but at a resolved time grain the period key must also be contiguous. For a query grouped by month:

- `trailing_3` on revenue sums the current month plus the two immediately preceding months when all three periods are present — regardless of whether those months have the same number of days.
- `moving_avg_30` on a daily query averages the current day and the 29 preceding contiguous days; a gap inside the frame returns `NULL` for that row.
- `lag(2)` on orders returns a value only when the preceding row sequence reaches the immediately prior period key.

The physical frame is `ROWS BETWEEN N-1 PRECEDING AND CURRENT ROW`. A period-key span/count guard makes that row frame period-safe after grouping and filtering; it does not insert zero-valued rows for empty periods.

The period key follows the selected calendar rather than assuming Gregorian months. Hourly rows use a contiguous hour index. Standard, Hijri, and retail 4-4-5 calendars use the actual period number on a 12-period year scale; a retail 53rd week remains part of period 12, so period 12 to period 1 is adjacent. Fiscal calendars derive period 1 from the configured fiscal start month, so March to April is adjacent for an April-start year.

## Partial windows at series boundaries

A window variant needs the full N rows to compute its advertised value. At the start of a series, fewer than N rows exist. This is a partial window.

### What happens

Tessallite does not pad missing rows with zeros. The window function only sees the rows that are actually present relative to the current row. The result is a best-effort computation over however many rows are available:

- **Row 1 of a query grouped by month with `moving_avg_3`**: only one row exists, so the "3-month moving average" is simply that month's value. It is not the average of one month and two phantom zeros — but it is also not a genuine 3-month average.
- **Row 2**: two rows exist, so the value is the average of two months.
- **Row 3 onward**: three rows exist, so the value is a genuine 3-month average.

The label "3-month moving average" describes the window size, not a guarantee that every data point at the start of the series is backed by three full months. Tools that chart these values should consider annotating or de-emphasising the leading partial-window points.

### Lag at boundaries

`lag(N)` at the boundary returns `NULL` because there is no row N positions earlier. Tessallite does not substitute zero — zero is a real value and would silently distort the comparison. A missing lag means the comparison cannot be made, and the absence is explicit.

## Gaps in data

If the source data has no rows for a period inside a requested frame, the window does not reach farther back to compensate. The affected `trailing_n`, `moving_avg_n`, or `lag` value is `NULL`; it does not insert a zero for the missing period.

### Sparse data

If most periods are missing, window variants produce explicit `NULL` values until the retained frame is contiguous:

- A `moving_avg_30` on a series with a missing day inside the frame returns `NULL` at that row rather than averaging across the gap.
- A `trailing_12` on a series with a missing quarter returns `NULL` while that gap remains inside the twelve-row frame; an initial frame with fewer than twelve contiguous rows remains a valid partial result.

The window size remains a ceiling at the start of a series: an initial frame may contain fewer than N contiguous rows. A gap inside that frame is different and is returned as `NULL`.

### Empty periods are skipped, not counted as zero

Tessallite does not count an empty period as zero. A `moving_avg_3` on a series where January has data, February is missing, and March has data returns `NULL` for March rather than producing (Jan + 0 + Mar)/3 or reaching back to an earlier row. This makes the missing-period condition explicit without inventing a value.

The trade-off is that an initial "N-period window" can contain fewer than N periods. If you need zero-filled calendar windows, use a calendar table or a hand-rolled expression with date filters; the window guard intentionally returns `NULL` rather than fabricating zeros.

## Example — sparse trailing sum

Consider a query grouped by month with `trailing_3` on a measure whose source data has gaps:

| Month | Revenue | trailing_3 |
|---|---|---|
| Jan | 100 | 100 |
| Feb | 120 | 220 |
| Mar | (no data) | (no row) |
| Apr | 90 | (NULL — March gap is inside the frame) |
| May | 110 | (NULL — March gap is inside the frame) |
| Jun | 130 | 330 |
| Jul | 80 | 420 |

**March has no row at all** — the source data for March is missing. April's `trailing_3` is `NULL` because the frame's period-key span includes a missing month. The window does not silently substitute an older existing row.

June is the first shown row whose retained frame is contiguous after the March gap leaves the frame; its partial result is Apr + May + Jun = 330. July continues that contiguous sequence with Apr + May + Jun + Jul = 420.

The period guard keeps the physical positional frame honest: rows are still not padded, and a missing period is visible as `NULL`. If you need a zero-filled calendar window, model the calendar rows explicitly.

## Period-to-date vs. window — the distinction

| Behaviour | Period-to-date (`ytd`, `qtd`, `mtd`, `wtd`) | Window (`trailing_n`, `moving_avg_n`, `lag`) |
|---|---|---|
| Calendar awareness | Yes — resets at period boundaries | Period-key adjacency is enforced when a time grain is resolved |
| Requires hierarchy + calendar type | Yes | No |
| Requires calendar table | Expression-based for standard/fiscal/ISO/Thai; physical table for Hijri/retail 4-4-5 | No |
| Partial periods | Well-defined — YTD on Jan 10 is Jan 1-10 | Same partial-window rules apply |
| Gaps | Each period window is computed independently; gaps are gaps | `NULL` while a gap is inside the requested frame; initial partial frames are allowed |
| Materialised in aggregates | Sometimes — a period-variant query can be served from an aggregate when the query shape is proven safe; otherwise it is computed from the base aggregate or the source | Yes — window-based variants materialise in CTAS |

## When not to use window variants

- **You need a single scorecard number, not a trend line.** Use a regular measure with a date filter (e.g. `WHERE date >= CURRENT_DATE - INTERVAL '6 MONTHS'`). Window variants produce one value per row; they are designed for charts, not single-cell KPIs.
- **You require zero-filled windows for missing calendar periods.** Use a calendar table or a hand-rolled expression with date filters; row windows return `NULL` for an unproven sparse frame.
- **Your source data has frequent gaps.** Expect `NULL` values while a gap remains inside the requested frame; the leading partial window may still contain fewer than N rows.

## Related

- [Configure Time Variants](configure-time-variants.md)
- [Define Measures](define-measures.md)
- [Configure Calendar Table](configure-calendar-table.md)

---

← [Configure Time Variants](configure-time-variants.md) | [Home](../index.md) | [Configure Calendar Table →](configure-calendar-table.md)
