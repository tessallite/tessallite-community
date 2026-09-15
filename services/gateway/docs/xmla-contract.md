# XMLA Server Contract - Excel Compatibility

## Overview

This document defines the XMLA (XML for Analysis) server contract for Excel compatibility. The gateway implements a traditional SSAS-style endpoint that allows Excel to connect using the Analysis Services wizard.

## Key Contract: Server-Level Endpoint with Catalog Routing

### Endpoint Behavior

When the URL path ends with `/xmla/`, the gateway behaves like a traditional SSAS server:

1. **Single server URL**: `http://host:8080/api/v1/xmla/`
   - No tenant information in the URL
   - Matches Excel's expectation of a server endpoint

2. **Tenant selection via Catalog property**:
   - Excel sends Catalog property in XMLA Properties section
   - Gateway routes requests to the corresponding tenant
   - No URL path changes after initial connection

3. **Server-level discovery requests**:
   - `DISCOVER_PROPERTIES` → server capabilities
   - `DISCOVER_DATASOURCES` → single server datasource
   - `MDSCHEMA_CATALOGS` → list of all visible tenants
   - `DISCOVER_SCHEMA_ROWSETS` → available schema types

### Catalog Property Resolution

The Catalog property is the primary mechanism for tenant selection:

```xml
<Properties>
  <PropertyList>
    <Catalog>demo</Catalog>
  </PropertyList>
</Properties>
```

**Resolution Rules**:
- If Catalog is provided and valid → route to that tenant
- If Catalog is provided but invalid → return error
- If Catalog is empty (discovery) → list all visible tenants
- Default tenant fallback → "demo" (for Execute requests without Catalog)

**Case Sensitivity**: Catalog property values are case-sensitive but comparisons should be case-insensitive for robustness.

## SSAS-Compatible Capability Flags

The gateway returns these capability flags in DISCOVER_PROPERTIES to match SSAS expectations:

| Property | Value | Description |
|----------|--------|-------------|
| `MdpropMdxSubqueries` | `3` | MDX subquery support (3 = supported) |
| `DbpropMsmdSubqueries` | `3` | MSMD subquery capability |
| `DbpropMsmdOptimizeResponse` | `3` | Response optimization support |
| `MdpropMdxDrillFunctions` | `3` | Drillthrough function support |
| `MdpropMdxNamedSets` | `3` | Named set support |
| `MdpropMdxDdlExtensions` | `2` | DDL extension support |
| `ProviderType` | `MDP` | Provider type: MDP = OLAP/Analysis Services |
| `ProviderName` | `Microsoft Analysis Services` | SSAS recognition for Excel |

**Critical**: Values `15` or `9` for capability flags are incorrect and will cause Excel to downgrade features or fail connections.

## Request Flow

### Excel Connection Wizard Flow

```
1. User enters server: http://host:8080/api/v1/xmla/
   ↓
2. Excel sends: DISCOVER_DATASOURCES (no Catalog)
   Gateway returns: Single datasource with SERVER_NAME
   ↓
3. Excel sends: MDSCHEMA_CATALOGS (no Catalog)
   Gateway returns: List of all visible tenants
   ↓
4. User selects: "demo" from catalog list
   ↓
5. Excel sends: DISCOVER_PROPERTIES (with Catalog=demo)
   Gateway returns: Server capabilities for demo tenant
   ↓
6. Excel sends: MDSCHEMA_CUBES (with Catalog=demo)
   Gateway returns: Cubes for demo tenant
   ↓
7. Subsequent requests all include: <Catalog>demo</Catalog>
   Gateway routes: All requests to demo tenant
```

### Request Types and Catalog Handling

| Request Type | Catalog Required | Behavior |
|--------------|------------------|----------|
| `DISCOVER_PROPERTIES` | No (optional) | Returns server capabilities or tenant-specific if Catalog provided |
| `DISCOVER_DATASOURCES` | No | Always lists single server datasource |
| `MDSCHEMA_CATALOGS` | No | Lists all visible tenants |
| `MDSCHEMA_CUBES` | Yes | Returns cubes for specified Catalog |
| `MDSCHEMA_DIMENSIONS` | Yes | Returns dimensions for specified Catalog |
| `MDSCHEMA_MEASURES` | Yes | Returns measures for specified Catalog |
| `DISCOVER_SCHEMA_ROWSETS` | No | Returns all available schema types |
| `DISCOVER_CSDL_METADATA` | Yes | Power BI / Tabular: minimal CSDL envelope describing the catalog (entity type + measure/dimension properties) |
| `DISCOVER_CALC_DEPENDENCY` | Yes | Power BI / Tabular: conformant empty rowset (no Tabular calculation-dependency objects) |
| `EXECUTE` | No (optional) | Executes query; uses Catalog if provided |

