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

## Personal Access Tokens (for single sign-on users)

If you sign in to Tessallite through single sign-on (SAML or OIDC), you do not have a password to type into a BI tool such as Excel or Power BI. Instead, generate a **Personal Access Token** and use it in place of the password.

**How to create one:**

1. Sign in to the Tessallite web app.
2. Open the account menu (top right) and choose **Personal Access Tokens**.
3. Click **Generate token**, give it a label (for example, "Excel on my laptop"), and optionally set an expiry.
4. Copy the token immediately. It is shown **once** and cannot be retrieved again. Store it somewhere safe, like a password manager.

**How to use one:**

- In Excel (Analysis Services / XMLA), Power BI (PostgreSQL connector on port 5433), or any JDBC/SQL client, enter your Tessallite **email** as the username and the **token** as the password.
- The token carries your own permissions. Revoking it, or deactivating your account, stops it working.

**Managing tokens:** the same page lists your tokens (label, a masked preview, when each was created, and when it was last used) and lets you **revoke** any token. A revoked or expired token is refused on its next connection; a BI session already open ends within a short window.

> A Personal Access Token is a secret, just like a password. Anyone who has it can query Tessallite as you until it is revoked or expires. Do not paste it into email, chat, or source code.

Password users can also generate a token — it is a convenient way to connect a BI tool without embedding your account password in a connection string.

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
