---
title: "Common Errors"
audience: all
area: Troubleshooting
updated: 2026-04-17
---

## What this covers

A reference table of error messages returned by Tessallite services and the Gateway, grouped by category.

---

## Connection errors

| Error message | Likely cause | Resolution |
|---------------|-------------|------------|
| `FATAL: database "X" does not exist` | Workspace slug wrong in Database field | Verify slug with Tenant Admin. Slug is case-sensitive. |
| `FATAL: password authentication failed for user "X"` | Wrong username or password | Use Tessallite email and password. Reset via Admin panel if needed. |
| `Connection refused (port 5433)` | Gateway not running or wrong host | Check: `docker compose ps gateway`. Verify hostname and port. |
| `SSL SYSCALL error: EOF detected` | Server requires SSL, client not configured | Append `?sslmode=require` to the JDBC URL or enable SSL in client settings. |

---

## Query errors

| Error message | Likely cause | Resolution |
|---------------|-------------|------------|
| `ERROR: column "X" does not exist` | Schema drift — column renamed or removed in source | Model Builder → Health tab → fix schema drift → re-publish and rebuild aggregates. |
| `ERROR: Query timeout exceeded` | Query exceeded workspace timeout | Increase timeout in Workspace Settings, or ask Modeller to build a matching aggregate. |
| `ERROR: permission denied for schema "X"` | DB user lacks read access to source schema | Grant `USAGE` on schema and `SELECT` on tables to the Tessallite database user. |

---

## Aggregate errors

| Error message | Likely cause | Resolution |
|---------------|-------------|------------|
| `Aggregate build failed: write permission denied` | Scheduler cannot write to target schema | Grant `CREATE TABLE`, `INSERT`, `DROP TABLE` on target schema. See [Aggregates Not Building](aggregates-not-building.md). |
| `Aggregate build failed: source timeout` | Source query timed out during aggregate build | Reduce aggregate grain or increase source query timeout. |
| `Aggregate retired` | Optimizer retired unused aggregate | Re-create manually in Model Builder, or lower the retirement threshold. |

---

## Related

- [Excel Connection Problems](excel-connection-problems.md)
- [Query Returns Wrong Results](query-returns-wrong-results.md)
- [Aggregates Not Building](aggregates-not-building.md)
- [Service Not Starting](service-not-starting.md)
- [JDBC Connection Guide](../integrations/jdbc-connection-guide.md)

---

← [Service Not Starting](service-not-starting.md) | [Home](../index.md) | [Tessallite Help →](../index.md)
