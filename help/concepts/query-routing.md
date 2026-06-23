---
title: "Query Routing"
audience: modeller
area: concepts
updated: 2026-04-23
---

## What this covers

This article explains how Tessallite decides whether a query is answered from a pre-computed summary or from the raw source table. It covers the routing conditions, the behaviour of non-additive measures, and what happens when no summary matches.

---

## The routing decision

Tessallite routes a query to a pre-computed aggregate when all three of the following conditions are true.

**The aggregate's grain is a superset of the query's grain.** The grain is the combination of dimensions at which an aggregate was computed. If a query asks for sales by country, an aggregate computed at country level satisfies the condition. An aggregate computed at city level does not — routing to a finer grain would require re-aggregating, which is not valid for all measure types.

**All measures the query requests exist in the aggregate.** A query asking for revenue and unit count cannot be routed to an aggregate that contains only revenue.

**Any column the query uses to filter is part of the aggregate's grain.** If a query filters by product category, the aggregate must have been computed with product category as a dimension.

If any one of these conditions is not met, the query runs against the raw source table.

![Query routing flow.](../assets/illustrations/query-routing-flow.svg)

---

## Non-additive measures

COUNT DISTINCT and other non-additive operations require an exact grain match. A coarser aggregate cannot be used even if it covers all the requested dimensions. Distinct counts cannot be re-aggregated from a summary computed at a coarser grain: summing the distinct counts from two regions does not yield the correct distinct count for the combined total. The query falls through to the raw source unless the aggregate was computed at exactly the same grain.

---

## What happens on a miss

When no suitable aggregate exists, the query runs against the raw source table. The miss is logged with the query's grain and measure pattern. The Optimizer reads the miss log. When the same pattern appears frequently enough, the Optimizer creates a new aggregate. Future queries matching that pattern are routed to the new aggregate.

---

## Hit rate

The hit rate is the proportion of queries that were served from an aggregate rather than the raw source. A higher hit rate indicates that more queries are being answered without scanning raw data. The hit rate metric is visible in the Diagnostics panel of the Model Builder.

---

## Grain

The grain of a query is the finest level of detail it requests. A query asking for total sales by country and year has a grain of (country, year). A query asking for a grand total with no grouping has a grain of () — the empty set. An aggregate's grain is the set of dimensions it was computed over.

---

## Routing conditions reference

| Condition | Required for routing to aggregate |
|---|---|
| Aggregate grain is a superset of query grain | Yes |
| All requested measures present in aggregate | Yes |
| All filter columns are part of the aggregate grain | Yes |
| Exact grain match | Only for non-additive measures (COUNT DISTINCT) |

---

## Route badge and force-live toggle

The Query panel shows a badge on every executed query so you can see which path served it. The badge reads `aggregate`, `pocket`, or `live (source)`. Hovering the badge reveals the routing reason — the same text the `/explain` endpoint returns — so you do not need a second round-trip to understand why a query took its path.

The **Force Live** toggle, sitting next to the Execute button, forces the next query to bypass the aggregate and pocket matchers and run directly against the source. Use it when you need to verify raw-source behaviour or compare pre-computed results against the underlying data. Row security is still applied — the force-live switch does not grant access to rows the principal would otherwise be filtered away from.

Force Live is a per-query flag. It resets to off on panel reload and never becomes the default for a model.

---

## Related

- [How Tessallite works](../getting-started/how-tessallite-works.md)
- [Configure aggregates](../modelling/configure-aggregates.md)
- [View diagnostics](../modelling/view-diagnostics.md)

---

← [Aggregates](aggregates.md) | [Home](../index.md) | [Model Health →](model-health.md)
