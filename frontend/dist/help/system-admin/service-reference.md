---
title: "Service Reference"
audience: system-admin
area: system-admin
updated: 2026-04-17
---

## What this covers

Per-service details for all six Tessallite services: default ports, health endpoints, inter-service dependencies, restart behavior, and log format. Includes a service summary table and dependency graph.

---

## gateway

**Purpose:** Public entry point for BI tool query traffic. Translates JDBC and XMLA queries into an internal format and forwards them to query-router.

- **Ports:** 5433 (JDBC, PostgreSQL wire protocol), 8080 (XMLA/HTTP)
- **Health endpoint:** `GET /api/health` on port 8080. Returns `{"status":"ok"}` when ready.
- **Depends on:** query-router
- **Restart behavior:** Stateless. Safe to restart at any time. Active JDBC connections will drop and must reconnect.
- **Log format:** Structured JSON lines on stdout.

---

## query-router

**Purpose:** Routes translated queries to a pre-built aggregate or to the raw data source. Records query misses.

- **Port:** Internal HTTP only. Not exposed to the host network.
- **Health endpoint:** `GET /api/health`. Returns `{"status":"ok"}` when ready.
- **Depends on:** model-service
- **Restart behavior:** Stateless. Safe to restart at any time.
- **Log format:** Structured JSON lines on stdout.

---

## model-service

**Purpose:** Serves model definitions to query-router and the frontend. Writes query misses to the internal database.

- **Port:** Internal HTTP only. Not exposed to the host network.
- **Health endpoint:** `GET /api/health`. Returns `{"status":"ok","db_migration":"VERSION"}` when ready.
- **Depends on:** internal PostgreSQL (postgres)
- **Restart behavior:** Stateless. All model data persisted in PostgreSQL. Safe to restart at any time.
- **Log format:** Structured JSON lines on stdout.

---

## optimizer

**Purpose:** Reads the query miss log, scores aggregate candidates by ROI, and creates build recommendations.

- **Port:** Internal HTTP only. Not exposed to the host network.
- **Health endpoint:** `GET /api/health`. Returns `{"status":"ok"}` when ready.
- **Depends on:** model-service
- **Restart behavior:** Stateless. Recommendations recalculated from miss log on each run.
- **Log format:** Structured JSON lines on stdout.

---

## scheduler

**Purpose:** Builds and refreshes aggregate tables in the user's target schema. Executes on a cron schedule and on demand.

- **Port:** Internal HTTP only. Not exposed to the host network.
- **Health endpoint:** `GET /api/health`. Returns `{"status":"ok"}` when ready.
- **Depends on:** model-service
- **Restart behavior:** Stateless. In-progress builds may be interrupted on restart; scheduler will re-queue them on the next run.
- **Log format:** Structured JSON lines on stdout.

---

## frontend

**Purpose:** Web management interface for model building, aggregate management, connection setup, and platform monitoring.

- **Port:** 3000 (HTTP). Exposed to the host network.
- **Health endpoint:** `GET /api/health` on port 3000. Returns `{"status":"ok"}` when ready.
- **Depends on:** model-service
- **Restart behavior:** Stateless. Active user sessions may need to re-authenticate after restart.
- **Log format:** Structured JSON lines on stdout.

---

## Service dependency graph

- **gateway** depends on query-router
- **query-router** depends on model-service
- **model-service** depends on internal PostgreSQL
- **optimizer** depends on model-service
- **scheduler** depends on model-service
- **frontend** depends on model-service

Start order from scratch: postgres → model-service → (query-router, optimizer, scheduler, frontend) → gateway.

---

## Service summary

| Service | Port | Health endpoint | Depends on | Stateless |
|---|---|---|---|---|
| gateway | 5433, 8080 | `GET :8080/api/health` | query-router | Yes |
| query-router | Internal | `GET /api/health` | model-service | Yes |
| model-service | Internal | `GET /api/health` | postgres | Yes |
| optimizer | Internal | `GET /api/health` | model-service | Yes |
| scheduler | Internal | `GET /api/health` | model-service | Yes |
| frontend | 3000 | `GET :3000/api/health` | model-service | Yes |

---

## Related

- [Configure Environment Variables](configure-environment-variables.md)
- [Architecture Overview](architecture-overview.md)
- [Teardown](teardown.md)

---

← [Credentials and the .env File](credentials-and-env.md) | [Home](../index.md) | [Teardown →](teardown.md)
