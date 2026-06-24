---
title: "Cold-start Latency"
audience: modeller
area: modelling
updated: 2026-04-25
---

## What this covers

Every deploy resets the warm cache. The **Cold-start Latency** dashboard, on the **Model Health** tab, lets the modeller see exactly what happens to query latency in the seconds and minutes after a deploy — whether predictive aggregates absorbed the first BI users, or whether queries fell through to the source.

---

## Where it lives

Open Model Builder → **Model Health** tab → **Cold-start latency** section. It sits between *Model information* and *Model alerts*.

The section is empty until the model has been deployed at least once.

---

## What you see

### Stat cards

| Card | Meaning |
|---|---|
| **Deployed** | Timestamp of the most recent deploy. |
| **Samples** | The number of queries observed since that deploy, capped by the configured `sample_size` (default 25). |
| **Median (post-deploy)** | Median execution time across the sampled queries. |
| **P95 (post-deploy)** | 95th-percentile execution time across the sampled queries. |
| **Baseline median (Nd before)** | Median execution time across the N days of queries *before* the deploy (default 14). The bar to compare against. |

### The chart

An inline SVG bar chart, one bar per sample in deploy order:

- **Green bar** — query was served by an aggregate (predictive or otherwise). The `aggregate_id` from the query log is non-null.
- **Blue bar** — query fell through to the source.
- **Dashed red horizontal line** — `baseline_median_ms`. Bars below the line ran faster than the pre-deploy median; bars above ran slower.

Hover any bar for a tooltip with the sample sequence, execution time, and aggregate flag.

---

## How to read it

A healthy post-deploy chart looks like:

- Most bars green (aggregates picked up the load).
- Bar heights at or below the dashed line (no regression versus pre-deploy).
- Clear convergence — even the first bars are not orders of magnitude taller than later ones.

A predictive-aggregate misfire looks like:

- Many blue bars (queries falling through to source).
- Bar heights well above the dashed line.
- Missing patterns visible in the **Aggregate Lifecycle** panel (no `built` events for the predicted candidates, or `failed` events instead).

A capacity issue looks like:

- Mostly green bars but all of them taller than the dashed line — aggregates are matching but the source warehouse itself is slower than it was before the deploy.

---

## API

The section reads from `GET /api/v1/models/{model_id}/cold-start`:

| Query param | Default | Meaning |
|---|---|---|
| `sample_size` | `25` | Cap on the number of post-deploy queries to render. |
| `baseline_days` | `14` | Window of pre-deploy queries used to compute the baseline median. |

The endpoint reads `Model.last_deployed_at` and joins against `query_logs.created_at`. Pre-deploy percentile is computed inline (median + p95) on at most 2,000 rows from the baseline window.

---

## When the section is empty

- *"Model has not been deployed yet"* — `Model.last_deployed_at` is null. Deploy the model and revisit.
- *"No queries observed since the last deploy"* — the model is deployed but nobody has issued a query yet.

---

## Related

- [Predictive Aggregates](predictive-aggregates.md)
- [Aggregate Lifecycle](aggregate-lifecycle.md)
- [Model Health (concept)](../concepts/model-health.md)
- [View Diagnostics](view-diagnostics.md)

---

← [Aggregate Lifecycle](aggregate-lifecycle.md) | [Home](../index.md) | [Configure Pocket Tables →](configure-pocket-tables.md)
