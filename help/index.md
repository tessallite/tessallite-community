---
title: "Tessallite Help"
audience: all
area: getting-started
updated: 2026-05-15
---

## What this covers

This library documents Tessallite: what it is, how to connect BI tools, how to build models, how the conversational agent works, and how to administer the platform. It covers the full product surface, from the Workspace Explorer and Model Builder to system configuration, BI integrations, and troubleshooting.

---

## Who this library is for

### Analyst

Connects BI tools to Tessallite via JDBC, XMLA, or the headless API and queries deployed models. Start with [What is Tessallite](getting-started/what-is-tessallite.md), [Workspace Explorer](getting-started/workspace-explorer.md), and [Connect a BI tool](getting-started/connect-a-bi-tool.md).

### Modeller

Builds and maintains semantic models: sources, joins, dimensions, measures, calendars, aggregates, pockets, security, lineage, and diagnostics. Start with [Projects and models](concepts/projects-and-models.md) and [Model canvas tour](modelling/model-canvas-tour.md).

### Tenant Admin

Manages users, roles, SSO, webhooks, project settings, audit views, and agent configuration for one workspace. Start with [Manage users](admin/manage-users.md), [Project settings](admin/project-settings.md), and [SSO group mappings](admin/group-mappings.md).

### System Admin

Deploys and operates the Tessallite platform. Start with [Architecture overview](system-admin/architecture-overview.md), [Deploy locally](system-admin/deploy-local.md), or [System configuration](system-admin/system-configuration.md).

---

## Getting Started

- [What is Tessallite](getting-started/what-is-tessallite.md)
- [How Tessallite Works](getting-started/how-tessallite-works.md)
- [First-Time Setup](getting-started/first-time-setup.md)
- [Welcome Wizard](getting-started/welcome-wizard.md)
- [Workspace Explorer](getting-started/workspace-explorer.md)
- [Demo tenant: acme-demo](getting-started/acme-demo-tenant.md)
- [Install Tessallite Locally](getting-started/install-local.md)
- [Deploy Tessallite to Google Cloud Platform](getting-started/install-gcp.md)
- [Connect a BI Tool via JDBC](getting-started/connect-a-bi-tool.md)
- [Connect Excel via XMLA](getting-started/connect-excel.md)
- [Tessallite Features](getting-started/tessallite-features.md)

## Core Concepts

- [Workspaces and Tenants](concepts/workspaces-and-tenants.md)
- [Projects and Models](concepts/projects-and-models.md)
- [Sources, Tables, and Joins](concepts/sources-tables-and-joins.md)
- [Dimensions and Measures](concepts/dimensions-and-measures.md)
- [Aggregates](concepts/aggregates.md)
- [Query Routing](concepts/query-routing.md)
- [Model Health](concepts/model-health.md)
- [Roles and Permissions](concepts/roles-and-permissions.md)
- [Calendar Types](concepts/calendar-types.md)
- [KPIs (Key Performance Indicators)](concepts/kpis.md)

## Modelling

- [Create a Project](modelling/create-a-project.md)
- [Manage Connections](modelling/manage-connections.md)
- [Add a Data Source](modelling/add-a-data-source.md)
- [Add Tables to a Model](modelling/add-tables-to-a-model.md)
- [Source Statistics](modelling/source-statistics.md)
- [Table Auto-Analysis](modelling/table-auto-analysis.md)
- [Model Canvas Tour](modelling/model-canvas-tour.md)
- [Canvas Undo/Redo](modelling/canvas-undo-redo.md)
- [Define Joins](modelling/define-joins.md)
- [Define Hierarchies](modelling/define-hierarchies.md)
- [Define Dimensions](modelling/define-dimensions.md)
- [Dimension Aliases](modelling/dimension-aliases.md)
- [Business Glossary](modelling/business-glossary.md)
- [Define Measures](modelling/define-measures.md)
- [Semi-Additive Measures](modelling/semi-additive-measures.md)
- [Calculated Measures](modelling/calculated-measures.md)
- [Configure Time Variants](modelling/configure-time-variants.md)
- [Configure Calendar Table](modelling/configure-calendar-table.md)
- [Associate Calendar with Dimensions](modelling/associate-calendar-with-dimensions.md)
- [Multi-Calendar Best Practices](modelling/multi-calendar-best-practices.md)
- [Measure Query Panel](modelling/measure-query-panel.md)
- [Pivot Conditional Formatting](modelling/pivot-conditional-formatting.md)
- [Query Panel](modelling/query-panel.md)
- [Live vs Aggregate](querying/live-vs-aggregate.md)
- [Drill-through](modelling/drill-through.md)
- [Curate drill-through](modelling/curate-drill-through.md)
- [Set a Query Target](modelling/set-a-query-target.md)
- [Configure Aggregates](modelling/configure-aggregates.md)
- [Predictive Aggregates](modelling/predictive-aggregates.md)
- [Aggregate Lifecycle](modelling/aggregate-lifecycle.md)
- [Cold-start Latency](modelling/cold-start-latency.md)
- [Configure Pocket Tables](modelling/configure-pocket-tables.md)
- [Run a Refresh](modelling/run-a-refresh.md)
- [Manage Aggregate Schedules](modelling/manage-aggregate-schedules.md)
- [Scheduler Dependencies](modelling/scheduler-dependencies.md)
- [Use the AI Optimiser](modelling/use-the-ai-optimiser.md)
- [Usage Analytics](modelling/usage-analytics.md)
- [Configure Row Security](modelling/configure-row-security.md)
- [Data Tags](modelling/data-tags.md)
- [Column-Level Security](modelling/column-level-security.md)
- [Configure Personas](modelling/configure-personas.md)
- [Parameterized Filters](modelling/parameterized-filters.md)
- [Data Quality Rules](modelling/data-quality-rules.md)
- [Impact Analysis](modelling/impact-analysis.md)
- [Named Lists](modelling/named-sets.md)
- [KPIs](modelling/kpis.md)
- [Data Preview](modelling/data-preview.md)
- [Schema Changes](modelling/schema-changes.md)
- [View Model Lineage](modelling/view-model-lineage.md)
- [View Diagnostics](modelling/view-diagnostics.md)
- [Save and Version a Model](modelling/save-and-version-a-model.md)
- [Deploy a Model](modelling/deploy-a-model.md)
- [Export and Import a Model](modelling/export-and-import-a-model.md)
- [Export and Import a Project](modelling/export-and-import-a-project.md)
- [Import from dbt](modelling/import-from-dbt.md)
- [Import from a Semantic Layer Tool](modelling/import-from-semantic-layer.md)
- [Model Templates](modelling/model-templates.md)
- [Model Translations (i18n)](modelling/model-translations.md)
- [Model Details](modelling/model-details.md)

