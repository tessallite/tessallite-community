---
title: "Tessallite Features"
audience: all
area: getting-started
updated: 2026-05-25
---

## What this covers

A complete reference of every Tessallite feature, what it does, and where to find it in the product. Use this page to discover capabilities you may not know about, or as a checklist when evaluating the platform.

---

## Semantic Modelling

| Feature | Description | Where to use it |
|---|---|---|
| Multi-source connections | Connect PostgreSQL, BigQuery, Spark/Hive, SQL Server, Snowflake, and Redshift as data sources | Model Builder > Connections panel |
| Tables and aliases | Add source tables to a model; create dimension aliases when the same table needs different business roles | Model Builder > Sources panel |
| Joins | Define relationships between fact and dimension tables; one-fact-per-model rule enforced | Model Builder > Canvas (drag to connect) or Joins panel |
| Dimensions | Column-based attributes with display name, data type, folder, and hidden flag | Model Builder > Dimensions panel |
| Calculated dimensions | Expression-based dimensions computed at query time from SQL (CASE, COALESCE, UPPER, etc.) | Dimensions panel > Create > Expression tab |
| Dimension aliases | Same dimension mapped from multiple tables with unique names | Dimensions panel > Alias option |
| Measures | Aggregations (SUM, COUNT, AVG, MIN, MAX, DISTINCT COUNT) with format tokens (currency, percent, decimal) | Model Builder > Measures panel |
| Calculated measures | Expressions over other measures using `measure("name")`, `safe_div`, arithmetic, and numeric literals | Measures panel > Create calculated |
| Semi-additive measures | Measures that aggregate differently across time: LAST_NON_EMPTY, FIRST_NON_EMPTY, AVG_OF_CHILDREN, MIN, MAX | Measures drawer > semi_additive_behavior dropdown |
| Time intelligence variants | 14 canonical time calculations: lag, prior year/quarter/month/week, YTD/QTD/MTD/WTD, YoY growth, trailing N, moving average | Measures drawer > Variants (fx+ button) |
| Hierarchies | Four types (explicit, date_embedded, segment, inferred) with time units and level-based calculations | Model Builder > Hierarchies panel |
| Date hierarchy batch-create | Auto-create Year/Quarter/Month/Day hierarchies for unassigned date columns | Hierarchies panel > Create date hierarchies |
| Calendar tables | Six calendar types: Standard, Fiscal, ISO Week, Retail 4-4-5, Hijri, Thai Buddhist with auto-generation | Model Builder > Calendar table setup |
| Multi-calendar support | Use multiple calendar types in the same model (e.g., fiscal + standard + retail) | Calendar setup on each date hierarchy |
| Business glossary | Propose, approve, and publish business term definitions with download and public link | Model Builder > Glossary panel |
| Named Lists (Named Sets) | Reusable dimension member selections: fixed members, Top N, filtered, or Advanced MDX | Model Builder > Named Lists panel |
| KPIs | Value vs goal comparison with status (good/warning/poor) and trend (improving/flat/declining) | Model Builder > KPIs panel |
| KPI Scorecard | Live traffic-light dashboard of all KPIs with grouping by display folder | Model Builder > KPIs tab (bottom bar) |
| KPI Template Gallery | Pre-built KPI definitions across Financial, Customer, Operations, HR, Sales categories | KPIs panel > Templates button |
| Data tags | Group columns by sensitivity (PII, Financial, Internal Only) for governance and security | Model Builder > Data Tags panel |
| Model versioning | Save immutable snapshots of a model; compare, restore, or revert to any version | Model Builder > Save and Version |
| Multi-language metadata (i18n) | Translate dimension, measure, and hierarchy display names into multiple locales; locale selector in Explorer | Model Builder > Translations API; Explorer > Locale selector |
| Deploy and undeploy | Publish a model version to make it available to BI tools; undeploy to remove access | Model Builder > Deploy button |
| Model export/import (JSON) | Export a project or model as a portable JSON bundle; import with connection rebinding | Project menu > Export / Import |
| Model templates | Pre-built starter kits (E-Commerce, SaaS Metrics, Financial Reporting) auto-imported when creating a new model | Explorer > Create Model > Template picker |
| Model export/import (YAML) | Human-readable YAML format designed for Git version control with clean diffs | Project menu > Export YAML |
| dbt importer | Import dbt v1.7+ semantic_models and metrics; maps dimensions, measures, and entities | Project menu > Import from dbt |
| AtScale importer | Import AtScale SML projects (10 object types: models, datasets, dimensions, metrics, calculations) | Project menu > Import from AtScale |
| Cube importer | Import Cube.dev YAML projects (cubes, measures, dimensions, joins) | Project menu > Import from Cube |

