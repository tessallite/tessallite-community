---
title: "Looker Studio Direct Connection"
audience: analyst
area: Integrations
updated: 2026-05-25
---

## What this covers

Shape A connects Looker Studio (formerly Data Studio) directly to Tessallite
through Studio's PostgreSQL connector. Use it for SQL-shaped reporting
without operating or licensing a Looker instance.

```
Looker Studio -> PostgreSQL connector -> Tessallite Gateway :5433 -> query router
```

The gateway support is internally tested, but live Studio connection and query
capture remain pending. Treat this guide as setup preview until your deployment
has recorded live validation evidence.

## When to choose Shape A

| Need | Choose |
|---|---|
| Direct report from deployed relational semantic relations | Shape A |
| LookML explores, drills, required filters, or suggestion hints | [Optional Looker-hosted workflow](looker-studio-via-looker-guide.md) |
| Native Excel/Pivot semantics | XMLA rather than Looker Studio |

## Prerequisites

- A deployed Tessallite model and user credentials.
- A gateway TCP endpoint reachable from Looker Studio.
- `GATEWAY_SSL_ENABLED=true` with a trusted certificate and key.

## Connection settings

In Looker Studio, create a PostgreSQL data source and use:

| Setting | Value |
|---|---|
| Host | Publicly reachable gateway TCP hostname |
| Port | `5433` |
| Database | Tessallite workspace slug |
| Username/password | Tessallite user credentials |
| SSL | Required for supported internet-facing Data Studio validation |

Follow Google's current PostgreSQL connector networking and allowlisting
instructions for the deployment region. Tessallite cannot infer Google's
outbound addresses from local configuration.

## Expected capabilities and limits

- Deployed semantic relations and published fields are browsable through
  PostgreSQL catalogue metadata.
- Queries continue through Tessallite routing and diagnostics.
- Query-log labeling for Studio is recorded only when the client exposes a
  stable recognized product identifier; exact live behavior remains a capture
  item.
- If Studio is indistinguishable from an ordinary PostgreSQL client in its
  startup metadata and SQL shape, it continues to function but cannot be
  labeled automatically in diagnostics without an observed stable signature.
- Shape A is a relational route. It does not consume generated LookML fields
  such as `drill_fields`, `always_filter`, or `suggest_dimension`.

## Troubleshooting

| Symptom | Resolution |
|---|---|
| TLS or certificate failure | Confirm a trusted gateway certificate and SSL-required connector configuration. |
| No relations appear | Deploy the model and confirm the workspace/database slug. |
| Need LookML behavior | Generated LookML requires a separate Looker instance; see [Optional Looker-hosted workflow](looker-studio-via-looker-guide.md). |

## Related

- [Optional Looker-hosted workflow](looker-studio-via-looker-guide.md)
- [JDBC Connection Guide](jdbc-connection-guide.md)
- [BI Tool Compatibility Matrix](bi-compatibility.md)

---

<- [JDBC Connection Guide](jdbc-connection-guide.md) | [Home](../index.md) | [Optional Looker-hosted workflow ->](looker-studio-via-looker-guide.md)
