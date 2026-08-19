---
title: "Parameterized Filters"
audience: modeler
area: Modelling
updated: 2026-08-02
---

# Parameterized Filters

## What this covers

A parameterized filter is a named placeholder in a model's query predicates that is resolved to a concrete value at query time. This page explains what parameters are, when to use them, how to define them, how values are resolved, and how BI tools pass parameter values through JDBC session variables.

---

## What parameters solve

Many analytical models include filters that vary by user, session, or audience — a region code, a date range, a product category. Without parameters, the modeller has three unsatisfying options: hard-code a default (inflexible), define separate models per filter value (duplication), or rely on downstream tools to append WHERE clauses (inconsistent).

Parameters give the modeller a single declaration point for each variable filter. The query engine resolves the value from the most authoritative source available, substitutes it into the SQL, and handles type-aware expansion (IN lists, BETWEEN ranges, quoted strings). The model stays clean; the variability moves to the parameter layer.

---

## Parameter types

| Type | Value shape | SQL expansion |
|---|---|---|
| `string` | A single text value | `= 'value'` |
| `number` | A single numeric value | `= value` |
| `multi_value` | A list of text values | `IN ('a', 'b', 'c')` |
| `date_range` | An object with `from` and `to` | `BETWEEN 'from' AND 'to'` |
| `boolean` | `true` or `false` | `= TRUE` or `= FALSE` |

Type validation happens at resolution time. A `multi_value` parameter supplied with a single string is auto-wrapped into a one-element list. A `number` parameter supplied with a non-numeric value fails with a clear error.

---

## Defining a parameter

1. Open a model in Model Builder.
2. Click the **Parameters** icon in the toolbelt to open the Parameters panel.
3. Click **Add Parameter**.
4. Fill in:
   - **Name** — must be `@` followed by a letter or underscore, then any mix of letters, digits, and underscores (e.g. `@region`, `@date_start`, `@_internal`). A name like `@1region` (digit first) or `@my-param` (hyphen) is rejected with a clear error, because the query engine has to recognise `@name` as a single token during substitution — a name that started with a digit or contained punctuation could not be told apart from the surrounding SQL.
   - **Display name** — optional friendly label shown in admin UIs.
   - **Type** — one of the five types above.
   - **Default value** — the value used when no higher-precedence source provides one. Type-dependent: a string for `string`, a number for `number`, a JSON array for `multi_value`, a `{"from": ..., "to": ...}` object for `date_range`, or `true`/`false` for `boolean`.
   - **Allowed values** — optional whitelist for `string`, `multi_value`, and `number` parameters. When set, any resolved value outside the list is rejected at resolution time with a clear error, so a stray or malicious filter value can never reach the source. For `multi_value`, *every* member must be on the list. Number lists are checked numerically, so a declared `[10, 20]` matches a resolved `10` or `"10"` — you do not have to worry about a "10" vs 10 spelling mismatch. A `date_range` parameter does **not** take an allowed-values list — a whitelist has no meaning for a from/to range, so the list is ignored there; constrain a date range with its `from`/`to` bounds instead.
   - **Description** — optional documentation.
5. Click **Save**.

Parameters are unique per model by name. Attempting to create a duplicate name returns HTTP 409.

---

## Resolution order

When a query references a parameter, the engine resolves its value using three sources, in order of precedence (highest first):

1. **Persona default filter.** If the query is bound to a persona and that persona defines a default filter matching the parameter, the persona's value wins.
2. **JDBC session variable.** If the BI tool set `app.<param_name>` via a `SET` command earlier in the session, that value is used.
3. **Model default value.** The default defined on the parameter definition.

If no value is available from any source and the parameter has no default, the query fails with a structured error naming the unresolved parameter.

---

## JDBC session variables

BI tools connected via JDBC (port 5433) can set parameter values per session using the standard PostgreSQL `SET` command:

```sql
SET app.region = 'APAC';
SET app.date_start = '2025-01-01';
SET app.date_end = '2025-12-31';
```

The `app.` prefix is required. Session variables persist for the lifetime of the JDBC connection and are cleared when the client disconnects.

This mechanism works with any PostgreSQL-compatible client: DBeaver, Tableau (custom SQL), psycopg2, JDBC drivers. The gateway captures the SET commands and passes the values to the query router as part of the query context.

### Setting a multi-value parameter over JDBC

