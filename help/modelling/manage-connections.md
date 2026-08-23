---
title: "Manage Connections"
audience: modeller
area: modelling
updated: 2026-08-11
---

## What this covers

The **Connections** panel manages reusable credentials and endpoints for a project. A connection answers "how does Tessallite reach this database or source system?" It is not the same thing as a model source. A source answers "which schema or dataset from that connection is added to this model?"

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

### Why secrets must never go in Config

Think of the two boxes differently. **Credentials** is a locked box: Tessallite scrambles everything you put in it, and nobody -- not even another user on your team -- can read it back. **Config** is a labelled box with a glass lid: it holds the harmless "where to look" details, and anyone who can view the project can see straight through it.

Put a password in the glass box and everyone can read it. So Tessallite refuses. If you try to save a Config entry whose name looks like a secret -- anything containing `password`, `secret`, `token`, `key`, `credential`, or a whole connection string -- the save is rejected and the message tells you which entry to move. If an older connection was created before this check existed and still has something secret-looking in Config, Tessallite hides it when the connection is displayed, so it cannot leak to a viewer.

The same rule applies to an LLM provider's Config section. The provider API key has its own locked box; the Config section is for plain settings such as the Google project and location.

**Example.** You are wiring up a Snowflake connection.

- Right: Credentials holds `password`. Config holds `warehouse` and `schema`.
- Wrong: Config holds `db_password`. Tessallite rejects the save and asks you to move it to Credentials.

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

A field you leave blank means "leave this one alone". That is what lets you change the host without retyping the password. It also means a blank field cannot be used to *erase* something you no longer want. If you are calling the REST API directly rather than using the screen, two special values cover that:

| Send this as the value | What happens |
|---|---|
| `__CLEAR__` | The field is removed completely, as if it had never been set. Use this to wipe a password you no longer want stored. |
| `__EMPTY__` | The field is kept but set to nothing. Use this when the connector needs to see the field present and blank. |

Deleting a connection can be blocked when sources or targets still depend on it. Remove or rebind those sources first. If deletion is allowed, any object still pointing at the old connection will stop working.

---

## Related

- [Add a Data Source](add-a-data-source.md)
- [Add Tables to a Model](add-tables-to-a-model.md)
- [Supported Data Sources](../integrations/supported-data-sources.md)

---

← [Create a Project](create-a-project.md) | [Home](../index.md) | [Add a Data Source →](add-a-data-source.md)
