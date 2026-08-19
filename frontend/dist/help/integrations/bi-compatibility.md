# BI Tool Compatibility Matrix

**Audience:** analyst | **Updated:** 2026-05-25

## Overview

Tessallite exposes two connection protocols: a **PostgreSQL wire protocol** gateway (JDBC, port 5433) and an **XMLA/DAX** endpoint (HTTP, port 8080). Most BI tools connect through one of these two protocols. This page lists supported tools, the protocol each uses, and the feature coverage for each.

## Protocol summary

| Protocol | Port | Query language | Best for |
|---|---|---|---|
| **JDBC (PostgreSQL wire)** | 5433 | SQL | DBeaver, Tableau, Superset, pgAdmin, Python (psycopg2), any JDBC/ODBC client |
| **XMLA/DAX** | 8080 | DAX, MDX | Excel PivotTable |

## Compatibility matrix

| Tool | Protocol | Connect | Browse schema | Query | Aggregates | Personas | Row security | Drill-through | Guide |
|---|---|---|---|---|---|---|---|---|---|
| **Microsoft Excel** | XMLA | Pass | Pass | Pass | Pass | Pass | Pass | Pass | [Guide](excel-xmla-connection-guide.md) |
| **Power BI Desktop** | JDBC (PostgreSQL) | Pass | Pass | Pass | Pass | N/A | N/A | N/A | [Guide](powerbi-connection-guide.md) |
| **DBeaver** | JDBC | Pass | Pass | Pass | Pass | N/A | N/A | N/A | [Guide](jdbc-connection-guide.md) |
| **Tableau Desktop** | JDBC | Pass | Pass | Pass | Pass | N/A | N/A | N/A | [Guide](jdbc-connection-guide.md) |
| **Apache Superset** | JDBC | Pass | Pass | Pass | Pass | N/A | N/A | N/A | [Guide](jdbc-connection-guide.md) |
| **pgAdmin / psql** | JDBC | Pass | Pass | Pass | Pass | N/A | N/A | N/A | [Guide](jdbc-connection-guide.md) |
| **Python (psycopg2)** | JDBC | Pass | Pass | Pass | Pass | N/A | N/A | N/A | [Guide](jdbc-connection-guide.md) |
| **Headless REST API** | HTTP | Pass | Pass | Pass | Pass | Pass | Pass | N/A | [Guide](headless-api.md) |
| **Looker Studio / Data Studio (direct)** | JDBC | Ready for live validation | Ready for live validation | Ready for live validation | Internal | Pending live | Pending live | N/A | [Guide](looker-studio-connection-guide.md) |
| **Optional Looker-hosted workflow** | JDBC + LookML | Deferred | Internal | Internal | Internal | Deferred | Deferred | Internal | [Guide](looker-studio-via-looker-guide.md) |

## Feature notes

### Aggregates

Aggregate routing is transparent. When a query matches a pre-built aggregate, the gateway rewrites the query to read from the aggregate table instead of the source. The BI tool does not need to be aware of aggregates.

### Personas

Persona catalogues are available over XMLA and JDBC as
`<model_slug>_<persona_slug>` relations, and the Headless API accepts its
governed persona context. A client must select the intended persona catalogue
or use the API context; a plain base JDBC relation is not implicitly scoped to
a persona.

### Row security

Row-level security is enforced server-side when a query is bound to its
applicable rule/persona context, including persona-catalogue JDBC queries.
Clients cannot bypass an applied rule by changing BI tools.

### Drill-through

Drill-through (clicking a PivotTable cell to see the underlying detail rows) is supported in Excel via the XMLA endpoint. JDBC clients, including Power BI over the PostgreSQL connector, can achieve the same result by querying with the appropriate WHERE clause.

## Choosing a protocol

- **Excel users** should connect via XMLA. This gives full PivotTable functionality, persona support, row security, and drill-through.
- **Power BI users** should connect via the PostgreSQL connector on port 5433 in DirectQuery mode. Power BI Desktop's Analysis Services connector only supports Windows authentication, so it cannot use the XMLA endpoint; the PostgreSQL route keeps every query inside the Tessallite semantic layer.
- **Tableau, Superset, DBeaver** and other SQL-based tools should connect via JDBC. They get SQL access to all published models with transparent aggregate routing.
- **Looker Studio / Data Studio** uses its direct PostgreSQL route for relational reports and does not consume LookML. This route does not require `LOOKER_GATEWAY_ENABLED`.
- **Optional Looker-hosted execution** uses generated LookML and the TLS JDBC gateway; enable `LOOKER_GATEWAY_ENABLED` only when a compatible Looker instance is available for controlled validation.
- **Programmatic access** (scripts, pipelines) should use the Headless REST API for the most control, or JDBC for standard SQL integration.

## Limitations

- JDBC clients see a flat relational view. Hierarchies and measure formatting defined in the semantic model are not exposed over the PostgreSQL wire protocol.
- XMLA connections require models to be deployed (published). JDBC connections also require deployment.
- Concurrent connection limits depend on the gateway configuration and deployment scale.
- Direct Data Studio live-product validation is still pending recorded evidence. The optional generated-LookML relation surface is default-off and rejects unsupported symmetric-aggregate window shapes instead of executing uncertain results.

---

[Previous: Power BI Connection Guide](powerbi-connection-guide.md) | [Home](../index.md) | [Next: Headless API](headless-api.md)