## Canonical Member Identity Across Discovery and Execute

`MDSCHEMA_MEMBERS` and every `Execute` axis must emit the same
`MEMBER_UNIQUE_NAME` for the same regular member. Excel keys PivotCache members
by this value; a member returned on an axis under a different name is not the
member that Excel discovered, even when its caption and value match.

The grammar is selected by hierarchy shape, not by response path:

- A flat attribute hierarchy has one data level and uses
  `[Dimension].[Hierarchy].[stable-key]`.
- A multi-level hierarchy uses
  `[Dimension].[Hierarchy].[Level].&[ancestor-key]...&[stable-key]` so a child
  key repeated under different parents remains unambiguous.
- The All member remains `[Dimension].[Hierarchy].[All]`. Excel receives that
  identity as `ALL_MEMBER`, `DEFAULT_MEMBER`, the `(All)` level member, and every
  requested Execute rollup tuple. Its root `PARENT_UNIQUE_NAME` is XML null:
  absent in the optional Discover rowset cell and `xsi:nil="true"` in Execute.
- Captions are display text only and never replace the stable key in a unique
  name.

An unrestricted `MDSCHEMA_MEMBERS` request is structural. It returns measures
and the synthetic All member for each visible field, but it does not execute a
distinct-value query for every field. A hierarchy, level, or member restriction
identifies the one field whose governed distinct members are needed. A
`DIMENSION_UNIQUE_NAME` restriction triggers member loading only when it names
one field; the `[Dimensions]`, `[Hierarchies]`, and `[Time]` containing nodes
remain structural. This keeps member values available for filter and expand
operations without transferring high-cardinality fields during cube discovery.

The shared member-name producer in `dax/member_uname.py` owns this choice. Plain
axes, subtotal axes, slicers, existing-axis tuples, and `MDSCHEMA_MEMBERS` call
that producer rather than choosing independent wire grammars (Bug-9789).

### Native Excel All-member and rollup contract (D6)

For Excel, hierarchy discovery and Execute publish the same synthetic All
identity. A live A/B against the same checkout disproved the prior omission:
Save failed in both images, while the omission made Excel lose the server grand
total. The supported contract therefore advertises `ALL_MEMBER`, keeps it equal
to `DEFAULT_MEMBER`, and returns that identity only for source rows whose
requested grain is All. The All level caption is `(All)` and the data level uses
the business field caption.

The synthetic All member is a hierarchy root. `PARENT_UNIQUE_NAME` is NULL, not
an empty string: `MDSCHEMA_MEMBERS` omits the optional parent element, while an
Execute axis that declares the property emits
`<PARENT_UNIQUE_NAME xsi:nil="true"/>`. Regular members name the synthetic All
member or their actual ancestor. This distinction is part of the PivotCache
identity contract, not a presentation choice.

Workbook persistence also depends on one byte-identical regular-member identity
across discovery and Execute. `MEMBER_ORDINAL` is part of that identity and is
scoped to the member's level, not the whole hierarchy: the sole member of the
hidden All level has ordinal zero, and the first member of the visible data
level independently has ordinal zero. Subtotal axes must retain those per-level
ordinals even when All and regular members share one tuple stream. Workbook
persistence also depends on rejecting a failed required grain before Excel
receives a partial cube. A dimension-only field-add request has no measure
to query at the grand-total grain, so the gateway emits its one structural All
tuple without inventing source SQL.
For stacked flat row fields, Execute returns only the nested-prefix tree Excel
requested: detail rows, each visible inner-field subtotal, each visible outer-
field subtotal, and the grand total -- never a Cartesian subtotal cube. AVG and
other non-composable values come from their exact source grain; the gateway
does not fold leaf averages. If any required grain fails, Execute returns one
SOAP fault and no partial cube (Bugs 9244, 9789, 9837).

`TESSALLITE_XMLA_ALL_MEMBER=false` and
`TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL=true` remain explicit diagnostic or
emergency containment controls. Neither is the supported Excel contract or an
accepted persistence fix.

### Native KPI eligibility contract (D6)

`MDSCHEMA_KPIS` advertises only rows from the deployed model snapshot whose
complete measure lineage is visible to the active persona and whose
`KPI_VALUE` resolves to a non-empty, executable `[Measures]` member. The
Discover rowset and Execute's native KPI goal/status support-member path use
the same filtered set and the same member resolver; a draft, hidden-backed,
unresolved, or composite-only KPI is withheld rather than advertised with a
member that Execute cannot run (Bug-9830).

### Power BI / Tabular surface (Bug-5430)

Power BI Desktop and "Analyze in Excel" connect to the XMLA endpoint as a
Tabular model and probe the Tabular metadata surface:

- `DISCOVER_CSDL_METADATA` and `DISCOVER_CALC_DEPENDENCY` arrive as `Discover`
  requests and are dispatched through the standard discovery path.
- `$SYSTEM.TMSCHEMA_*` DMVs (`MODEL`, `TABLES`, `COLUMNS`, `MEASURES`,
  `HIERARCHIES`, `LEVELS`, `PARTITIONS`, `RELATIONSHIPS`) arrive as `Execute`
  statements (`SELECT ... FROM $SYSTEM.TMSCHEMA_<table>`). They are intercepted
  before MDX translation and answered from model metadata as a flat Rowset.
  Unknown TMSCHEMA tables return a conformant empty rowset.

These are minimally-conformant projections of the semantic model — enough for a
Power BI discovery sequence to succeed rather than hard-fail on an unrecognised
request type.

### Response compression and Cancel (Bug-5436b)

- **Compression:** when the client advertises `Accept-Encoding: gzip` or
  `deflate`, SOAP responses over ~512 bytes are compressed and tagged with
  `Content-Encoding` (and `Vary: Accept-Encoding`). Identity is used otherwise.
- **`<Cancel>`:** an `Execute` whose `Command` is `<Cancel>` is acknowledged with
  an empty-success `ExecuteResponse`. Tessallite runs each Execute synchronously
  to completion (no long-running cancellable server-side cursor), so the
  acknowledgement is the conformant minimal behaviour.

## Backward Compatibility

The old tenant-specific endpoint is retained for non-Excel clients:

```
/api/v1/xmla/{tenant_slug}  ← Power BI, API clients, testing
/api/v1/xmla/               ← Excel, SSAS-style clients
```

**Usage Guidelines**:
- Use `/xmla/` + Catalog property → Excel connections
- Use `/xmla/{tenant}` → Power BI, API clients, direct testing

## Authentication

### Auth Flow

1. **Initial request**: Excel sends without Authorization header
2. **Gateway response**: `401` with `WWW-Authenticate: Basic realm="Analysis Services"`
3. **Excel retry**: Sends Basic auth with user credentials
4. **Gateway validation**: Validates username/password against user store
5. **Subsequent requests**: Excel may or may not include auth header
6. **Session cache**: Gateway uses session ID to maintain auth state

### Auth Headers

**Required on 401 response**:
```http
WWW-Authenticate: Basic realm="Analysis Services"
```

**Single scheme only**: Only one `WWW-Authenticate` header should be returned (not multiple schemes like Basic + Negotiate).

### Connection Bursts and Authority Failures

Concurrent equivalent login, session-validation, and metadata misses are
coalesced within one gateway process. Metadata sharing is limited to the same
security principal and model/project scope. Failed or degraded results are not
cached. The coalescing and short-lived auth/metadata cache state is bounded,
process-local, and cleared on gateway restart; no timeout increase is part of
this behavior.

## Response Formats

### DISCOVER_DATASOURCES Response

```xml
<row>
  <DATASOURCE_NAME>Tessallite</DATASOURCE_NAME>
  <DATASOURCE_DESCRIPTION>Tessallite Semantic Aggregation Layer</DATASOURCE_DESCRIPTION>
  <URL>http://localhost:8080/api/v1/xmla/</URL>
  <DATASOURCE_INFO>Tessallite</DATASOURCE_INFO>
  <PROVIDER_NAME>Microsoft Analysis Services</PROVIDER_NAME>
  <PROVIDER_TYPE>MDP</PROVIDER_TYPE>
  <AUTHENTICATION_MODE>Authenticated</AUTHENTICATION_MODE>
</row>
```

### MDSCHEMA_CATALOGS Response

`CATALOG_NAME` is the combination that is actually unique — tenant, project,
model, and the persona for a persona view — joined by `__` (Bug-9825). A model
slug is unique only WITHIN a project and a project slug only within a tenant, so
a bare model slug cannot be a catalog identity: two accessible projects that
both contain a `sales` model published the same catalog name twice, and
resolution returned whichever came first.

```xml
<row>
  <CATALOG_NAME>acme__alpha__sales</CATALOG_NAME>
  <DESCRIPTION>Sales</DESCRIPTION>
  <ROLES></ROLES>
  <DATE_MODIFIED>2026-03-30T00:00:00</DATE_MODIFIED>
</row>
```

A persona view is a catalog of its own, `acme__alpha__sales__technical`, with the
persona label in the description. `MDSCHEMA_CUBES` sets `CUBE_NAME` to the
catalog name and therefore publishes the same string — which is also what gives
a client's cube list a visible persona viewpoint to pick. The two rowsets are
pinned to agree by
`test_bug9825_catalog_naming.py::TestCubesAgreeWithCatalogs`.

The separator is `__`, matching the convention the JDBC catalogue already uses
to qualify a colliding relation (`project_slug__name`).

