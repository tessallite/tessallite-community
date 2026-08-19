---
title: "Deploy on GCP"
audience: system-admin
area: system-admin
updated: 2026-06-14
---

## What this covers

GCP-specific architecture for a production Tessallite deployment: how services are distributed across Cloud Run and a Compute Engine VM, the database connection model, service account permissions, health check configuration, and instance scaling.

---

## Architecture on GCP

Most Tessallite services run as Cloud Run services. The PostgreSQL metadata database and the JDBC/XMLA gateway run together on a single **Compute Engine VM** (see `deploy/gcp/db-vm/`). This split exists because Cloud Run only serves HTTP — it cannot accept raw TCP connections on port 5433 that JDBC clients require, and it cannot host a persistent PostgreSQL process.

The Compute Engine VM hosts:
- PostgreSQL (the system database plus per-tenant schemas)
- The JDBC/XMLA gateway, which listens on TCP 5433 (JDBC) and TCP 8080 (XMLA)

The Cloud Run services (model-service, query-router, optimizer, scheduler, agent-service, frontend) connect to PostgreSQL using a full connection URL stored in Secret Manager.

---

## Environment variables for GCP

The three secrets that every Cloud Run service must have at startup are stored in Secret Manager and mounted as environment variables:

| Variable | Purpose |
|---|---|
| `SYSTEM_DATABASE_URL` | Full PostgreSQL connection URL pointing at the Compute Engine VM. Format: `postgresql+asyncpg://user:password@<vm-ip>:5432/tessallite_system` |
| `CREDENTIAL_ENCRYPTION_KEY` | Fernet key used to encrypt source database credentials at rest. Generate with `from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())` |
| `JWT_SECRET_KEY` | Secret used to sign user session tokens. Minimum 32 characters. |

The `SYSTEM_ADMIN_PASSWORD` is also required on first startup. For the complete variable list, see the [Configuration Reference](../../../docs/guides/guides_configuration-reference.md).

---

## Service account permissions

| Role | Required by | Purpose |
|---|---|---|
| `roles/secretmanager.secretAccessor` | All Cloud Run services | Read secrets from Secret Manager at startup |
| `roles/run.invoker` | All services | Allow inter-service HTTP calls within Cloud Run |
| `roles/artifactregistry.writer` | Build / deploy service account | Push and pull images from Artifact Registry |
| `roles/run.admin` | Build / deploy service account | Deploy Cloud Run services |

---

## Health checks

Each Cloud Run service exposes `GET /health` on its HTTP port. A healthy response returns HTTP 200 with `{"status":"ok"}`. The scripted deploy configures Cloud Scheduler wake-up jobs that ping this endpoint to keep the scheduler and optimizer alive (see Instance scaling below).

---

## Instance scaling, scale-to-zero, and wake-up jobs

Cloud Run bills only while an instance is handling a request, so most Tessallite services are deployed to **scale to zero** — when no traffic arrives, Google stops the container and you pay nothing for it. The trade-off is the *cold start*: the first request after an idle period waits a second or two while a new instance boots.

How the scripted deploy (`deploy/gcp`) configures each service:

- **gateway** — keep warm where JDBC is in use (`--min-instances=1`), because JDBC clients hold long-lived connections that cannot tolerate a cold start mid-session. An XMLA-only gateway can tolerate scale-to-zero.
- **scheduler and optimizer** — deployed with `min-instances=0` and `cpu-throttling=false` on gen2 execution. They cost nothing while idle but keep full CPU during a request.
- **other platform services** — scale to zero; a cold start on the first request is acceptable.

**The wake-up problem.** The scheduler and optimizer fire their refresh and sweep jobs with an in-process scheduler (APScheduler). A container that has scaled to zero has *no running process*, so it cannot fire a job on its own — a refresh due at 02:00 would simply never run if nothing woke the service. To solve this without paying for an always-on instance, the deploy creates two **Cloud Scheduler** jobs that ping each service's `/health` endpoint on a `*/15 * * * *` schedule (every 15 minutes, on the quarter hour, UTC):

| Cloud Scheduler job | Wakes | What it does |
|---|---|---|
| `tessallite-scheduler-wakeup` | scheduler | Brings the scheduler up so any due refresh jobs fire |
| `tessallite-optimizer-wakeup` | optimizer | Brings the optimizer up so its sweeps fire |

