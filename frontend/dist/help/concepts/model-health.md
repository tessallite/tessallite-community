---
title: "Model Health"
audience: modeller
area: concepts
updated: 2026-08-02
---

![Model Builder — Health tab.](../assets/screencaps/model-health-tab.png)

## What this covers

This article explains the Model Builder's Health tab: what it checks, the three severity levels, each specific issue and how to resolve it, and when health checks run.

---

## What model health means

The Health tab is a diagnostic panel inside the Model Builder. It runs a set of structural checks against the model definition and surfaces issues that would prevent correct query routing or aggregate building. Health checks are not runtime monitors — they inspect the model definition itself, not live query traffic.

An issue in the Health tab means something in the model configuration is inconsistent with the current state of the source schema, or the model is structurally incomplete. Until those issues are resolved, the affected parts of the model will not function correctly.

---

## Severity levels

| Severity | Effect | Action required |
|---|---|---|
| Error | Blocks aggregate builds or prevents correct routing for affected objects. | Required before aggregate builds will run or routing will be reliable. |
| Warning | Routing may be degraded — some queries may fall through to raw data more often than expected. | Recommended. |
| Info | Advisory only. No functional impact. | Optional. |

---

## Common issues and how to resolve them

### Join column not found

**Severity: Error**

A column referenced in a join definition no longer exists in the source schema. This happens when the source has been modified since the model was last synced.

To resolve: open the join in the Model Builder Drawer, update the left or right column to a column that currently exists in the source, and save. If the join is no longer valid, remove it entirely. Then run a schema sync to confirm the model is consistent with the current source.

### Measure column not numeric

**Severity: Error**

A measure is defined using an aggregation function (SUM, AVG, MIN, MAX) that requires a numeric column, but the selected source column has a text or boolean type.

To resolve: open the measure definition in the Drawer and either select a numeric source column or change the aggregation type to COUNT or COUNT DISTINCT.

### Fact table missing

**Severity: Error**

The model has multiple tables but none is designated as `fact`. A
single-table model is implicitly fact; only a multi-table model needs an
explicit fact anchor before deploy. Without that anchor, the Scheduler cannot
generate governed aggregate build SQL.

To resolve: open the table in the Canvas, select it, and use the Drawer to change its type to `fact`. If the model has multiple candidates, designate the primary transaction table and reclassify the others.

### Schema drift detected

**Severity: Error**

A source table's schema has changed since the model was last synced. Tessallite detected the drift during a scheduled schema sync.

To resolve: open the Model Builder and run a schema sync from the table's context menu. After syncing, review dimensions and joins that reference the changed table — any that reference a removed column will appear as additional errors.

Running a schema sync only refreshes the available column list. You must manually update any dimension, measure, or join definitions that reference columns that no longer exist.

### What Re-check covers

**Re-check model** reruns structural validation and combines it with unresolved hierarchy issues, failed pockets, and unacknowledged schema-drift events already recorded by the Scheduler. The panel displays these recorded-signal counts and the latest recorded drift time.

Re-check does not connect to the live source. A source change that occurred after the latest scheduled drift scan will not appear until that scan runs; the result explicitly says that live source introspection was not performed.

### Aggregate refresh overdue

**Severity: Warning**

An aggregate has not been refreshed within the expected window defined by its refresh schedule. Queries may be served from stale data or may fall through to raw data.

To resolve: open the Scheduler job log for the model and look for the failed or skipped refresh job. Fix the underlying cause and trigger a manual refresh from the Model Builder.

### Dimension with no queries

**Severity: Info**

A dimension exists in the model but has not appeared in any query within the usage window. The dimension is valid but may be unused. Consider whether it is needed, or whether users are accessing the data through a different name.

### Join population health

The **Join population health** section shows the deploy-time evidence for each
declared join and the model rollup: **OK**, **WARNING**, or **BLOCKED**. A
warning means a join can filter or multiply rows and needs modeller attention;
blocked means the measured effect is above the configured system threshold or
the join is not declared safely. A measured policy blocker prevents deployment
until the modeller declares the population role accurately or fixes the
join/source data. `preserve_base_rows` remains warning-only; unavailable,
failed, timed-out, budget-exhausted, and validation-off checks never block.
The per-model rollup may be unevaluated while a measured blocker still blocks,
because enforcement is per join rather than hidden by another unmeasured row.

