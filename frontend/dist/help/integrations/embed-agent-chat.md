---
title: "Embed Agent Chat"
audience: developer
area: Integrations
updated: 2026-05-19
---

## What this covers

This article explains how to embed Tessallite's conversational agent inside a third-party web application. The embedded chat lets end users ask questions about semantic models without leaving the host application. It builds on the [Embed API](embed-api.md), which handles token minting and scope control.

Embedding uses a lightweight iframe that loads the Tessallite conversational client at `/embed/chat`. The host application mints a scoped embed token server-side, passes it to the iframe via a URL parameter, and handles token refresh when the token nears expiry.

---

## Architecture

The embed flow involves three participants:

1. **ISV backend** — authenticates to Tessallite as a tenant admin, mints an embed token via `POST /api/v1/auth/embed-token`, and passes it to the browser.
2. **Host page** — renders an iframe pointing at the Tessallite conversational client's `/embed/chat` route with the token as a URL parameter.
3. **Embedded chat** — authenticates using the embed token, loads the scoped project, and renders a full-screen chat interface with no login page, navigation bar, or sidebar.

The embedded chat communicates with the host page using `postMessage` for token refresh. No Tessallite credentials are exposed to the end user.

---

## Embedding with an iframe

The simplest integration is a single iframe element:

```html
<iframe
  id="tessallite-chat"
  src="https://chat.tessallite.example.com/embed/chat?token=eyJhbGci..."
  style="width: 100%; height: 600px; border: none; border-radius: 8px;"
  allow="clipboard-write"
></iframe>
```

The `?token=...` parameter is the embed JWT returned by the embed token endpoint. The iframe renders the chat interface immediately — no login page is shown.

### Sizing

The embedded chat is responsive and fills its container. Set the iframe dimensions to fit the host layout. A minimum height of 400px is recommended; 600-800px provides a comfortable experience.

### Sandbox attributes

If your content security policy requires `sandbox` on iframes, include at minimum:

```html
<iframe sandbox="allow-scripts allow-same-origin allow-forms" ...></iframe>
```

`allow-same-origin` is required for the embed token to be sent with API requests. `allow-scripts` is required for the chat to function.

---

## Token refresh

Embed tokens expire after the configured `expiry_minutes` (default 3 hours). When the token is within 10 minutes of expiry, the embedded chat posts a message to the parent frame:

```json
{ "type": "tessallite:token-expiring" }
```

The host page should listen for this message, mint a fresh token from its backend, and send it back:

```javascript
window.addEventListener("message", async (event) => {
  if (event.data?.type === "tessallite:token-expiring") {
    const freshToken = await fetchFreshEmbedToken();
    const iframe = document.getElementById("tessallite-chat");
    iframe.contentWindow.postMessage(
      { type: "tessallite:token", token: freshToken },
      "https://chat.tessallite.example.com"
    );
  }
});
```

The embedded chat picks up the new token and continues without interruption.

### Refresh best practices

- Set `expiry_minutes` to 30-60 minutes in production.
- Always specify the target origin in `postMessage` — never use `"*"` in production.
- The `fetchFreshEmbedToken()` function should call your own backend, which in turn calls Tessallite's embed token endpoint. Never expose admin credentials in client-side code.

---

## Scope and capabilities

The embed token controls what the chat can do. The `capabilities` field in the token request restricts available features:

| Capability | Effect in embedded chat |
|---|---|
| `chat` | Required. Without it the embed route shows an error. |
| `query` | Allows the agent to execute queries. If excluded, the agent can still respond using cached context but cannot run new queries. |
| `explore` | Allows the agent to browse model metadata for context. |

The `persona_id` field locks the session to a specific persona's row-level security filters and column restrictions. The `model_ids` field restricts which models the agent can query.

---

## Choosing a model

A project agent can be configured with several semantic models. By default a conversation can draw on every model the agent allows. The **model picker** — the dataset icon in the strip at the top of the embedded chat — lets the end user restrict a single conversation to one model.

- Picking a model restricts the agent to that model alone for every turn in the conversation, overriding both the project allow-list and the project's primary model. The restriction applies to prompt context and to query execution.
- Picking **Project default** clears the restriction and returns the conversation to the project's normal set of models.
- The choice is saved on the conversation, so it persists across reloads and applies to every later turn.

