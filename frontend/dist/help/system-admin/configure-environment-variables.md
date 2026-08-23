---
title: "Configure Environment Variables"
audience: system-admin
area: system-admin
updated: 2026-06-24
---

## What this covers

The variables you set in `.env` (Community/local) or Secret Manager (GCP) to start Tessallite. These are the **bootstrap** variables — the bare minimum the services need before they can read anything from the database. Operational tuning knobs (rate limits, optimizer thresholds, scheduler cadences) are set in the System Admin UI and are not covered here.

For Community Edition, `.env` is created by `install.sh` inside the signed release bundle. You normally do not copy it by hand. Review it when you need to change ports, public URLs, or operator-owned secrets. Licence upload and replacement happen later in **System Admin → License & edition** and are stored in the platform database.

The **complete, always-current list** of every configurable value is in the [Configuration Reference](../../../docs/guides/guides_configuration-reference.md). When this page and the reference disagree, the reference is authoritative.

---

## Required bootstrap variables

These must be set in `.env` (Community/local) or stored as Secret Manager secrets (GCP). The services will refuse to start if any credential fields are left at placeholder values. In the Community bundle, `install.sh` generates missing secrets and preserves values already present.

| Variable | Required | Default | Description |
|---|---|---|---|
| `POSTGRES_PASSWORD` | Yes | — | Password for the internal PostgreSQL user. Used by Docker Compose to construct the connection URL automatically. On GCP the full URL (`SYSTEM_DATABASE_URL`) is stored in Secret Manager instead. |
| `CREDENTIAL_ENCRYPTION_KEY` | Yes | — | Fernet key (base64, 32 bytes) used to encrypt source database credentials at rest. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `JWT_SECRET_KEY` | Yes | — | Secret used to sign user session tokens. Minimum 32 characters. |
| `SYSTEM_ADMIN_EMAIL` | No | `admin@tessallite.local` | Email address of the system-level administrator. |
| `SYSTEM_ADMIN_PASSWORD` | Yes | — | Password for the system administrator. Production deployments require a minimum of 12 characters, with at least one uppercase letter, one lowercase letter, and one digit; the installers enforce this and set `ENFORCE_SECRET_STRENGTH=true`. |
| `ENFORCE_SECRET_STRENGTH` | No | `false` | When `true`, a `SYSTEM_ADMIN_PASSWORD` that does not meet the strength floor is a fatal startup error. The production installers set this to `true`. Leave it `false` (or unset) for dev/test/CI/demo, where a short bootstrap password only logs a warning and starts normally. |
| `LICENSE_FILE_HOST` | No | `./license.json` | Legacy/bootstrap host path to a signed local licence file. The License Manager upload stores the active licence in the platform database. |
| `LICENSE_PUBLIC_KEYS` | No | built-in key | Optional public verification key override. Normal installs use the built-in Tessallite public key. |
| `LICENSE_ENFORCEMENT_ENABLED` | No | `true` | Requires a valid signed licence at startup. Leave enabled for normal Community installs. |

---

## GCP-specific: SYSTEM_DATABASE_URL

On GCP the Cloud Run services cannot reach the local Docker network. Instead of `POSTGRES_PASSWORD`, store the full connection URL as a Secret Manager secret:

| Variable | Purpose |
|---|---|
| `SYSTEM_DATABASE_URL` | Full PostgreSQL connection URL. Format: `postgresql+asyncpg://postgres:<password>@<vm-ip>:5432/tessallite_system` |

The scripted GCP deploy (`deploy/gcp/`) sets this automatically using the Compute Engine VM's IP address. You do not set it by hand unless you are connecting to a custom database host.

---

## Optional variables

| Variable | Default | Description |
|---|---|---|
| `JDBC_PORT` | `5433` | Port on which the gateway listens for JDBC connections. |
| `XMLA_PORT` | `8080` | Port on which the gateway listens for XMLA connections. |
| `GATEWAY_XMLA_TLS_ENABLED` | `false` | Set `true` only when BI clients connect directly to the gateway and you need TLS on that hop. Leave `false` when TLS is terminated upstream (load balancer or Cloud Run). |
| `GCLOUD_ADC_PATH` | — | Path to a Google Application Default Credentials file, mounted into containers that talk to GCP services (Docker Compose only). |
| `LOG_LEVEL` | `info` | Log verbosity: `debug`, `info`, `warn`, or `error`. |

---

## Setting variables in Docker Compose

For the signed Community bundle, run `./install.sh`. It creates `.env` from `.env.example` if missing and fills empty secret values:

```
POSTGRES_PASSWORD=your-strong-password
CREDENTIAL_ENCRYPTION_KEY=your-fernet-key-here
JWT_SECRET_KEY=your-jwt-secret-min-32-chars
SYSTEM_ADMIN_PASSWORD=your-admin-password
LICENSE_FILE_HOST=./license.json
LICENSE_PUBLIC_KEYS=tessallite-prod-2026:<public-key>
```

Docker Compose reads this file automatically. Never commit `.env` or `license.json` to source control. A developer source checkout may still create `.env` manually from `.env.example`, but that is not the public install path.

After the stack is running, system administrators replace a signed licence from **System Admin → License & edition**. Tessallite verifies the uploaded `license.json`, stores it in the platform database, and applies it immediately. `LICENSE_PUBLIC_KEYS` is only needed when you must override the built-in Tessallite public key.

---

## Setting variables in Cloud Run

The scripted GCP deploy handles secrets automatically. If you need to update a secret manually:

```bash
# Update a secret value
echo -n "new-value" | gcloud secrets versions add SECRET_NAME --data-file=-

# Set a non-secret env var on a service
gcloud run services update SERVICE_NAME \
  --region REGION \
  --set-env-vars LOG_LEVEL=debug
```

---

## Security

Never commit credential values to source control. In Community installs, keep `.env` with your installation backup, but outside git. Keep downloaded `license.json` files somewhere secure as records, but the active uploaded licence lives in the platform database. On GCP, secrets (`SYSTEM_DATABASE_URL`, `CREDENTIAL_ENCRYPTION_KEY`, `JWT_SECRET_KEY`, and other secret values) are stored in Secret Manager and mounted into Cloud Run at deploy time — they never appear in the service YAML. For key rotation, see `CREDENTIAL_ENCRYPTION_KEY_PREVIOUS` in the [Configuration Reference](../../../docs/guides/guides_configuration-reference.md).

---

## Related

- [Deploy Locally](deploy-local.md)
- [Deploy on GCP](deploy-gcp.md)
- [Service Reference](service-reference.md)

---

← [Create a Tenant](create-a-tenant.md) | [Home](../index.md) | [System Configuration →](system-configuration.md)
