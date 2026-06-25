---
title: "Upgrade"
audience: system-admin
area: system-admin
updated: 2026-04-17
---

## What this covers

How to upgrade a running Tessallite installation to a new version for both local (Docker Compose) and GCP deployments. Includes database backup, image update, health verification, and rollback procedure.

> **Warning:** Always back up the internal PostgreSQL database before upgrading. A new version may apply schema migrations that cannot be reversed automatically.

---

## Local upgrade (Docker Compose)

1. **Back up the internal database.**
   ```bash
   docker compose exec postgres pg_dump -U tessallite tessallite > backup.sql
   ```
   Store `backup.sql` outside the Docker volume before continuing.

2. **Pull the new images.**
   ```bash
   docker compose pull
   ```

3. **Apply the new containers.**
   ```bash
   docker compose up -d
   ```
   Docker Compose restarts each service with the new image in dependency order.

4. **Verify service health.**
   ```bash
   docker compose ps
   ```
   All services should show status `Up`. For any service showing `Exit` or `Restarting`:
   ```bash
   docker compose logs -f SERVICE_NAME
   ```

5. **Verify the database migration version.**
   ```bash
   curl http://localhost:3000/api/health
   ```
   Confirm the `db_migration` field matches the expected version in the release notes.

---

## GCP upgrade (Cloud Run)

1. **Back up Cloud SQL.**
   ```bash
   gcloud sql backups create --instance=INSTANCE_NAME
   ```
   Verify the backup appears in the Cloud SQL backups list before continuing.

2. **Update each service image.** For each Cloud Run service:
   ```bash
   gcloud run services update SERVICE_NAME \
     --image IMAGE_REPOSITORY/SERVICE_NAME:NEW_TAG \
     --region REGION
   ```
   Update in this order: model-service, query-router, optimizer, scheduler, frontend, gateway.

3. **Verify each service after update.**
   ```bash
   curl https://SERVICE_URL/api/health
   ```
   Confirm `{"status":"ok"}` before updating the next service.

4. **Verify the migration version** on the frontend service:
   ```bash
   curl https://FRONTEND_URL/api/health
   ```
   Confirm the `db_migration` field matches the expected version.

---

## Rollback

If a service fails after upgrade:

- **Local:** Pin the previous image tag in `docker-compose.yml`, run `docker compose up -d`, then restore:
  ```bash
  docker compose exec -T postgres psql -U tessallite tessallite < backup.sql
  ```

- **GCP:** Roll back the Cloud Run revision from the Cloud Console (Revisions tab > select previous revision > Send all traffic). Then restore Cloud SQL from the manual backup created in step 1.

---

## Related

- [Teardown](teardown.md)
- [Deploy Locally](deploy-local.md)
- [Deploy on GCP](deploy-gcp.md)
- [JDBC Connection Guide](../integrations/jdbc-connection-guide.md)

---

← [Teardown](teardown.md) | [Home](../index.md) | [Monitoring Stack →](monitoring-stack.md)