The picker only ever lists models the session is already entitled to: the project allow-list, further narrowed by the embed token's `persona_id` and `model_ids` scope. It can only narrow access, never widen it. If a chosen model later leaves that scope, the conversation silently reverts to the project default. This makes the picker a convenience for focusing an answer on one subject area, not a way to reach data the token does not already permit.

---

## Theming

The conversational client supports light and dark mode. By default it follows the user's system preference. You can override this by appending `&theme=light` or `&theme=dark` to the embed URL:

```
/embed/chat?token=eyJ...&theme=dark
```

For deeper customisation (brand colours, typography), deploy the conversational client with a custom theme configuration file. See the conversational client's `tessallite-theme.json` for the available theme tokens.

---

## Security considerations

- **Token storage.** Embed tokens are bearer tokens. Anyone with the token can use it until it expires. Never store embed tokens in localStorage or expose them in URLs that might be logged or cached by proxies.
- **CORS.** Configure `ALLOWED_EMBED_ORIGINS` on the Tessallite backend to include only the specific domains that will embed the chat. Do not use `*` in production.
- **HTTPS.** Always use HTTPS in production. Embed tokens transmitted over plain HTTP can be intercepted.
- **Target origin.** Always specify the exact origin when calling `postMessage` for token refresh. Using `"*"` allows any page to intercept the token.
- **Audit trail.** The `user_identity` field in the embed token appears in Tessallite's audit log. Use a meaningful identifier (email, employee ID) so administrators can trace embedded usage.

---

## Complete example

### Server-side (Python)

```python
import requests

TESSALLITE_URL = "http://localhost:3000"

# 1. Authenticate as tenant admin
login = requests.post(f"{TESSALLITE_URL}/api/v1/auth/login", json={
    "tenant_id": "acme-demo",
    "email": "admin@acme-demo.com",
    "password": "acme-demo",
})
cookies = login.cookies

# 2. Mint an embed token scoped to chat only
resp = requests.post(
    f"{TESSALLITE_URL}/api/v1/auth/embed-token",
    json={
        "tenant_id": "acme-demo",
        "user_identity": "viewer@customer.com",
        "capabilities": ["chat", "query"],
        "expiry_minutes": 60,
    },
    cookies=cookies,
)
embed_token = resp.json()["token"]

# 3. Return embed_token to the browser via your page template
```

### Client-side (HTML)

```html
<!DOCTYPE html>
<html>
<head><title>Embedded Tessallite Chat</title></head>
<body>
  <h1>Ask our data</h1>
  <iframe
    id="tessallite-chat"
    src="http://localhost:3333/embed/chat?token=TOKEN_FROM_SERVER"
    style="width: 100%; height: 700px; border: 1px solid #ddd; border-radius: 8px;"
  ></iframe>

  <script>
    window.addEventListener("message", async (event) => {
      if (event.data?.type === "tessallite:token-expiring") {
        const resp = await fetch("/api/refresh-embed-token");
        const { token } = await resp.json();
        document.getElementById("tessallite-chat")
          .contentWindow.postMessage(
            { type: "tessallite:token", token },
            "http://localhost:3333"
          );
      }
    });
  </script>
</body>
</html>
```

---

## Troubleshooting

- **"No embed token provided" error.** The `?token=` parameter is missing from the iframe URL. Check that your server is injecting the token into the src attribute.
- **"This embed token does not include the 'chat' capability" error.** The token was minted without the `chat` capability. Re-mint with `"capabilities": ["chat"]` or omit the field to get all capabilities.
- **CORS errors in browser console.** The Tessallite backend's `ALLOWED_EMBED_ORIGINS` does not include the host page's origin. Add it to the environment variable.
- **Token refresh not working.** Check that the `postMessage` target origin matches the conversational client's actual origin. Check the browser console for message delivery issues.

---

## Related

- [Embed API](embed-api.md)
- [API Authentication](api-authentication.md)
- [Agent Chat](../agent/agent-chat.md)

---

← [Embed API](embed-api.md) | [Home](../index.md) | [Solidatus Integration →](solidatus-integration.md)