Both jobs fire on the cadence set by `SCHEDULER_WAKEUP_CRON` (default `0 * * * *`). The deploy enables the `cloudscheduler.googleapis.com` API automatically, and `teardown.sh` removes both jobs.

**What this means for you as an operator.** Scheduled work runs on the quarter-hour grid, not to the exact second you configured — a job set for 02:07 effectively runs at the next wake-up (02:15). If refreshes look like they are being *missed*, do not suspect the scheduler first: confirm both wake-up jobs exist with `gcloud scheduler jobs list` and are not failing. A disabled or deleted wake-up job is the most common cause of "my aggregates stopped refreshing in the cloud".

---

## Non-interactive (autonomous) deploy

`deploy/gcp/deploy.sh` is the interactive, step-tracked deploy used for the first bring-up. For repeat deploys, CI, or unattended runs, use `deploy/gcp/auto-deploy.sh`, which builds and deploys without prompts:

```bash
bash auto-deploy.sh                 # all 8 services (default)
bash auto-deploy.sh platform        # the 7 platform services only
bash auto-deploy.sh chat            # conversational-client only
bash auto-deploy.sh <service-name>  # one named service, e.g. tessallite-scheduler
```

Useful flags: `--skip-build` (deploy existing images without rebuilding), `--skip-iam` (skip the public-invoker IAM binding), and `--dry-run` (print the plan and resolved URLs without changing anything). Run the interactive `deploy.sh` once first — `auto-deploy.sh` assumes the one-time bootstrap (project/API enablement, IAM, database, Artifact Registry) is already in place.

---

## Database firewall (scripted deploy)

The scripted deploy opens a firewall rule so the Cloud Run services can reach the metadata database on TCP 5432. By default the allowed source range is `0.0.0.0/0` — **open to the whole internet**, guarded only by the database password — because Cloud Run egress addresses are not fixed. The deploy prints a loud warning every time this default is left in place.

For anything beyond a throwaway demo you should scope this down. Set `POSTGRES_SOURCE_RANGES` in `config.env` to the CIDR range your services actually egress from (for example a VPC connector / NAT range). The rule is re-applied on every deploy, so a tightened value takes effect on the next run — you never delete the old rule by hand. Leaving the database reachable from the public internet is acceptable only for a disposable demo; a production deployment should put the database on a private IP behind a VPC connector so it has no public exposure at all.

---

## Demo login on cloud builds

The frontend can be built with a one-click **"Sign in to demo"** button, controlled by build-time variables baked into the served bundle:

- `VITE_ENABLE_DEMO_LOGIN` — set to `true` to show the demo-login button.
- `VITE_DEMO_TENANT_SLUG`, `VITE_DEMO_EMAIL`, `VITE_DEMO_PASSWORD` — the demo credentials the button signs in with.

Because these are compiled into the JavaScript bundle, the demo password is **not a secret** — anyone who opens the page can read it. Only enable demo login on a throwaway demo tenant, never on a deployment that holds real data. To disable it, leave `VITE_ENABLE_DEMO_LOGIN` unset (or `false`) and rebuild the frontend image.

---

## Estimated cost

Cost depends on region, VM shape, disk type and size, reserved external-IP state, Cloud Run traffic, and network egress. Suspending stops VM compute, but the persistent disk and a reserved external IP can continue to incur charges while idle. Check the current [Compute Engine disk pricing](https://cloud.google.com/compute/disks-image-pricing), [external IP pricing](https://cloud.google.com/vpc/network-pricing#ipaddress), and [Cloud Run pricing](https://cloud.google.com/run/pricing) for the deployment region before budgeting. Use `suspend.sh` to stop compute between sessions; do not treat suspension as zero cost.

Suspend and resume now fail closed: success is printed only after the wake-up scheduler
jobs, public IAM, gateway, database readiness, and VM state have been verified. A partial
transition exits nonzero; correct the reported state before retrying.

---

## Related

- [Deploy Locally](deploy-local.md)
- [Configure Environment Variables](configure-environment-variables.md)
- [Install on GCP](../getting-started/install-gcp.md)

---

← [Deploy Locally](deploy-local.md) | [Home](../index.md) | [Deploy on Kubernetes →](deploy-kubernetes.md)
