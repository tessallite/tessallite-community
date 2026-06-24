---
title: "Table Auto-Analysis"
audience: modeller
area: modelling
updated: 2026-05-02
---

## What this covers

Table auto-analysis runs a heuristic engine against a source table's column names and data types to suggest a table type (fact or dimension) and a role for each column (measure, dimension, date_key, or ignore). This page explains how the heuristics work, what confidence levels mean, and how to use the suggestions.

## When to use auto-analysis

Auto-analysis is useful when:

- You have added a table from a schema you are not deeply familiar with and want a first-pass classification.
- You are onboarding a model and want to confirm that your fact/dimension designations match expected naming conventions.
- You want a quick audit of column roles before defining measures and dimensions manually.

Auto-analysis does not modify the model. It returns suggestions only. You still define each dimension and measure manually, using the suggestions as a guide.

## How it works

The heuristic engine examines each column's name and data type:

**Table type classification.** A table is suggested as `fact` if it has numeric measure-like columns (names matching patterns like `amount`, `quantity`, `count`, `revenue`, `total`, `price`, `value`) or an auto-increment integer primary key alongside non-PK numeric columns. It is suggested as `dimension` if the majority of columns are string or small-integer types with code-like names.

**Column role classification.**

| Suggested role | Signal |
|---|---|
| `measure` | Numeric column with an aggregation-friendly name (e.g. `amount`, `qty`, `revenue`, `cost`, `count`) |
| `dimension` | String column, or integer column with categorical naming (e.g. `type`, `status`, `code`, `category`, `group`) |
| `date_key` | Column name contains `date`, `day`, `ts`, `timestamp`, `_at`, `_on` and the data type is date, timestamp, or integer |
| `ignore` | Auto-increment primary keys, internal row-hash columns, ETL watermark columns |

**Confidence levels.**

| Level | Meaning |
|---|---|
| `high` | Multiple strong signals align. Classification is almost always correct for tables conforming to standard naming conventions. |
| `medium` | Some signals present, some absent. Review before accepting. |
| `low` | Naming is ambiguous or atypical. Manual inspection is required. |

## Running analysis

1. Open the model canvas.
2. Hover over a table node; click the auto-analysis icon (wand icon) in the table header toolbar.
3. The Auto-Analysis dialog opens.
4. Click **Run Analysis**. The engine runs synchronously; typical analysis takes under a second.
5. Review the suggested table type, confidence level, and reasoning text.
6. Review the column suggestions table.
7. Close the dialog and manually create dimensions and measures using the suggestions as a guide.

To refresh the analysis after adding columns or changing the connection, click **Re-run** in the dialog.

## Interpreting results

The **reasoning** text explains which patterns drove the table type classification. For example: _"Table has 4 columns matching measure name patterns (amount, qty, price, total) and an integer primary key. Classified as fact."_

The **potential calendar column** field, when present, identifies a date/timestamp column that could be used as the calendar anchor for time-variant measures. This is a suggestion only; it does not bind the calendar automatically.

## Limits

- Auto-analysis is a naming convention heuristic. It does not query the data. A column named `status_code` holding monetary values will be misclassified as a dimension.
- Non-standard naming conventions (e.g. German or abbreviated column names) will produce low-confidence or incorrect suggestions.
- The analysis runs on the current column list of the table in the model. If the source schema has changed since the table was added, sync columns first.
- Auto-analysis requires at least one column on the table. An empty table returns an empty suggestion list.

---

## Related

- [Add tables to a model](add-tables-to-a-model.md)
- [Define dimensions](define-dimensions.md)
- [Define measures](define-measures.md)

---

← [Source Statistics](source-statistics.md) | [Home](../index.md) | [Model Canvas Tour →](model-canvas-tour.md)
