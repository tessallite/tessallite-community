---
title: "API Authentication"
audience: modeller
area: Integrations
updated: 2026-04-17
---

## What this covers

The Tessallite REST API uses HTTP Basic authentication on every request. There are no session tokens, API keys, or OAuth flows.

---

## How authentication works

Each request must include an `Authorization` header with HTTP Basic credentials: a Tessallite username (email address) and password, base64-encoded.

**Base URL**: `http://HOST:3000/api/v1`

Most HTTP clients (curl, Python requests, Postman) handle the encoding automatically.

> HTTP Basic auth transmits credentials as base64, not encrypted. Use HTTPS in all production environments.

---

## Permissions

| Role | API access |
|------|-----------|
| System Admin | Full access to all endpoints, including workspace management |
| Tenant Admin | Project and workspace-level endpoints within their tenant |
| Modeller | Project, model, and aggregate endpoints |
| Analyst / Viewer | Read-only access; no administrative endpoints |

The `ADMIN_USER` / `ADMIN_PASS` credentials have full access to all endpoints.

---

## Example: curl

```
curl -u username:password http://HOST:3000/api/v1/health
```

---

## Example: Python requests

```python
import requests

response = requests.get(
    "http://HOST:3000/api/v1/health",
    auth=("user@example.com", "yourpassword")
)
print(response.json())
```

---

## Common authentication errors

| HTTP status | Meaning | Resolution |
|-------------|---------|------------|
| `401 Unauthorized` | No credentials or wrong credentials | Verify email and password; confirm Authorization header is sent |
| `403 Forbidden` | Credentials correct but role insufficient | Use an account with the required role |

---

## Security recommendations

- Always use HTTPS in production.
- Do not store credentials in source code. Use environment variables or a secrets manager.
- Use a dedicated service account with the minimum required role for API automation.
- Rotate the service account password if it may have been exposed.

---

## Related

- [API Reference](api-reference.md)
- [Supported Data Sources](supported-data-sources.md)
- [Common Errors](../troubleshooting/common-errors.md)

---

← [Supported Data Sources](supported-data-sources.md) | [Home](../index.md) | [API Reference →](api-reference.md)