## Query Execution

| Feature | Description | Where to use it |
|---|---|---|
| Transparent query routing | Queries are automatically routed to the fastest path: aggregate, pocket table, or live source | Automatic (visible via route badge) |
| Route visibility | Every query response shows which path served it (aggregate/pocket/source) and why | Query panel > Route badge |
| Force Live toggle | Bypass aggregates and pockets to execute directly against the source database | Query panel > Force Live toggle |
| Canvas undo/redo | Ctrl+Z / Ctrl+Y to undo and redo node position changes on the model canvas | Model Builder > Canvas toolbar Undo/Redo buttons |
| Measure Query Panel (pivot) | Multi-dimensional pivot surface: up to 3 row x 3 column dimensions with subtotals, grand totals, slicers | Model Builder > Pivot tab |
| Pivot conditional formatting | Color-scale, data-bars, and threshold-based cell formatting in the pivot grid | Pivot tab > Conditional Format controls |
| Drill-through | Click a cell to see the contributing fact rows with pagination and column selection | Measure Query Panel > Click cell > Drill drawer |
| Drill-through curation | Customize detail columns, joined dimensions, source-table override, and row limits per measure | Measures drawer > Drill-through sub-row |
| Query panel | Execute raw SQL against the semantic model with syntax validation and formatted results | Model Builder > Query tab |
| Parameterized filters | Named parameters (@region, @date_range) with five types: string, number, multi_value, date_range, boolean | Measures drawer > Parameters section |
| Multi-dialect transpilation | Canonical PostgreSQL SQL transpiled to BigQuery, Spark, SQL Server, Snowflake, and Redshift via sqlglot | Automatic (transparent to user) |
| Query result caching | Semantic-aware cache with per-model eviction; avoids re-executing identical queries | Automatic |
| Headless REST API | JSON-in, JSON-out query interface for apps and services that need governed metrics without writing SQL | `POST /query-router/api/v1/headless/query` |

## Aggregates and Optimisation

| Feature | Description | Where to use it |
|---|---|---|
| Aggregate creation | Create pre-computed acceleration tables with selected grain (dimensions) and measures | Model Builder > Aggregates panel > Create |
| Exact grain routing | Queries at the same grain as an aggregate route directly to the pre-built table | Automatic |
| Partial grain routing | Coarser queries re-aggregate from a finer-grained aggregate instead of hitting the source | Automatic |
| Multi-stat materialisation | One aggregate stores multiple stat types per measure (SUM, COUNT, MIN, MAX) for flexible re-aggregation | Automatic (during CTAS build) |
| Aggregate scheduling | Per-aggregate cron schedules for automated refresh; manual trigger available | Aggregates drawer > Schedule tab |
| Full refresh | DROP + CTAS rebuild of the entire aggregate table | Run a Refresh > Full |
| Incremental refresh | Watermark-based partial update: DELETE rows in the changed window, INSERT fresh data; multi-dialect via sqlglot transpilation | Run a Refresh > Incremental |
| Scheduler dependency chains | Define refresh ordering (B after A) with topological sorting and cycle detection | Aggregates panel > Dependencies |
| Predictive aggregates | Auto-probe sources, score candidates by hit_rate x row_reduction / cost, top-K auto-build | Aggregates panel > Predictive tab |
| AI optimiser | LLM-based analysis of miss patterns to suggest high-ROI aggregates | Aggregates panel > AI Optimiser |
| Cost/ROI advisor | Rank aggregates by net savings (storage + refresh compute vs query savings) | Aggregates panel > ROI chips |
| Aggregate lifecycle | Automatic events: created, validated, refreshed, retired (idle), evicted (over cap) | Model Health > Lifecycle panel |
| Idle retirement | Aggregates with zero hits over a configurable window are automatically retired | Scheduler > Idle retirement sweep |
| Pocket tables | Materialised model slices (`SELECT * FROM model WHERE predicate`) for filtered workloads | Model Builder > Pocket Tables panel |
| Pocket incremental refresh | Refresh only when the existing row key and safety checks pass and the captured deployed snapshot has complete watermark coverage for every table; under the current single-column contract, multi-table pockets always use a full CTAS rebuild (Bug-8745) | Pocket Tables drawer > Schedule tab |
| Pocket auto-suggestion | Optimizer analyses repeated WHERE predicates and suggests candidates within a size budget | Pocket Tables panel > Suggestions tab |

