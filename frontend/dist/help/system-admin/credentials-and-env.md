---
title: "Credentials and the .env File"
audience: system-admin
area: system-admin
updated: 2026-04-18
---

# Credentials and the .env file

![Editor view of the .env file with bootstrap credentials.](../assets/screencaps/credentials-env-file.png)

## Why credentials are not in the UI

By design, no credential is ever stored in the database-backed settings tables. Source database passwords, LLM API keys, the Fernet encryption key, the JWT signing key, and the bootstrap administrator password all live in `.env` on the host. The UI surfaces them in the System Configuration page's read-only Environment tab — masked — so an operator can verify what is in effect without exposing any secret.

This separation keeps two important properties true:

- Database snapshots, audit logs, and replication streams never contain a plaintext credential.
- Rotating a credential is a host-level operation (edit `.env`, restart) that does not require touching the database.

## Required variables

| Variable | Purpose |
|---|---|
| `CREDENTIAL_ENCRYPTION_KEY` | Fernet symmetric key used to encrypt every source-DB and LLM provider credential at rest. Generate once with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. It **can** be rotated without downtime using the dual-key window described in **Rotating a secret** below — you no longer have to choose between leaking a key forever and losing every stored credential. |
| `JWT_SECRET_KEY` | HMAC signing key for issued JWTs. Long, random, and unique per environment. Rotation invalidates every active session. |
| `POSTGRES_PASSWORD` | System database password. Used in the constructed DSN unless `SYSTEM_DATABASE_URL` is set explicitly. |
| `SYSTEM_ADMIN_EMAIL` | Login email for the bootstrap system administrator. Default `admin@tessallite.local`. |
| `SYSTEM_ADMIN_PASSWORD` | Password for the bootstrap system administrator. |

## Optional but recommended

| Variable | Purpose |
|---|---|
| `SYSTEM_DATABASE_URL` | Full DSN override when PostgreSQL lives outside the docker-compose stack. |
| `JWT_ALGORITHM` | JWT signing algorithm. Default `HS256`. |
| `JDBC_PORT`, `XMLA_PORT` | Gateway listen ports. Defaults `5433` and `8080`. |
| `QUERY_ROUTER_URL`, `MODEL_SERVICE_URL`, `OPTIMIZER_URL` | Internal service URLs. Defaults assume the docker-compose service names. |
| `CORS_ORIGINS` | Comma-separated CORS allow-list. Set to override the default loopback list entirely. |
| `CORS_LAN_IP` | Optional LAN IP that gets prepended to the default CORS list (handy for testing the SPA from another machine on the same network without overriding the whole list). |
| `CREDENTIAL_ENCRYPTION_KEY_PREVIOUS` | Comma-separated list of *old* encryption keys, set **only during a key-rotation window**. While set, the platform encrypts new values with `CREDENTIAL_ENCRYPTION_KEY` and can still decrypt anything that was written under a listed previous key. Leave it unset in normal running; add the old key here when you start a rotation, and remove it once the rotation is complete. |

## LLM provider keys

LLM API keys are never written to the database settings tables. When you create an **LLM Provider Config** row from the Model Builder Settings panel, the API key you supply is encrypted with `CREDENTIAL_ENCRYPTION_KEY` before it is stored in `llm_provider_configs`. To seed the bootstrap value, set the relevant variable in `.env` (e.g. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`) and the bootstrap script reads it.

The non-secret routing fields — provider base URLs, model-name suggestions, the Anthropic API version header — live in `system_settings` under the `llm.*` keys and are editable from System Admin → Configuration.

## Rotating a secret

1. Edit `.env` on the host with the new value.
2. Restart the relevant service:

   ```
   docker compose restart model-service
   docker compose restart gateway
   docker compose restart query-router
   docker compose restart optimizer
   docker compose restart scheduler
   ```

3. For `JWT_SECRET_KEY`: every active session is invalidated; users will be prompted to log in again.
4. For `CREDENTIAL_ENCRYPTION_KEY` (the Fernet key that encrypts every stored credential): rotate it **without downtime** using the built-in dual-key window. The encryption layer uses `MultiFernet`, which encrypts with the current key but can decrypt with any of a list of keys, so old and new keys coexist while you migrate. Step by step:

   1. Generate a new key (same command as above).
   2. Move the *current* key into `CREDENTIAL_ENCRYPTION_KEY_PREVIOUS` and put the new key in `CREDENTIAL_ENCRYPTION_KEY`.
   3. Restart the services (the list above). New writes now use the new key; everything already stored still decrypts under the previous key.
   4. Re-encrypt every stored credential under the new key by calling, as a system administrator, `POST /api/v1/admin/rotate-credentials`. This walks every project connection and LLM provider config and re-writes it with the current key.
   5. When that call reports **zero failures**, remove `CREDENTIAL_ENCRYPTION_KEY_PREVIOUS` and restart again. The old key is now fully retired.

   Do **not** change `CREDENTIAL_ENCRYPTION_KEY` by editing `.env` alone and skipping the dual-key window — without the previous key kept readable, every stored credential becomes undecryptable. The full operator procedure (including the GCS-backed backup and the multi-tenant `failed_tenants` reporting contract) is in the [Secret Rotation Runbook](../../../docs/guides/guides_secret-rotation-runbook.md).

## What lives where

| Item | Lives in |
|---|---|
| Source DB passwords (per project_connection) | `project_connections.encrypted_credentials` — encrypted with `CREDENTIAL_ENCRYPTION_KEY` |
| LLM API keys (per llm_provider_config) | `llm_provider_configs.encrypted_api_key` — encrypted with the same key |
| JWT signing key, Fernet key, system admin password | `.env` only |
| JWT lifetime, rate limits, scheduler cadences, etc. | `system_settings` table (editable from the UI) |

> **Never commit `.env` to source control.** The repository's `.gitignore` excludes it; `.env.example` is the safe template to commit.

## Related

- [System Configuration](system-configuration.md)
- [Configure environment variables](configure-environment-variables.md)
- [Workspace settings (tenant level)](../admin/workspace-settings.md)

---

← [System Configuration](system-configuration.md) | [Home](../index.md) | [Service Reference →](service-reference.md)
