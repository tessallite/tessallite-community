---
title: "Webhooks"
audience: tenant-admin
area: Admin
updated: 2026-05-01
---

# Webhooks

## What this covers

Tessallite can push notifications to external systems when significant events occur — a model is deployed, a user is created, or a setting changes. This page explains the webhook system: which events are available, how to create and manage endpoints, how to verify payload signatures, and how to handle delivery failures.

---

## When to use webhooks

Webhooks are the right tool when an external system needs to react to something that happened inside Tessallite:

- **CI/CD pipelines** that trigger downstream processing when a model is published.
- **Audit systems** that ingest structured events from all platforms.
- **Notification channels** (Slack, Teams, PagerDuty) that alert on destructive actions.
- **Data catalogs** that sync model metadata when it changes.

Webhooks are not the right tool for polling or querying state. Use the REST API for that.

---

## Event types

| Event | Fired when |
|---|---|
| `model.published` | A model is deployed to a version. |
| `model.undeployed` | A model's deploy pointer is cleared. |
| `model.reverted` | A model is reverted to an earlier version. |
| `model.deleted` | A model is permanently deleted. |
| `user.created` | A new user account is created (local, JIT, or SSO). |
| `user.deleted` | A user account is deleted. |
| `settings.changed` | A tenant, project, or model setting is saved. |
| `schema_drift.detected` | The scheduler detects that a source's schema has drifted from the model. |
| `refresh.completed` | An aggregate refresh (scheduled or manual) finishes successfully. |
| `refresh.failed` | An aggregate refresh ends in error. |
| `refresh.sla_breach` | An aggregate's freshness exceeds its configured SLA threshold. |
| `refresh.sla_recovered` | An aggregate that had breached its SLA returns to freshness. |

The `refresh.*` family is the most useful for orchestration: subscribe to `refresh.completed` to trigger a downstream job only after data is fresh, and to `refresh.failed` / `refresh.sla_breach` to alert when it is not. The authoritative list of event names lives in `shared/webhooks/event_types.py`; a subscription can name specific events or use the wildcard `*` to receive all of them.

There is also a `test.ping` event. It is **not** a real platform event — it is sent only when you click **Send test** on an endpoint, so you can confirm connectivity and signature verification without waiting for something to happen.

Each event type sends a JSON payload with the event type, a timestamp, the actor's identity, and an event-specific detail object.

---

## Creating an endpoint

1. Navigate to *Admin > Webhooks*.
2. Click **New Endpoint**.
3. Fill in:
   - **Name** — a descriptive label (e.g., "Slack model alerts").
   - **URL** — the HTTPS endpoint that will receive POST requests.
   - **Event filters** — tick the event types this endpoint should receive, or select all.
4. Click **Save**.

Tessallite generates an HMAC-SHA256 signing secret for the endpoint. The secret is shown once on creation and can be rotated later.

---

## Payload format and signature verification

Each delivery is a `POST` request with a JSON body:

```json
{
  "event_type": "model.published",
  "timestamp": "2026-05-01T14:30:00Z",
  "actor": "admin@acme.com",
  "detail": {
    "model_id": "...",
    "model_slug": "sales",
    "version": 3
  }
}
```

The request includes an `X-Tessallite-Signature` header:

```
t=1746105000,v1=5d41402abc4b2a76b9719d911017c592...
```

To verify:

1. Extract the `t` (timestamp) and `v1` (signature) values from the header.
2. Compute `HMAC-SHA256(signing_secret, t + "." + raw_request_body)`.
3. Compare the hex digest to `v1`. If they match, the payload is authentic and untampered.

Maximum payload size is 64 KB. Payloads exceeding this limit are truncated.

---

## Delivery and retry

Tessallite delivers each event as a fire-and-forget async task. Delivery attempts follow this pattern:

| Attempt | Delay after failure |
|---|---|
| 1st | immediate |
| 2nd | 10 seconds |
| 3rd | 60 seconds |
| 4th (final) | 300 seconds |

