---
title: "Source Statistics"
audience: modeller
area: modelling
updated: 2026-05-15
---

## What this covers

The **Source Statistics** panel shows table and column profiling data collected from model sources. Tessallite uses these statistics to explain data shape, identify low-cardinality dimensions, and feed optimiser decisions for aggregates and query routing.

This page is for the Source Statistics drawer, not predictive aggregates. Predictive aggregates are recommendations; source statistics are the measured facts those recommendations can use.

---

## What the panel shows

| Area | Meaning |
|---|---|
| Source selector | Chooses the model source whose table statistics are displayed. |
| Recompute | Runs the profiler again for the selected source. |
| Table rows and bytes | Approximate size signals used for modelling and optimisation decisions. |
| Column null rate | Share of profiled rows where the column is null. |
| Distinct values | Cardinality estimate for filtering, grouping, and aggregate grain choices. |
| Refresh cadence | How often Tessallite should consider statistics stale for that table. |

---

## Recompute options

When you click **Recompute**, choose a sample size based on the problem you are solving:

- Use the default sample for ordinary modelling guidance.
- Use a custom sample when the table is large and the default is too small to represent skewed data.
- Use a full scan only when accuracy matters more than profiling cost.

The low-cardinality threshold comes from model settings. Lower values make Tessallite more conservative about suggesting grouping columns.

---

## How to read the output

High distinct counts are useful for identifiers but usually poor aggregate grains. Low distinct counts are often good dimension candidates, especially for status, channel, geography, product family, and calendar attributes.

A high null percentage does not automatically make a column unusable. It means analysts need to understand whether null is meaningful, missing, or caused by incomplete source modelling.

---

## Related

- [Table Auto-Analysis](table-auto-analysis.md)
- [Use the AI Optimiser](use-the-ai-optimiser.md)
- [Configure Aggregates](configure-aggregates.md)

---

← [Add Tables to a Model](add-tables-to-a-model.md) | [Home](../index.md) | [Table Auto-Analysis →](table-auto-analysis.md)
