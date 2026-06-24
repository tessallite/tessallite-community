---
title: "Query Log"
audience: tenant-admin
area: Admin
updated: 2026-05-18
---

# Query Log

## What this covers

The query log records every SQL, DAX, and MDX query that flows through the Tessallite gateway and query-router. This page explains what information is captured, how to filter and search the log, how to export it as CSV, and how to configure retention.

---

## Why query logging matters

The query log serves three audiences:

- **Administrators** need to know which users are running queries, how often, and whether those queries are hitting aggregates or falling through to the source database.
- **Modellers** use query logs to identify slow queries, missing aggregates, and patterns that the optimizer has not yet addressed.
- **Security teams** need a record of which data was accessed, by whom, and whether row-level security rules were applied.

Without a query log, diagnosing a slow PivotTable refresh or an unexpected data access means searching raw service logs. The query log gives every stakeholder a single, filterable, exportable view.

---

## What is captured

Each query log entry records:

| Field | Description |
|---|---|
| Timestamp | When the query was received. |
| User identity | The authenticated user or JDBC/XMLA session identity. |
| Protocol | `jdbc`, `xmla`, `rest`, or `internal`. |
| Raw query | The original SQL, DAX, or MDX text. |
| Route type | How the query was served: `source` (direct to database), `aggregate` (hit a pre-built aggregate), or `pocket` (hit a pocket table). |
| Status | `success` or `error`. |
| Error type | If the query failed: `timeout`, `result_too_large`, `parse_error`, `auth_error`, etc. |
| Execution time | Wall-clock milliseconds from receive to response. |
| Rows returned | Number of result rows. |
| Rewritten query | If the query-router rewrote the SQL (e.g., to target an aggregate), the rewritten version. |
| Query fingerprint | A normalised hash of the query structure, used to group similar queries. |

---

## Accessing the query log

Open any model in the Explorer. The Diagnostics panel appears in the lower section. Select the **Query Log** tab.

The query log is scoped to the current project. All models in the project share a single log view.

---

## Filtering

Six filters narrow the results. All filters combine with AND logic.

| Filter | Behaviour |
|---|---|
| **From / To** | Date-time range. Only queries within this window are shown. |
| **User** | Partial text match against the user identity field. |
| **Status** | Dropdown: All, Success, Error. |
| **Route** | Dropdown: All, Source, Aggregate, Pocket. |
| **Error type** | Available via the API: exact match against the error type field. |
| **Model** | Available via the API: filter to a single model within the project. |

Type a date range and press Enter or click away to apply. Text filters apply as you type with a short debounce.

---

## Viewing query details

Click any row to open the detail dialog. The dialog shows:

- Status, route type, protocol, execution time, and rows returned as badges.
- The user identity (if present).
- The full original query text.
- The rewritten query (if the query-router rewrote it).
- Error type and error detail (for failed queries).
- Aggregate or pocket ID (if the query hit a cached result).
- The query fingerprint.

---

## CSV export

Click the download icon in the toolbar to export the current filtered view as a CSV file. The export includes all matching rows (not just the current page) with these columns:

`created_at`, `user_identity`, `protocol`, `raw_query`, `route_type`, `status`, `error_type`, `execution_ms`, `rows_returned`, `query_fingerprint`

Use the filters to narrow the export before downloading. For large tenants with millions of queries, apply a date range to keep the export manageable.

---

## Retention

The `query_log.retention_days` tenant setting controls how long query logs are kept:

- Default: **90 days**.
- Set to **0** for indefinite retention (logs are never auto-deleted).

A daily scheduler job (`query_log_purge`) deletes query log entries and their associated route log entries older than the retention threshold. The job runs once per day per tenant at 05:30 UTC.

Configure retention in *Admin > Settings* by adding or updating the `query_log.retention_days` key.

---

## API reference

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/v1/projects/{project_id}/logs/queries` | List query logs (paginated). Params: `model_id`, `status`, `error_type`, `route_type`, `user_identity`, `date_from`, `date_to`, `page`, `page_size`. |
| GET | `/api/v1/projects/{project_id}/logs/queries/export` | CSV download with the same filter parameters. |
| GET | `/api/v1/projects/{project_id}/logs/misses` | List query miss logs (cache misses tracked by the optimizer). |

All endpoints require authentication and project membership.

---

## Best practices

- **Review the hit/miss ratio regularly.** A high miss rate means queries are bypassing aggregates and hitting the source database directly. Use the route filter to isolate misses and check whether the optimizer has candidates queued.
- **Export before retention purges.** If you need query logs for capacity planning or billing, schedule a periodic CSV export. The query log is not a permanent archive.
- **Use the user filter for access reviews.** Filter by a specific user to see what data they queried and when.
- **Set retention to match your needs.** 90 days is sufficient for most operational use. Extend to 365 for compliance-heavy environments.

---

## Related

- [Audit Log](audit-log.md) -- platform action audit trail (separate from query execution logs)
- [Workspace Settings](workspace-settings.md)

---

<- [Audit Log](audit-log.md) | [Home](../index.md) | [Alert Configuration ->](alert-configuration.md)