## Security and Access Control

| Feature | Description | Where to use it |
|---|---|---|
| Project-scoped RBAC | admin / modeler / viewer roles enforced per project; immediately revocable | Admin > Project Settings > Access |
| Model-scoped RBAC | Fine-grained access bindings at the individual model level | Admin > Project Settings > Model-level access |
| Row-level security | Per-model rules restrict rows by JWT role (predicate DSL) or user-mapping table lookup | Model Builder > Row Security panel |
| Column-level security | Persona tag restrictions block or auto-exclude sensitive columns across all query paths | Personas drawer > Tag restrictions |
| Personas | Named audience subsets with allow lists on measures, dimensions, hierarchies, and default filters | Model Builder > Personas panel |
| Persona row-security bypass | Elevated personas (finance, audit) can bypass RLS while respecting catalogue restrictions | Personas drawer > Bypass row security toggle |
| Per-persona aggregates | Aggregates and pockets scoped to a specific persona; precedence over global | Aggregates/Pockets panels > Persona badge |
| Data tags | User-defined sensitivity labels (PII, Financial) applied to columns for governance | Model Builder > Data Tags panel |
| Row security audit trail | Logged record of which security rules were applied to each query | Admin > Security Audit Trail |

## Authentication and Identity

| Feature | Description | Where to use it |
|---|---|---|
| Local authentication | Email/password with httpOnly cookie JWT and CSRF double-submit protection | Login page |
| SSO: SAML 2.0 | SP-initiated SSO for Auth0, Google Workspace, and generic SAML providers | Workspace Settings (project drawer) > Identity provider |
| SSO: OIDC | Authorization-code flow for generic OIDC providers | Workspace Settings (project drawer) > Identity provider |
| SSO group mappings | Map IdP groups to Tessallite project/model roles; sync on each login | Workspace Settings (project drawer) > SSO Mappings |
| LDAP authentication | Bind-and-search with configurable DN, filter, and attribute mapping | Workspace Settings (project drawer) > Identity provider (status only; host/filters are server settings) |
| GCP IAM OIDC | Google IAM backend with audience and domain validation | Workspace Settings (project drawer) > Identity provider (status only; audience is a server setting) |
| JIT user adoption | Auto-create local user record from IdP attributes on first SSO login | Automatic on SSO login |
| Embed tokens | Signed JWT tokens for third-party iframe embedding with scope (tenant, persona, model, capabilities) | Workspace Settings (project drawer) > Embed tokens; API: `POST /api/v1/auth/embed-token` |
| User management | Create, edit, delete users; reset password; promote/demote roles per tenant | Admin > Users tab |

## BI Tool Integrations

| Feature | Description | Where to use it |
|---|---|---|
| JDBC gateway | PostgreSQL wire protocol on port 5433 for DBeaver, Tableau, Superset, psycopg2 | BI tool > New connection > PostgreSQL |
| XMLA gateway | DAX/MDX endpoint on port 8080 for Excel and Power BI | Excel/Power BI > Analysis Services connection |
| Excel PivotTables | Grand totals, subtotals, multi-select, drill-down hierarchies, Show Values As, Timelines | Excel > Insert PivotTable > Tessallite connection |
| Excel calculated fields | User-defined measures via WITH MEMBER; session-scoped, round-trip safe | Excel PivotTable > Analyze > Calculated Field |
| Excel Add-in | Office.js task pane with Report Builder, Ask Tessallite chat, CUBE/KPI/Named List formula wizard, drill-through, stale detection, persona switcher | Excel > Insert > Get Add-ins > Tessallite |
| Power BI XMLA | DirectQuery and Import modes with aggregate routing and persona support | Power BI > Get Data > Analysis Services |
| Looker Studio / Data Studio direct | PostgreSQL connector path through the JDBC gateway; no LookML or Looker license required; live validation ready to execute | [Looker Studio Direct Connection](../integrations/looker-studio-connection-guide.md) |
| Optional Looker-hosted workflow | Generated LookML views/explores backed by governed gateway relations; deferred until a compatible Looker instance exists | [Optional Looker-hosted workflow](../integrations/looker-studio-via-looker-guide.md) |
| LookML emitter | Export-dialog ZIP download and offline CLI with model-hash drift detection; artifact generation does not require Looker access | [LookML Emitter](../integrations/lookml-emitter-guide.md) |
| Multi-catalogue exposure | Business, Technical, and Persona-scoped catalogues exposed via JDBC and XMLA metadata | BI tool > Schema/catalogue browser |
| BI compatibility matrix | Feature coverage grid for Excel, Power BI, DBeaver, Tableau, Superset, and Headless API | Help > BI Tool Compatibility Matrix |
| Supported data sources | PostgreSQL, BigQuery, Spark/Hive, SQL Server, Snowflake, Redshift | Model Builder > Connections panel |

