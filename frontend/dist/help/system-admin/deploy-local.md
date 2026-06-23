---
title: "Deploy Locally"
audience: system-admin
area: system-admin
updated: 2026-06-14
---

![Terminal — `docker compose ps` showing all services healthy.](../assets/screencaps/deploy-local-docker-ps.png)

## What this covers

Detailed configuration reference for a local Docker Compose deployment. This complements the guided install article in Getting Started. It covers environment variable configuration, volume management, resource requirements, and debug logging.

---

## Requirements

- Docker Desktop 24 or later with Compose v2.
- Minimum 2 CPUs and 4 GB RAM for a development deployment. 4 CPUs and 8 GB RAM recommended for sustained use.
- Ports 5433, 8080, 3000, and 3443 free on the host. (3000 serves the web UI over plain HTTP; 3443 serves it over HTTPS — see below.)

---

## Starting and stopping

```bash
# Start all services in the background
docker compose up -d

# Check service status
docker compose ps

# Stream logs for a specific service
docker compose logs -f query-router

# Stop all services (data volumes preserved)
docker compose down
```

---

## Accessing the web UI (HTTP and HTTPS)

A local deployment serves the web UI two ways:

- **`http://localhost:3000`** — plain HTTP. Fine for everyday browser use on your own machine.
- **`https://localhost:3443`** — HTTPS, using a locally-generated certificate. This is the address you use for the **Excel add-in**: Microsoft Office only loads add-ins served over HTTPS, so the add-in's manifest points at `https://localhost:3443`, not the HTTP port.

The HTTPS certificate for `localhost:3443` is created during deployment (the certificate step, `03b_certs`). It is a self-signed development certificate, so a browser may warn you the first time — that warning is expected for local development and does not apply to a real deployment, which serves over a trusted certificate. If the Excel add-in cannot connect, confirm port 3443 is free and that the certificate step ran successfully.

---

## Persistent data

Tessallite uses one named volume: `tessallite_pgdata` — stores workspace metadata, model definitions, aggregate build history, and the query miss log.

This volume persists across `docker compose down` and `docker compose up` cycles. To delete it and all Tessallite data, use `docker compose down -v`. See the Teardown article for details.

---

## Environment variable configuration

Copy `.env.example` to `.env` in the same directory as `docker-compose.yml`. Docker Compose reads this file automatically. The minimum required values are:

```
POSTGRES_PASSWORD=your-strong-password
CREDENTIAL_ENCRYPTION_KEY=your-fernet-key-here
JWT_SECRET_KEY=your-jwt-secret-minimum-32-characters
SYSTEM_ADMIN_PASSWORD=your-admin-password
```

Never commit `.env` to source control — it is listed in `.gitignore`. Change all four values from their placeholder defaults before running in any shared environment. See [Configure Environment Variables](configure-environment-variables.md) for the full variable list and how to generate the Fernet key.

---

## Environment variable reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `POSTGRES_PASSWORD` | Yes | — | Password for the internal PostgreSQL user. Docker Compose uses this to build the connection URL for every service. |
| `CREDENTIAL_ENCRYPTION_KEY` | Yes | — | Fernet key for encrypting source database credentials. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `JWT_SECRET_KEY` | Yes | — | Signs user session tokens. Minimum 32 characters. |
| `SYSTEM_ADMIN_EMAIL` | No | `admin@tessallite.local` | System admin login email. |
| `SYSTEM_ADMIN_PASSWORD` | Yes | — | System admin password. |
| `JDBC_PORT` | No | `5433` | Host port for the JDBC listener. |
| `XMLA_PORT` | No | `8080` | Host port for the XMLA listener. |
| `LOG_LEVEL` | No | `info` | One of: `debug`, `info`, `warn`, `error`. |

---

## Enabling debug logging

```
LOG_LEVEL=debug
```

Add this to your `.env` file. Debug output is high volume — use it for short-duration troubleshooting only.

---

## Related

- [Architecture Overview](architecture-overview.md)
- [Configure Environment Variables](configure-environment-variables.md)
- [Install Tessallite Locally](../getting-started/install-local.md)

---

← [Architecture Overview](architecture-overview.md) | [Home](../index.md) | [Deploy on GCP →](deploy-gcp.md)
