---
title: "SSO Configuration"
audience: tenant-admin
area: Admin
updated: 2026-05-01
---

# SSO Configuration

## What this covers

Tessallite supports SAML 2.0 and OpenID Connect (OIDC) for single sign-on. This page explains how SSO works, how to configure each protocol, how to set up group-to-role mappings, and what happens when a user signs in via SSO for the first time.

---

## How SSO fits into the authentication chain

Tessallite authenticates users through a chain of backends. Each tenant can enable one or more of: `local` (username/password), `ldap`, `gcp_iam`, `saml`, and `oidc`. The login page adapts to show the available options — when `saml` or `oidc` is enabled, a **Sign in with SSO** button appears alongside the standard credential form.

SAML and OIDC are redirect-based. The user's browser is sent to the identity provider (IdP) for authentication. After successful authentication, the browser is redirected back to Tessallite, which validates the response, creates or updates the local user record, and issues a JWT for the session.

---

## SAML 2.0 setup

### Step 1 — Register Tessallite as a Service Provider

Tessallite publishes its SP metadata at:

```
GET /api/v1/auth/saml/metadata
```

Download this XML and upload it to your IdP (Auth0, Google Workspace, Okta, AD FS, or any SAML 2.0 IdP). The metadata contains the entity ID and the Assertion Consumer Service (ACS) URL.

### Step 2 — Configure the IdP in Tessallite

In the tenant settings, provide:

You can also set these from **Workspace Settings → project configuration drawer → Identity provider** without restarting the service. Process environment variables remain the deployment default; the workspace overlay is used for that tenant's login flow.

| Setting | Purpose |
|---|---|
| `SAML_IDP_METADATA_URL` | URL to the IdP's SAML metadata. Tessallite fetches and parses it automatically at login time. Use this OR the XML field below — not both. The URL must be a public `https://` address the Tessallite host can reach; internal, loopback, and cloud-metadata addresses are refused for security. If the metadata cannot be fetched or parsed, SAML login fails cleanly (it never falls back to a half-configured state) and the reason is written to the service logs. |
| `SAML_IDP_METADATA_XML` | Paste the IdP metadata XML directly. Use when the metadata URL is not accessible from the Tessallite host. |
| `SAML_SP_ENTITY_ID` | The entity ID Tessallite uses to identify itself. Must match what is registered in the IdP. |

### Step 3 — Map attributes

The IdP sends user attributes in the SAML assertion. Tessallite reads three:

| Setting | Default | What it maps to |
|---|---|---|
| `SAML_ATTR_EMAIL` | NameID | The user's email address (used as the primary identifier). |
| `SAML_ATTR_DISPLAY_NAME` | (none) | The user's display name. |
| `SAML_ATTR_GROUPS` | (none) | A multi-valued attribute containing group memberships. Required for group-to-role mapping. |

Defaults work for Auth0 and Google Workspace. Other IdPs may use different attribute names — check your IdP's attribute configuration.

### SAML endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/auth/saml/metadata` | GET | Returns SP metadata XML for IdP registration. |
| `/api/v1/auth/saml/login` | GET | Initiates the SSO flow (redirects to IdP). |
| `/api/v1/auth/saml/acs` | POST | Receives the SAML assertion from the IdP. |

---

## OIDC setup

### Step 1 — Register Tessallite with the IdP

Create an OAuth 2.0 / OIDC client in your IdP. Set the redirect URI to:

```
GET /api/v1/auth/oidc/callback
```

Note the client ID and client secret.

### Step 2 — Configure the IdP in Tessallite

| Setting | Purpose |
|---|---|
| `OIDC_ISSUER` | The issuer URL. Tessallite fetches `.well-known/openid-configuration` from this URL to discover endpoints. |
| `OIDC_CLIENT_ID` | The client ID from step 1. |
| `OIDC_CLIENT_SECRET` | The client secret from step 1. Stored Fernet-encrypted at rest. |
| `OIDC_SCOPES` | OAuth scopes to request. Default: `openid email profile`. |
| `OIDC_GROUPS_CLAIM` | The claim name in the ID token containing group memberships. Default: `groups`. |

### OIDC endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/auth/oidc/login` | GET | Initiates the authorization-code flow (redirects to IdP). |
| `/api/v1/auth/oidc/callback` | GET | Receives the authorization code, exchanges it for tokens. |

---

## Group-to-role mapping

Both SSO backends extract group memberships from the IdP response (SAML assertion attribute or OIDC ID token claim). Group-to-role mappings tell Tessallite which Tessallite role to assign for each IdP group.

### How it works

