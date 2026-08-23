---
title: "Create a Workspace"
audience: system-admin
area: Admin
updated: 2026-07-17
---

## What this covers

Workspace (tenant) creation is a System Admin operation. Only the platform administrator -- signed in with the `SYSTEM_ADMIN_EMAIL` and `SYSTEM_ADMIN_PASSWORD` credentials from the `.env` file -- can provision new workspaces.

For the full walkthrough, including what gets provisioned under the hood, required fields, edition limits, slug best practices, and verification steps, see **[Create a Tenant](../system-admin/create-a-tenant.md)** in the System Admin section.

---

## Quick summary

1. Sign in as the System Admin (default email: `admin@tessallite.local`).
2. Open **System Administration** and click the **Workspaces** tab.
3. Click **New Workspace**, fill in the display name, slug, and Tenant Admin email, then click **Create**.

Tessallite provisions a SystemTenant record, two isolated PostgreSQL schemas (`<slug>_meta` and `<slug>_aggregates`), and a Tenant Admin user automatically.

---

## Related

- [Create a Tenant (full guide)](../system-admin/create-a-tenant.md)
- [Manage Users](manage-users.md)
- [Workspaces and Tenants (concepts)](../concepts/workspaces-and-tenants.md)

---

← [Agent Stop Button](../agent/agent-stop-button.md) | [Home](../index.md) | [Manage Users →](manage-users.md)
