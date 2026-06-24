---
title: "Deploy Tessallite to Google Cloud Platform"
audience: system-admin
area: getting-started
updated: 2026-06-24
---

## What this covers

Deploying Tessallite to Google Cloud Platform for an operator-managed environment. This is not the Community desktop bundle flow: local Community installs come from the signed website bundle, while GCP deployments use the `deploy/gcp` automation because they create cloud infrastructure.

## Before you start

- Role required: System Admin.
- A Google Cloud project with billing enabled is required.
- The gcloud CLI must be installed and authenticated. Run `gcloud auth login` before proceeding.
- Docker must be installed and running on your machine.
- The following GCP APIs must be enabled: Cloud Run, Artifact Registry, Secret Manager, Cloud SQL / Compute Engine, and Cloud Scheduler. The script enables them automatically if your account has permission.

## Steps

1. Open a terminal and navigate to `deploy/gcp/`.
2. Run the deploy script. On Windows, type `deploy.bat`. On Mac or Linux, type `./deploy.sh`.
3. When prompted, enter your GCP project ID and the region you want to deploy to (for example, `us-central1`).
4. The script enables the required GCP APIs, creates the database/gateway infrastructure, stores secrets in Secret Manager, and sets IAM permissions for the Cloud Run service account.
5. The script builds Docker images for the platform services and pushes them to Artifact Registry.
6. The script deploys the Cloud Run services: model-service, query-router, optimizer, scheduler, agent-service, and frontend. The gateway and PostgreSQL run on the Compute Engine side of the deployment when JDBC is required.
7. The script runs database migrations and creates the first workspace and admin user. It prints prompts for the workspace slug and admin credentials.
8. When deployment completes, the script prints the public URL of each service. The XMLA endpoint is reachable over HTTPS. JDBC needs the gateway VM or another TCP-capable gateway host.

## JDBC on GCP

Cloud Run does not route raw TCP traffic. The JDBC endpoint (PostgreSQL wire protocol, port 5433) requires a separate TCP load balancer. For Excel and Power BI connections using XMLA, no load balancer is needed — the HTTPS URL works directly.

## Configuration

On GCP, deployment settings live in `deploy/gcp/config.env`. Non-secret values are written into the Cloud Run service manifests at deploy time; secrets such as the database URL, encryption key, JWT key, and admin password are stored in Secret Manager, not committed to the repo. The full list of every parameter — and which file carries it in each deployment mode — is in the configuration reference (`docs/guides/guides_configuration-reference.md`).

For GCP, install or replace the signed licence from **System Admin → License & edition** after the deployment is running. Tessallite verifies the uploaded `license.json`, stores it in the platform database, and applies it immediately.

The **System Admin → License & edition** page also shows edition, enforcement, capacity, and whether a licence is installed.

### Important: LLM cost safety

A hosted deployment is where LLM cost can run away fastest, so this setting matters most here:

- **`LLM_ALLOW_SERVICE_ACCOUNT_AUTH`** — keep it `false` (the default). While `false`, the conversational agent can authenticate **only** with a bring-your-own API key, so usage is billed to the key's owner. Setting it `true` would also allow cloud service-account billing (for example Google Vertex AI), which charges **your** GCP project directly — including for the public demo agent. Only enable it if you intend to pay for LLM usage from this project's billing account, and pair it with a hard quota on the Generative Language / Vertex AI API.

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
