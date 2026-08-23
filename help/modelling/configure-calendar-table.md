---
title: "Configure Calendar Table"
audience: modeller
area: modelling
updated: 2026-08-21
---

## What this covers

A calendar table holds one row per date with pre-computed period columns — year, quarter, month, week, day. Period-aware time variants (`_ytd`, `_prior_year`, `_yoy_growth`, ...) can use calendar table columns for period boundaries when the table is present.

A calendar table is **not required** for most time variants. Tessallite computes standard, fiscal, ISO Week, and Thai Buddhist period boundaries from SQL expressions derived from the hierarchy calendar type. Physical calendar tables are required only for table-bound calendars and explicit physical-table workflows:

- **Retail 4-4-5 calendars** - the 4-week/5-week period pattern cannot be expressed as simple date arithmetic and requires a physical lookup table.
- **Hijri calendars** - month and year boundaries depend on the calendar authority used, so Tessallite must read the mapping from a bound table.
- **Dense date enumeration** - when a query needs every date in a range (including dates with no fact rows), a calendar table provides the dense spine.
- **Custom period definitions** - non-standard period boundaries (e.g. company-specific fiscal periods, 13-period years) that do not follow a formulaic pattern.

Tessallite supports six calendar types: Standard (Gregorian), Fiscal, ISO Week, Retail 4-4-5, Hijri (Islamic), and Thai Buddhist. A single model can use multiple calendar types, for example fiscal for finance and 4-4-5 for retail reporting. A physical table is only needed when the calendar type or modelling workflow requires one. See [Calendar Types](../concepts/calendar-types.md) for when to use each type.

This article explains how to create, bind, and manage calendar tables.

## Calendar table requirement rule

Use a physical calendar table only when the calendar or workflow needs one. Standard, fiscal, ISO Week, and Thai Buddhist time variants can use the calendar type on the time hierarchy without a bound table. Retail 4-4-5 and Hijri time variants need a bound physical calendar table. Dense date enumeration, custom business period columns, coverage checks, and role-playing calendar aliases also need a bound table because those workflows join to or inspect real calendar rows.

When you bind an existing table, map the real source columns. Do not keep default names such as `date_key` or `year_no` unless those columns actually exist in the source.

---

## Before you start

