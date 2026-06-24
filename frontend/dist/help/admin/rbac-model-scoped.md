---
title: "Model-Scoped RBAC"
audience: tenant_admin
area: admin
updated: 2026-06-12
---

## What this covers

Model-scoped RBAC lets you grant a user a different access level on a specific model than they have at the project level. This page explains the binding hierarchy, the fallback rules, and when model-scoped bindings are the right tool.

## The binding hierarchy

Access in Tessallite is determined by checking bindings in this order:

0. **Workspace-admin bypass** — `tenant_admin` and `system_admin` users skip binding checks entirely and can access any project and model.
1. **Model-scoped binding** — `user_access_bindings` with `model_id` set. Takes precedence for operations on that specific model.
2. **Project-scoped binding** — `user_access_bindings` with `project_id` set and `model_id` null. Used when no model-scoped binding exists.
3. **Bootstrap rule** — If the project has **no bindings at all**, any signed-in workspace user is treated as the project's admin. This open period ends the moment the first binding is saved, and each use is recorded in the audit log as `rbac.bootstrap_admin_grant`. See [Manage Roles — the bootstrap rule](manage-roles.md#open-project-access-before-the-first-binding-the-bootstrap-rule) for why this is deliberate and how to close it early.

If none of these match, the request is rejected with 403.

## Why model-scoped bindings exist

The typical use case is a project with multiple models where different teams own different models:

- The Data Engineering team needs `modeler` rights on the raw-data model but should be `viewer` only on the marketing attribution model.
- An external partner can be granted `viewer` on one model without having any access to the rest of the project.
- A model under active development can be scoped to `modeler`-only access while all other models in the project remain available to viewers.

Without model-scoped bindings, you would need a separate project per team — duplicating connections, targets, and shared dimension definitions.

## Creating a model-scoped binding

1. Open **Tenant Administration → Projects → [your project] → Access**.
2. Locate the user row.
3. Click the model-scope icon next to the user's role chip.
4. Select the target model from the dropdown.
5. Select the role to grant on that model.
6. Save.

Or via API:

```
POST /api/v1/projects/{project_id}/access
{
  "user_id": "<uuid>",
  "role": "modeler",
  "model_id": "<uuid>"
}
```

## Revoking a model-scoped binding

Delete the specific model-scoped binding. The user retains their project-level binding for other models.

```
DELETE /api/v1/projects/{project_id}/access/{binding_id}
```

## Interaction with row security and personas

Model-scoped bindings control which CRUD operations a user can perform on model metadata. They do not affect which rows the user sees in queries. Row security and persona restrictions apply independently of the binding level.

## Roles reference

| Role | What it permits |
|---|---|
| `viewer` | Read model metadata, run queries, view diagnostics |
| `modeler` | All viewer rights plus create/edit/delete dimensions, measures, aggregates, rules |
| `admin` | All modeler rights plus manage connections, targets, deploy/undeploy |

---

## Related

- [Manage Roles](manage-roles.md)
- [Configure Row Security](../modelling/configure-row-security.md)
- [Project Settings](project-settings.md)

---

← [Manage Roles](manage-roles.md) | [Home](../index.md) | [Workspace Settings →](workspace-settings.md)
