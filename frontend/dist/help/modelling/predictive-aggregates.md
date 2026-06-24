---
title: "Predictive Aggregates"
audience: modeller
area: modelling
updated: 2026-06-21
---

## What this covers

Predictive aggregates are aggregates Tessallite builds *before* any query is observed, using **source statistics** collected from the connected database — row counts, distinct counts, null ratios, top-N values, and join selectivity. This article explains the idea behind the predictor, the four moving parts the modeller controls (storage budget, eviction policy, approval gate, feedback loop), what each control actually does, and how predictive aggregates coexist with manual and demand-defined ones.

---

## The cold-start problem this solves

A freshly deployed model has no observed workload — no query log, no miss patterns, nothing for the demand-driven optimiser to learn from. The very first BI user therefore pays the full cold-path cost: a scan against the source, with no aggregate to short-circuit it. The demand optimiser only helps *after* a pattern of repeated queries has been seen, which is exactly what a brand-new model does not have yet.

Predictive aggregates close that gap. Instead of waiting for queries, Tessallite samples the source, scores plausible aggregates by expected speed-up per unit of storage, and builds the most promising ones within the model's budget — so the first user already lands on a fast path.

**Source coverage.** Statistics collection ships for **PostgreSQL, BigQuery, and Spark/Hive** sources. Each uses its own catalogue probes (PostgreSQL `pg_stats`, BigQuery `INFORMATION_SCHEMA` + `APPROX_*`, Spark `ANALYZE TABLE`). Snowflake and SQL Server are not yet fully supported for statistics — their probes return sparse results, so the predictor has little to work with on those sources today.

---

## When to use predictive aggregates

Reach for predictive aggregates when:

- you are launching a **new model** and want it fast on day one, before any usage history exists;
- the model has a clear star shape (a fact table with low-cardinality dimensions) where coarse-grain roll-ups give large row reductions;
- you can afford a modest, bounded amount of target storage for speculative aggregates that the feedback loop will later confirm or retire.

They are less useful when the model is already mature (the demand optimiser has real miss data to act on) or when every dimension is near-unique (no grain gives a meaningful row reduction, so the scorer rejects them anyway).

---

## The four moving parts

### 1. Source statistics

Statistics are collected from each connected source and stored as:

- `source_statistics` — per-table row count, table size, and last-refresh timestamps
- `source_column_statistics` — per-column distinct count, null ratio, top-N values
- `source_join_statistics` — per-declared-join selectivity (rows out / rows in)

Probes lean on the database's own catalogue and approximate functions to stay cheap — `pg_stats` and small `TABLESAMPLE` reads on PostgreSQL, `INFORMATION_SCHEMA` and `APPROX_*` functions on BigQuery, `ANALYZE TABLE` summaries on Spark.

You refresh statistics from the **Connections** page (each source carries a freshness chip and a **Recompute** button) or by setting a per-source cadence (`daily` / `weekly` / `monthly`). The **Statistics inspector** — opened from the **Sources** panel in the Model Builder — shows the raw numbers, which are useful even outside the predictor for choosing dimensions and sizing aggregates.

> **Important — statistics are not collected automatically on deploy.** Deploying a model does *not* trigger a statistics refresh. A brand-new model has no statistics until you run **Recompute** (or a cadence fires). Until statistics exist, the predictor has nothing to score and builds nothing. The recommended sequence for a new model is: deploy → open **Connections** → **Recompute** each source → let the hourly build sweep pick it up (see part 3).

If you open the **Predictive** tab on a model that has no source statistics yet, the panel now shows an explicit warning — *"No source statistics have been collected for this model yet. Refresh source statistics before deploying or building predictive aggregates."* — instead of an empty "no candidates" message. This distinguishes the two states that previously looked identical: "statistics exist, but nothing is worth building" versus "the predictor simply has nothing to work with yet". If you see the warning, recompute statistics first.

### 2. The scorer

The scorer (`predictive_scorer.py`) is a pure function. Given the statistics plus your model metadata (dimensions and measures), it ranks plausible aggregates — single dimensions, pairs, and triples — by a benefit-over-cost formula:

```
expected_hit_rate × row_reduction
─────────────────────────────────
   build_cost × storage_cost
```

