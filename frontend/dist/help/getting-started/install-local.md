---
title: "Install Tessallite Locally"
audience: system-admin
area: getting-started
updated: 2026-04-17
---

## What this covers

Installing Tessallite on a local machine using Docker Compose. The deploy script handles all steps automatically.

## Before you start

- Role required: System Admin. You are setting up the platform itself.
- Docker Desktop 24 or later with Compose v2 must be installed and running.
- At least 4 GB of RAM is required. 8 GB is recommended.
- Ports 5433 and 8080 must be free on your machine before running the script.

## Steps

1. Clone or download the Tessallite repository to your machine.
2. Open a terminal and navigate to `deploy/local/`.
3. Run the deploy script. On Windows, type `deploy.bat` and press Enter. On Mac or Linux, type `./deploy.sh` and press Enter.
4. The script checks that Docker is running and that the required ports are available. If any check fails, the script reports the problem and stops. Address the issue before re-running.
5. The script prompts for a PostgreSQL password, a JWT signing key, and a Fernet encryption key. Enter values when prompted, or press Enter to accept auto-generated values.
6. The script builds the container images and starts all services. This takes several minutes on the first run.
7. When the script reports "All steps complete", open a browser and navigate to `http://localhost:3000`. The sign-in screen should appear.

## What starts

After the script completes, the following services are running:

| Service | Address | What it does |
|---|---|---|
| Frontend | http://localhost:3000 | Web management interface |
| Model Service | http://localhost:8001 | Metric definitions and access control |
| Query Router | http://localhost:8002 | Query parsing, routing, and execution |
| Optimizer | http://localhost:8003 | Automatic aggregate creation |
| Scheduler | http://localhost:8004 | Aggregate refresh and drift detection |
| Gateway (JDBC) | localhost:5433 | BI tool database connection |
| Gateway (XMLA) | http://localhost:8080 | Excel and Power BI connection |

## Re-running the script

The script is idempotent. Re-running it skips steps that already completed. To force a step to re-run, open `deploy/local/config.env` and set the relevant flag to `0`.

## Stopping the services

To stop all containers, run `teardown.bat` (Windows) or `./teardown.sh` (Mac/Linux) from the `deploy/local/` folder. This stops the containers and removes their data volumes.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Port already in use | Another application is using port 5433 or 8080 | Stop the conflicting application or change the port in the deploy config |
| Docker not found | Docker Desktop is not installed or not running | Install Docker Desktop and start it before running the script |
| Build fails | Network issue during image download | Check internet connection and re-run the script |
| localhost:3000 shows nothing | Services still starting | Wait 60 seconds and refresh |

## Related

- [First-time setup](first-time-setup.md)
- [Install on GCP](install-gcp.md)
- [Service reference](../system-admin/service-reference.md)

---

← [Demo tenant: acme-demo](acme-demo-tenant.md) | [Home](../index.md) | [Deploy Tessallite to Google Cloud Platform →](install-gcp.md)
