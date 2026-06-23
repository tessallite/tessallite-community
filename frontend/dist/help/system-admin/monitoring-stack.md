---
title: "Monitoring Stack (Prometheus & Grafana)"
audience: system-admin
area: system-admin
updated: 2026-05-23
---

## What this covers

How to deploy, configure, and extend the optional Prometheus + Grafana monitoring stack that provides real-time observability into every Tessallite service. This page explains the architecture, what data is collected, how the dashboard is organised, and how to add your own metrics and panels.

---

## Overview

Tessallite ships with an optional monitoring stack that runs as a completely separate Docker Compose project. It is not required to run the platform and can be started or stopped independently without affecting any Tessallite service.

The stack consists of three containers:

| Container | Image | Purpose |
|---|---|---|
| **prometheus** | `prom/prometheus:v2.51.0` | Scrapes `/metrics` from every service every 15 seconds, stores time-series data for up to 15 days |
| **grafana** | `grafana/grafana:10.4.0` | Visualises metrics through a pre-built dashboard with 21 panels across three sections |
| **nginx-exporter** | `nginx/nginx-prometheus-exporter:1.1` | Translates the frontend's nginx `stub_status` into Prometheus-format metrics |

All three containers live in the `monitoring/` directory at the workspace root, separate from the main `tessallite/infra/` Docker Compose stack.

---

## How it connects to Tessallite

Both stacks share a Docker network called `tessallite_net`. This allows Prometheus (running in the monitoring stack) to reach every Tessallite service by container name, even though they are managed by different Docker Compose projects.

The main stack creates the network automatically on `docker compose up`. The monitoring stack's deploy script also creates it if it does not exist, so either stack can be started first.

---

## Deploying the monitoring stack

### Prerequisites

- Docker and Docker Compose v2 installed
- The main Tessallite stack running (or at least one `docker compose up` to create the shared network)

### Steps

1. Navigate to the monitoring directory:

   ```bash
   cd monitoring/
   ```

2. Create the environment file:

   ```bash
   cp .env.example .env
   ```

3. Set the Grafana admin password in `.env`:

   ```
   GRAFANA_ADMIN_PASSWORD=your-password-here
   ```

4. Deploy:

   ```bash
   bash deploy.sh        # Linux / macOS / Git Bash
   deploy.bat            # Windows
   ```

5. Open the dashboards:
   - **Prometheus:** http://127.0.0.1:9090
   - **Grafana:** http://127.0.0.1:3001 (username: `admin`, password: your `.env` value)

### Teardown

```bash
bash teardown.sh              # stop and remove data
bash teardown.sh --keep-data  # stop but preserve Prometheus and Grafana volumes
```

---

## What data is collected

### Scraped services

Prometheus scrapes metrics from all seven Tessallite services:

| Service | Port | Metrics source |
|---|---|---|
| model-service | 8001 | `prometheus-client` Python library via FastAPI middleware |
| query-router | 8000 | `prometheus-client` Python library via FastAPI middleware |
| optimizer | 8000 | `prometheus-client` Python library via FastAPI middleware |
| scheduler | 8000 | `prometheus-client` Python library via FastAPI middleware |
| agent-service | 8000 | `prometheus-client` Python library via FastAPI middleware |
| gateway | 8080 | `prometheus-client` Python library via FastAPI middleware |
| frontend | nginx-exporter (9113) | nginx `stub_status` translated by the exporter sidecar |

### Platform metrics

Every Python service (all except frontend) exposes these metrics automatically via shared middleware:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `tessallite_http_requests_total` | Counter | service, method, path, status | Total HTTP requests handled |
| `tessallite_http_request_duration_seconds` | Histogram | service, method, path | Request latency in seconds |

### Query routing metrics

The query-router emits these after every query execution:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `tessallite_query_router_queries_total` | Counter | routed_to | Queries routed to source, aggregate, or pocket |

### Model-level usage metrics

The query-router tracks per-model analytics:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `tessallite_model_queries_total` | Counter | tenant, project, model_name, protocol, route_type | Query volume per model |
| `tessallite_model_query_errors_total` | Counter | tenant, project, model_name, error_type | Failed queries per model |
| `tessallite_model_query_duration_seconds` | Histogram | tenant, project, model_name | Query execution time per model |
| `tessallite_model_bytes_processed_total` | Counter | tenant, project, model_name | Bytes scanned per model |
| `tessallite_model_rows_returned_total` | Counter | tenant, project, model_name | Rows returned per model |

### Aggregate refresh metrics

The scheduler emits these on every refresh run (full or incremental):

| Metric | Type | Labels | Description |
|---|---|---|---|
| `tessallite_refresh_runs_total` | Counter | status | Completed vs failed refreshes |
| `tessallite_refresh_run_duration_seconds` | Histogram | mode | Refresh duration (full or incremental) |

### Frontend (nginx) metrics

The nginx-exporter translates nginx's `stub_status` into standard metrics:

| Metric | Type | Description |
|---|---|---|
| `nginx_connections_active` | Gauge | Current active client connections |
| `nginx_connections_accepted` | Counter | Total accepted connections |
| `nginx_connections_handled` | Counter | Total handled connections |
| `nginx_http_requests_total` | Counter | Total HTTP requests served |

---

## Dashboard sections

The Grafana dashboard is organised into three collapsible sections with four filter variables at the top: **Service**, **Tenant**, **Project**, and **Model**. Selecting a tenant narrows the project dropdown, and selecting a project narrows the model dropdown.

### Service Health

| Panel | What it shows |
|---|---|
| Service Status | Live UP/DOWN tiles for all seven services |
| Service Uptime | Availability over time (1 = up, 0 = down) |
| Scrape Duration | How long Prometheus takes to collect from each target |
| Request Rate per Service | HTTP requests per second |
| Error Rate per Service | 5xx errors per second |
| Service Latency p95 | 95th percentile response time per service |
| Service Latency p50 | Median response time per service |

### Query Routing and Aggregates

| Panel | What it shows |
|---|---|
| Query Routing Distribution | Rate of queries routed to source, aggregate, or pocket |
| HTTP Error Rate | 5xx errors broken down by service |
| Refresh Run Duration p95 | 95th percentile refresh time (full vs incremental) |
| Refresh Run Completions | Rate of completed vs failed refreshes |

### Model Health and Usage

| Panel | What it shows |
|---|---|
| Queries per Model | Query throughput broken down by model |
| Model Query Latency p95 | 95th percentile query execution time per model |
| Protocol Distribution | SQL vs DAX query volume (pie chart) |
| Route Distribution by Model | Source/aggregate/pocket split (pie chart) |
| Model Query Errors | Failed query rate per model by error type |
| Data Scanned per Model | Bytes processed rate per model |
| Rows Returned per Model | Result volume rate per model |

---

## Adding custom metrics

All Tessallite metrics are defined in `tessallite/shared/metrics.py` using the Python `prometheus-client` library. To add a new metric:

1. **Define the metric** in `shared/metrics.py`:

   ```python
   MY_COUNTER = Counter(
       "tessallite_my_counter_total",
       "Description of what this counts",
       ["label_a", "label_b"],
   )
   ```

2. **Instrument your code** in the relevant service:

   ```python
   from shared.metrics import MY_COUNTER

   MY_COUNTER.labels(label_a="value", label_b="value").inc()
   ```

3. **Rebuild the service** to include the new code:

   ```bash
   docker compose build <service-name>
   docker compose up -d <service-name>
   ```

The new metric will appear automatically on the service's `/metrics` endpoint. Prometheus will begin scraping it on the next 15-second cycle. No Prometheus configuration changes are needed.

---

## Adding a Grafana panel

1. Open Grafana at http://127.0.0.1:3001 and sign in.
2. Navigate to the **Tessallite Platform Overview** dashboard.
3. Click **Edit** (pencil icon) and then **Add panel**.
4. Write a PromQL query referencing your metric, for example:

   ```
   sum by (label_a) (rate(tessallite_my_counter_total[5m]))
   ```

5. Save the dashboard.

To make the panel permanent (survives container restarts), export the dashboard JSON and save it to `monitoring/grafana/tessallite-dashboard.json`. Grafana's file provisioner reloads it every 30 seconds.

---

## Adding a new scrape target

If you add a new service to the Tessallite stack:

1. Add a `PrometheusMiddleware` and `/metrics` endpoint to the service (see any existing service's `main.py` for the pattern).
2. Add a scrape job to `monitoring/prometheus.yml`:

   ```yaml
   - job_name: my-new-service
     static_configs:
       - targets: ["my-new-service:8000"]
   ```

3. Restart Prometheus:

   ```bash
   cd monitoring/ && docker compose restart prometheus
   ```

---

## Data retention

Prometheus retains time-series data for **15 days** by default (configured via `--storage.tsdb.retention.time=15d` in `docker-compose.yml`). To change this, edit the Prometheus command arguments and restart.

---

## Frequently asked questions

**Do I need the monitoring stack to run Tessallite?**
No. The monitoring stack is entirely optional. Tessallite operates normally without it. The services always expose `/metrics` endpoints regardless of whether anything scrapes them.

**Will stopping the monitoring stack affect Tessallite?**
No. The monitoring containers are independent. Stopping them has zero impact on the platform.

**Where is monitoring data stored?**
Prometheus data is in the `monitoring_prometheus_data` Docker volume. Grafana configuration and any manual dashboard changes are in `monitoring_grafana_data`. Both are preserved across container restarts. Use `teardown.sh --keep-data` to stop without deleting them.

**Can I use this in production?**
Yes. The setup is production-ready for small to medium deployments. For high-availability production environments, consider external Prometheus (e.g., Grafana Cloud, Datadog, or a managed Prometheus service) and point it at the same `/metrics` endpoints.

---

## Related

- [Architecture Overview](architecture-overview.md)
- [Service Reference](service-reference.md)
- [Deploy Locally](deploy-local.md)
- [System Configuration](system-configuration.md)

---

<- [Upgrade](upgrade.md) | [Home](../index.md)
