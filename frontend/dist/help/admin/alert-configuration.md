# Alert Configuration

**Audience:** modeler | **Updated:** 2026-05-18

## What this covers

Tessallite can send email and Slack notifications when platform lifecycle events occur: aggregate refresh failures, schema drift, SLA breaches, query failure spikes, aggregate retirements, and upstream refresh failures. This page explains how alert routing works, how to configure routes, and best practices for operational alerting.

## How alerting works

The alert system consists of three layers:

1. **Event emitters** — the scheduler, query-router, and optimizer emit events when something operationally significant happens.
2. **Notification routes** — per-tenant configuration records that map event types to delivery channels. Each route specifies an event type, a channel (email or Slack), the channel target (recipients or webhook URL), and an enabled flag.
3. **Senders** — the SMTP email sender and the Slack webhook sender. These are configured at the system level via environment variables.

When a lifecycle event fires, the dispatcher looks up all enabled notification routes for that event type, deduplicates within a 5-minute window, and dispatches to each matching channel.

## Supported events

| Event | Trigger | Typical action |
|---|---|---|
| **Refresh Failure** | A scheduled aggregate refresh fails (full or incremental). | Investigate the source database or refresh query. Check the Diagnostics panel for the error detail. |
| **Schema Drift** | The daily schema drift sweep detects column additions, removals, or type changes in a source table. | Review the Schema Changes panel. Column removals are always flagged as breaking. Type changes on dimensions or measures are breaking. |
| **SLA Breach** | The hourly SLA monitor finds an aggregate whose freshness exceeds the configured SLA threshold. | Check the SLA Configuration panel for the breached aggregate. Verify the source database is accessible and the refresh schedule is running. |
| **Query Failure Spike** | More than 10 queries fail within a 5-minute window in the query-router. | Check the Query Log for the error pattern. Common causes: source database down, credential rotation, or connection pool exhaustion. |
| **Aggregate Retired** | The daily retirement sweep removes aggregates that exceed the per-model cap. | Review the retired aggregates in the Diagnostics panel. If important aggregates were retired, increase the model's aggregate limit. |
| **Upstream Refresh Failed** | A scheduled refresh did not run because an upstream refresh it depends on failed first. | Fix the upstream refresh, then re-run the dependent job. Check the Scheduler Dependencies for the chain order — a failure early in the chain blocks everything that depends on it. |

## Configuring alert routes

Open any model in the Model Builder. Click the **Alerts** icon in the toolbelt (bell icon, near the bottom). This opens the Alerts panel.

### Adding a route

1. Click **Add Route**.
2. Select the **Event Type** — the lifecycle event that triggers the alert.
3. Select the **Channel** — Email or Slack.
4. For **Email**: enter one or more recipient email addresses, separated by commas.
5. For **Slack**: enter the Slack incoming webhook URL for your channel.
6. Toggle **Enabled** on (enabled by default).
7. Click **Create**.

### Editing and deleting routes

Hover over any row in the alert routes table to reveal action buttons. Click the edit icon to modify the route, or the delete icon to remove it. Use the toggle switch to enable or disable a route without deleting it.

### Testing a route

Click the send icon on any row to dispatch a test alert through the configured channel. The test result appears inline. If the test fails, check the channel configuration (SMTP settings for email, webhook URL validity for Slack).

## Deduplication

The dispatcher suppresses duplicate alerts within a 5-minute window. If the same event type fires repeatedly for the same channel target, only the first alert is sent. This prevents notification storms during cascading failures.

The window resets after 5 minutes, so the next occurrence after the window will trigger a fresh alert.

## SMTP email setup

Email alerts require SMTP configuration at the system level. These settings are environment variables (not per-tenant):

| Variable | Description | Default |
|---|---|---|
| `SMTP_HOST` | SMTP server hostname. If empty, email sending is disabled. | (empty) |
| `SMTP_PORT` | SMTP server port. | 587 |
| `SMTP_TLS` | Enable STARTTLS. | true |
| `SMTP_USER` | SMTP authentication username. Leave empty for unauthenticated relays. | (empty) |
| `SMTP_PASSWORD` | SMTP authentication password. | (empty) |
| `SMTP_FROM` | Sender address for alert emails. | noreply@tessallite.local |

If `SMTP_HOST` is not set, the email sender logs a warning and skips delivery. Alert routes with email channels will silently fail until SMTP is configured.

## Slack webhook setup

Slack alerts use incoming webhooks. To set up:

1. In your Slack workspace, go to **Apps** > **Incoming Webhooks**.
2. Create a new webhook and select the target channel.
3. Copy the webhook URL (starts with `https://hooks.slack.com/services/`).
4. Paste it into the alert route's Webhook URL field.

Each alert route can target a different Slack channel by using a different webhook URL.

## API reference

All endpoints require modeler-level access.

| Endpoint | Description |
|---|---|
| `GET /api/v1/projects/{id}/notifications` | List all notification routes for the project. |
| `POST /api/v1/projects/{id}/notifications` | Create a new notification route. |
| `PUT /api/v1/projects/{id}/notifications/{route_id}` | Update an existing route. |
| `DELETE /api/v1/projects/{id}/notifications/{route_id}` | Delete a route. |
| `POST /api/v1/projects/{id}/notifications/test` | Send a test alert through the specified channel configuration. |

## Best practices

- **Start with refresh failures and schema drift.** These are the two events most likely to require immediate operator attention.
- **Use Slack for high-frequency events** (query failure spikes) and email for low-frequency ones (schema drift, SLA breaches).
- **Do not route all events to the same channel.** Notification fatigue leads to ignored alerts. Be selective about what generates a notification.
- **Test routes after creation.** Use the test button to verify delivery before relying on the route during an incident.
- **Review disabled routes periodically.** Disabling a route during maintenance and forgetting to re-enable it is a common source of missed alerts.

---

[Previous: Query Log](query-log.md) | [Home](../index.md) | [Next: SSO Configuration](sso-configuration.md)
