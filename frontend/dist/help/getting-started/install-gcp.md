---
title: "Deploy Tessallite to Google Cloud Platform"
audience: system-admin
area: getting-started
updated: 2026-04-17
---

## What this covers

Deploying Tessallite to Google Cloud Platform using Cloud Run. The deploy script handles all steps.

## Before you start

- Role required: System Admin.
- A Google Cloud project with billing enabled is required.
- The gcloud CLI must be installed and authenticated. Run `gcloud auth login` before proceeding.
- Docker must be installed and running on your machine.
- The following GCP APIs must be enabled: Cloud Run, Container Registry, Secret Manager, Cloud SQL. The script enables them automatically if your account has permission.

## Steps

1. Open a terminal and navigate to `deploy/gcp/`.
2. Run the deploy script. On Windows, type `deploy.bat`. On Mac or Linux, type `./deploy.sh`.
3. When prompted, enter your GCP project ID and the region you want to deploy to (for example, `us-central1`).
4. The script enables the required GCP APIs, creates a Cloud SQL PostgreSQL 15 instance, creates four secrets in Secret Manager (encryption key, JWT key, database URL, database password), and sets IAM permissions for the Cloud Run service account.
5. The script builds Docker images for all services and pushes them to Container Registry.
6. The script deploys five services to Cloud Run in order: Model Service first, then Query Router, Optimizer, Scheduler, and Gateway.
7. The script runs database migrations and creates the first workspace and admin user. It prints prompts for the workspace slug and admin credentials.
8. When deployment completes, the script prints the public URL of each service. The Gateway's XMLA endpoint is reachable immediately. Note the Gateway URL — this is what Excel and Power BI connect to.

## JDBC on GCP

Cloud Run does not route raw TCP traffic. The JDBC endpoint (PostgreSQL wire protocol, port 5433) requires a separate TCP load balancer. For Excel and Power BI connections using XMLA, no load balancer is needed — the HTTPS URL works directly.

## Re-running the script

The script skips completed steps. To force a step to re-run, set its flag to `0` in `deploy/gcp/config.env`.

## Updating after code changes

Re-run the script. It always rebuilds images and redeploys services, even if other steps are skipped.

## Removing the deployment

Run `teardown.bat` or `./teardown.sh` from `deploy/gcp/`. This removes Cloud Run services, Cloud SQL, and secrets from Secret Manager.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| "API not enabled" | Script could not enable APIs automatically | Enable them manually in the GCP console and re-run |
| Build fails | Docker not running or network issue | Start Docker and check connectivity |
| JDBC connection refused | No TCP load balancer configured | Set up a Network Load Balancer or use XMLA instead |
| Services show unhealthy | Secrets not created before deploy | Re-run; the script creates secrets before deploying |

## Related

- [Install locally](install-local.md)
- [First-time setup](first-time-setup.md)
- [Configure environment variables](../system-admin/configure-environment-variables.md)

---

← [Install Tessallite Locally](install-local.md) | [Home](../index.md) | [Connect a BI Tool via JDBC →](connect-a-bi-tool.md)
