/**
 * Sign in against a real Tessallite server and seed `OfficeRuntime.storage`
 * with exactly the keys the task pane writes.
 *
 * This is the same seeding a `VITE_TESSALLITE_TEST_PROFILE=1` build performs at
 * startup (deliverable (d) in
 * `docs/architecture/architecture_excel-plugin-test-harness.md`), which is why
 * the harness proves the runtime works with no pane interaction at all.
 *
 * Credentials come from the environment only. Nothing here is committed.
 */

/** Storage keys — must stay identical to `src/utils/storage.ts` STORAGE_KEYS. */
export const STORAGE_KEYS = {
  JWT: 'tessallite_jwt',
  SESSION_PROFILE: 'tessallite_session_profile',
  PROJECT_ID: 'tessallite_project_id',
  MODEL_ID: 'tessallite_model_id',
  MODEL_SLUG: 'tessallite_model_slug',
  MODEL_NAME: 'tessallite_model_name',
  ACTIVE_PERSONA_ID: 'tessallite_active_persona',
  CACHE_GENERATION: 'tessallite_cache_generation',
};

async function json(url, { token, method = 'GET', body } = {}) {
  const res = await fetch(url, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`${method} ${url} -> ${res.status}: ${text.slice(0, 300)}`);
  }
  return text ? JSON.parse(text) : null;
}

export async function login({ serverUrl, tenant, email, password }) {
  const res = await json(`${serverUrl}/api/v1/auth/login`, {
    method: 'POST',
    body: { tenant_id: tenant, email, password },
  });
  if (!res?.access_token) throw new Error('Login returned no access_token.');
  return res.access_token;
}

function pick(list, key) {
  return Array.isArray(list) ? list : (list?.items ?? []);
}

/** Resolve the configured slugs/names to the ids the runtime needs. */
export async function resolveContext({ serverUrl, token, config }) {
  const projects = pick(await json(`${serverUrl}/api/v1/projects`, { token }));
  const project = projects.find(p => p.slug === config.project || p.name === config.project);
  if (!project) {
    throw new Error(`Project "${config.project}" not found. Available: ${projects.map(p => p.slug).join(', ')}`);
  }

  const models = pick(await json(`${serverUrl}/api/v1/projects/${project.id}/models`, { token }));
  const find = (want) => models.find(m => m.slug === want || m.name === want);
  const model = find(config.model);
  if (!model) {
    throw new Error(`Model "${config.model}" not found. Available: ${models.map(m => m.slug).join(', ')}`);
  }
  const wrongModel = find(config.wrongModel);
  if (!wrongModel) {
    throw new Error(`Wrong-model fixture "${config.wrongModel}" not found; the fail-closed check needs a second model.`);
  }

  const base = `${serverUrl}/api/v1/projects/${project.id}/models/${model.id}`;
  const kpis = pick(await json(`${base}/kpis?deployed_only=true`, { token }));
  const kpi = kpis.find(k => k.name === config.kpi || k.display_name === config.kpi);
  if (!kpi) {
    throw new Error(`KPI "${config.kpi}" not found. Available: ${kpis.map(k => k.name).join(', ')}`);
  }

  const namedSets = pick(await json(`${base}/named-sets?deployed_only=true`, { token }));
  const namedSet = namedSets.find(n => n.name === config.namedSet || n.display_name === config.namedSet);
  if (!namedSet) {
    throw new Error(`Named set "${config.namedSet}" not found. Available: ${namedSets.map(n => n.name).join(', ')}`);
  }

  // Optional: the persona the `deployed_only` / `persona_id` scoping check
  // switches to. Absent is not an error — the check then reports that scoping
  // was not exercised rather than passing vacuously.
  let persona = null;
  if (config.persona) {
    const personas = pick(await json(`${base}/personas?deployed_only=true`, { token }));
    persona = personas.find(p => p.name === config.persona || p.display_name === config.persona) ?? null;
  }

  return { project, model, wrongModel, kpi, namedSet, persona };
}

/** Write the pane-owned session state into the stubbed storage. */
export async function seedStorage(storage, { serverUrl, token, tenant, email, project, model }) {
  const profile = {
    id: 'harness',
    name: 'harness',
    serverUrl,
    tenantId: tenant,
    email,
    createdAt: new Date().toISOString(),
  };
  await storage.setItem(STORAGE_KEYS.JWT, token);
  await storage.setItem(STORAGE_KEYS.SESSION_PROFILE, JSON.stringify(profile));
  await storage.setItem(STORAGE_KEYS.PROJECT_ID, project.id);
  await storage.setItem(STORAGE_KEYS.MODEL_ID, model.id);
  if (model.slug) await storage.setItem(STORAGE_KEYS.MODEL_SLUG, model.slug);
  if (model.name) await storage.setItem(STORAGE_KEYS.MODEL_NAME, model.name);
  await storage.setItem(STORAGE_KEYS.CACHE_GENERATION, '1');
}

/**
 * Query `/api/v1/plugin/execute` directly, so a harness assertion can compare
 * what the runtime handed a cell against the raw server payload (and show that
 * the server really does serialise measures as strings).
 */
export async function rawExecute({ serverUrl, token, projectId, modelId, measures, dimensions, filters }) {
  return json(`${serverUrl}/api/v1/plugin/execute`, {
    token,
    method: 'POST',
    body: {
      project_id: projectId,
      model_id: modelId,
      measures,
      dimensions,
      filters,
    },
  });
}
