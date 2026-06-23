---
title: "Teardown"
audience: system-admin
area: system-admin
updated: 2026-04-17
---

## What this covers

How to remove a Tessallite deployment for both local (Docker Compose) and GCP targets. Covers what data is destroyed, what is left behind, and how to clean up aggregate tables in your data source.

---

## What teardown destroys

A full teardown removes:
- All workspace metadata (workspaces, users, connection definitions)
- All semantic model definitions (tables, joins, dimensions, measures, aggregates)
- The query miss log and optimizer recommendations
- Aggregate build history and refresh schedules

Teardown does **not** remove:
- Your source data — Tessallite never writes to your source tables.
- Aggregate tables built in your target schema. These are plain tables in your data source and must be dropped manually.

---

## Local teardown (Docker Compose)

### Stop containers, preserve data

```bash
docker compose down
```

Running `docker compose up -d` afterward restores the platform with all data intact.

### Stop containers and delete all data

> **Warning:** `docker compose down -v` deletes all Tessallite metadata permanently. Workspaces, models, and aggregate definitions cannot be recovered. Aggregate data tables in your source must be dropped manually if you want to remove them.

```bash
docker compose down -v
```

This removes the `tessallite_pgdata` named volume. All records in the internal PostgreSQL database are permanently deleted.

### Remove aggregate tables from your data source

After running `docker compose down -v`, connect to your data source and drop aggregate tables from the target schema. Tessallite creates aggregate tables with names beginning with `_tess_` by default. Verify the naming pattern in your workspace connection settings before dropping.

---

## GCP teardown

Delete resources in this order:

1. Delete each Cloud Run service:
   ```bash
   gcloud run services delete SERVICE_NAME --region REGION
   ```
   Repeat for: gateway, query-router, model-service, optimizer, scheduler, frontend.

2. Delete the Cloud SQL instance:
   ```bash
   gcloud sql instances delete INSTANCE_NAME
   ```
   This permanently deletes the internal database and all Tessallite metadata.

3. Delete load balancer forwarding rules and backend services via the Cloud Console: Network Services > Load Balancing.

4. Delete the service account:
   ```bash
   gcloud iam service-accounts delete SERVICE_ACCOUNT_EMAIL
   ```

5. Delete secrets from Secret Manager if used:
   ```bash
   gcloud secrets delete tessallite-db-pass
   gcloud secrets delete tessallite-session-secret
   ```

6. Optionally drop aggregate tables from your data source. Connect to your data warehouse and drop them manually.

> **Warning:** Deleting the Cloud SQL instance is irreversible. Back up the database before proceeding if you may need the model definitions or workspace configuration later.

---

## Related

- [Upgrade](upgrade.md)
- [Deploy Locally](deploy-local.md)
- [Deploy on GCP](deploy-gcp.md)

---

← [Service Reference](service-reference.md) | [Home](../index.md) | [Upgrade →](upgrade.md)