## Conversational Agent

- [Agent Chat](agent/agent-chat.md)
- [Configure your project agent](agent/configure-agent.md)
- [Project-Level Personas](agent/project-personas.md)
- [Author the glossary alias map](agent/glossary-alias-map.md)
- [Write a judge rubric](agent/write-a-judge-rubric.md)
- [Cross-model calculation recipes](agent/cross-model-recipes.md)
- [Agent log screen](agent/agent-log-screen.md)
- [Agent Session Memory](agent/session-memory.md)
- [Agent Conversation Export](agent/agent-export.md)
- [Agent Stop Button](agent/agent-stop-button.md)

## Admin

- [Create a Workspace](admin/create-a-workspace.md)
- [Manage Users](admin/manage-users.md)
- [Manage Roles](admin/manage-roles.md)
- [Model-Scoped RBAC](admin/rbac-model-scoped.md)
- [Workspace Settings](admin/workspace-settings.md)
- [Project Settings](admin/project-settings.md)
- [Model Configuration](admin/model-configuration.md)
- [Audit Log](admin/audit-log.md)
- [Query Log](admin/query-log.md)
- [Alert Configuration](admin/alert-configuration.md)
- [SSO Configuration](admin/sso-configuration.md)
- [SSO Group Mappings](admin/group-mappings.md)
- [Webhooks](admin/webhooks.md)
- [Security Audit Trail](admin/security-audit-trail.md)
- [Refresh SLA Declarations](admin/scheduler-sla.md)

## System Admin

- [Architecture Overview](system-admin/architecture-overview.md)
- [Deploy Locally](system-admin/deploy-local.md)
- [Deploy on GCP](system-admin/deploy-gcp.md)
- [Deploy on Kubernetes](system-admin/deploy-kubernetes.md)
- [Configure Environment Variables](system-admin/configure-environment-variables.md)
- [System Configuration](system-admin/system-configuration.md)
- [Credentials and the .env File](system-admin/credentials-and-env.md)
- [Service Reference](system-admin/service-reference.md)
- [Teardown](system-admin/teardown.md)
- [Upgrade](system-admin/upgrade.md)
- [Monitoring Stack (Prometheus & Grafana)](system-admin/monitoring-stack.md)

## Integrations

- [JDBC Connection Guide](integrations/jdbc-connection-guide.md)
- [Looker Studio Direct Connection](integrations/looker-studio-connection-guide.md)
- [Optional Looker-hosted LookML Workflow](integrations/looker-studio-via-looker-guide.md)
- [Looker Cloud Core - Not Required](integrations/looker-cloud-core-connection-guide.md)
- [LookML Emitter](integrations/lookml-emitter-guide.md)
- [Excel XMLA Connection Guide](integrations/excel-xmla-connection-guide.md)
- [Excel PivotTable Features](integrations/excel-pivottable-features.md)
- [Tessallite Excel Add-in](integrations/excel-add-in.md)
- [Named List MDX Composition](integrations/named-list-mdx-composition.md)
- [Named List Parameterisation](integrations/named-list-parameterisation.md)
- [Power BI Connection Guide](integrations/powerbi-connection-guide.md)
- [BI Tool Compatibility Matrix](integrations/bi-compatibility.md)
- [Supported Data Sources](integrations/supported-data-sources.md)
- [API Authentication](integrations/api-authentication.md)
- [API Reference](integrations/api-reference.md)
- [Headless API](integrations/headless-api.md)
- [Embed API](integrations/embed-api.md)
- [Embed Agent Chat](integrations/embed-agent-chat.md)
- [MCP Server (Model Context Protocol)](integrations/mcp-server.md)
- [Solidatus Integration](integrations/solidatus-integration.md)
- [Collibra Integration](integrations/collibra-integration.md)

## Troubleshooting

- [Excel Connection Problems](troubleshooting/excel-connection-problems.md)
- [Query Returns Wrong Results](troubleshooting/query-returns-wrong-results.md)
- [Field Compatibility Warnings](troubleshooting/field-compatibility-warnings.md)
- [Aggregates Not Building](troubleshooting/aggregates-not-building.md)
- [Service Not Starting](troubleshooting/service-not-starting.md)
- [Common Errors](troubleshooting/common-errors.md)
