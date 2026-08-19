---
title: "Named Queries"
audience: modeller
area: modelling
updated: 2026-08-14
---

## What this covers

A Named Query is a saved, named database question whose answer is a whole table. A modeller writes the question once, gives it a name, and every user (in SQL, the Explorer, or a BI tool) can then ask for it by writing one short line:

```sql
SELECT * FROM @top_three_transaction_cities;
```

Tessallite keeps a pre-computed copy of the answer table and serves it directly. If the copy is not ready, or not allowed for a particular user, Tessallite quietly runs the original question live instead — so the answer is always correct, never stale-by-accident.

This article explains what a Named Query is, how it differs from the other named things in Tessallite (Named Lists, Saved Queries, Pockets, Aggregates), how to create and refresh one, how to use it in SQL, and what happens behind the scenes.

---

## Think of it as a named answer sheet

Imagine a teacher preparing answer sheets for a class. Each sheet has a title (the name), the working-out written at the top (the definition), and the final answers in a table below (the materialised result). The teacher photocopies the answer table so the class can read it quickly. If the photocopy is lost, or a student needs a personalised version, the teacher redoes the working-out on the spot.

A Named Query works the same way:

| Part | What it is |
|---|---|
| **Name** | The short reference used in SQL, like `@top_three_transaction_cities`. |
| **Definition** | The original question, written in SQL against the model (measures and dimensions), never against the raw database. |
| **Materialised result** | A real table Tessallite builds by running the definition, stored on the query target. |
| **Fallback** | If the stored table is not usable, Tessallite runs the definition live against the source instead. |

---

## How a Named Query is different from the other named things

Tessallite has several features with "named" in the title. They each solve a different problem:

| Feature | What it holds | Is a copy of the answer kept? | How you use it |
|---|---|---|---|
| **Named List** | A flat list of dimension member values | No — values are expanded into `IN (...)` when you query | `WHERE channel IN (@ActiveChannels)` |
| **Named Query** | A whole query whose result is a table | Yes — a materialised result table | `SELECT * FROM @Name` |
| **Saved Query** | A personal bookmark of query text | No | Your own saved text in the UI |
| **Pocket** | One hot row-slice of a model (`SELECT * ... WHERE ...`) | Yes | Used automatically to speed up queries |
| **Aggregate** | A grain summary (pre-grouped numbers) | Yes | Used automatically to speed up queries |

A Named Query can hold things a pocket or aggregate cannot, such as a grouped ranking with an `ORDER BY ... LIMIT 3`. It is a new kind of object, not a special pocket.

---

## The two shapes

Every Named Query has a **shape**, worked out automatically when the definition is validated:

| Shape | What it means | Example |
|---|---|---|
| **projection** | A slice of rows, each row whole. No grouping. | `SELECT * FROM modely WHERE branch_id = '3279863'` |
| **aggregated** | A grouped summary with totals, counts, rankings. | `SELECT city_name, SUM(transaction_value) AS total FROM modely GROUP BY city_name ORDER BY total DESC LIMIT 3` |

The shape decides which of the existing security checks Tessallite reuses when serving the stored table (see "Security" below).

---

## Creating a Named Query

A Named Query is governed model content: modellers (and above) can create it, everyone can read it. In v1 the authoring surface is the model-service API under
`/projects/{project_id}/models/{model_id}/named-queries`; the Model Builder drawer and a visual query builder follow in a later release.

The steps are the same whichever surface you use:

1. **Write the definition.** The definition is SQL against the model's logical surface — the model slug plus its measures and dimensions — exactly like a pocket definition. It is never raw database SQL: Tessallite compiles it to whichever database dialect is needed at refresh and query time, which is what keeps it portable across PostgreSQL, Redshift, and BigQuery.
2. **Validate.** Tessallite checks the definition binds to the model (every measure and dimension exists), derives the output-column list and the shape, and checks the caps. Anything invalid is rejected with the reason — never silently stored.
3. **Name it.** The name must be unique within the model, ignoring capitalisation, and must not clash with a model parameter or a Named List: all three share the `@` namespace, so `@region_filter` cannot be a parameter and a Named Query at the same time.
4. **Deploy the model.** A Named Query only reaches SQL users after the model is deployed. Editing a definition does not change what queries see until the next deploy.