- The data source must be added to the project. See [Add a data source](add-a-data-source.md).
- You need either: (a) write access on the source — enable "Allow Tessallite to run DDL on this source" on the connection, see [Auto-create requires write_access](#auto-create-requires-write_access) below — in which case Tessallite can run the DDL and auto-create the calendar; or (b) an existing calendar table on the source that follows the standard column shape below.
- Auto-create is supported for all three source types: **PostgreSQL**, **BigQuery**, and **Spark/Hive**.

---

## Standard column shape

| Period | Column name |
|---|---|
| Date | `date_key` |
| Year | `year_no` |
| Year caption | `year_label` (generated calendar tables; caption-only) |
| Half | `half_no` |
| Quarter | `quarter_no` |
| Month | `month_no` |
| Week | `week_no` |
| Day of year | `day_no` |

`date_key` is the only required column. Period columns are optional but a variant that needs a missing period column will be silently dropped from the catalog.

Standard generated calendars also include `year_label`, with the same plain
integer value as `year_no`; the tenant format only changes captions for fiscal
and NRF retail years that span two calendar years.

## Fiscal and retail year captions

Fiscal and NRF retail calendars emit a caption-only `year_label` beside the
numeric `year_no`/`retail_year` key. Choose the tenant-wide convention with
`calendar.fiscal_year_label_format` through
`GET`/`PUT /api/v1/tenants/{tenant_id}/calendar-settings`:

| Token | Example for a year starting in February |
|---|---|
| `start_year` (default) | `2025` |
| `span_short` | `2025-26` |
| `span_long` | `2025-2026` |
| `span_fy` | `FY25-26` |
| `end_year` | `FY2026` |

The numeric year remains the key, sort, and join field. January-start fiscal
calendars and ISO week calendars always use the plain integer. Existing source
tables need a normal calendar rebuild before `year_label` is available; until
then Excel, Power BI, and XMLA metadata deliberately fall back to the numeric
key. Unknown tokens are rejected with HTTP 422.

---

## Choosing the right calendar type

| Type | Use when | Fiscal start month |
|---|---|---|
| Standard | Reporting follows the common Gregorian calendar | n/a |
| Fiscal | Financial year starts on a month other than January | Required |
| ISO Week | You need ISO 8601 week numbering (logistics, EU reporting) | n/a |
| Retail 4-4-5 | Retail or CPG like-for-like weekly comparisons | n/a |
| Hijri | Islamic finance or Middle Eastern government reporting | n/a |
| Thai Buddhist | Thai government or financial institutions | n/a |

Select the type in the **Calendar type** dropdown when creating or binding a calendar. The type determines which period columns the DDL generates and how period boundaries are computed.

---

## Steps — auto-create

1. Open the **Sources** panel in Model Builder.
2. On the data source row, click the **calendar icon**. The icon shows blue once a calendar is bound.
3. Switch to the **Auto-create** tab.
4. Select the **Calendar type** from the dropdown.
5. If Fiscal is selected, set the **Fiscal year start month** (e.g. April).
6. Set **Table name** (e.g. `calendar_fiscal`). Enter the table name only — Tessallite qualifies it automatically using the source's schema or dataset configuration:
   - **PostgreSQL:** prefixed with the connection's schema setting (e.g. `demo_data.calendar_fiscal`).
   - **BigQuery:** fully qualified as `project_id.dataset.table_name` (e.g. `my-project.analytics.calendar_fiscal`). The project ID comes from the connection credentials; the dataset comes from the source configuration. Identifiers containing hyphens (common in GCP project IDs) are automatically backtick-quoted in the generated DDL.
   - **Spark/Hive:** prefixed with the database setting if configured.
7. Choose a **start date** and **end date** for the calendar range. Best practice: cover 5 years past and 3 years future to avoid NULL period values on edge dates.
8. Click **Generate**. Tessallite emits the dialect-specific DDL (Postgres `generate_series`, BigQuery `GENERATE_DATE_ARRAY`, or Spark `sequence`/`explode`) and runs it against the source. The new table is registered with `autocreated = true` and bound automatically. A companion model table alias is created so the calendar participates in joins and time-variant measures.

### Auto-create requires write access

The Generate button only runs DDL when the project connection has "Allow Tessallite to run DDL on this source" enabled. This is a deliberate opt-in so Tessallite can never write to a source the operator hasn't explicitly approved. If the flag is off, Generate returns the DDL in the error payload and asks you to use the script + bind path.

To enable the flag: open **Connections**, edit the connection, and check the "Allow Tessallite to run DDL on this source" checkbox under the connection settings. For BigQuery, the service account also needs the BigQuery Data Editor role (or higher) on the target dataset.

---

## Steps — bind an existing table

1. Open **Sources → Calendar** as above.
2. Click **Bind existing**.
3. Enter the **table name** (schema-qualified if required by your source).
4. Confirm the column mapping. Tessallite probes the table for the standard column names; if any are missing or use different names, edit the mapping inline.
5. Click **Bind**. The catalog refreshes; period-aware variants on measures linked to this source's hierarchy now become admissible.

---

## Unbinding

Click **Unbind** in the Calendar panel. This removes the registration only — the physical table on the source is never dropped, even if Tessallite created it.

---

## Script-only mode

If your security policy disallows write access from Tessallite, switch to the **Get script** tab, click **Show DDL**, copy the output, and run it on your source. Then come back, switch to **Bind existing**, and bind the table. Same outcome as auto-create, with you in control of the write.

---

## Using the calendar in the model

Period-aware time variants no longer require a calendar table. Setting a **calendar type** on the time hierarchy is sufficient — Tessallite derives period boundaries from SQL expressions. See [Configure Time Variants](configure-time-variants.md) for the full setup.

When a calendar table IS bound, Tessallite uses its pre-computed columns instead of expression-based boundaries for backward compatibility. This is automatic — the query router detects the calendar table and switches to the column-based path.

A calendar table adds value in these scenarios:

1. **Retail 4-4-5 periods** — the irregular 4-week/5-week pattern cannot be expressed as date arithmetic. You must bind a 4-4-5 calendar table for retail period variants to work.
2. **Dense date spine** — queries that need every date in a range (including dates with no fact rows) use the calendar table as a dense join source.
3. **Custom period columns** — if your organisation uses non-standard period definitions (e.g. 13-period years, company-specific fiscal quarters), store them as columns in a calendar table.

---

## Check calendar coverage

Use **Check calendar coverage** to confirm the calendar actually spans your fact data *before* you rely on period rollups. The check compares the calendar's date range against the range of dates present in the fact table.

This matters because of a quiet failure mode: if some fact dates fall **outside** the calendar — for example the fact table has 2026 rows but the calendar only goes to 2025 — those rows get NULL period values and **drop silently out of period rollups**. Your year-to-date and monthly totals would simply under-count, with no error to warn you. The coverage check turns that silent gap into a clear message: it reports the exact fact range and calendar range so you can see the shortfall and extend the calendar to cover it. A green result means every fact date is covered.

Run this whenever you load new fact data, change the calendar's range, or notice a period total looking lower than you expected.

---

## Troubleshooting

- **"The connection does not have 'Allow Tessallite to run DDL on this source' enabled"** — the write access checkbox is not checked on the connection. Edit the connection and enable it, or use the Get script + Bind existing path instead.
- **DDL syntax error with hyphens** — if you see an error mentioning `Expected end of input but got "-"`, the table name contains an unquoted identifier with hyphens. This is fixed in current builds; Tessallite automatically backtick-quotes BigQuery identifiers. If you still see it, check that the table name field contains only the bare table name (e.g. `calendar`), not the full path.
- **Calendar DDL execution failed** — the DDL ran but the source rejected it. Common causes: insufficient permissions, schema or dataset doesn't exist, table name collision. The error message includes the underlying SQL exception from the source.
- **Generate succeeded but a time variant still doesn't show** — check the variant uses a period column you actually have. `_yoy_growth` needs `year_no`; `_qtd` needs `quarter_no`; missing optional columns silently drop the variant from the catalog.
- **Calendar already exists with a different shape** — bind the existing table instead, then edit the column mapping inline (see Bind existing).

---

## Best practices

- **Naming convention:** Use `calendar_<type>` naming — `calendar_standard`, `calendar_fiscal`, `calendar_445`. Consistent names make it obvious which table serves which purpose.
- **Date range:** Cover at least 5 years past and 3 years future. If your fact data extends beyond the calendar range, queries for those dates return NULL period values.
- **One calendar per type per source:** Don't create two standard calendars on the same source. If you need the same calendar type for two different date columns, use dimension aliases instead (see [Associate Calendar with Dimensions](associate-calendar-with-dimensions.md)).
- **Don't over-provision:** Only create calendar types you actually use. Each adds a physical table and increases aggregate combinatorics.

---

## Pitfalls

- **Calendar doesn't cover the fact data range.** If your sales data goes back to 2018 but the calendar starts at 2021, any time variant for 2018-2020 returns NULLs. Check the date range before creating.
- **Duplicate calendar tables.** Creating two standard calendars on the same source causes ambiguity when associating dimensions. Delete or unbind the duplicate.
- **Forgetting to register after manual DDL.** If you used Get script + ran the DDL yourself, you still need to bind the table in Tessallite. The physical table exists on the source but Tessallite doesn't know about it until you bind.

---

## Related

- [Calendar Types](../concepts/calendar-types.md)
- [Associate Calendar with Dimensions](associate-calendar-with-dimensions.md)
- [Multi-Calendar Best Practices](multi-calendar-best-practices.md)
- [Configure Time Variants](configure-time-variants.md)
- [Add a data source](add-a-data-source.md)

---

← [Understanding Window Functions](window-functions.md) | [Home](../index.md) | [Associate Calendar with Dimensions →](associate-calendar-with-dimensions.md)
