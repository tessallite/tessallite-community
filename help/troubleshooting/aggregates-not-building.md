---
title: "Aggregates Not Building"
audience: modeller
area: Troubleshooting
updated: 2026-04-17
---

![Health tab — aggregate build failed (write permission denied).](../assets/screencaps/aggregate-build-error.png)

## What this covers

Diagnosing situations where aggregates fail to build, are stuck building, or never appear. Aggregates are built by the Tessallite Scheduler service. Most failures trace back to a connectivity, permission, or configuration problem.

---

## Symptom reference

| Symptom | Likely cause | Resolution |
|---------|-------------|------------|
| Aggregate stuck in "Building" for >30 minutes | Scheduler service crashed | Check: `docker compose ps scheduler`. If Exit/Restarting, read logs: `docker compose logs --tail=50 scheduler`. Restart: `docker compose restart scheduler`. |
| Aggregate status shows "Error" | Build query failed | Model Builder → Health tab → "Aggregate build failed" → expand for error message. |
| "write permission denied on target schema" | DB user cannot write to aggregate target schema | Grant `CREATE TABLE`, `INSERT`, `DROP TABLE` on target schema to the Tessallite database user. |
| "source query timeout" | Source query exceeded timeout during aggregate build | Increase query timeout in Workspace Settings, or reduce the aggregate grain. |
| "source connection refused" | Source data source not reachable | Verify source connection params in project settings. Confirm source DB is running and reachable from Tessallite host. |
| No new aggregates after AI Optimizer run | Miss log is empty — no queries captured yet | Run several queries via a BI tool first, then re-open Optimizer. |
| AI Optimizer suggests nothing with query history | Score threshold too high | Reduce `OPTIMIZER_SCORE_THRESHOLD` env var and restart Optimizer service. |
| Existing aggregate disappears | Optimizer retired unused aggregate | Re-create manually in Model Builder, or lower the retirement threshold. |

---

## View Scheduler logs

```
docker compose logs -f scheduler
```

The `-f` flag streams new lines. Press Ctrl+C to stop. Look for `ERROR` or `FATAL` entries.

For Cloud Run or other managed deployments, access logs through the platform's log viewer filtered by the scheduler service name.

---

## Common permission grant (PostgreSQL)

Run as a database superuser:

```sql
GRANT USAGE ON SCHEMA target_schema TO tessallite_user;
GRANT CREATE ON SCHEMA target_schema TO tessallite_user;
GRANT INSERT, SELECT, DROP ON ALL TABLES IN SCHEMA target_schema TO tessallite_user;
```

Replace `target_schema` and `tessallite_user` with the values from your data source configuration.

---

## Why didn't my query get relabel-served

A query grouped by a dimension detail column (for example `country_name`) can be served from an aggregate built on the key (for example `country_code`) only when a proven one-to-one relationship links the two. When that does not happen and the query goes to the source instead, the relationship failed closed for a reason. This is a safety behaviour, not a bug — the answer is always correct; it is just not accelerated. Common reasons:

| Reason | What it means | Resolution |
|--------|---------------|------------|
| No relationship declared | The detail column is only a display caption, not a declared relationship | Declare an attribute relationship on the dimension linking the key and detail columns. |
| Relationship not proven | The relationship is declared but has not passed data verification for the current model version | Re-deploy the model so the relationship is checked against the full data. |
| Relationship broke on the data | The mapping is no longer one-to-one (two keys now share a detail, or vice versa) | Fix the source data, or reclassify the relationship as many-to-one if that is the true shape. |
| Null endpoints | The key or detail column contains null values, which the REJECT_NULL rule refuses | Clean the column so every row is populated, then re-deploy. |
| Enriched aggregate not built | The aggregate does not carry the detail column as a passenger | Enable derived-expression auto-build on the model (approval or automatic) so the enriched aggregate is created. |
| Serving disabled | The system-wide relabel-serving switch is off | An administrator must enable relabel serving before proven relationships route live queries. |
| Row-level security active | The query runs under a persona that filters rows | Relabel serving is intentionally disabled under row security; the query correctly uses the source path. |

See [Dimension Attribute Relationships](../modelling/dimension-attribute-relationships.md) for the full concept and [Query Routing](../concepts/query-routing.md) for how the relabel route fits the overall routing decision.

---

## Related

- [Query Returns Wrong Results](query-returns-wrong-results.md)
- [Service Not Starting](service-not-starting.md)
- [Common Errors](common-errors.md)
- [Supported Data Sources](../integrations/supported-data-sources.md)

---

← [Field Compatibility Warnings](field-compatibility-warnings.md) | [Home](../index.md) | [Service Not Starting →](service-not-starting.md)