### The validation rules

- The definition must be a single `SELECT` statement.
- It must not contain DML keywords (`INSERT`, `UPDATE`, `DELETE`, `DROP`, and so on) — defence in depth on top of the router's own read-only enforcement.
- It must not reference other `@` placeholders (a Named Query inside a Named Query would never end).
- The number of output columns is capped by the `named_query.max_columns` setting (default 200); the cap rejects the definition at authoring time — it never silently truncates columns.

---

## Refreshing (building the answer table)

A Named Query does not materialise itself. A refresh is what builds the stored answer table:

1. Tessallite takes a per-query lock so only one refresh runs at a time.
2. It re-validates the definition against the current deployed model (this is also how a changed model is detected).
3. It compiles the definition to the target database's dialect and builds the whole result table from scratch (full CTAS in v1 — incremental updates are a planned follow-up).
4. It counts the rows and checks the row cap (`named_query.max_rows`, default 100,000; a per-query override is allowed). If the answer is bigger than the cap, the table is dropped and the query is marked failed with `ROW_CAP_EXCEEDED` — rejected, never truncated.
5. It records the output-column manifest and stamps the build with the model version it was built for. Only then is the result marked `fresh`.

A refresh can be triggered manually (`POST /{id}/refresh`) or by a cron schedule stored on the refresh policy row (mirroring pocket schedules).

### Health states

| State | Meaning | What serving does |
|---|---|---|
| **fresh** | The stored table matches the deployed definition. | Serve the stored table (when security allows). |
| **stale** | The definition or its inputs changed since the last build. | Fall back to running the definition live. |
| **failed** | The last refresh failed, with the reason recorded. | Fall back to live; if live also fails, a clear error is returned. |
| **invalidating** | A refresh is running or was interrupted. | Never serve; a new refresh re-picks it after the recovery window. |

A refresh failure never breaks `SELECT * FROM @name` — the query simply runs live against the source, under the normal pipeline, until the next successful refresh.

---

## Using a Named Query in SQL

From any SQL channel (JDBC, REST, the query panel, Explorer), the whole statement is the reference:

```sql
SELECT * FROM @branch_3279863;
SELECT * FROM @top_three_transaction_cities;
```

That exact shape — `SELECT * FROM @Name` and nothing else — is the only one v1 accepts. A projection subset (`SELECT branch_id FROM @name`), a `WHERE` against it, a join, or a nested reference is rejected with a clear `NQ_UNSUPPORTED_SHAPE` error, so no query can silently mean something other than what it says.

Useful errors you may see:

| Error | What it means |
|---|---|
| `NQ_UNKNOWN_REFERENCE` | No Named Query with that name is deployed on the model. Check the spelling, or deploy. |
| `NQ_WRONG_TYPE` | The name belongs to a Named List, not a Named Query. Named Lists work inside `IN (...)`; Named Queries work as `SELECT * FROM @Name`. |
| `NQ_UNSUPPORTED_SHAPE` | The reference is not the exact whole-statement shape. |

---

## How serving decides: stored table or live?

When a user asks for `SELECT * FROM @name`, Tessallite runs the same decision every time:

1. Is there a stored table for this query, and is it `fresh`?
2. Was it built for the version of the model that is deployed right now?
3. Is it past its overdue safety limit (for scheduled queries)?
4. Does the existing security proof hold for THIS user?

If the answer to all four is yes, the stored table serves — fast, no source round-trip. If any answer is no, the definition is dispatched through the ordinary pipeline, exactly as if the user had typed the definition themselves. The user's security context applies in both paths, so a row-security-filtered user never sees more rows through a Named Query than they would see by hand.

---

## Security: reused, never reinvented

Named Queries add no new security mechanism. They borrow the two already-hardened proofs, chosen by shape:

- **Projection shape** reuses the pocket rules: the stored table serves a row-security-filtered user only when the definition is a plain row-slice, every security column is recorded in the stored table's manifest, and the same per-scan filter the live route would apply is injected on every read. Anything unproven falls back to live.
- **Aggregated shape** is more careful: pre-grouped numbers cannot be re-filtered afterwards without wrong answers, so a user with active row security gets the live path (re-aggregated under their own filter).
- A column-level-security (persona) restriction on the result always falls back to live in v1 — Tessallite will not quietly project columns out of a shared stored table.

The outcome: identical numbers whether the answer came from the stored table or from the live run — the same rows a hand-written query returns.

---

## Deploy semantics (why an edit does not change answers immediately)

A Named Query definition is part of the model's deployed snapshot, exactly like measures, dimensions, and Named Lists. Editing a definition changes the draft model; queries keep using the last deployed definition until you deploy again. Refreshes also re-validate against the deployed model, so a refresh always builds what queries will actually use.

---

## Limits and caps

| Cap | Default | Enforced when | Behaviour |
|---|---|---|---|
| `named_query.max_columns` | 200 | Create / validate | Definition rejected, never truncated. |
| `named_query.max_rows` | 100,000 | Refresh (counted on the real result) | Table dropped, marked `failed` with `ROW_CAP_EXCEEDED`, serving falls back to live. |

Both caps can be overridden per Named Query (`column_cap` / `row_cap`). The discipline is always **reject, never truncate**: a truncated answer would be a wrong answer.

---

## Worked scenarios

### Scenario 1 — a fixed slice of one branch

A regional analyst wants "everything about branch 3279863" as a dependable table. A modeller creates:

```sql
SELECT * FROM modely WHERE branch_id = '3279863'
```

Shape: projection. After deploy and refresh, anyone (including a BI tool) runs `SELECT * FROM @branch_3279863` and gets the stored table. A user whose row security allows only branch 4000001 gets the same rows they would get if they wrote the definition by hand — no more.

### Scenario 2 — a top-three ranking

A manager wants the three cities with the highest transaction totals, updated on a schedule. A modeller creates:

```sql
SELECT city_name,
       SUM(transaction_value)         AS sum_trx_values,
       COUNT(1)                        AS row_count
FROM   modely
GROUP  BY city_name
ORDER  BY sum_trx_values DESC
LIMIT  3
```

Shape: aggregated. After deploy and refresh, `SELECT * FROM @top_three_transaction_cities` serves the stored ranking. Users with active row security automatically get the live re-aggregation under their own filter, so the ranking always respects what they are allowed to see.

### Scenario 3 — source data changes between refreshes

The stored table is a photograph of the source. If the source changes and the schedule has not run yet, the stored answer is still the honest answer of the last refresh (with the overdue gate keeping it from drifting indefinitely past a missed schedule). To bring it up to date, run a refresh.

---

## Tips

- Keep definition SQL against the model's logical surface, never a hard-coded database name. The same Named Query then survives export/import to a different source and target, and the first refresh rebuilds it in the new dialect.
- Prefer short, specific names that say what the answer is (`@top_three_transaction_cities`, not `@query2`).
- Watch the health badge after a deploy that renamed a measure: refresh re-validates and will mark the query failed with the structured reason, while serving falls back to live until you fix the definition.
- An empty answer is a valid answer: `SELECT * FROM @name` returning zero rows is not an error.

---

## Before you start

- You need a deployed model with measures/dimensions to write the definition against, and modeller (or higher) access to author Named Queries.
- Materialising requires the model to have a query target configured; without one, refresh fails with a clear reason and serving always falls back to live.
- In v1 the surface is the API; the visual editor and health badge in Model Builder, and catalogue exposure to DAX/XMLA clients (Excel, Power BI), follow in later releases.

---

## Related

- [Named Lists](named-sets.html)
- [Configure Pocket Tables](configure-pocket-tables.html)
- [Configure Aggregates](configure-aggregates.html)
- [Configure Row Security](configure-row-security.html)
- [Column-Level Security](column-level-security.html)
- [Deploy a Model](deploy-a-model.html)
- [KPIs](kpis.html)
