---
title: "Security Audit Trail"
audience: tenant_admin
area: admin
updated: 2026-05-02
---

## What this covers

The security audit trail records every row-security rule evaluation that fires during query execution. This page explains what is logged, how to read the audit log, how to filter it, and what access controls apply.

## Why the security audit trail matters

Row security enforces data access by filtering query results at runtime. For compliance purposes you need to prove that:

- The correct rule fired for the correct user on the correct query.
- No rule was bypassed or skipped for a privileged query.
- You can reconstruct which rows a specific user could have seen at a point in time.

The security audit trail provides this evidence. Each log entry links the rule, the principal, the persona (where applicable), and the timestamp of the query.

## What is logged

Every time the query router applies a row-security rule, it writes an audit event containing:

| Field | Contents |
|---|---|
| `rule_id` | UUID of the `RowSecurityRule` that fired |
| `rule_name` | Display name of the rule |
| `principal_id` | User ID from the JWT |
| `principal_email` | Email address of the caller |
| `persona_id` | UUID of the persona, if the query was routed through one |
| `applied_at` | UTC timestamp of the query |
| `query_fingerprint` | Normalized query fingerprint |
| `predicate_applied` | The SQL predicate injected by the rule |
| `attribute_source` | The attribute source used (`jwt_role`, `idp_group`, `saml_claim`, `oidc_scope`) |

## Accessing the audit log

The security audit log is accessible from **Tenant Administration → Security → Audit Log**.

The same data is available via API:

```
GET /api/v1/security-audit
```

Query parameters:

| Parameter | Type | Description |
|---|---|---|
| `from_date` | ISO date string | Include events on or after this date |
| `to_date` | ISO date string | Include events on or before this date |
| `rule_id` | UUID | Filter to a specific rule |
| `principal_id` | UUID | Filter to a specific user |
| `limit` | integer | Maximum records per page (default 100, max 1000) |
| `offset` | integer | Records to skip for pagination |

## Access control

Only users with the `tenant_admin` role can read the security audit log. Modellers and viewers receive HTTP 403. The purpose is to protect the log from being used to infer which data other users can see.

## Attribute source values

Row-security rules can derive the filter value from four sources. The audit log records which source was used:

| `attribute_source` | Where the value comes from |
|---|---|
| `jwt_role` | The `role` claim in the caller's JWT |
| `idp_group` | The group membership claim from the identity provider |
| `saml_claim` | A named claim in a SAML assertion |
| `oidc_scope` | A named scope in an OIDC token |

If the attribute source is absent from the token, the rule logs the evaluation but may produce an empty result set (the query runs but returns no rows) rather than a 403.

## Retention

Audit events are kept for 90 days by default. The retention period can be adjusted in Workspace Settings.

---

## Related

- [Configure Row Security](../modelling/configure-row-security.md)
- [Manage Roles](manage-roles.md)

---

← [Webhooks](webhooks.md) | [Home](../index.md) | [Refresh SLA Declarations →](scheduler-sla.md)