Looker version 1 limitations: no persistent derived tables against Tessallite,
no bidirectional LookML import, no Looker Action API integration, and no
certification of optional Looker-hosted execution. Live Data Studio evidence
must be recorded before those integrations are described as validated.

## Conversational Agent

| Feature | Description | Where to use it |
|---|---|---|
| Agent Chat | Multi-turn conversational interface with SSE streaming, citations, and chart visualisations | Frontend > Agent Chat tab |
| Agent conversation export | Export chat conversations as Markdown or PDF with full turn history, queries, and results | Agent Chat > Export button |
| Agent stop button | Abort an in-progress SSE streaming response mid-turn; Stop button replaces Send during streaming | Agent Chat > Stop button (during streaming) |
| Multi-LLM support | Configure Claude, GPT, or Gemini as the agent's LLM provider per project | Admin > Project Agent > LLM Configuration |
| Agent session memory | Configurable history depth (default 20 turns); older turns truncated to conserve tokens | Admin > Project Agent > Advanced |
| Agent guardrails | Daily token budget, cost budget, and query complexity limits with refusal messages | Admin > Project Agent > Budget and limits |
| Agent cost tracking | Per-turn cost ledger with provider rates and daily/project totals | Admin > Project Agent > Cost reporting |
| Judge rubric | Custom evaluation rubrics for answer quality scoring; bulk import and eval runs | Admin > Project Agent > Judge rubrics |
| Glossary alias map | Business synonyms and abbreviations for entity resolution (e.g., ARR = Annual Recurring Revenue) | Admin > Project Agent > Glossary alias map |
| Cross-model recipes | Predefined multi-model calculation patterns (YoY, Ratio, etc.) the agent can execute | Admin > Project Agent > Cross-model recipes |
| Conversational Client | Standalone chat-first web app (React/Flask on port 3333) with ECharts visualisations; iframe-embeddable | `conversational-client/` (separate deployment) |
| Embed agent chat | Iframe-embeddable chat powered by signed embed tokens with scope and CORS control | Embed API > `/embed/chat` route |

## Monitoring and Observability

| Feature | Description | Where to use it |
|---|---|---|
| Query log | Filterable execution history with date, user, route type, status, and CSV export | Admin > Query Log |
| Miss log | Record of every query that fell back to the source because no aggregate matched | Model Health > Diagnostics > Miss logs |
| Usage analytics | Query volume (daily buckets), top measures, top aggregates, hit rate, average latency | Model Builder > Usage Analytics tab |
| Model Health dashboard | Alerts, invalid objects, refresh runs, optimizer runs, cold-start metrics in one view | Model Builder > Model Health tab |
| Data quality rules | Configurable validation rules (not_null, unique, range, regex, custom_sql) with warn/error severity | Model Health > Data Quality Rules |
| Schema drift detection | Automatic detection and notification of source schema changes | Model Health > Schema Changes panel |
| Model alerts | Dedup-aware alerts for structural breaks, refresh failures, optimizer failures, query fallbacks | Model Health > Alerts |
| Lineage visualisation | ReactFlow dependency graph: source > joins > dimensions > measures > aggregates | Model Builder > Lineage panel |
| Impact analysis | Downstream asset tagging and query-log scanning for column references | Model Builder > Impact panel |
| Email and Slack alerting | Notifications for refresh failure, schema drift, SLA breach, query spike, aggregate retirement | Admin > Alert Configuration |
| Prometheus metrics | 10 custom `tessallite_` metrics covering HTTP requests, query routing, model usage, and refresh runs | Monitoring stack (port 9090) |
| Grafana dashboard | 21-panel dashboard with Service Health, Query Routing, and Model Health sections; tenant/project/model filters | Monitoring stack (port 3001) |

