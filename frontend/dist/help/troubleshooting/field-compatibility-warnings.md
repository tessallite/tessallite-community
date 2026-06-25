---
title: "Field Compatibility Warnings"
audience: analyst
area: Troubleshooting
updated: 2026-06-14
---

## What this covers

Tessallite warns or blocks a query when a selected measure cannot be grouped by a selected dimension. The common message is:

```text
There is no aggregation path between <Measure> and <Dimension>.
<Measure> can be used with: <Dimension A>, <Dimension B>, <Dimension C>.
```

An aggregation path means the model has a valid semantic relationship from the data behind the measure to the dimension you want to group by. This protects you from invalid pivots and misleading totals.

---

## Example

`Average Student Age` may be valid by `Student Grade` and `School`, but not by `Teacher Name` unless the model defines a valid relationship between those business areas.

Use one of the suggested compatible dimensions, remove the incompatible dimension, or ask a model owner to add the relationship if the business relationship is real.

---

## Where you see it

| Surface | Behavior |
|---------|----------|
| Web pivot table | Incompatible dimensions are disabled where possible. Saved layouts that already contain incompatible fields show a warning and Execute is blocked. |
| Query validation and dry run | Validation returns the same message before execution. Freeform SQL is not blocked while typing, but validation can block execution once the fields are resolved. |
| Excel Report Builder | Incompatible dimensions are disabled after measures are placed in Values. Run, Insert Table, and Pivot are blocked while incompatible fields remain. |
| Excel CUBE Formula Wizard | Optional dimension filters are limited to compatible dimensions. Unavailable fields may appear disabled when the workflow offers a show-unavailable option. |
| Native Excel pivots over inserted tables | Existing local worksheet pivots are not dynamically restricted. Compatibility is checked before Tessallite generates or refreshes the source table. |
| XMLA and JDBC clients | The query fails during validation or execution with the aggregation-path message. |
| Conversational agent | The agent may try the requested analysis, then explain the structured compatibility failure and suggest valid dimensions. |

---

## Multiple measures

When a pivot contains multiple measures, a dimension must be compatible with every selected measure. Tessallite uses the common compatible dimensions across the selected measures.

If no common dimensions exist, split the analysis into separate pivots or choose measures from the same business area.

---

## Security-filtered suggestions

Suggested dimensions are filtered by your current persona and column-level-security rules. You may see fewer options than a model owner sees.

Tessallite does not reveal hidden or unauthorized dimensions as alternatives. If your access level restricts a measure to aggregated use only, the warning uses security-aware language and suggests only dimensions available to your persona.

---

## What to do

1. Choose one of the suggested compatible dimensions.
2. Remove the incompatible dimension or slicer.
3. Split multi-measure pivots into separate analyses when the measures do not share dimensions.
4. Ask a model owner to add or correct relationships if the business relationship should be valid.
5. Switch persona only when you are authorized to do so and need a different access scope.

---

## Related

- [Measure Query Panel](../modelling/measure-query-panel.md)
- [Query Panel](../modelling/query-panel.md)
- [Tessallite Excel Add-in](../integrations/excel-add-in.md)
- [Excel XMLA Connection Guide](../integrations/excel-xmla-connection-guide.md)
- [JDBC Connection Guide](../integrations/jdbc-connection-guide.md)
- [Agent Chat](../agent/agent-chat.md)
- [Column-Level Security](../modelling/column-level-security.md)

---

← [Query Returns Wrong Results](query-returns-wrong-results.md) | [Home](../index.md) | [Aggregates Not Building →](aggregates-not-building.md)
