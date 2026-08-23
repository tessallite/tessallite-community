---
title: "Configure Aggregates"
audience: modeller
area: modelling
updated: 2026-08-02
---

![Model Builder — Aggregates panel with new aggregate form.](../assets/screencaps/configure-aggregate-form.png)

## What this covers

An aggregate is a pre-computed summary table stored in the query target. When a BI query matches an available aggregate, Tessallite reads from the summary instead of scanning the full fact table. This article covers the two ways aggregates are created, the manual configuration workflow, aggregate properties, and how the grain controls which queries an aggregate can serve.

---

## Two ways aggregates are created

- **Manual configuration** — You define the aggregate explicitly: you choose the name, grain, measures, and refresh schedule. Manual aggregates persist until you delete them.
- **AI Optimizer auto-creation** — The Optimizer analyses query patterns observed by the Gateway and creates aggregates automatically at grains it calculates will reduce the most query cost. Auto-created aggregates can be retired by the Optimizer if query patterns change. They are visible in the Canvas alongside manual aggregates.

Both types coexist in the same model. Manual aggregates give you explicit control over high-priority query patterns; auto-created aggregates fill in the gaps based on actual usage.

---

## Aggregate properties

| Property | Description |
|---|---|
| Name | Internal identifier for the aggregate. Used as the summary table name in the query target schema. |
| Grain (dimensions) | The set of dimensions that define the level of detail in the summary. Every distinct combination of dimension values becomes one row in the summary table. |
| Measures | The measures to include in the summary. Only measures defined in the model can be selected. |
| Refresh schedule | A cron expression or preset that controls when the Scheduler re-queries the source and overwrites the summary. |

---

## How the grain controls query matching

The Query Router matches an incoming query to an aggregate when the query's requested dimensions are a subset of the aggregate's grain and the requested measures are all present in the aggregate. A query asking for revenue by country and month matches an aggregate whose grain includes country and month — even if the aggregate also includes a region dimension that the query does not use. The router selects the aggregate with the smallest grain that still covers the query.

For additive measures (SUM, COUNT, AVG, MAX, MIN), the router can re-aggregate from coarser summaries. For COUNT DISTINCT, the grain must match exactly.

---

## Cron schedule presets

| Preset | Cron expression | When it runs |
|---|---|---|
| Every hour | `0 * * * *` | At the start of every hour |
| Every 6 hours | `0 */6 * * *` | At 00:00, 06:00, 12:00, 18:00 UTC |
| Daily at 02:00 | `0 2 * * *` | Once per day at 02:00 UTC |
| Weekly (Sunday 03:00) | `0 3 * * 0` | Every Sunday at 03:00 UTC |

You can enter any valid cron expression in the schedule field if none of the presets match your requirements. All times are interpreted as UTC.

---

## The Aggregate Drawer

Manual aggregate work — both creation and editing — happens in the **Aggregate Drawer**: a right-anchored 720 px panel with three tabs.

| Tab | Purpose |
|---|---|
| Definition | Target, grain (dimensions), measures, include-quantiles toggle, status. |
| Schedule | Cron picker plus the **Schedule enabled** switch and the recent run history. |
| Advanced | Read-only metadata — physical table name, status, created timestamp, retired timestamp, estimated hit rate, creation reason, AI rationale, invalid reason. |

In edit mode the drawer header carries a **Refresh now** button that triggers an immediate full rebuild (the Scheduler endpoint runs the same job a cron tick would have run). Closing the drawer with unsaved changes prompts for confirmation.

---

## Authoring workflow (create)

1. Open the model in Model Builder. Confirm that a query target is set under **Settings** before proceeding — aggregates cannot be built without a target.
2. Click **Aggregates** in the Toolbelt.
3. Click **New**. The Aggregate Drawer opens on the **Definition** tab.
4. Pick a **Target** (where the summary table is stored).
5. Select the **Grain (dimensions)**. Every distinct combination of dimension values becomes one row in the summary table. Dimensions whose value is functionally equivalent to another dimension's value (a redundant partner) are disabled with a tooltip explaining why.
6. Select the **Measures** to include. The default aggregation function (`SUM`, `AVG`, etc.) shown next to each measure name is what will be computed at create time. Non-additive measures are flagged with a chip.
7. Optionally enable the advanced statistical columns:
   - **Include median column (p50)** — lets the router serve `MEDIAN` (the 50th percentile) queries from the aggregate. Other percentiles (p90, p95, p99, and so on) are temporarily not materialised while percentile query routing is being completed; those queries return the correct answer from the source table in the meantime.
   - **Include dispersion-stat columns (STDDEV_POP, STDDEV_SAMP, VAR_POP, VAR_SAMP)** — lets the router serve standard-deviation and variance queries from the aggregate.
   Both are opt-in because they add extra columns per numeric measure, and both can only be served at the aggregate's exact grain (see *How aggregates store data*).
