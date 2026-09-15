---
title: "Project-Level Personas"
audience: tenant-admin
area: agent
updated: 2026-05-15
---

## What this covers

Project-level personas restrict what the conversational agent can see across a project. They are different from model-level personas, which publish scoped catalogues for JDBC/XMLA and in-app querying. Use project-level personas when the agent serves several audiences from the same project and each audience should see a different subset of models, measures, or dimensions.

## How they differ from model personas

| Aspect | Project-level persona | Model-level persona |
|---|---|---|
| Primary consumer | Conversational agent | Query router, JDBC, XMLA, Measure Query Panel |
| Scope | One project, many models | One model |
| Restricts | Models, measures, dimensions exposed to the agent context | Measures, dimensions, hierarchies, filters, row-security bypass, tag restrictions |
| Enforcement point | Agent context assembly and query validation | Gateway/query-router persona gate |
| Typical user | Tenant admin or data steward configuring agent audiences | Modeller publishing audience-specific catalogues |

## How the agent uses a project persona

When a persona is selected in Agent Chat, the prompt builder filters model profiles before the LLM sees them. Excluded models and attributes are not described to the model. If a later query plan references excluded objects, validation rejects it before execution.

A persona contains one or more model scopes. If a model scope is absent, that model is hidden from the persona. If a model scope exists with empty measure or dimension lists, that axis is unrestricted for that model. Non-empty lists act as allow lists.

## What the agent can see

A project-level persona only ever narrows further. It never widens past what the model itself already allows the caller. Underneath the project persona, every model still applies its own rules. This includes row security, column security, and the model-level persona for the person asking the question. The agent is grounded on the model as that person sees it. The project persona then trims that further, for the conversation.

This means the agent can never describe, summarise, or chart something the person could not see. That covers any row, measure, or column the person could not query directly with another tool. Say a measure is blocked by column security. The agent does not know it exists. It will not name it, even if asked. Say a KPI or summary table cannot be narrowed to the person's rows. The agent then gets a live, correctly filtered answer instead. It never falls back to a shortcut built for a wider audience. This is the same fallback a JDBC or XMLA client gets. There is nothing the agent can reach that the person's own query access could not also reach.

## Recommended design

Create personas for audiences, not individuals: Finance, Operations, Partner Support, Executive Review. Start broad enough for the audience to answer its real questions, then narrow only where governance requires it. Keep descriptions explicit because the persona name becomes part of the user-facing chat context.

## Example

A Finance persona can include the payments and settlements models, allow revenue/cost/margin measures, and include fiscal calendar dimensions. A Partner persona can include only shipment count and SLA measures while excluding customer identity dimensions and cost measures.

## Related

- [Agent Chat](agent-chat.md)
- [Configure your project agent](configure-agent.md)
- [Configure model-level personas](../modelling/configure-personas.md)
- [Column-Level Security](../modelling/column-level-security.md)

---

← [Configure your project agent](configure-agent.md) | [Home](../index.md) | [Author the glossary alias map →](glossary-alias-map.md)
