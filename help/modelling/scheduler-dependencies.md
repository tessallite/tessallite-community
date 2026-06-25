---
title: "Scheduler Dependencies"
audience: modeller
area: modelling
updated: 2026-05-24
---

## What this covers

When one aggregate depends on data produced by another, you need to ensure the upstream aggregate refreshes before the downstream one begins. Scheduler dependencies let you define these ordering constraints. This page explains how to create, view, and delete dependencies, and how topological sorting and cycle detection work.

---

## Why dependencies matter

Without dependencies, all aggregates refresh independently according to their own cron schedules. If aggregate B reads from a table that aggregate A materialises, and A hasn't finished its refresh yet, B will see stale data. Dependencies guarantee that B's refresh job waits until A completes.

---

## Creating a dependency

1. Open the **Aggregates panel** in the Model Builder.
2. Select the downstream aggregate (the one that should wait).
3. In the aggregate drawer, open the **Dependencies** section.
4. Click **Add dependency** and select the upstream aggregate.
5. Save. The scheduler will now ensure the upstream finishes before the downstream starts.

Both aggregates must belong to the **same model**. Cross-model dependencies are not supported and the API will reject them with a 400 error.

---

## Viewing dependencies

The **Dependencies** section in each aggregate drawer lists both:

- **Depends on** (upstream) — aggregates that must complete before this one starts.
- **Required by** (downstream) — aggregates that wait for this one to complete.

The scheduler processes aggregates in topological order, so the refresh sequence always respects the dependency graph.

---

## Cycle detection

If adding a dependency would create a circular chain (A depends on B, B depends on C, C depends on A), the API rejects the request with a **400 Bad Request** error and a message identifying the cycle. You must remove an existing dependency before the new one can be added.

---

## Deleting a dependency

1. Open the downstream aggregate drawer.
2. In the **Dependencies** section, click the delete icon next to the upstream entry.
3. Confirm. The scheduler will no longer enforce ordering between the two.

---

## API reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/v1/models/{model_id}/dependencies` | POST | Create a dependency (upstream_id, downstream_id) |
| `/api/v1/models/{model_id}/dependencies` | GET | List all dependencies for the model |
| `/api/v1/models/{model_id}/dependencies/{id}` | DELETE | Remove a dependency |

---

## Related

- [Manage Aggregate Schedules](manage-aggregate-schedules.md)
- [Configure Aggregates](configure-aggregates.md)
- [Run a Refresh](run-a-refresh.md)

---

<- [Manage Aggregate Schedules](manage-aggregate-schedules.md) | [Home](../index.md) | [Use the AI Optimiser ->](use-the-ai-optimiser.md)