`row_reduction` is the ratio of source rows to the aggregate's estimated rows — a *multiplier* (for example, ten million rows collapsing to two hundred is a 50,000× reduction). The preview shows it with a `×` suffix, not as a percentage. Candidates whose grain would exceed 50% of the source cardinality are rejected outright (there is no worthwhile reduction there). Each candidate carries a rationale string that explains why it ranked where it did.

The displayed `score` is a small benefit-density number, useful for *ranking* candidates relative to one another rather than as an absolute percentage. The `expected_hit_rate` shown is a heuristic assumption (it rises with the number of measures sharing the grain), not a measured hit rate — treat it as a planning estimate, not evidence.

### 3. Auto-build sweep (hourly cron)

An **hourly** background sweep (`predictive_build_sweep`) walks every active tenant and looks for models where:

- the model has a deployed version,
- aggregations are enabled,
- approval is **not** required (auto-approve mode — the default),
- the deployed version has not already been processed by the predictor.

For each match it scores the candidates, applies the storage budget, and builds the survivors through the same CTAS path that manual and demand aggregates use. Builds are tagged `creation_reason='predictive'` so they stay visually distinct in every panel.

Because the sweep is a cron, there is a delay of up to roughly an hour between statistics becoming available and the first predictive build. If a model is deployed before any statistics exist, the sweep leaves it open for reconsideration and re-checks it on the next tick once statistics land — it is not permanently skipped.

### 4. The feedback loop

A daily sweep (`predictive_feedback_sweep`, at the hour configured by `scheduler.predictive_feedback_hour`, on the half-hour) closes the loop:

- **Validate.** A predictive aggregate that earns at least `predictive.validation_min_hits` successful query hits inside the last `predictive.evaluation_window_days` is stamped `predictive_validated_at = now` and emits a `validated` lifecycle event. The `creation_reason` is intentionally **not** changed — a validated predictive aggregate keeps its `predictive` label.
- **Retire.** A predictive aggregate older than `predictive.unused_retire_days` that has **never** earned a successful hit is flipped to `status='retired'`, its underlying physical table is dropped to reclaim target storage, and a `retired_unused` lifecycle event is emitted. One that has been hit at least once — even below the validation threshold — is left alone.

The validation timestamp is the only feedback signal the eviction policy reads (see below).

In the panels, a predictive aggregate that has passed validation shows a green **Validated** badge, with a tooltip giving the date the feedback sweep confirmed it (*"Validated by feedback sweep on …"*). The badge is a plain-language signal that real query traffic has hit this speculative aggregate, so it has earned its place — useful when you are deciding what to keep under storage pressure. Because the badge sits *alongside* the origin tag rather than replacing it, a validated aggregate still reads as "predictive": it tells you both where the aggregate came from and that it is now proven.

Each model carries a dual-axis budget, edited in the **Aggregates** panel under the **Predictive** tab:

| Setting | Default | Notes |
|---|---|---|
| `predictive_storage_budget_bytes` | unset (no byte cap) | Cap on bytes selected per build batch. |
| `predictive_storage_budget_count` | unset (no count cap) | Cap on the number of predictive aggregates selected per build batch. |

Either or both may be left unset. When **both** are unset, the build still stops at a small fixed number of aggregates per model as a safety belt, so the predictor can never run away.

> **What the byte budget bounds.** Before selecting candidates, the build sweep queries all **active** predictive aggregates for the model and computes their estimated byte footprint. That running total is subtracted from the budget before any new candidate is considered, so the budget bounds the model's **total live predictive footprint** — not just one build batch. If a model already sits at its byte cap, a re-run of the sweep selects nothing new. The estimate uses the same heuristic (rows multiplied by a fixed per-row byte factor multiplied by measure count) for both live aggregates and candidates, so the comparison is consistent even though it is approximate. When a predictive aggregate is **retired** by the feedback sweep, its underlying physical table is dropped and its byte footprint is reclaimed — the next build sweep sees the freed headroom and may fill it with fresh candidates. The count axis works the same way: live predictive aggregate count is subtracted before new candidates are admitted.

---

## Eviction policy

The model's `predictive_eviction_policy` governs cap enforcement: when the model is at or above its `max_aggregates` cap and a new aggregate needs room, the policy chooses what to retire. Cap enforcement sorts **all** active aggregates for the model — predictive, demand, and manual — and retires the lowest-ranked ones. Read each policy as a *kill-order rule*, not a protection guarantee:

| Policy | What actually happens |
|---|---|
| `predicted_first` | **All** predictive aggregates sort to the front of the kill order, validated or not — validation is ignored under this policy. Within that front group they are ranked by hit-rate, then recency, so the lowest-hit-rate predictive aggregate dies first. Demand and manual aggregates sit behind the whole predictive group. Use `validated_survives` instead if you want proven predictive aggregates to escape the front group. |
| `validated_survives` | Like `predicted_first`, but a *validated* predictive aggregate is pulled out of the predictive-first group and ranked alongside everything else by hit-rate and recency. Under sustained cap pressure a validated aggregate **can still be retired** if it has the lowest hit-rate of the survivors — the name means "validated survives *longer*", not "never". |
| `lru` | Retires by least-recent activity (last refresh, falling back to creation time) across **all** origins, regardless of `creation_reason`. A manual or demand aggregate can be evicted first under this policy if it is the oldest. This is recency-based, not query-hit-based. |
| `never_evict` | Disables cap enforcement: nothing is retired to make room, so a new aggregate simply lands over the cap. It does **not** refuse or fail new builds. |

> **Pitfall.** There is no policy that fully exempts manual or demand aggregates from predictive cap enforcement. Under `lru` in particular, a long-lived manual aggregate that has not refreshed recently can be the first thing retired. If you have a manual aggregate that must never be auto-retired, keep the model's `max_aggregates` cap above the number of aggregates you expect, or use `never_evict` to disable cap enforcement entirely.

---

## Approval gate

`predictive_requires_approval` defaults to **false** on a fresh model — that is, predictive aggregates **auto-build by default**. The hourly sweep will score candidates and build the budgeted survivors without any approval step. If you want nothing built without your sign-off, you must explicitly turn approval **on**.

When approval **is** required, the auto-build sweep skips the model and instead leaves its candidates in the **what-if preview**. Open the **Aggregates** panel, switch to the **Predictive** tab, and the preview lists each scored candidate with its rank, score, row-reduction multiplier, rationale, and estimated rows. From there you select candidates and click **Build selected** (or **Build all under budget**) to materialise them. A pending-approval indicator appears on the tab when approval is on and candidates are waiting.

> **Decision guide.** Leave approval **off** (the default) when you trust the predictor on this model's shape and want hands-off cold-start coverage. Turn it **on** when storage governance matters and you want to review every speculative build before it spends target storage.

---

## How predictive interacts with demand and manual

| Path | Trigger | Tag | Eviction |
|---|---|---|---|
| Manual | Modeller clicks **New** in the Aggregates panel | `creation_reason='manual'` | Subject to `lru` cap enforcement; otherwise not predictive-first |
| Demand (AI optimiser) | Repeat miss pattern | `creation_reason='demand'` | Subject to `lru` cap enforcement; retired when patterns shift |
| Predictive | Statistics-driven prediction, built by the hourly sweep | `creation_reason='predictive'` | Predictive-first under most policies; validation extends its life |
| Workload | Training-mode capture (not in service) | `creation_reason='workload'` | n/a |

The query router treats all four identically at match time — the tag is purely for governance, eviction, and reporting.

---

## Settings reference

| Key | Resolves at | Default | Restart |
|---|---|---|---|
| `scheduler.predictive_feedback_hour` | system | `6` | yes |
| `predictive.evaluation_window_days` | model → system | `7` | no |
| `predictive.validation_min_hits` | model → system | `3` | no |
| `predictive.unused_retire_days` | model → system | `14` | no |

The three `predictive.*` keys resolve with two levels: a value set on the model wins over the system value, which falls back to the default. These keys are **not** tenant-scoped — there is no tenant-wide predictive threshold; set the value on each model, or change the system default. A model that relaxes `unused_retire_days` for a slow-burn model keeps its predictive aggregates longer than the global clock would; a model that tightens it retires them sooner. The feedback sweep reads each model's effective value, so model overrides take effect.

---

## Related

- [Configure Aggregates](configure-aggregates.md)
- [Aggregate Lifecycle](aggregate-lifecycle.md)
- [Cold-start Latency](cold-start-latency.md)
- [Aggregates (concept)](../concepts/aggregates.md)
- [Use the AI Optimiser](use-the-ai-optimiser.md)

---

← [Configure Aggregates](configure-aggregates.md) | [Home](../index.md) | [Aggregate Lifecycle →](aggregate-lifecycle.md)
