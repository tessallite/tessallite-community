---
title: "Embed Agent Chat"
audience: developer
area: Integrations
updated: 2026-06-21
---

## What this covers

This article explains how to embed Tessallite's conversational agent inside a third-party web application. The embedded chat lets end users ask questions about semantic models without leaving the host application. It builds on the [Embed API](embed-api.md), which handles token minting and scope control.

Embedding uses a lightweight iframe that loads the Tessallite conversational client at `/embed/chat`. The host application mints a scoped embed token server-side, delivers it to the iframe via the postMessage handshake, and handles token refresh when the token nears expiry.

---

## Architecture

The embed flow involves three participants:

1. **ISV backend** -- authenticates to Tessallite as a tenant admin, mints an embed token via `POST /api/v1/auth/embed-token`, and passes it to the browser.
2. **Host page** -- renders an iframe pointing at the Tessallite conversational client's `/embed/chat` route, waits for the iframe to signal readiness, then sends the token to the iframe via `postMessage`.
3. **Embedded chat** -- receives the token via `postMessage`, authenticates, loads the scoped project, and renders a full-screen chat interface with no login page, navigation bar, or sidebar.

The embedded chat communicates with the host page using `postMessage` for both initial token delivery and token refresh. No Tessallite credentials are exposed to the end user. The token never appears in the URL.

---

## Bootstrap: the postMessage handshake

The recommended way to deliver the embed token to the iframe is the **postMessage handshake**. This avoids placing the token in the URL, where it would be visible in browser history, server logs, referrer headers, and proxy caches.

The handshake works as follows:

1. The host page creates an iframe pointing at `/embed/chat` with no token parameter.
2. As soon as the iframe loads, the embedded chat posts a **ready** message to the parent frame: `{ "type": "tessallite:ready" }`.
3. The host page listens for the ready message, then sends the token: `{ "type": "tessallite:bootstrap-token", "token": "<JWT>" }`.
4. The embedded chat validates the token, authenticates, and renders the chat interface.

The host page must be listed in the `ALLOWED_EMBED_ORIGINS` environment variable on the Tessallite backend, and the origin used in `postMessage` must match the conversational client's actual origin. The embedded chat ignores messages from origins that are not in the allowed list.

### Host page code

```html
<iframe
  id="tessallite-chat"
  src="https://chat.tessallite.example.com/embed/chat"
  style="width: 100%; height: 600px; border: none; border-radius: 8px;"
  allow="clipboard-write"
></iframe>

<script>
  const TESSALLITE_ORIGIN = "https://chat.tessallite.example.com";

  window.addEventListener("message", (event) => {
    if (event.origin !== TESSALLITE_ORIGIN) return;

    if (event.data?.type === "tessallite:ready") {
      // The iframe is loaded and waiting for its token.
      // embedToken should have been fetched from your own backend.
      const iframe = document.getElementById("tessallite-chat");
      iframe.contentWindow.postMessage(
        { type: "tessallite:bootstrap-token", token: embedToken },
        TESSALLITE_ORIGIN
      );
    }
  });
</script>
```

The iframe renders a loading spinner until it receives the bootstrap token. Once authenticated, the chat interface appears immediately with no login page.

---

## Deprecated: token via URL parameter

An older integration method passes the token as a `?token=` URL parameter:

```
/embed/chat?token=eyJhbGci...
```

**This method is deprecated.** It continues to work, but the embedded chat logs a deprecation warning to the browser console on every use. It will be removed in a future release.

**Security caution.** Placing a bearer token in the URL exposes it in browser history, HTTP referrer headers, server access logs, and proxy caches. Any of these can leak the token to unintended parties. Use the postMessage handshake instead.

If you are currently using the URL parameter method, migrate to the postMessage handshake described above. The only change required on the host page is: remove the `?token=` from the iframe `src`, add a listener for the `tessallite:ready` message, and post the token via `postMessage`.

---

## Sizing

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

The host page should listen for this message, mint a fresh token from its backend, and send it back using the `tessallite:token` message type:

```javascript
window.addEventListener("message", async (event) => {
  if (event.origin !== TESSALLITE_ORIGIN) return;

  if (event.data?.type === "tessallite:token-expiring") {
    const freshToken = await fetchFreshEmbedToken();
    const iframe = document.getElementById("tessallite-chat");
    iframe.contentWindow.postMessage(
      { type: "tessallite:token", token: freshToken },
      TESSALLITE_ORIGIN
    );
  }
});
```

The embedded chat picks up the new token and continues without interruption.

### Refresh best practices

- Set `expiry_minutes` to 30-60 minutes in production.
- Always specify the target origin in `postMessage` -- never use `"*"` in production.
- The `fetchFreshEmbedToken()` function should call your own backend, which in turn calls Tessallite's embed token endpoint. Never expose admin credentials in client-side code.

---

## Scope and capabilities

