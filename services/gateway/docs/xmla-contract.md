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

```xml
<row>
  <CATALOG_NAME>demo</CATALOG_NAME>
  <DESCRIPTION>Demo tenant</DESCRIPTION>
  <ROLES></ROLES>
  <DATE_MODIFIED>2026-03-30T00:00:00</DATE_MODIFIED>
</row>
```

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

## Error Handling

### 400 Bad Request

- Invalid XML format
- Missing required elements
- Invalid request type

### 401 Unauthorized

- Missing or invalid Authorization header
- Invalid credentials
- Session not found in cache

### 500 Internal Server Error

- Unhandled exceptions
- Downstream service failures
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
