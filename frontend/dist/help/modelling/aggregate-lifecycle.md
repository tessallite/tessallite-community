---
title: "Aggregate Lifecycle"
audience: modeller
area: modelling
updated: 2026-04-25
---

## What this covers

The **Aggregate Lifecycle** panel is a chronological audit trail of every event that has happened to every aggregate in the current model. It answers questions like *"why did this aggregate disappear?"*, *"when did the predictor validate that one?"*, and *"how often does this build fail?"* without dropping into the database.

---

## Opening the panel

Click the **Aggregate Lifecycle** icon (clock face) in the Toolbelt of Model Builder. The panel opens in the right drawer.

The panel is read-only — it surfaces what happened, it does not change anything.

---

## Event types

Every row carries a chip-coloured event type:

| Event | Emitted by | Meaning |
|---|---|---|
| `created` | Model service / optimiser / predictor | The aggregate definition was inserted. The `reason` field discriminates `manual` / `demand` / `predictive` / `workload`. |
| `built` | Scheduler | A successful CTAS build completed. Payload carries `rows_written` and `duration_ms`. |
| `failed` | Scheduler | A build failed. Payload carries the warehouse error message. |
| `validated` | Predictor feedback sweep | A predictive aggregate hit the threshold (`predictive.validation_min_hits` queries within `predictive.evaluation_window_days`) and is now considered useful. Payload carries `hit_count` and `window_start`. |
| `retired_unused` | Predictor feedback sweep | A predictive aggregate older than `predictive.unused_retire_days` with zero hits ever was flipped to `status='retired'`. |
| `evicted` | Build orchestrator | The aggregate was dropped to make room for a higher-scoring predictive candidate. Payload carries the eviction policy that fired. |

---

## Filtering

The **Event** dropdown above the table filters the list to a single event type. Default is "All events".

To filter by a specific aggregate, navigate to that aggregate's card in the Aggregates panel and use the per-aggregate timeline view (separate from this model-wide log).

---

## Columns

| Column | Source |
|---|---|
| **When** | Event timestamp (UTC). |
| **Event** | Chip-coloured type — see table above. |
| **Aggregate** | Short form of the aggregate id (last 8 hex chars). Hover for the full UUID. |
| **Reason** | Free-form discriminator from the emitter — for example `feedback_sweep`, `predictive`, `lru`, `manual`. |
| **Detail** | Pretty-printed payload. For `built` / `failed` rows this is the row count or error; for `validated` it is the hit count + window; for `evicted` it is the policy + the displaced aggregate id. |

---

## Risk-4 note (predictive validation)

When the feedback sweep validates a predictive aggregate, the panel shows two changes side by side:

- A `validated` event row appears with `reason=feedback_sweep`.
- The aggregate's `creation_reason` stays as `predictive` — it does **not** flip to `workload` or `demand`.

This is intentional. The validation event records that the aggregate has earned its keep, but the *origin* (predictor) is preserved so eviction policies (`validated_survives`, `predicted_first`) can still distinguish predictive aggregates from demand-defined ones.

---

## API

The panel reads from `GET /api/v1/models/{model_id}/aggregates/lifecycle`:

| Query param | Meaning |
|---|---|
| `event_type` | Filter to one event type. |
| `aggregate_id` | Filter to a single aggregate. |
| `limit` | Cap the number of rows returned (default 200). |

Events are ordered newest-first by `occurred_at`.

---

## Related

- [Predictive Aggregates](predictive-aggregates.md)
- [Cold-start Latency](cold-start-latency.md)
- [Configure Aggregates](configure-aggregates.md)
- [Aggregates (concept)](../concepts/aggregates.md)

---

← [Predictive Aggregates](predictive-aggregates.md) | [Home](../index.md) | [Cold-start Latency →](cold-start-latency.md)