8. The **Aggregate estimate** card appears live as you change selections, showing the ROI score and any non-additive warnings.
9. Switch to the **Schedule** tab. Turn the **Schedule enabled** switch on, pick a cron expression with the picker, then click **Save**.
10. The aggregate is queued for an initial build by the Scheduler. The card shows status **creating** and transitions to **active** once the first refresh completes.

> The first build runs as soon as the Scheduler picks up the job, typically within one minute. If the status stays in **creating** for more than five minutes, check the Diagnostics panel for Scheduler errors.

---

## Edit mode

Editing scope is intentionally narrow:

- **Editable** — schedule (cron + enabled), status (active / retired), include-quantiles and include-stats toggles, refresh-now trigger.
- **Read-only** — target, grain, measures. The drawer renders these as static chips with the note "*To change the shape, delete this aggregate and create a new one.*" Aggregate shape changes go through delete-and-recreate so the physical table identity matches the definition.

Administrative API clients can change `target_schema`. Moving an aggregate to another target schema marks it stale and clears its last-refresh timestamp. The query router withholds it until a full rebuild succeeds at the new location; submitting the current target schema again does not invalidate the build.

The **Definition** tab shows the AI rationale card when the aggregate was created by the AI optimiser. The **Advanced** tab exposes the rest of the metadata.

The footer carries a **Delete** button (left) that asks for confirmation before retiring the aggregate and dropping its physical table.

---

## How aggregates store data

Each measure included in an aggregate generates multiple physical columns in the summary table. This allows the Query Router to serve a wider range of SQL functions from the same aggregate without re-scanning the source.

| Measure type | Physical columns created |
|---|---|
| SUM measure | `sum`, `count`, `min`, `max` |
| AVG measure | `avg`, `sum`, `count`, `min`, `max` |
| COUNT measure | `count`, `min`, `max` |
| MIN measure | `min`, `count`, `max` |
| MAX measure | `max`, `count`, `min` |
| Any numeric measure, **Include quantiles** on | adds `p50` (the median). Other percentiles are temporarily not materialised while percentile routing is completed. |
| Any numeric measure, **Include stats** on | adds `stddev_pop`, `stddev_samp`, `var_pop`, `var_samp` |

**AVG at query time.** AVG is computed at query time from the stored SUM and COUNT columns (`SUM / COUNT`). It is not stored as a separate physical value. This avoids the mathematical error of averaging averages.

**COUNT(\*).** Every aggregate automatically includes a row-count column so that `COUNT(*)` queries can be served directly.

**Median, percentile, and dispersion stats.** `MEDIAN`/`PERCENTILE_CONT`/`PERCENTILE_DISC`, `STDDEV_*`, and `VAR_*` are *not re-aggregatable* — the median of two groups is not the median of their union. The median (p50) and the dispersion stats can be served from an aggregate, but only when:

- the aggregate was built with the **Include quantiles** and/or **Include stats** option, and
- the query's grain matches the aggregate's grain **exactly** (no coarser roll-up).

Non-median percentiles (p90, p95, p99, and the rest) are **temporarily served from the source** even when the aggregate has the quantiles option on: the routing that maps an arbitrary `PERCENTILE_CONT`/`PERCENTILE_DISC` fraction to a stored column is still being completed, so materialising those columns would only add storage and refresh cost without speeding any query up. They are therefore not built for new aggregates yet. The answers stay correct — they just come from the source table. At any coarser grain, or when the columns were not materialised, the Query Router sends the query to the source table. Two notes on exactness by source engine for the median it does materialise:

- **PostgreSQL** quantiles are exact.
- **Spark** quantiles are exact for a same-engine refresh (Spark source and Spark target). A cross-engine refresh (Spark source into a non-Spark target) cannot guarantee exact quantiles, so they are not materialised and such queries go to the source.
- **BigQuery** only offers approximate quantiles, so percentile queries on a BigQuery-sourced model always go to the source for an exact answer. Standard-deviation and variance are exact on all three engines.

**Functions that always hit the source.** Order-dependent or distribution-shaped functions — `MODE`, `STRING_AGG`/`ARRAY_AGG`/`LISTAGG`, and `APPROX_COUNT_DISTINCT` — cannot be served from pre-computed columns and always route to the source table.

