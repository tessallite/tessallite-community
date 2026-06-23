---
title: "Audit Log"
audience: tenant-admin
area: Admin
updated: 2026-06-12
---

# Audit Log

## What this covers

The audit log records a trail of significant actions across the tenant: who did what, when, and to which object. This page explains what is logged, how to configure the logging level, how to search and export the log, and how to set a retention policy.

---

## Why audit logging matters

Compliance and governance require a record of material changes to data models, user accounts, security rules, and platform settings. Without an audit trail, investigating an incident means sifting through service logs or guessing from database state. The audit log gives tenant administrators a single, searchable, exportable record of every significant action, anchored to the actor who performed it.

Beyond compliance, the audit log is a diagnostic tool. When a model behaves unexpectedly after a settings change, the log shows exactly which setting was changed, by whom, and when — without needing shell access.

---

## What is logged

Events fall into six categories. Each event records the actor (email), the action performed, the target object (type, ID, and display name), a severity level, a JSONB detail payload with action-specific context, and the actor's IP address when available.

| Category | Example actions |
|---|---|
| Model lifecycle | `model.create`, `model.deploy`, `model.undeploy`, `model.revert`, `model.delete` |
| Connections | `connection.create`, `connection.update`, `connection.delete` |
| Users | `user.create`, `user.delete`, `user.role_change`, `user.password_reset` |
| Security | Row-security rule create/update/delete, persona changes, `rbac.bootstrap_admin_grant` (open-project bootstrap access — see [Manage Roles](manage-roles.md)) |
| Settings | Tenant, project, and model setting changes |
| Refresh SLAs | `refresh_sla_config.create`, `refresh_sla_config.update`, `refresh_sla_config.delete` |
| Notification routes | `notification_route.create`, `notification_route.update`, `notification_route.delete` |
| Authentication | `auth.login_success`, `auth.login_failure` |

Refresh-SLA and notification-route changes are recorded at **warn** severity, with the project, the target object, and the fields that changed. For notification routes the audit detail deliberately **omits the channel configuration**, so a Slack webhook URL or other channel secret is never written into the audit log.

---

## Audit log level

The `AUDIT_LOG_LEVEL` tenant setting controls which events are recorded. The four levels form a hierarchy — each level includes everything in the levels above it:

- **off** — no events recorded.
- **critical** — authentication failures, security changes, and destructive actions (deletes, undeploys, reverts).
- **warn** — adds model deploy/undeploy, user CRUD, and settings changes.
- **info** (default) — adds all remaining CRUD, query execution logging, and connection changes.

Set the level in *Admin > Settings*. Changes take effect immediately — no service restart required.

Choose your level based on compliance requirements. Most organisations start with `info` and tighten to `warn` or `critical` only if storage or noise becomes a concern. Setting `off` is appropriate only for development environments where no compliance obligation exists.

---

## Viewing the audit log

Navigate to *Admin > Audit Log*. The page is available to tenant administrators only.

### Filter bar

Five filters narrow the results:

- **Actor email** — substring match against the actor's email address.
- **Action** — exact match against the action string (e.g., `model.deploy`).
- **Target type** — filter by object category (`model`, `user`, `connection`, `setting`).
- **Severity** — `info`, `warn`, or `critical`.
- **Date range** — `from` and `to` dates (both inclusive).

Filters combine with AND logic. Clear a filter to remove it.

### Results table

Each row shows:

| Column | Content |
|---|---|
| Timestamp | When the event occurred (local time). |
| Actor | Email of the user or "system" for automated actions. |
| Action | The action string. |
| Target | Object type and display name. |
| Severity | Colour-coded chip. |
| Detail | Expandable — click to see the full JSONB payload. |

The table paginates. Default page size is 50.

### CSV export

Click **Export CSV** to download the current filtered results. The export respects all active filters and includes the full detail payload as a JSON string column.

---

## Retention

The `AUDIT_RETENTION_DAYS` tenant setting controls how long events are kept:

- Default: **365 days**.
- Minimum: **30 days**.
- Set to **0** for indefinite retention (events are never auto-deleted).

A daily scheduler job (`audit_purge`) deletes events older than the retention threshold. The job runs once per day per tenant.

Shortening the retention period does not delete existing events retroactively on the next save — it takes effect on the next scheduler run. If you need to purge immediately, reduce the setting and wait for the next daily run, or contact a system administrator.

---

## API reference

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/v1/admin/audit-events` | List events (paginated, filterable by `actor_email`, `action`, `target_type`, `severity`, `from_date`, `to_date`, `limit`, `offset`) |
| GET | `/api/v1/admin/audit-events/export` | CSV download with the same filter parameters |

Both require `tenant_admin` role.

---

## Best practices

- **Start with `info` level.** You can always tighten later. Starting restrictive means you miss the events you did not know you needed.
- **Set retention to match your compliance window.** If your organisation requires 2 years of audit records, set `AUDIT_RETENTION_DAYS` to 730.
- **Export periodically.** For long-term archival, schedule a CSV export to your document management system. The audit log is not a permanent archive — it lives in the tenant's meta schema and is subject to retention policy.
- **Review after security incidents.** Filter by `severity=critical` and the incident timeframe to see authentication failures and security changes.

---

## Related

- [Workspace Settings](workspace-settings.md)
- [Manage Users](manage-users.md)
- [Manage Roles](manage-roles.md)

---

← [Model Configuration](model-configuration.md) | [Home](../index.md) | [Query Log ->](query-log.md)
