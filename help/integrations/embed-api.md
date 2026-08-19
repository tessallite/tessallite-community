---
title: "Embed API"
audience: developer
area: Integrations
updated: 2026-06-21
---

## What this covers

The Embed API lets third-party applications embed Tessallite's conversational agent or query capabilities inside their own products. Instead of requiring users to log in to Tessallite directly, the host application requests a scoped, time-limited token from its backend, then passes it to the embedded component. The end user never sees Tessallite credentials.

---

## How embedding works

The flow has three participants: the ISV backend (your server), the Tessallite API, and the end user's browser.

1. The ISV backend authenticates to Tessallite as a tenant admin and calls `POST /api/v1/auth/embed-token`.
2. Tessallite returns a signed JWT with restricted scope (tenant, persona, models, capabilities, expiry).
3. The ISV backend passes the token to the browser (in page props, a cookie, or via a server-rendered attribute).
4. The browser-side embed component (iframe or React component) uses the token for all Tessallite API calls.
5. When the token nears expiry, the component fires a callback; the ISV backend requests a fresh token.

No Tessallite login page is shown. The end user's identity comes from the `user_identity` field in the token request, which appears in Tessallite's audit log.

---

## Requesting an embed token

**Endpoint:** `POST /api/v1/auth/embed-token`

**Authentication:** Requires a tenant admin or system admin JWT (via cookie or Bearer header).

**Request body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `tenant_id` | string | yes | - | Target tenant slug |
| `user_identity` | string | yes | - | Display name for audit trail |
| `persona_id` | string | no | null | Lock to this persona's permissions |
| `project_ids` | string[] | no | null | Restrict access to these project IDs only |
| `model_ids` | string[] | no | null | Restrict visible models |
| `capabilities` | string[] | no | `[]` (deny-all) | Allowed features: `query`, `chat`, `explore`. Omit or pass `[]` for no capabilities. |
| `expiry_minutes` | integer | no | 180 | Token lifetime (min 5, max 1440) |

**Response:**

```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "expires_at": "2026-05-19T15:30:00+00:00",
  "scope": {
    "tenant_id": "acme-demo",
    "user_identity": "viewer@customer.com",
    "persona_id": null,
    "project_ids": null,
    "model_ids": null,
    "capabilities": ["query", "chat", "explore"],
    "expiry_minutes": 180
  }
}
```

---

## Scope fields

**`persona_id`** locks the session to a specific persona. All row-level security filters and measure/dimension access rules for that persona apply. If omitted, the embedded user sees the default (unfiltered) view.

**`project_ids`** restricts which projects the embedded session can access. API calls targeting a project not in the list return 403. The embedded chat only loads and displays projects that appear in this list. If omitted, all projects in the tenant are accessible (subject to normal RBAC).

**`model_ids`** restricts which models the token can access. Any query or chat request targeting a model not in the list returns 403. If omitted, all models in the tenant are accessible.

**`capabilities`** controls which features the embed can use:
- `query` — execute semantic queries via the query-router
- `chat` — use the conversational agent
- `explore` — browse model metadata (dimensions, measures, hierarchies)

If omitted or an empty list is sent, **no** capabilities are granted (deny-all). Pass an explicit list such as `["query", "chat", "explore"]` to enable features. If a capability is excluded, API calls to the corresponding service return 403. This lets you embed only the chat, without exposing the query panel.

---

## Token lifetime

The default token lifetime is 3 hours (180 minutes), long enough for demos and presentations without interruption. The maximum is 24 hours (1440 minutes). The minimum is 5 minutes.

For production embedding, set the expiry shorter (30-60 minutes) and implement token refresh via the `onTokenExpiring` callback in the embed component.

---

## Revoking a token early

A token normally stays valid until it expires. If a token leaks, or you simply want to cut a session short before its expiry, you can revoke it:

**Endpoint:** `DELETE /api/v1/auth/embed-token/{jti}`

The `{jti}` is the token's unique ID — the `jti` claim inside the signed JWT you minted. Only a tenant admin may revoke. Once revoked, the token stops working within about a minute (the platform refreshes its revocation list roughly once a minute), and every query or chat call made with it is rejected as unauthorised (401). The revocation is recorded in the Audit Log as `embed_token.revoked`.

A revocation only applies to tokens belonging to your own tenant, so naming a token id that is not yours does nothing. If you are signed in as a **system administrator** — whose account is not itself inside any tenant — add `?tenant_id=<tenant-slug>` to say which tenant's token you are revoking; without it the request is rejected rather than quietly having no effect.

The platform deletes the revocation record automatically once the token would have expired anyway (at most 24 hours, the maximum token lifetime), so the blocklist does not grow without bound.

Revocation fails safe: if the platform cannot confirm a token is still valid, it refuses the request rather than letting it through. Build a revoke call into your "sign out" or "rotate credentials" flow so a leaked link can be cut off immediately rather than waiting out its expiry.

---

## CORS configuration

Embedded components load from the ISV's domain. The Tessallite backend must allow cross-origin requests from that domain.

Set the `ALLOWED_EMBED_ORIGINS` environment variable (comma-separated list of allowed origins):

```
ALLOWED_EMBED_ORIGINS=https://app.customer.com,https://portal.customer.com
```

This is merged with the standard `CORS_ORIGINS` setting. Both model-service and agent-service read it.

---

## Example: Python

```python
import requests

TESSALLITE_URL = "http://localhost:3000"

# 1. Log in as tenant admin
login = requests.post(f"{TESSALLITE_URL}/api/v1/auth/login", json={
    "tenant_id": "acme-demo",
    "email": "admin@acme-demo.com",
    "password": "acme-demo",
})
cookies = login.cookies

# 2. Request an embed token
resp = requests.post(
    f"{TESSALLITE_URL}/api/v1/auth/embed-token",
    json={
        "tenant_id": "acme-demo",
        "user_identity": "viewer@customer.com",
        "capabilities": ["chat"],
        "expiry_minutes": 60,
    },
    cookies=cookies,
)
embed_token = resp.json()["token"]

# 3. Pass embed_token to the browser-side component
```

## Example: curl

```bash
# Get admin session cookie
curl -s -c cookies.txt -X POST http://localhost:3000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"acme-demo","email":"admin@acme-demo.com","password":"acme-demo"}'

# Request embed token
curl -s -b cookies.txt -X POST http://localhost:3000/api/v1/auth/embed-token \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "acme-demo",
    "user_identity": "demo-viewer",
    "capabilities": ["query", "chat"],
    "expiry_minutes": 180
  }'
```

---

## Security considerations

- Embed tokens carry no password — they are signed JWTs with scoped claims. Treat them as bearer tokens: anyone with the token can use it until it expires **or until you revoke it** (see *Revoking a token early* above). If a token leaks, revoke it immediately rather than waiting for it to expire.
- Never expose the admin credentials used to mint embed tokens in client-side code. The minting call must happen server-side.
- Use HTTPS in production. Embed tokens transmitted over plain HTTP can be intercepted.
- Set `ALLOWED_EMBED_ORIGINS` to specific domains in production. Do not use `*` outside of development.
- The `user_identity` field is logged in the audit trail. Use a meaningful identifier (email, employee ID) so admin can trace embedded usage.

---

## Related

- [API Authentication](api-authentication.md)
- [API Reference](api-reference.md)
- [Headless API](headless-api.md)

---

← [Headless API](headless-api.md) | [Home](../index.md) | [Embed Agent Chat →](embed-agent-chat.md)
