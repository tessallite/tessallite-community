# XMLA Connection Status

## Current Implementation (2026-03-31)

### Recent Fixes (2026-03-31)

1. **DISCOVER_DATASOURCES Returns Valid Datasource** - Excel requires at least one datasource to complete connection. Now returns "Tessallite" datasource pointing to XMLA endpoint.

2. **Complete Column Set in Rowsets** - All DISCOVER response rowsets now return all columns defined in `mdschema_config.json`, preventing Excel parsing failures

3. **Added Missing Columns**:

1. **Complete Column Set in Rowsets** - All DISCOVER response rowsets now return all columns defined in `mdschema_config.json`, preventing Excel parsing failures
2. **Added Missing Columns**:
   - `MDSCHEMA_CATALOGS`: Added `ROLES` column
   - `MDSCHEMA_CUBES`: Added `DESCRIPTION` column
   - `MDSCHEMA_DIMENSIONS`: Added `DIMENSION_CARDINALITY`, `DEFAULT_HIERARCHY`, `DESCRIPTION`, `IS_VIRTUAL`, `IS_READWRITE`, `DIMENSION_MASTER_UNIQUE_NAME` columns
   - `MDSCHEMA_MEASURES`: Added `MEASURE_IS_VISIBLE`, `DESCRIPTION`, `EXPRESSION` columns

### Authentication Setup

The BasicAuthMiddleware is active and configured with these credentials:

| Username | Password |
|----------|----------|
| `demo` | `demo` |
| `admin@demo.com` | `admin` |
| `alex` | `sup3rSecret` |
| `admin` | (from `SYSTEM_ADMIN_PASSWORD` env var, default `admin`) |

### Fixed Issues

1. **XML Normalization Disabled** - `XmlaAdapter.normalize_inbound()` now passes XML through unchanged to prevent malformed XML issues

2. **Case-Insensitive Tag Matching** - `_tag_matches()` and `_local_name()` functions handle mixed-case XML tags (e.g., `<PropertyName>` vs `<PROPERTY_NAME>`)

3. **DISCOVER_DATASOURCES Returns Empty** - Prevents Excel from treating XMLA as a database connection and expecting a database list

4. **BasicAuthMiddleware Active** - Enforces authentication on `/api/v1/xmla/*` paths

### Endpoint Configuration

- **Base URL**: `http://localhost:8080/api/v1/xmla/{tenant_slug}`
- **Authentication**: Basic Auth
- **Method**: POST (GET probe also supported for MSOLAP compatibility)

## Testing Excel Connection

### Connection Settings

When connecting from Excel Data → Get Data → From Database → From Analysis Services:

1. **Server name**: `http://localhost:8080/api/v1/xmla/demo` (or `demoorg` if that's your tenant)
   - Note: Include the full path, not just `localhost:8080`

2. **User name**: `demo`
3. **Password**: `demo`

4. **Use Windows authentication**: **Unchecked** (Windows Auth is not supported - use Basic Auth only)

   **Important**: Do NOT check "Use Windows authentication". This will always fail with "Access Denied" because Tessallite XMLA only supports Basic Auth (username/password).

### Expected Behavior

1. Excel should successfully authenticate with Basic Auth
2. DISCOVER_DATASOURCES returns "Tessallite" datasource (allows Excel to complete connection)
3. DISCOVER_PROPERTIES should return server properties
4. DISCOVER_SCHEMA_ROWSETS should list available rowsets
5. MDSCHEMA_CATALOGS should list the demo model/catalog

### Troubleshooting

If Excel shows "cannot obtain list of databases from specified source":

**This should now be fixed** - DISCOVER_DATASOURCES now returns a valid "Tessallite" datasource. If you still see this error, check the gateway logs for any errors.

If authentication fails:

1. Check the gateway logs for:
   - `[XMLA-AUTH]` messages showing authentication source
   - `[XMLA-RAW]` showing the raw XML request
   - `[XMLA-METHOD]` showing the request type (Discover/Execute)

2. Verify credentials:
   - User: `demo` / Password: `demo`
   - User: `admin@demo.com` / Password: `admin`

3. Check middleware is loaded:
   - `BasicAuthMiddleware` is added in `main.py` line 71

4. Verify endpoint path:
   - Should be `http://localhost:8080/api/v1/xmla/demo`
   - Middleware checks for `/api/v1/xmla/` prefix

### Known Limitations

- Excel treats DISCOVER_DATASOURCES as a database connection attempt - this is mitigated by returning empty results
- JWT exchange for Basic Auth is not yet implemented - all Basic Auth users use "anon" token in POC mode
- Demo tenant has special bypasses for development/testing

### Next Steps for Testing

1. Try connecting with `demo` / `demo` credentials
2. If successful, test the schema browser (adding tables from sources)
3. Monitor gateway logs for:
   - Request type: `[XMLA-METHOD] Discover`
   - Authentication: `[XMLA-AUTH] tenant='demo' source='basic'`
   - Response rows: `[XMLA-RESP] DISCOVER_SCHEMA_ROWSETS rows=...`

4. If still failing, check:
   - Exact error message from Excel
   - Gateway logs for `[XMLA-WARN]` or authentication errors
   - Network connectivity to `localhost:8080`
