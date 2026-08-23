---
title: "Workspace Settings"
audience: tenant-admin
area: Admin
updated: 2026-08-17
---

# Workspace Settings

## What this covers

Tessallite splits configuration across three levels so each setting has one clear owner:

- **Project-level** — connections, LLM provider bundles, the conversational agent, identity-provider overlay, and embed-token inventory — lives in the project configuration drawer (Explorer gear icon on the project, or the project row gear).
- **Model-level** — aggregate cron, optimizer knobs, pocket / predictive policy, per-model ceilings — lives in the Model Builder Settings panel, Configuration sub-tab.
- **System-level** — scheduler cadences, gateway timeouts, rate limits, source-DB fallbacks, Spark Thrift, XMLA session cache — lives in the System Configuration screen (system admin only).

Workspace identity (display name, slug) and workspace deletion are managed by a system admin from System Administration.

SSO (SAML/OIDC) can be configured per workspace from the project drawer **Identity provider** section without restarting the process. Group-to-role mappings stay on the separate **SSO Mappings** section of the same drawer. The deployment still needs a trusted public origin (`PUBLIC_BASE_URL` or `CORS_ORIGINS`) so login redirects cannot be pointed at an attacker host.

## Where each setting lives

| Setting | Screen |
|---|---|
| Source / target connections (PostgreSQL, BigQuery, Hadoop / Spark) | Project drawer, Connections tab |
| LLM provider bundles (OpenAI, Anthropic, Gemini, DeepSeek, GLM, Ollama) | Project drawer, LLM Configurations tab |
| Conversational agent (brief, tone, safety, judge, webhook, retention) | Project drawer, seven agent tabs |
| Default aggregate schedule | Model Configuration, Aggregates tab |
| AI optimizer sweep knobs | Model Configuration, AI Optimizer tab |
| Pocket-table behaviour | Model Configuration, Pocket tab |
| Predictive-aggregate thresholds | Model Configuration, Predictive tab |
| Per-model ceilings | Model Configuration, Limits tab |
| Query timeout, schema sync cadence, rate limits, Spark Thrift defaults | System Configuration |
| Workspace display name, slug, deletion | System Administration (system admin only) |
| SSO (SAML / OIDC overlay) | Project configuration drawer, Identity provider |
| SSO group mappings | Project configuration drawer, SSO Mappings |
| Embed token inventory | Project configuration drawer, Embed tokens |

## Related

- [Project settings](project-settings.md) — connections, LLM configurations, and the seven agent tabs
- [Model configuration](model-configuration.md) — per-model overrides
- [System Configuration](../system-admin/system-configuration.md) — system-wide defaults
- [SSO Configuration](sso-configuration.md)
- [Create a Workspace](create-a-workspace.md)
- [Workspaces and Tenants (concepts)](../concepts/workspaces-and-tenants.md)

---

← [Model-Scoped RBAC](rbac-model-scoped.md) | [Home](../index.md) | [Project Settings →](project-settings.md)
