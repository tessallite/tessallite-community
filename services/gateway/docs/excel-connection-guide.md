# Excel XMLA Connection Guide - Troubleshooting

## Error: "Unable to connect to data source. Reason: Unable to locate database server."

This error means Excel cannot find or connect to the Tessallite server.

## Common Causes and Solutions

### 1. Incorrect URL Format

**Problem**: Excel expects a specific URL format for Analysis Services.

**What to use**:
```
http://localhost:8080/api/v1/xmla/
```
- ❌ Do NOT use: `http://localhost:8080/api/v1/xmla/demo`
- ❌ Do NOT use: `localhost:8080/api/v1/xmla/demo`
- ✅ Include the trailing slash: `/api/v1/xmla/`

### 2. Network/Firewall Issue

**Problem**: Excel cannot reach the server.

**Check**:
- Gateway is running: `curl http://localhost:8080/health`
- From command line: `netstat -an | grep 8080`
- From browser: Open `http://localhost:8080/health`

**Solution**:
- Ensure gateway is running: `docker-compose up -d gateway`
- Check Docker network: `docker network inspect infra_default`

### 3. Excel Session Authentication Issue

**Problem**: Excel establishes a session then doesn't send credentials on subsequent requests, causing 401 errors.

**Current behavior**: BasicAuthMiddleware requires Authorization header on every request.

**Workaround**: Try closing and reopening Excel after authentication.

## Verification Steps

### Step 1: Verify Gateway is Running

```bash
curl http://localhost:8080/health
# Should return: {"status":"ok","service":"gateway"}
```

### Step 2: Test XMLA Endpoint Directly

```bash
# Test MDSCHEMA_CATALOGS (should list "demo")
curl -X POST http://localhost:8080/api/v1/xmla/ \
  -H "Content-Type: text/xml" \
  -H "Authorization: Basic ZGVtbzpkZW1v" \
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

# Should return: <CATALOG_NAME>demo</CATALOG_NAME>
```

### Step 3: Check Gateway Logs

```bash
docker logs infra-gateway-1 2>&1 | tail -20
```

Look for:
- `[XMLA-SERVER]` entries - shows requests received
- HTTP status codes - should be 200 OK for successful requests
- Any error messages

## Excel Connection Instructions

### Option A: Use Server Endpoint (Recommended)

```
1. Excel → Data → Get Data → From Database → From Analysis Services

2. Server: http://localhost:8080/api/v1/xmla/
   Note: Include trailing slash

3. Click OK

4. User name: demo
5. Password: demo

6. Uncheck "Use Windows authentication"

7. Excel will show "demo" in catalog list

8. Click "Connect" (select "demo" catalog)
```

### Option B: Use Tenant Endpoint (Alternative)

If server endpoint doesn't work, try the tenant-specific endpoint:

```
Server: http://localhost:8080/api/v1/xmla/demo
Username: demo
Password: demo
```

## What to Check in Gateway Logs

### Successful Connection Pattern

```
[XMLA-SERVER] user='demo' <soap:Envelope>...
[XMLA-SERVER] resolved_tenant=None request_type=MDSCHEMA_CATALOGS
[XMLA-RESP] MDSCHEMA_CATALOGS rows=1 body[:4000]=<DiscoverResponse>...
INFO:     ... "POST /api/v1/xmla/ HTTP/1.1" 200 OK
```

### Error Pattern (401 Unauthorized)

```
INFO:     ... "POST /api/v1/xmla/ HTTP/1.1" 401 Unauthorized
```

If you see many 401 errors, Excel is not sending the Authorization header.

## Testing Checklist

- [ ] Gateway is running: `curl http://localhost:8080/health`
- [ ] Can access XMLA with curl from command line
- [ ] MDSCHEMA_CATALOGS returns "demo" catalog
- [ ] DISCOVER_PROPERTIES returns ServerName property
- [ ] No 401 errors in gateway logs (except initial auth)
- [ ] Excel shows catalog list with "demo"

## Quick Fix Attempts

### 1. Restart Gateway

```bash
docker-compose restart gateway
sleep 3
# Try Excel connection again
```

### 2. Rebuild Gateway

```bash
docker-compose build gateway --no-cache
docker-compose up -d gateway
sleep 3
# Try Excel connection again
```

### 3. Check Environment Variables

```bash
docker-compose exec gateway env | grep -E "XMLA_PORT|CORS"
```

Should see:
- XMLA_PORT=8080
- CORS_ORIGINS includes appropriate URLs

### 4. Try Different Browser/Tool

If Excel fails, try:
- Power BI Desktop: Has better XMLA handling
- Postman/Insomnia: Can test raw XMLA requests
- curl from command line

## Known Limitations

- Excel's session management may require Basic Auth on every request (current implementation)
- Windows Authentication is NOT supported (must use username/password)
- SSL/TLS not tested - may not work with https
- Excel Data Connection Wizard is strict about XMLA compliance

## Next Steps if Still Failing

1. Provide exact URL you entered in Excel
2. Screenshot of the error dialog
3. Gateway logs for the connection attempt
4. Your Windows version and Office version
