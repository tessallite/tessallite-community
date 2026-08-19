---
title: "Headless API"
audience: developer
area: integrations
updated: 2026-07-22
---

## What this covers

The headless API is for applications that need governed metrics but should not have to generate SQL. A mobile app can ask for `revenue` by `region`; an embedded dashboard can request a persona-safe KPI table; a backend service can fetch the same metric definition that Excel users see. Tessallite still applies the model, aggregate routing, personas, row security, and column restrictions.

Use this API when you are building a product integration. Do not use it to bypass BI tools for ordinary analysis, and do not connect your app directly to the source just because JSON feels easier. The business value is that every surface asks Tessallite the same governed question.

---

## When to use headless API vs JDBC/XMLA

| Use case | Recommended interface |
|---|---|
| BI tools (Excel, Power BI, Tableau, DBeaver) | JDBC or XMLA |
| Mobile apps | Headless API |
| Microservice-to-microservice integration | Headless API |
| Embedded analytics in a web app | Headless API |
| Ad-hoc SQL exploration | JDBC |
| Automated reporting scripts | Either (headless is simpler if you don't need SQL) |

The headless API and JDBC/XMLA share the same semantic layer, the same aggregate routing, and the same security model. The difference is the query language: headless uses JSON measure/dimension names; JDBC/XMLA use SQL or DAX.

Headless is deliberately narrower than SQL. It is excellent for stable app screens, scorecards, scheduled snapshots, and embedded tables. Use JDBC or the Query Panel when a human needs ad-hoc exploration with joins, expressions, or SQL-specific debugging.

---

## Authentication

The headless API uses the same JWT tokens as the Tessallite SPA. Obtain a token via the login endpoint:

```bash
TOKEN=$(curl -s -X POST http://host:3000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"acme-demo","email":"admin@acme-demo.com","password":"acme-demo"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
```

Include the token in all subsequent requests:

```
Authorization: Bearer $TOKEN
```

---

## Query endpoint

### POST /query-router/api/v1/headless/query

Resolves measure and dimension names against the semantic model, builds SQL via the routing pipeline (aggregate/pocket/source), executes, and returns rows.

**Request body:**

```json
{
  "project_id": "uuid",
  "model_id": "uuid",
  "measures": ["<measure name from /measures>"],
  "dimensions": ["<dimension name from /dimensions>"],
  "filters": [
    {"dimension": "<dimension name>", "operator": "eq", "value": "US"},
    {"dimension": "<time dimension name>", "operator": "between", "values": ["2025-01-01", "2025-12-31"]}
  ],
  "limit": 100,
  "offset": 0,
  "order_by": [{"field": "<measure or dimension name>", "direction": "desc"}]
}
```

**Use the exact semantic `name` from discovery.** `measures`, `dimensions`, `filters[].dimension`, and `order_by[].field` must be the `name` values returned by the `/measures` and `/dimensions` endpoints — not the human display names. For example a model may expose the measure `name` `Revenue` and the dimension `name` `region_code`; a request for `revenue` or `region` then returns 422 `Unknown column`. Always list measures and dimensions first (steps 2-3 of the worked example) and copy the `name` fields verbatim.

**Required fields:** `project_id`, `model_id`, `measures` (at least one). The `project_id` must be the project the model belongs to — a mismatch returns 403.

**Optional fields:** `dimensions`, `filters`, `limit`, `offset`, `order_by`, `persona_id`.

**Choosing a persona:** if your user is assigned more than one persona on the model, pass `persona_id` to say which one the query should run as. Users with a single assigned persona are locked to it automatically, and embed tokens always use the persona baked into the token — in both cases you can leave the field out. The persona's column restrictions and default filters are applied to every query, exactly as they are in the Query Panel and through BI tools.

**Filter operators:**

| Operator | Description | Value field |
|---|---|---|
| `eq` | Equals | `value` |
| `neq` | Not equals | `value` |
| `gt` | Greater than | `value` |
| `gte` | Greater than or equal | `value` |
| `lt` | Less than | `value` |
| `lte` | Less than or equal | `value` |
| `in` | In list | `values` (array) |
| `not_in` | Not in list | `values` (array) |
| `between` | Between (inclusive) | `values` (2-element array) |
| `like` | SQL LIKE pattern (you supply the `%`/`_` wildcards) | `value` |
| `not_like` | SQL NOT LIKE pattern | `value` |
| `is_null` | Is NULL | (none) |
| `is_not_null` | Is not NULL | (none) |

**Friendly aliases.** If your client already speaks a Cube-style filter vocabulary, the API accepts these spellings and converts them for you: `ne` and `notEquals` mean `neq`, `equals` means `eq`, `set` means `in`, `inDateRange` means `between`, `contains` becomes a `like` with the value wrapped in `%...%` (so `contains "VI"` matches "VISA"), and `notContains` becomes a `not_like` wrapped the same way. For the scalar operators you may also put the value in `values[0]` instead of `value`.

**Mistakes fail loudly.** An unknown operator, a scalar operator without a value, an empty `in` list, or a `between` without exactly two values returns 422 with a message listing what is accepted — the API never guesses and never runs a weaker filter than you asked for.

**Unknown fields are rejected.** The request body is strict: a misspelled or unrecognised top-level field (for example `dimentions` instead of `dimensions`, or `filterz` instead of `filters`) returns 422 naming the offending field. The API never silently ignores a field, so a typo can never quietly turn a filtered, grouped request into an unfiltered grand total.

**Response:**

```json
{
  "columns": ["region", "revenue", "order_count"],
  "rows": [
    {"region": "US", "revenue": 125000, "order_count": 430},
    {"region": "EU", "revenue": 98000, "order_count": 312}
  ],
  "page_row_count": 2,
  "total_rows": 2,
  "row_limit": 100,
  "has_more": false,
  "complete": true,
  "query_id": "a1b2c3d4e5f6g7h8",
  "route": {
    "route_type": "aggregate",
    "reason": "matched aggregate on region grain",
    "aggregate_id": "uuid",
    "pocket_id": null
  }
}
```

**Reading the response fields.**

- `page_row_count` is the number of rows in **this page** of the result — the count of objects in `rows`. It is not a total-matching-row count; the API does not run a separate `COUNT(*)`.
- `total_rows` carries the same value as `page_row_count`. It is kept only for wire back-compat with older clients and will be removed in a future version — prefer `page_row_count`.
- `row_limit` is the effective row cap the server applied to **this** request (your `limit`, capped at the hard maximum of 100,000).
- `has_more` is `true` when at least one more row matched beyond `row_limit`. The server detects this by fetching one extra row internally and trimming it, so you never have to request `limit + 1` yourself. When `has_more` is `true`, page forward with `offset` to retrieve the rest.
- `complete` is the inverse of `has_more`: `true` means every matching row is in this response. **Never treat a result as complete unless `complete` is `true`** — a capped extract with `complete: false` is a partial dataset, not the whole answer.
- `route` is a safe summary of how the query was served: `route_type` (`aggregate` / `pocket` / `source`), a human-readable `reason`, and the `aggregate_id` / `pocket_id` when one was used. It deliberately does **not** include the rewritten physical SQL — ordinary integration credentials do not see internal schema/table names or security predicates.
- `query_id` identifies **this exact page** of this query: it folds in `limit`, `offset` and `order_by`, so two different pages of the same query get different ids. It is safe to use as a result-cache key. It is a stable hash, not a server-side handle — you cannot fetch a result by replaying the id.

---

## Metadata endpoints

### GET /query-router/api/v1/headless/models

Lists all models accessible to the authenticated user's tenant.

```json
[
  {
    "id": "uuid",
    "project_id": "uuid",
    "slug": "sales-model",
    "display_name": "Sales Model",
    "description": "Revenue and order metrics",
    "deployed": true
  }
]
```

Only deployed models are listed — an undeployed model cannot be queried (it returns 409), so it is excluded from discovery. The `deployed` field is therefore always `true` in the current contract and is carried for forward-compatibility.

### GET /query-router/api/v1/headless/models/{model_id}/measures

Lists the measures in the specified model that your persona is allowed to see. If your user has more than one persona on the model, add `?persona_id=<uuid>` to pick one — the listing then matches exactly what that persona can query.

```json
[
  {
    "id": "uuid",
    "name": "revenue",
    "display_name": "Revenue",
    "description": "Total revenue",
    "format": "$#,##0",
    "aggregation_type": "sum",
    "variant_kind": null
  }
]
```

### GET /query-router/api/v1/headless/models/{model_id}/dimensions

Lists the dimensions in the specified model that your persona is allowed to see (same `?persona_id=` rule as measures). `data_type` is the source column's database type — useful for choosing sensible filter operators (for example, `between` for dates and numbers). Calculated dimensions without a source column report `null`.

```json
[
  {
    "id": "uuid",
    "name": "region",
    "display_name": "Region",
    "description": "Sales region",
    "is_time_dim": false,
    "data_type": "character varying"
  }
]
```

---

## Rate limiting

The headless API enforces a per-tenant rate limit (default: 100 requests per minute). When the limit is exceeded, the API returns:

```
HTTP 429 Too Many Requests
```

Successful (`2xx`) responses include the `X-RateLimit-Remaining` header showing how many requests remain in the current window; monitor it to implement client-side throttling. A `429` response instead carries a `Retry-After` header with the number of seconds to wait before retrying (it does not carry `X-RateLimit-Remaining`).

The rate limit is configurable via the `HEADLESS_RATE_LIMIT` environment variable. The same per-tenant limit and headers apply to the plugin execution endpoint (`/query-router/api/v1/plugin/execute`); both surfaces draw from one shared per-tenant budget.

**Multi-instance note.** The limiter counts requests per server process. When the query-router runs as more than one instance (for example, several Cloud Run replicas), each instance keeps its own counter, so the effective per-tenant ceiling is the configured limit multiplied by the number of running instances, and counters reset when an instance is recycled. The limit therefore protects the source from sustained floods but is not an exact per-tenant quota across a horizontally scaled deployment.

---

## Worked example: curl

```bash
# 1. Authenticate
TOKEN=$(curl -s -X POST http://localhost:3000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"acme-demo","email":"admin@acme-demo.com","password":"acme-demo"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

# 2. List models
curl -s http://localhost:3000/query-router/api/v1/headless/models \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# 3. List measures and dimensions for a model — copy the exact `name` fields
MODEL_ID="<model-uuid-from-step-2>"
curl -s "http://localhost:3000/query-router/api/v1/headless/models/$MODEL_ID/measures" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
curl -s "http://localhost:3000/query-router/api/v1/headless/models/$MODEL_ID/dimensions" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# 4. Query — substitute the measure/dimension `name` values from step 3
#    (they are semantic names, e.g. "Revenue" / "region_code", NOT display text)
curl -s -X POST http://localhost:3000/query-router/api/v1/headless/query \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"project_id\": \"<project-uuid>\",
    \"model_id\": \"$MODEL_ID\",
    \"measures\": [\"<measure-name-from-step-3>\"],
    \"dimensions\": [\"<dimension-name-from-step-3>\"],
    \"limit\": 10
  }" | python3 -m json.tool
```

---

## Pitfalls

- **Forgetting pagination.** Without `limit`, the API returns up to a hard cap of 100,000 rows — it does not stream an unbounded result set. For large datasets, always set an explicit `limit` and page with `offset`. Check the `complete` field on every response: when it is `false`, more rows matched than were returned and you must page forward with `offset` — never publish a `complete: false` result as the whole dataset.
- **Persona restrictions.** If the authenticated user's persona restricts certain columns, querying those measures or dimensions returns 403. Check the persona configuration if you get unexpected 403 errors.
- **Measure names are semantic names, not display names.** Use the metadata endpoint to discover the correct `name` field (e.g. `revenue`, not `Revenue`).

---

## Related

- [API Authentication](api-authentication.md)
- [API Reference](api-reference.md)
- [JDBC Connection Guide](jdbc-connection-guide.md)

---

← [API Reference](api-reference.md) | [Home](../index.md) | [Embed API →](embed-api.md)