## Administration

| Feature | Description | Where to use it |
|---|---|---|
| Workspace creation | Provision a new tenant with isolated database schemas | System Admin > Create Workspace |
| Project settings | Configure model limits, aggregate budgets, agent config, alert routes per project | Admin > Project Settings |
| Workspace settings | Auth backends, retention policies, email/Slack notification routes | Admin > Workspace Settings |
| Model configuration | Per-model overrides for aggregate cap, pocket budget, AI optimizer settings | Admin > Model Configuration |
| Audit logging | Configurable levels (off/critical/warn/info) for model CRUD, auth, and security events | Admin > Audit Log |
| Webhook events | Outbound webhooks with HMAC-SHA256 signing, retries, dead-letter queue, and secret rotation | Admin > Webhooks |
| Scheduler SLA | Declare expected refresh times and latency SLAs; automatic breach alerting | Admin > Scheduler SLA |
| System configuration | Database-backed settings editable from the UI; replaces most environment variables | System Admin > Configuration |

## Infrastructure and Deployment

| Feature | Description | Where to use it |
|---|---|---|
| Community local deployment | Signed website bundle with licence file, `.env` generation, migrations, demo seed, and Docker Compose startup | Download page > `install.sh` |
| GCP Cloud Run deployment | Scripted Cloud Run + Cloud SQL setup with domain mapping and budget alerts for managed/operator deployments | `deploy/gcp/deploy.bat` or `deploy.sh` |
| Docker Compose | Multi-service local stack with isolated network and easy teardown | Community bundle `docker-compose.yml` |
| Monitoring stack | Optional standalone Prometheus + Grafana in `monitoring/`; no impact on platform when stopped | `monitoring/deploy.sh` |
| Service proxy | All backend ports hidden behind nginx reverse proxy; only 3000, 3443, 5433, 8080 exposed | Frontend nginx |
| Connection pooling | Lazy asyncpg pool per (host, port, database, user) for source database queries | Automatic |
| Distributed locking | PostgreSQL advisory locks prevent double-runs on multi-instance deployments | Automatic (APScheduler jobs) |
| Health checks | `/health` endpoint on every service for Docker, Kubernetes, and Cloud Run probes | `GET /health` on each service |
| HTTPS with HSTS | nginx HTTP-to-HTTPS redirect with Strict-Transport-Security header; gated by environment variable | Frontend nginx (DISABLE_HTTPS_REDIRECT) |
| Rate limiting | Per-tenant request throttling (default 60 req/min) for service APIs; configurable per headless API | Automatic; configurable in system settings |

## API and Developer

| Feature | Description | Where to use it |
|---|---|---|
| REST API (model CRUD) | Full endpoints for projects, models, dimensions, measures, aggregates, pockets, personas, security rules | `GET/POST/PUT/DELETE /api/v1/...` |
| REST API (execution) | Query execution with route control, persona binding, and force-live option | `POST /api/v1/execute` |
| REST API (drill-through) | Fact-row drill with filter operators, pagination, and column selection | `POST /api/v1/measures/{id}/drill-through` |
| REST API (admin) | Webhooks, SLAs, audit log, user management, LLM config | `GET/POST /api/v1/admin/...` |
| API authentication | Bearer JWT tokens with per-tenant rate limiting | All API endpoints |
| Interactive API docs | Swagger/OpenAPI documentation with authenticated request tester | `http://localhost:8001/docs` |
| MCP server | Model Context Protocol server for Claude Desktop, Cursor, and Claude Code | `tessallite/mcp-server/` (standalone) |
| Embed API | Signed JWT tokens for third-party iframe embedding with scope and CORS control | `POST /api/v1/auth/embed-token` |
| Plugin execution API | Persona-scoped query execution endpoint for the Excel add-in and conversational client | `POST /api/v1/plugin/execute` |

---

## Related

- [What is Tessallite](what-is-tessallite.md)
- [How Tessallite Works](how-tessallite-works.md)
- [Architecture Overview](../system-admin/architecture-overview.md)
- [BI Tool Compatibility Matrix](../integrations/bi-compatibility.md)

---

<- [Connect Excel via XMLA](connect-excel.md) | [Home](../index.md) | [Choose Your Connection ->](../analyst-guides/choosing-your-connection.md)