1. On each SSO login, Tessallite reads the user's groups from the IdP response.
2. It looks up every group name in the `idp_group_role_mappings` table.
3. For each match, it creates or updates a `UserAccessBinding` entry with the mapped role and project scope.
4. If none of the user's groups match any mapping, the user inherits the tenant's JIT default role.

### Managing mappings

Navigate to *Admin > Settings > Group Mappings*. Each mapping has three fields:

- **IdP group name** — the exact group name as sent by the IdP (case-sensitive).
- **Project** — the project to scope the role to. Leave blank for a tenant-wide mapping.
- **Role** — `admin`, `modeler`, or `viewer`.

A group can map to different roles in different projects. A single user who belongs to multiple groups receives the union of all matching roles.

The API is `GET/POST/PUT/DELETE /api/v1/admin/group-mappings` (tenant admin only).

---

## What ends up inside the login token

When a user signs in through SAML or OIDC, Tessallite does **not** copy every attribute the identity provider sends into the session token. It keeps only what it actually needs:

- the attributes your **row-security rules** reference (so those rules can evaluate per-user), and
- any extra attribute names you explicitly list in `AUTH_JWT_CLAIMS_ALLOWLIST` (comma-separated).

Everything else the identity provider released is dropped. This keeps the token small and stops attributes a rule never uses from riding along inside it.

There is a size limit, `AUTH_JWT_CLAIMS_MAX_BYTES` (4096 bytes by default). If the attributes Tessallite must keep do not fit inside that cap, the login is **refused with HTTP 413** rather than quietly trimming an attribute a rule depends on. That is deliberate: a silently-dropped attribute would make a row-security rule match the wrong rows, so Tessallite fails the login rather than risk leaking data to the wrong person.

**If an admin sees a 413 on login**, the fix is one of: shorten the identity provider's attribute *values*, narrow which attributes the row-security rules reference, or raise `AUTH_JWT_CLAIMS_MAX_BYTES`. Every dropped attribute name is logged, so the service logs tell you exactly what was trimmed.

---

## JIT user creation

When a user authenticates via SSO for the first time and no local user record exists, Tessallite automatically creates one. The new user's:

- **Email** comes from the SAML assertion or OIDC ID token.
- **Display name** comes from the mapped attribute/claim (if configured).
- **Auth source** is set to `saml` or `oidc`.
- **Role** is determined by group-to-role mappings, falling back to the JIT default role.

Users with a non-local auth source cannot change their password via Tessallite — the identity provider manages their credentials.

---

## Best practices

- **Test with one user first.** Enable SSO, sign in yourself, and verify the attribute mapping before rolling out to the team.
- **Keep local auth enabled as a fallback.** Until SSO is stable, keep `local` in the `AUTH_BACKENDS` list so administrators can still sign in with credentials if the IdP is unreachable.
- **Set `SAML_ATTR_GROUPS` or `OIDC_GROUPS_CLAIM` early.** Group-to-role mapping only works when the IdP sends group information. Some IdPs require explicit configuration to include groups in the assertion or token.
- **Use tenant-wide mappings sparingly.** A tenant-wide `admin` mapping gives the group admin access to every project. Prefer project-scoped mappings for least-privilege.

---

## Troubleshooting

- **"Sign in with SSO" button does not appear.** Check that `saml` or `oidc` is listed in the `AUTH_BACKENDS` tenant setting and that the IdP configuration fields are non-empty.
- **"Invalid assertion" error after redirect.** The SP entity ID in Tessallite does not match what is registered in the IdP, or the ACS URL is wrong. Compare the SP metadata XML with the IdP's configuration.
- **SAML login fails immediately when using a metadata URL.** Tessallite could not fetch or parse the document at `SAML_IDP_METADATA_URL`. Check the service logs for the exact reason. Common causes: the URL is not reachable from the Tessallite host, it points at an internal/loopback/cloud-metadata address (these are refused on purpose), it is served over plain `http://` (only `https://` is accepted unless `WEBHOOK_ALLOW_HTTP` is enabled), or the returned document is not valid SAML metadata. As a workaround, paste the metadata into `SAML_IDP_METADATA_XML` instead.
- **User created but has no project access.** The user's IdP groups do not match any entry in group-to-role mappings, and the JIT default role is `viewer` (which may have no project bindings). Add the appropriate mapping.

---

## Related

- [Manage Users](manage-users.md)
- [Manage Roles](manage-roles.md)
- [Workspace Settings](workspace-settings.md)

---

← [Alert Configuration](alert-configuration.md) | [Home](../index.md) | [SSO Group Mappings →](group-mappings.md)