The embed token controls what the chat can do. The `capabilities` field in the token request restricts available features:

| Capability | Effect in embedded chat |
|---|---|
| `chat` | Required. Without it the embed route shows an error. |
| `query` | Allows the agent to execute queries. If excluded, the agent can still respond using cached context but cannot run new queries. |
| `explore` | Allows the agent to browse model metadata for context. |

The `persona_id` field locks the session to a specific persona's row-level security filters and column restrictions. The `model_ids` field restricts which models the agent can query. The `project_ids` field restricts which projects the embedded session can access; the chat loads only projects in this list.

---

## Choosing a model

A project agent can be configured with several semantic models. By default a conversation can draw on every model the agent allows. The **model picker** -- the dataset icon in the strip at the top of the embedded chat -- lets the end user restrict a single conversation to one model.

- Picking a model restricts the agent to that model alone for every turn in the conversation, overriding both the project allow-list and the project's primary model. The restriction applies to prompt context and to query execution.
- Picking **Project default** clears the restriction and returns the conversation to the project's normal set of models.
- The choice is saved on the conversation, so it persists across reloads and applies to every later turn.

The picker only ever lists models the session is already entitled to: the project allow-list, further narrowed by the embed token's `persona_id` and `model_ids` scope. It can only narrow access, never widen it. If a chosen model later leaves that scope, the conversation silently reverts to the project default. This makes the picker a convenience for focusing an answer on one subject area, not a way to reach data the token does not already permit.

---

## Theming

The conversational client supports light and dark mode. By default it follows the user's system preference. You can override this by appending `?theme=light` or `?theme=dark` to the embed URL:

```
/embed/chat?theme=dark
```

For deeper customisation (brand colours, typography), deploy the conversational client with a custom theme configuration file. See the conversational client's `tessallite-theme.json` for the available theme tokens.

---

## Security considerations

- **Token delivery.** Always use the postMessage handshake to deliver the token. Placing the token in the URL exposes it in browser history, referrer headers, and server logs.
- **Token storage.** Embed tokens are bearer tokens. Anyone with the token can use it until it expires. Never store embed tokens in localStorage.
- **CORS.** Configure `ALLOWED_EMBED_ORIGINS` on the Tessallite backend to include only the specific domains that will embed the chat. Do not use `*` in production.
- **HTTPS.** Always use HTTPS in production. Embed tokens transmitted over plain HTTP can be intercepted.
- **Target origin.** Always specify the exact origin when calling `postMessage`. Using `"*"` allows any page to intercept the token.
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
    src="http://localhost:3333/embed/chat"
    style="width: 100%; height: 700px; border: 1px solid #ddd; border-radius: 8px;"
  ></iframe>

  <script>
    const TESSALLITE_ORIGIN = "http://localhost:3333";

    // embedToken is injected by the server-side template
    const embedToken = "TOKEN_FROM_SERVER";

    window.addEventListener("message", async (event) => {
      if (event.origin !== TESSALLITE_ORIGIN) return;

      if (event.data?.type === "tessallite:ready") {
        // Iframe is loaded -- send the bootstrap token
        document.getElementById("tessallite-chat")
          .contentWindow.postMessage(
            { type: "tessallite:bootstrap-token", token: embedToken },
            TESSALLITE_ORIGIN
          );
      }

      if (event.data?.type === "tessallite:token-expiring") {
        // Token is about to expire -- fetch a fresh one
        const resp = await fetch("/api/refresh-embed-token");
        const { token } = await resp.json();
        document.getElementById("tessallite-chat")
          .contentWindow.postMessage(
            { type: "tessallite:token", token },
            TESSALLITE_ORIGIN
          );
      }
    });
  </script>
</body>
</html>
```

---

## Troubleshooting

- **"No embed token provided" error.** The iframe did not receive a bootstrap token. Check that your host page is listening for the `tessallite:ready` message and posting back a `tessallite:bootstrap-token` message with the token. If you are using the deprecated URL parameter method, check that `?token=` is present in the iframe src.
- **"This embed token does not include the 'chat' capability" error.** The token was minted without the `chat` capability. Re-mint with `"capabilities": ["chat"]` or omit the field to get all capabilities.
- **CORS errors in browser console.** The Tessallite backend's `ALLOWED_EMBED_ORIGINS` does not include the host page's origin. Add it to the environment variable.
- **Token refresh not working.** Check that the `postMessage` target origin matches the conversational client's actual origin. Check the browser console for message delivery issues.
- **Deprecation warning in console.** If you see a warning about `?token=` being deprecated, migrate to the postMessage handshake. See the *Deprecated: token via URL parameter* section above.

---

## Related

- [Embed API](embed-api.md)
- [API Authentication](api-authentication.md)
- [Agent Chat](../agent/agent-chat.md)

---

← [Embed API](embed-api.md) | [Home](../index.md) | [MCP Server →](mcp-server.md)