A delivery is considered successful when the endpoint returns an HTTP 2xx status code. Any other response (including timeouts) counts as a failure.

After all retries are exhausted, the delivery moves to the **dead-letter queue** (DLQ).

---

## Managing endpoints

The *Admin > Webhooks* page provides:

- **Endpoint list** — name, URL (domain only, path masked), subscribed event types, active/inactive toggle, and last delivery status.
- **Active toggle** — disable an endpoint without deleting it. Disabled endpoints stop receiving deliveries immediately.
- **Edit** — change the name, URL, or event filters. **Changing the URL replaces the signing secret.** A secret is a key cut for one particular door: if the endpoint now points somewhere else, whoever runs the old address must not still be holding a working key. Tessallite therefore cuts a new one whenever you save a different URL — and shows it to you straight away, in the same one-time dialog you saw when you created the endpoint. Copy it before you close that dialog and give it to the new receiver: until the new receiver has it, it cannot verify the signature and will reject every delivery. If you close the dialog without copying the value, press **Rotate secret** to cut another one. Changing only the name or the event filters never touches the secret, and no dialog appears. Deliveries that were already waiting in the queue when you saved are not redirected: each one remembers the address it was queued for, so it still goes to the old receiver. The new address applies to events raised from now on.
- **Delete** — permanently remove the endpoint and all its delivery history.
- **Test delivery** — send a synthetic `webhook.test` event to verify connectivity and signature handling.
- **Rotate secret** — generate a new signing secret. The old secret stops working immediately; the new secret is shown once so you can copy it to your receiving system.

### Delivery history

Click an endpoint row to open its delivery history drawer. Each entry shows:

- Timestamp, event type, HTTP status code, and delivery status (`delivered`, `failed`, `pending`, `dlq`).
- For failed deliveries, the error message (timeout, connection refused, non-2xx status).

### Dead-letter queue

The **DLQ** tab lists all deliveries that exhausted their retries. For each entry you can:

- **Retry** — re-enqueue the delivery for one more attempt.
- **Delete** — permanently discard the failed delivery.

---

## Best practices

- **Always verify signatures.** Without verification, any system that discovers your endpoint URL can send fake events.
- **Respond quickly.** Return a 2xx within 5 seconds. Do heavy processing asynchronously after acknowledging receipt.
- **Use event filters.** Subscribe only to the events you need. A catch-all endpoint that ignores most events wastes delivery capacity.
- **Monitor the DLQ.** A growing DLQ means your endpoint is failing. Investigate and fix before retrying.
- **Rotate secrets on team changes.** When someone who knew the signing secret leaves the team, rotate it.

---

## API reference

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/v1/admin/webhooks` | List all endpoints |
| POST | `/api/v1/admin/webhooks` | Create an endpoint |
| PUT | `/api/v1/admin/webhooks/{id}` | Update an endpoint |
| DELETE | `/api/v1/admin/webhooks/{id}` | Delete an endpoint |
| POST | `/api/v1/admin/webhooks/{id}/test` | Send a test event |
| POST | `/api/v1/admin/webhooks/{id}/rotate-secret` | Regenerate signing secret |
| GET | `/api/v1/admin/webhooks/{id}/deliveries` | Delivery history (paginated) |
| GET | `/api/v1/admin/webhooks/dlq` | Dead-letter queue entries |
| POST | `/api/v1/admin/webhooks/dlq/{id}/retry` | Retry a DLQ entry |
| DELETE | `/api/v1/admin/webhooks/dlq/{id}` | Delete a DLQ entry |

All endpoints require `tenant_admin` role.

---

## Related

- [Audit Log](audit-log.md)
- [Workspace Settings](workspace-settings.md)

---

← [SSO Group Mappings](group-mappings.md) | [Home](../index.md) | [Security Audit Trail →](security-audit-trail.md)
