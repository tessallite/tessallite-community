---
title: "Manage Connections"
audience: modeller
area: modelling
updated: 2026-05-15
---

## What this covers

The **Connections** panel manages reusable credentials and endpoints for a project. A connection answers "how does Tessallite reach this database or warehouse?" It is not the same thing as a model source. A source answers "which schema or dataset from that connection is added to this model?"

Use this page when the help icon is opened from the Connections panel or the standalone Connections page.

---

## What belongs in a connection

| Field | Purpose |
|---|---|
| Display name | Human-readable name shown when modellers bind sources and targets. |
| Connection type | PostgreSQL, BigQuery, Hadoop/Spark, or another configured connector. |
| Credentials | Secrets such as password, service-account JSON, or token material. |
| Config | Non-secret options such as schema defaults, dataset, write access, and connector flags. |

Credentials are stored by the backend and are never shown back in full after save. Edit screens may show previews or blank password fields; leaving a password blank preserves the saved secret.

---

## Typical workflow

1. Open the project.
2. Open **Connections** from the Model Builder toolbelt, or open the project Connections page.
3. Click **Add Connection**.
4. Enter a display name and select the connection type.
5. Fill the required fields for the connector.
6. Click **Test** before saving. A successful test confirms the gateway can authenticate and reach the endpoint.
7. Save the connection.
8. Go to **Sources & Targets** to bind schemas, datasets, model sources, or writable targets to this connection.

---

## Test results

The test button checks network reachability, authentication, and connector-specific credential shape. It does not prove that every table a modeller later wants is accessible. Table access is validated when a source is added and tables are discovered.

For BigQuery, a successful connection test confirms that the service-account JSON is valid. Dataset-level permissions are still checked when the dataset is used as a source or target.

---

## Editing and deleting

Editing a connection updates future reads and writes that use that connection. Existing sources remain bound to the same connection record, so a corrected host, token, or service account takes effect without recreating all model tables.

Deleting a connection can be blocked when sources or targets still depend on it. Remove or rebind those sources first. If deletion is allowed, any object still pointing at the old connection will stop working.

---

## Related

- [Add a Data Source](add-a-data-source.md)
- [Add Tables to a Model](add-tables-to-a-model.md)
- [Supported Data Sources](../integrations/supported-data-sources.md)

---

← [Create a Project](create-a-project.md) | [Home](../index.md) | [Add a Data Source →](add-a-data-source.md)
