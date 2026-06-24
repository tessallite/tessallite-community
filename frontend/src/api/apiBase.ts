/**
 * Resolve the runtime base URL for each backend service.
 *
 * Priority (highest first):
 *   1. Operator override from the `frontend.api_base_overrides` system
 *      setting, exposed via systemDefaults.
 *   2. The `VITE_*` env var baked in at build time.
 *   3. Same-origin relative path (proxied by the frontend's nginx).
 *
 * Default is same-origin: the SPA only ever talks to its own host, and
 * the embedded nginx reverse-proxies each prefix (/api, /query-router,
 * /optimizer, /scheduler) to the appropriate backend. That makes CORS
 * disappear in dev, GCP, and on-prem deployments — the same built
 * image runs everywhere.
 */

import { apiBaseOverrideFor } from "./systemDefaults";

function resolve(
  service:
    | "model_service"
    | "query_router"
    | "optimizer"
    | "scheduler"
    | "agent_service",
  viteVar: string | undefined,
  relativePrefix: string,
): string {
  const override = apiBaseOverrideFor(service);
  if (override) return override;
  if (viteVar) return viteVar;
  return relativePrefix;
}

export function modelServiceBaseUrl(): string {
  return resolve(
    "model_service",
    import.meta.env.VITE_API_BASE_URL as string | undefined,
    "",
  );
}

export function queryRouterBaseUrl(): string {
  return resolve(
    "query_router",
    import.meta.env.VITE_QUERY_ROUTER_URL as string | undefined,
    "/query-router",
  );
}

export function optimizerBaseUrl(): string {
  return resolve(
    "optimizer",
    import.meta.env.VITE_OPTIMIZER_URL as string | undefined,
    "/optimizer",
  );
}

export function schedulerBaseUrl(): string {
  return resolve(
    "scheduler",
    import.meta.env.VITE_SCHEDULER_URL as string | undefined,
    "/scheduler",
  );
}

export function agentServiceBaseUrl(): string {
  return resolve(
    "agent_service",
    import.meta.env.VITE_AGENT_SERVICE_URL as string | undefined,
    "/agent",
  );
}

/**
 * Public issuer service base URL — serves the curated security/update advisory
 * feed (`/advisories`). This is an external, unauthenticated service operated by
 * Tessallite; it is NOT proxied through the gateway. The build-time
 * `VITE_ISSUER_URL` override lets air-gapped operators point at a mirror; the
 * default is the live issuer.
 */
const DEFAULT_ISSUER_URL = "https://issuer-6pjlis7ega-uc.a.run.app";

export function issuerBaseUrl(): string {
  const override = (import.meta.env.VITE_ISSUER_URL as string | undefined)?.trim();
  return override || DEFAULT_ISSUER_URL;
}