The table uses translated join type and participation labels, shows the
measured classification (**Filtering**, **Multiplying**, or **Neutral**), and
shows **Unavailable** when no classification was recorded. A row marked
**Stale — redeploy to recheck** has no current deploy evidence; an error or
empty state tells you whether the health request failed or no joins have been
measured yet. The check is not guessed from a column name and it is not run for
every query. It uses the last deploy's source measurement and key metadata. If
a join's type, columns, or population participation changes, the row is marked
stale; deploy the model again to refresh the evidence. Change the declaration in
the Joins panel only after confirming whether the join is population-defining,
preserves base rows, or is enrichment-only.

---

## Query routing metrics

The Model Health panel has a separate **Query routing** section. Unlike the structural health checks above — which inspect the model *definition* — this section reports on **live query traffic**, so it answers a different question: are the model's speed-up tables actually earning their keep? Pick a window (**Last 24 hours**, **Last 7 days**, or **Last 30 days**) and it shows:

- **Acceleration rate** — the share of queries served from an aggregate or pocket table instead of going all the way to the source. Higher is better: it means more queries took the fast path.
- **Total queries**, broken down into **Aggregate hits**, **Pocket hits**, and **Source queries** — where each query was actually served.
- **Data scanned avoided** — how much source data the fast paths saved you from scanning.
- **Hourly query volume** — a simple chart of traffic across the window.
- When pockets are in use, a **Pocket savings** block adds **Pocket storage**, **Time saved**, and **Evictions (24h)**.

**How to read it.** A *low* acceleration rate together with a *high* source-query count is the signal to act: queries are not matching any aggregate. That is a cue to look at the [AI Optimiser](../modelling/use-the-ai-optimiser.md) or to add an aggregate for the shapes people actually run. If the section says **"No queries recorded in this window"**, the model simply has not been queried in that period — not a problem, just no data to show yet.

---

## Aggregate health

The **Aggregate health** section gives you a one-glance count of how your model's speed-up tables (aggregates) are doing. Every aggregate is one of two things:

- **Healthy** — it is *active* (serving queries) or *disabled* (intentionally paused but still built). These are the aggregates that can actually accelerate a query.
- **Unhealthy** — it is not serving queries right now. There are three flavours:
  - **Pending rebuild** — the aggregate's definition exists but its table still needs to be built. The most common reason is that the model was **imported, reseeded, or refreshed from a bundle**: an imported aggregate's stored table belongs to the *source* model, so Tessallite sets it aside as *pending* and rebuilds it against this model's own target before it is allowed to serve a query. This is deliberate, not data loss.
  - **Invalid** — the aggregate's grain or a measure it depends on no longer matches the model (for example a dimension was removed). It needs a definition fix before it can rebuild.
  - **Retired** — the aggregate was decommissioned. Its definition is kept for history, and its physical table is **dropped immediately** when it retires so it no longer takes up storage.

**Why "pending" matters.** Right after an import or refresh you may see the active count drop and a batch of aggregates sit in **Pending rebuild**, with an information alert in the Alerts section. The aggregates are not gone. Tessallite **kicks off the rebuild straight away** after an import or reseed, so the wait is short; the scheduled refresh sweep also rebuilds any stragglers automatically (promoting them back to *active*), and you can rebuild one yourself from the **Aggregates** drawer with **Rebuild now**. The AI Optimiser also treats an unhealthy aggregate as *not covering* its grain, so it will happily propose a fresh replacement rather than assuming the broken one is doing the job.

**How many aggregates a model keeps.** A model has a **maximum aggregates** hard cap (set in the Model Builder's **Aggregates → Predictive** controls). When the number of healthy aggregates would exceed the cap, the *When full* rule removes the lowest-value ones first. Raising the cap lets the model keep more speed-up tables; lowering it keeps storage tight.

---

## How to navigate to an issue

Each row in the Health tab identifies the affected object by name and type. Click a row to jump directly to that object in the Canvas. The object will be selected and the Drawer will open with its full definition, ready to edit.

---

## When health checks run

- After you save any change to the model definition.
- After a scheduled schema sync completes (by default, once per day).
- After an aggregate refresh job completes or fails.
- On demand, by clicking "Run health check" in the Health tab toolbar.

---

## Related

- [Query routing](query-routing.md)
- [Roles and permissions](roles-and-permissions.md)
- [View diagnostics](../modelling/view-diagnostics.md)
- [Aggregates](aggregates.md)

---

← [Query Routing](query-routing.md) | [Home](../index.md) | [Roles and Permissions →](roles-and-permissions.md)
