/**
 * Cache of system-level defaults that the SPA reads at boot.
 *
 * The values come from `/api/v1/system/settings`. They are persisted in
 * localStorage so the next page load can render synchronously without
 * waiting on a network round-trip.
 *
 * Hardcoded literals are deliberately avoided here — every default in
 * this module is registered in `shared/config/registry.py` and seeded
 * into `system_settings`. The registry default is the only fallback if
 * the cache is empty (first ever page load) AND the API call hasn't
 * returned yet.
 */

const STORAGE_KEY = "tess.systemDefaults.v1";

export type ApiBaseOverrides = {
  model_service: string | null;
  query_router: string | null;
  optimizer: string | null;
  scheduler: string | null;
  agent_service: string | null;
};

type Cache = {
  llmModelSuggestions: Record<string, string[]>;
  endpointDefaults: {
    model_service_port: number;
    query_router_port: number;
    optimizer_port: number;
    scheduler_port: number;
    gateway_http_port: number;
    gateway_jdbc_port: number;
  };
  apiBaseOverrides: ApiBaseOverrides;
  loadedAt: string | null;
};

// Registry-mirrored seed used until the API returns. The values match
// the SettingDef defaults in shared/config/registry.py so behaviour is
// identical to a freshly seeded system.
const REGISTRY_SEED: Cache = {
  llmModelSuggestions: {
    openai: ["gpt-4o"],
    google: ["gemini-1.5-pro"],
    anthropic: ["claude-sonnet-4-5-20250514"],
    deepseek: ["deepseek-chat"],
    glm: ["glm-4.5-flash"],
    ollama: ["llama3"],
  },
  endpointDefaults: {
    model_service_port: 8001,
    query_router_port: 8002,
    optimizer_port: 8003,
    scheduler_port: 8004,
    gateway_http_port: 8080,
    gateway_jdbc_port: 5433,
  },
  apiBaseOverrides: {
    model_service: null,
    query_router: null,
    optimizer: null,
    scheduler: null,
    agent_service: null,
  },
  loadedAt: null,
};

let _cache: Cache | null = null;

function readStorage(): Cache | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<Cache>;
    return {
      llmModelSuggestions:
        parsed.llmModelSuggestions ?? REGISTRY_SEED.llmModelSuggestions,
      endpointDefaults: {
        ...REGISTRY_SEED.endpointDefaults,
        ...(parsed.endpointDefaults ?? {}),
      },
      apiBaseOverrides: {
        ...REGISTRY_SEED.apiBaseOverrides,
        ...(parsed.apiBaseOverrides ?? {}),
      },
      loadedAt: parsed.loadedAt ?? null,
    };
  } catch {
    return null;
  }
}

function writeStorage(cache: Cache) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(cache));
}

export function getSystemDefaults(): Cache {
  if (_cache) return _cache;
  _cache = readStorage() ?? REGISTRY_SEED;
  return _cache;
}

/**
 * Pull the current values from the system-settings API and overwrite
 * the local cache. Safe to call repeatedly; failures are swallowed
 * (the previous cache or the registry seed remains in effect).
 */
export async function refreshSystemDefaults(): Promise<void> {
  try {
    // Lazy import: systemDefaults is imported by apiBase which is imported
    // by client, so a top-level import here would form a cycle and blow up
    // the production bundle with a TDZ error.
    const { systemSettingsApi } = await import("./client");
    const items = await systemSettingsApi.list();
    const byKey = new Map(items.map((it) => [it.key, it.value]));

    const llmRaw = byKey.get("llm.model_name_suggestions");
    const epRaw = byKey.get("frontend.endpoint_defaults");
    const overridesRaw = byKey.get("frontend.api_base_overrides");

    const next: Cache = {
      llmModelSuggestions:
        (llmRaw && typeof llmRaw === "object"
          ? (llmRaw as Record<string, string[]>)
          : REGISTRY_SEED.llmModelSuggestions),
      endpointDefaults: {
        ...REGISTRY_SEED.endpointDefaults,
        ...(epRaw && typeof epRaw === "object" ? (epRaw as Record<string, number>) : {}),
      },
      apiBaseOverrides: {
        ...REGISTRY_SEED.apiBaseOverrides,
        ...(overridesRaw && typeof overridesRaw === "object"
          ? (overridesRaw as Partial<ApiBaseOverrides>)
          : {}),
      },
      loadedAt: new Date().toISOString(),
    };
    _cache = next;
    writeStorage(next);
  } catch {
    // Silent — the SPA renders fine with the existing cache or the seed.
  }
}

/**
 * Convenience accessor for the LLM model placeholder list.
 * Returns the first suggested model name, or "" if no suggestions
 * are registered for the provider.
 */
export function defaultModelNameFor(provider: string): string {
  const list = getSystemDefaults().llmModelSuggestions[provider];
  if (!list || list.length === 0) return "";
  return list[0];
}

/**
 * Operator-configured override for a backend service base URL. Returns
 * null when no override is configured — the caller then derives the URL
 * from window.location.
 */
export function apiBaseOverrideFor(
  service: keyof ApiBaseOverrides,
): string | null {
  const raw = getSystemDefaults().apiBaseOverrides[service];
  if (typeof raw !== "string" || raw.trim() === "") return null;
  return raw;
}
