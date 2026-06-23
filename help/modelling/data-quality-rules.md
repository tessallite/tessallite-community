---
title: "Data Quality Rules"
audience: modeller
area: modelling
updated: 2026-05-02
---

## What this covers

Data quality rules let you declare constraints on physical source columns and automatically detect violations after each aggregate refresh. This page explains the five rule types, when to use them, how violations are recorded, and the `block_on_failure` option.

## Why data quality rules matter

Aggregates are only useful if the underlying source data is clean. A fact table with NULL transaction amounts produces misleading subtotals. Duplicate keys on a dimension corrupt join cardinalities. A date column accepting free text silently breaks time-variant measure calculations. By the time a data analyst notices that "March revenue" doubled overnight, the corrupt aggregate has already been served to a dashboard.

Data quality rules close this loop. They run automatically after each refresh and write violation counts back to the aggregate metadata. When violations breach a threshold, a model alert is raised. If `block_on_failure` is enabled, the alert is escalated to error severity and clearly signals that downstream data should not be trusted.

## Rule types

| Rule type | What it checks | Configured by |
|---|---|---|
| `not_null` | Count of NULL values in the column | No extra config needed |
| `unique` | Count of duplicate values in the column | No extra config needed |
| `range` | Rows where the value falls outside `[min, max]` | `rule_config: { "min": 0, "max": 100 }` |
| `regex` | Rows where the column does not match a regular expression | `rule_config: { "pattern": "^\\d{4}-\\d{2}-\\d{2}$" }` |
| `custom_sql` | A hand-written SQL query that must return the violation count as its first column | `rule_config: { "sql": "SELECT COUNT(*) FROM my_table WHERE status NOT IN ('active','closed')" }` |

### `custom_sql` considerations

- The SQL runs against the source database, not against the aggregate.
- The query must return a single value in the first column of the first row.
- Use parameterless SQL only. Session state, temp tables, and DDL are not supported.
- Return `0` for "no violations", any positive integer for "N violations found".

## Targets

A rule targets a specific object within the model:

| `target_type` | Meaning |
|---|---|
| `column` | A specific `ModelColumn` row; the rule's `target_id` is the column UUID |
| `dimension` | The source column of a dimension; `target_id` is the dimension UUID |
| `measure` | The source column of a standard measure; `target_id` is the measure UUID |

## Severity levels

| Severity | Effect on model alerts |
|---|---|
| `info` | No model alert raised |
| `warn` | Warning alert raised |
| `error` | Error alert raised |

When `block_on_failure` is enabled, any violation produces an error-severity alert regardless of the rule's `severity` setting.

## block_on_failure

When `block_on_failure` is set to `true` on a rule:

- Violations of any count produce an error-severity model alert.
- The alert detail includes the message "Queries blocked until resolved."
- The aggregate's violations chip in the Aggregates panel shows an error badge.

Use `block_on_failure` for rules where a single violation means the data is definitively wrong and the aggregate should not be served. Examples: a primary key column on the fact table that must be unique, a date column that must never be NULL.

## Automatic validation

Rules run automatically after each full aggregate refresh. You can also trigger validation manually using the **Run Checks** button in the Data Quality panel.

After validation completes:
- `last_violation_count` on each rule is updated.
- New `DataQualityViolation` rows are written with the violation count, a sample of failing values (where available), and the aggregate ID for which violations were found.
- Model alerts are raised for `warn`/`error`/`block_on_failure` rules that have violations.

## Violation history

Each rule keeps a violation history. Click the expand button on a rule row to see past violation events with timestamps and sample values. To clear stale violation history, use the **Clear** button on the rule row.

## Common patterns

**Fact table NOT NULL guard.** Create a `not_null` rule targeting the fact table's primary key column with `severity=error` and `block_on_failure=true`. Any NULL in that column means the CTAS joined incorrectly and the aggregate should not be trusted.

**Dimension code uniqueness.** Create a `unique` rule targeting the natural key of a dimension table. A duplicate key means two rows will join to the same fact record, inflating measures.

**Date format guard.** For a string-typed date column, create a `regex` rule with a date pattern. Out-of-format values will cause time-variant measure calculations to fail silently.

**Business range check.** For a `unit_price` column, create a `range` rule with `min=0` and `max=99999`. Negative or absurdly large prices indicate upstream ETL issues.

## Limits

- Rules run synchronously after each refresh. Many rules on a large fact table will extend refresh time.
- `custom_sql` rules run against the source database using the model's source connection. They cannot reference the aggregate schema.
- There is no version history for rule definitions. Deleting a rule also deletes its violation history.

---

## Related

- [Configure aggregates](configure-aggregates.md)
- [Run a refresh](run-a-refresh.md)
- [View diagnostics](view-diagnostics.md)

---

← [Parameterized Filters](parameterized-filters.md) | [Home](../index.md) | [Impact Analysis →](impact-analysis.md)
