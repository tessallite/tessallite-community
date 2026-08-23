---
title: "Deploy on Kubernetes"
audience: system-admin
area: system-admin
updated: 2026-06-24
---

# Deploy on Kubernetes (Helm)

Tessallite Community runs on any Kubernetes cluster through a Helm chart. This page
explains what the chart deploys, what you must provide, and how to install, verify, and
tear it down — including a zero-cost way to try it on your laptop first.

## When to use this

Use Kubernetes when you want Tessallite to run as a managed, self-healing workload:
rolling restarts, horizontal scaling of the stateless services, and a cloud load
balancer in front of the gateway so BI tools get a stable address. If you just want a
single host, the Docker Compose bundle is simpler — see [Deploy Locally](deploy-local.md).

## What the chart deploys

The chart `tessallite-community` runs the **pre-built Community images** (it never builds
from source). It creates:

- Seven service **Deployments**: model-service, query-router, optimizer, scheduler,
  agent-service, gateway, and the frontend.
- A Postgres **StatefulSet** with a **PersistentVolumeClaim** (your data survives pod
  restarts; it uses the cluster's default StorageClass unless you set one).
- **Services**: most are internal (ClusterIP). The gateway is a **LoadBalancer** because
  BI/SQL clients need raw TCP on 5433 (JDBC) in addition to HTTP/XMLA on 8080.
- An optional **Ingress** for the UI and a default-deny **NetworkPolicy**.

## What you must provide

The chart has no insecure defaults — it refuses to render if a required value is missing.

| Value | Meaning |
|---|---|
| `secrets.postgresPassword` | Database password |
| `secrets.credentialEncryptionKey` | Fernet key (44-character urlsafe base64) that encrypts stored source credentials |
| `secrets.jwtSecretKey` | Session signing secret — must be **at least 32 characters** |
| `secrets.systemAdminPassword` | The first system administrator's password |
| `license.publicKeys` | The licence verifier key, as `key_id:base64publickey` |
| `license.signedDocument` | Your signed Community licence JSON (the chart mounts it as a Secret) |

You also need a place your cluster can pull images from. The install bundle ships the
images as an OCI tarball; for a real cluster, push the seven `ghcr.io/tessallite/<svc>`
images plus `postgres:15` to a registry your nodes can reach and point `global.registry`
and `global.tag` at it.

## The deploy / teardown script

This single script handles every step and, critically, tears the cluster down so you do
not leave cloud resources running. Save it next to the chart (it also ships in the
product at `deploy/community/helm/deploy-kubernetes.sh`).

```bash
#!/usr/bin/env bash
# Tessallite Community — deploy on Kubernetes via Helm.
# Subcommands: secrets | gke-create | install | verify | teardown | gke-delete
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$HERE/tessallite-community"

NAMESPACE="${NAMESPACE:-tessallite}"
RELEASE="${RELEASE:-tessallite}"
REGISTRY="${REGISTRY:-ghcr.io/tessallite}"
TAG="${TAG:-0.1.0}"                       # use an immutable digest in production
LICENSE_FILE="${LICENSE_FILE:-license.json}"   # signed Community licence JSON
SECRETS_ENV="${SECRETS_ENV:-$HERE/k8s-secrets.env}"
LICENSE_PUBLIC_KEYS="${LICENSE_PUBLIC_KEYS:-tessallite-prod-2026:/by3PGaeLD425bBkMrAbD2NeHROHyjLHVA37pS5w3+0=}"
GKE_CLUSTER="${GKE_CLUSTER:-tessallite-community}"
GKE_ZONE="${GKE_ZONE:-us-west1-a}"
GKE_PROJECT="${GKE_PROJECT:-$(gcloud config get-value project 2>/dev/null || echo '')}"
GKE_NODES="${GKE_NODES:-2}"
GKE_MACHINE="${GKE_MACHINE:-e2-standard-2}"

die() { echo "ERROR: $*" >&2; exit 1; }

cmd_secrets() {
  command -v openssl >/dev/null || die "openssl required."
  command -v python3 >/dev/null || die "python3 required (Fernet key)."
  [ -f "$SECRETS_ENV" ] && die "$SECRETS_ENV exists — refusing to overwrite."
  cat > "$SECRETS_ENV" <<EOF
# Tessallite Community Kubernetes secrets — generated $(date -u +%FT%TZ). KEEP PRIVATE.
POSTGRES_PASSWORD=$(openssl rand -hex 24)
CREDENTIAL_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
JWT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
SYSTEM_ADMIN_EMAIL=admin@tessallite.local
SYSTEM_ADMIN_PASSWORD=$(openssl rand -base64 18)Aa1
EOF
  chmod 600 "$SECRETS_ENV"
  echo "Wrote $SECRETS_ENV (chmod 600). Review it, then run: $0 install"
}

cmd_gke_create() {
  command -v gcloud >/dev/null || die "gcloud required."
  [ -n "$GKE_PROJECT" ] || die "set GKE_PROJECT or run 'gcloud config set project ...'."
  gcloud services enable container.googleapis.com --project "$GKE_PROJECT"
  gcloud container clusters create "$GKE_CLUSTER" \
    --project "$GKE_PROJECT" --zone "$GKE_ZONE" \
    --num-nodes "$GKE_NODES" --machine-type "$GKE_MACHINE" \
    --disk-size 50 --no-enable-master-authorized-networks
  gcloud container clusters get-credentials "$GKE_CLUSTER" \
    --project "$GKE_PROJECT" --zone "$GKE_ZONE"
  echo "GKE cluster '$GKE_CLUSTER' ready. COST IS NOW ACCRUING — run '$0 gke-delete' when done."
}

cmd_install() {
  command -v helm >/dev/null || die "helm (v3) required."
  [ -f "$SECRETS_ENV" ] || die "no $SECRETS_ENV — run '$0 secrets' first."
  [ -f "$LICENSE_FILE" ] || die "no licence file at $LICENSE_FILE (signed Community licence)."
  set -a; . "$SECRETS_ENV"; set +a
  helm upgrade --install "$RELEASE" "$CHART" -n "$NAMESPACE" --create-namespace \
    --set global.registry="$REGISTRY" --set global.tag="$TAG" \
    --set secrets.postgresPassword="$POSTGRES_PASSWORD" \
    --set secrets.credentialEncryptionKey="$CREDENTIAL_ENCRYPTION_KEY" \
    --set secrets.jwtSecretKey="$JWT_SECRET_KEY" \
    --set secrets.systemAdminEmail="$SYSTEM_ADMIN_EMAIL" \
    --set secrets.systemAdminPassword="$SYSTEM_ADMIN_PASSWORD" \
    --set license.publicKeys="$LICENSE_PUBLIC_KEYS" \
    --set-file license.signedDocument="$LICENSE_FILE"
  echo "Installed. Run '$0 verify'."
}

cmd_verify() {
  command -v kubectl >/dev/null || die "kubectl required."
  echo "Waiting for pods to become Ready (timeout 5m)..."
  kubectl -n "$NAMESPACE" wait --for=condition=ready pod --all --timeout=300s || true
  kubectl -n "$NAMESPACE" get pods
  echo
  echo "Gateway (BI/SQL clients) external address (LoadBalancer):"
  kubectl -n "$NAMESPACE" get svc "${RELEASE}-gateway"
  echo "UI: kubectl -n $NAMESPACE port-forward svc/${RELEASE}-frontend 3000  # then http://localhost:3000"
}

cmd_teardown() {
  helm uninstall "$RELEASE" -n "$NAMESPACE" || true
  kubectl delete namespace "$NAMESPACE" --ignore-not-found
  echo "Workload removed. (If on GKE, also run '$0 gke-delete' to stop cluster cost.)"
}

cmd_gke_delete() {
  command -v gcloud >/dev/null || die "gcloud required."
  gcloud container clusters delete "$GKE_CLUSTER" \
    --project "$GKE_PROJECT" --zone "$GKE_ZONE" --quiet
  echo "GKE cluster '$GKE_CLUSTER' deleted — cluster cost stopped."
}

case "${1:-}" in
  secrets)    cmd_secrets ;;
  gke-create) cmd_gke_create ;;
  install)    cmd_install ;;
  verify)     cmd_verify ;;
  teardown)   cmd_teardown ;;
  gke-delete) cmd_gke_delete ;;
  *) echo "usage: $0 {secrets|gke-create|install|verify|teardown|gke-delete}"; exit 2 ;;
esac
```

## Step by step

```bash
bash deploy-kubernetes.sh secrets        # 1. generate secrets (review k8s-secrets.env)
GKE_PROJECT=my-project \
  bash deploy-kubernetes.sh gke-create   # 2. optional: create a managed cluster
bash deploy-kubernetes.sh install        # 3. put license.json in place, then install
bash deploy-kubernetes.sh verify         # 4. wait for Ready, print gateway + UI access
bash deploy-kubernetes.sh teardown       # 5. remove the workload when done
bash deploy-kubernetes.sh gke-delete     #    and delete the cluster (stops all cost)
```

When `verify` finishes, all eight pods (the seven services plus `tess-postgres-0`) show
`1/1 Running`. Open the UI with the printed `kubectl port-forward` command and point BI
tools at the gateway Service's external address.

## A note on TLS

The UI serves HTTPS. If you do not provide a certificate, the frontend container
**generates a self-signed one at start-up**, so the install never stalls waiting for
certificates. For a browser-trusted certificate, terminate TLS at your Ingress or load
balancer, or mount your own certificate as a Secret over
`/etc/nginx/certs/localhost.crt` and `/etc/nginx/certs/localhost.key`.

## Try it free on your laptop (kind)

You can validate the chart on real Kubernetes with no cloud spend using `kind`
(Kubernetes-in-Docker). Load the images straight into the node instead of pulling, and
expose the gateway as a NodePort (a local cluster has no cloud load balancer):

```bash
kind create cluster --name tessallite-test
kind load docker-image --name tessallite-test \
  ghcr.io/tessallite/model-service:0.1.0 ghcr.io/tessallite/query-router:0.1.0 \
  ghcr.io/tessallite/optimizer:0.1.0 ghcr.io/tessallite/scheduler:0.1.0 \
  ghcr.io/tessallite/agent-service:0.1.0 ghcr.io/tessallite/gateway:0.1.0 \
  ghcr.io/tessallite/frontend:0.1.0 postgres:15
helm install tess deploy/community/helm/tessallite-community -n tessallite --create-namespace \
  --set global.tag=0.1.0 --set global.imagePullPolicy=Never \
  --set secrets.postgresPassword=... --set secrets.credentialEncryptionKey=<fernet> \
  --set secrets.jwtSecretKey=<32+ chars> --set secrets.systemAdminPassword=... \
  --set license.publicKeys="tessallite-prod-2026:/by3PGaeLD425bBkMrAbD2NeHROHyjLHVA37pS5w3+0=" \
  --set-file license.signedDocument=license.json \
  --set ingress.enabled=false --set networkPolicy.enabled=false \
  --set services.gateway.serviceType=NodePort
kind delete cluster --name tessallite-test     # clean up
```

## Common issues

- **A pod is `CrashLoopBackOff` right after install.** Check its logs with
  `kubectl -n tessallite logs <pod>`. The most common cause is a too-short
  `jwtSecretKey` (it must be at least 32 characters) or a missing required value.
- **The gateway Service stays `Pending`.** That is expected on a cluster with no load
  balancer (such as `kind`). Use a NodePort, or run on a managed cluster.
- **Pods can't pull images.** Your cluster has no access to the registry. Push the images
  to a registry your nodes can reach and set `global.registry`/`global.tag`, or load them
  directly for local testing.

## Related

- [Deploy on GCP](deploy-gcp.md)
- [Deploy Locally](deploy-local.md)
- [Configure Environment Variables](configure-environment-variables.md)
- [Teardown](teardown.md)

---

← [Deploy on GCP](deploy-gcp.md) | [Home](../index.md) | [Create a Tenant →](create-a-tenant.md)