A `multi_value` parameter holds a list, but a `SET` command can only send one piece of text. There are two ways to write that list.

**The simple way — separate the values with commas.** Use this when none of your values contain a comma.

```sql
SET app.region = 'EMEA,APAC,AMER';
SELECT country, SUM(revenue) FROM sales WHERE region IN (@region) GROUP BY country;
```

That filters on three regions: `EMEA`, `APAC` and `AMER`.

**The safe way — write the list as a JSON array.** Use this whenever a value might contain a comma, a quote, or leading/trailing spaces.

```sql
SET app.city = '["New York, NY","Paris"]';
SELECT store, SUM(revenue) FROM sales WHERE city IN (@city) GROUP BY store;
```

That filters on exactly two cities: `New York, NY` and `Paris`.

**Why this matters.** With the simple comma form, `'New York, NY,Paris'` cannot be told apart from three separate values, so the filter would look for `New York`, `NY` and `Paris` — three cities that are not what you asked for, and no error to warn you. The JSON array form removes the guesswork: whatever is inside each pair of quotes is one value, commas and all.

**Rules for the JSON form**

- Wrap the whole list in square brackets and put each value in double quotes.
- Values must be text or numbers. A `true`/`false`, a `null`, a nested list, or an object is rejected with a clear message, because none of those is a filter value.
- Numbers are treated as text, so `[10, 20]` and `'10,20'` filter identically.
- The list must contain at least one value. An empty list is rejected — there is no such thing as a filter on nothing.
- If the text starts with `[` **or** ends with `]`, it is treated as attempted JSON. Malformed or partial arrays fail with an error naming the parameter rather than quietly falling back to comma splitting.

**Tip.** Most SQL clients let you build the string in a variable. If your tool escapes quotes differently, run the `SET` line on its own first and check that the following query returns the row count you expect.

---

## Worked example

A modeller defines two parameters on the `sales` model:

| Name | Type | Default |
|---|---|---|
| `@region` | `string` | `"ALL"` |
| `@report_date` | `date_range` | `{"from": "2025-01-01", "to": "2025-12-31"}` |

A DBeaver user connects and runs:

```sql
SET app.region = 'EMEA';
SELECT country, SUM(revenue) FROM sales WHERE region = @region GROUP BY country;
```

The query engine resolves `@region` to `'EMEA'` (from the session variable, which takes precedence over the default). The `@report_date` parameter is not referenced in this query, so it is not resolved.

A different user connects via a persona that defines `default_filters: {"region": "APAC"}`. Their query:

```sql
SELECT country, SUM(revenue) FROM sales WHERE region = @region GROUP BY country;
```

The engine resolves `@region` to `'APAC'` — the persona filter takes precedence over both the session variable (not set in this session) and the model default.

---

## Editing and deleting parameters

- **Edit** — click the pencil icon on a parameter row. All fields except the name are editable. Changing the type clears the default value and allowed values.
- **Delete** — click the delete icon and confirm. Queries that reference a deleted parameter will fail at resolution time until the reference is removed or the parameter is recreated.

---

## Best practices

- **Use parameters for values that vary by audience.** If every user sees the same filter, a regular WHERE clause is simpler.
- **Set sensible defaults.** A parameter without a default requires every caller to supply a value. Use a default that returns a safe, non-empty result set.
- **Constrain with allowed values.** For `string` and `multi_value` parameters, an allowed-values list prevents unexpected filter values and makes the parameter self-documenting.
- **Name consistently.** Adopt a convention like `@report_date_start`, `@report_date_end`, `@region_filter` so parameters are recognisable in queries.

---

## API reference

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/v1/models/{model_id}/parameters` | List model parameters |
| POST | `/api/v1/models/{model_id}/parameters` | Create a parameter |
| PUT | `/api/v1/models/{model_id}/parameters/{id}` | Update a parameter |
| DELETE | `/api/v1/models/{model_id}/parameters/{id}` | Delete a parameter |

All require `modeler` role on the model's project.

---

## Related

- [Configure Personas](configure-personas.md) — persona default filters interact with parameter resolution
- [Define Dimensions](define-dimensions.md)
- [JDBC Connection Guide](../integrations/jdbc-connection-guide.md)

---

← [Configure Personas](configure-personas.md) | [Home](../index.md) | [Data Quality Rules →](data-quality-rules.md)