**Include all measures.** This model setting is **off by default**, and new models start with it off. With it off, each summary table holds only the measures the queries that triggered it actually asked for. That is usually what you want: a narrow summary table is faster to build, cheaper to store, and — importantly — much more likely to be usable.

Turn it on when you want every summary table on a model to carry every measure, so that a brand-new question about an existing grouping is answered instantly instead of waiting for the Optimizer to notice it. The trade is size and build time, and one subtler cost worth understanding.

**Why "all measures" is not always better.** A summary table is only allowed to answer a question when it was built over exactly the same set of rows the question would have scanned. Adding a measure that lives on a different table pulls that table into the summary's build, and if joining it drops or duplicates rows, the summary now covers a *different* population — so Tessallite refuses to answer from it and quietly goes back to the source table. Switch the setting on for a model with measures spread across several tables and you can end up with many summary tables that are never used. Tessallite guards against this automatically: when the setting is on, it only adds the measures that fit the same population as the query that triggered the build. But the narrower default avoids the problem entirely.

**Existing summary tables when you change the setting.** Nothing is destroyed the moment you flip the switch.

- **Turning it on** — the next Optimizer sweep rebuilds each live summary table with the wider set of measures.
- **Turning it off** — new summary tables are built narrow from then on. Summary tables you already have keep every column they were built with, so no report that relies on them stops working. Where an old wide table cannot answer a question, the Optimizer builds a narrow one **next to it** rather than replacing it — the wide table may still be the right answer for a report that asks for all of those measures at once. Tessallite does not delete the old table for you: it stays until the model's summary-table limit pushes it out, or until you retire it yourself from the Aggregates panel.

  **Tip.** If you turned the setting off because a model had many summary tables that were never being used, check the Aggregates panel a few days later. The new narrow tables will show hits; the old wide ones that show none are safe to retire by hand.

If a new measure is added to the model while the setting is on, the lifecycle sweep detects the gap, retires the stale summary table, and rebuilds it — keeping the columns it already had and adding the missing ones.

---

## Enriched aggregates for relabelled queries

When a dimension has a declared, proven **attribute relationship** (for example `country_code` to `country_name`), Tessallite can build an *enriched* aggregate that stores the detail column alongside the key. A query grouped by the detail column is then served from the aggregate built on the key, with identical results — see [Dimension Attribute Relationships](dimension-attribute-relationships.md).

Whether the optimiser builds these enriched aggregates is controlled by a per-model setting, **Derived-expression auto-build** (off, approval, or automatic), on the AI Optimizer tab of [Model Configuration](../admin/model-configuration.md). Leave it off and ordinary aggregates are unaffected; set it to approval or automatic to let the enriched columns be added. A separate system-wide switch controls whether live queries are actually relabelled from them, so nothing changes about routing until both are enabled.

---

## Manual versus auto-created aggregates

Manual aggregates are displayed in the Canvas with a solid border on their dashed outline. Auto-created aggregates show an Optimizer badge. Both respond to the same status indicators (Ready, Stale, Refreshing, Error). Manual aggregates are permanent — the Optimizer will not retire them. Auto-created aggregates may be removed by the Optimizer if the query patterns they serve drop off; you receive a notification in the Health tab when this occurs.

---

## Deleting an aggregate

Deleting an aggregate removes both halves: the definition you see in Tessallite **and** the summary table it built on your target database. Tessallite records what needs removing before it deletes the definition, deletes the definition, and only then drops the table — in that order, so there is never a moment where Tessallite still thinks it can answer from a table that has already gone.

If the drop itself does not succeed (the target database is briefly unreachable, for example), the delete still goes through and the table is dropped on a later retry — the record of what to remove is kept.

One case is refused rather than half-done: if Tessallite cannot work out where the summary table actually lives — its target connection has been deleted, or now belongs to a different project — you get an error asking you to repair the connection first, and the aggregate is left alone. Deleting it at that point would leave a table on your database with nothing left in Tessallite pointing at it.

---

## Related

- [Set a Query Target](set-a-query-target.md)
- [Run a Refresh](run-a-refresh.md)
- [Aggregates (concept)](../concepts/aggregates.md)
- [Use the AI Optimiser](use-the-ai-optimiser.md)
- [Manage Aggregate Schedules](manage-aggregate-schedules.md)

---

← [Set a Query Target](set-a-query-target.md) | [Home](../index.md) | [Predictive Aggregates →](predictive-aggregates.md)