Accepted on the way in, in order:

1. the qualified name — always resolves to exactly one model. Matched by
   rebuilding each accessible model's name rather than by splitting the string,
   because a slug may itself contain the separator;
2. a bare model UUID — legacy, unchanged;
3. an unqualified slug or display name — accepted ONLY when exactly one
   accessible deployed model matches, so workbooks saved before this change keep
   working. When more than one matches, the request is refused with a SOAP fault
   naming the collision. It is never resolved by list order.

A name whose model half matches but whose persona half does not resolves to
nothing rather than falling back to the unrestricted base view.

`DBLITERAL_CATALOG_NAME` advertises a maximum length of 100. The previous value,
24, was copied from OlaPy and was never true of this product — a
`<slug>_<persona-slug>` catalog passes it easily, and a qualified name is longer
still.

## Session Management

### Session Lifecycle

1. **BeginSession**: Excel sends `<BeginSession>` in SOAP Header
2. **Gateway response**: Returns new SessionId in SOAP Header
3. **Subsequent requests**: Excel echoes SessionId in SOAP Header
4. **Gateway validation**: Validates SessionId exists in cache
5. **Session termination**: Not explicitly handled; sessions expire on gateway restart

### Session Cache

- Storage: JSON file on disk (`services/gateway/src/dax/session_store.py`).
  Default path `/tmp/xmla_sessions.json`; overrideable via the
  `XMLA_SESSION_CACHE_PATH` env var for tests.
- Key: SessionId (UUID).
- Value: `{"token": <jwt>, "last_used_at": <unix-seconds>}`.
- Scope: Single gateway instance, but the file survives
  `docker restart` so Excel sessions survive a quick container
  bounce without re-authenticating.
- Persistence: Written atomically via tempfile + os.replace.
  Entries older than `SESSION_TTL_SECONDS` (3 hours) are pruned
  on every save — no background job needed.

When a resumed session reaches the session authority, a temporary authority
failure returns HTTP `503` with a `Retry-After` header and retains the XMLA
session. An invalid or revoked credential/session remains HTTP `401` and clears
the session.

## Error Handling

### 400 Bad Request

- Invalid XML format
- Missing required elements
- Invalid request type

### 401 Unauthorized

- Missing or invalid Authorization header
- Invalid credentials
- Session not found in cache

Invalid or revoked authentication also returns `401` and clears the resumed
session.

### 503 Service Unavailable

- Temporary session-authority failure
- `Retry-After` tells the client when to retry
- The resumed XMLA session is retained

### 500 Internal Server Error

- Unhandled exceptions
- Downstream failures that are not classified as temporary session-authority failures
- Database errors

## Testing

### Integration Test Coverage

See `tests/test_excel_flow.py` for automated tests covering:
- DISCOVER_PROPERTIES response validation
- DISCOVER_DATASOURCES server endpoint
- MDSCHEMA_CATALOGS tenant listing
- BeginSession session establishment
- Catalog property routing

### Manual Testing

```bash
# Test catalog discovery
curl -X POST http://localhost:8080/api/v1/xmla/ \
  -H "Authorization: Basic ZGVtbzpkZW1v" \
  -H "Content-Type: text/xml; charset=utf-8" \
  -d '<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>MDSCHEMA_CATALOGS</RequestType>
      <Restrictions><RestrictionList/></Restrictions>
      <Properties><PropertyList/></Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>'

# Test tenant-specific request
curl -X POST http://localhost:8080/api/v1/xmla/ \
  -H "Authorization: Basic ZGVtbzpkZW1v" \
  -H "Content-Type: text/xml; charset=utf-8" \
  -d '<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>MDSCHEMA_CUBES</RequestType>
      <Properties><PropertyList><Catalog>demo</Catalog></PropertyList></Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>'
```

## Future Enhancements

1. **User permission filtering**: Query actual tenants visible to user (vs hardcoded list)
2. **Session persistence**: Store sessions in Redis or database for restart resilience
3. **Multi-tenant ACL**: Enforce per-user access permissions
4. **Tenant metadata**: Include tenant descriptions, quotas, permissions
5. **Performance**: Cache tenant list and catalog responses
6. **Metrics**: Track connection success rates, common error patterns

## References

- [Microsoft XMLA for Analysis Specification](https://learn.microsoft.com/en-us/analysis-services/xmla/xml-for-analysis-xmla-reference)
- [Excel Data Connection Wizard Requirements](https://support.microsoft.com/en-us/office/connect-to-an-analysis-services-database-9a9c3e90-3c2b-4b1b-8d24-2c3e3b4f7e6)
- [SSAS Provider Behavior](https://learn.microsoft.com/en-us/analysis-services/client-libraries-md?)
