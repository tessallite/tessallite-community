/**
 * Tessallite custom Excel functions (TESS namespace).
 *
 * These run in the custom functions runtime (separate from the task pane).
 * Auth token and model context are shared via OfficeRuntime.storage.
 */

import { getJwt, getActiveProfile, getModelContext, getActivePersonaId } from './utils/storage';

async function apiRequest<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const [jwt, profile] = await Promise.all([getJwt(), getActiveProfile()]);
  if (!jwt) throw new Error('Not signed in. Open the Tessallite panel and sign in first.');
  if (!profile) throw new Error('No active connection profile.');

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${jwt}`,
  };

  const res = await fetch(`${profile.serverUrl.replace(/\/$/, '')}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    if (res.status === 401) throw new Error('Session expired. Re-open the Tessallite panel and sign in.');
    if (res.status === 403) throw new Error('Access denied.');
    if (res.status === 404) throw new Error('Resource not found.');
    if (res.status >= 500) throw new Error('Server error. Try again later.');
    throw new Error('Request failed.');
  }

  return res.json() as Promise<T>;
}


interface KpiEvalResult {
  value: number | null;
  goal: number | null;
  status: number | null;
  trend: number | null;
  status_label: string | null;
  trend_label: string | null;
  formatted_value: string | null;
  formatted_goal: string | null;
}

interface PreviewItem {
  ordinal: number;
  caption: string;
  key: string;
}

interface PreviewResult {
  items: PreviewItem[];
  total_count: number;
  truncated: boolean;
}

const kpiCache = new Map<string, { data: KpiEvalResult; ts: number }>();
const listCache = new Map<string, { data: string[][]; ts: number }>();
const CACHE_TTL_MS = 60_000;

/**
 * Clear all custom function caches.
 * Callable from the task-pane bundle on profile switch and logout.
 *
 * F-025-13: this is NOT sufficient on its own. The custom-functions runtime is
 * a separate JS context (functions.html) with its own module-level Maps, so a
 * call from the pane bundle clears the pane's copies — not the live CF
 * runtime's. The structural guard that actually prevents stale/cross-profile
 * reads in the CF runtime is the per-read prefix purge in
 * ``purgeForeignProfileEntries`` below, which drops any cached entry whose key
 * does not belong to the currently-active profile/server/model/persona before
 * a cache hit can be returned.
 */
export function clearFunctionCaches(): void {
  kpiCache.clear();
  listCache.clear();
}

interface FullContext {
  profileId: string;
  serverUrl: string;
  projectId: string;
  modelId: string;
  // F-025-17: the active "Viewing as" persona (null = user's default). Part of
  // the cache key so a persona switch never serves another persona's values.
  personaId: string | null;
}

async function requireFullContext(): Promise<FullContext> {
  const [profile, ctx, personaId] = await Promise.all([
    getActiveProfile(),
    getModelContext(),
    getActivePersonaId(),
  ]);
  if (!profile) throw new Error('No active connection profile.');
  if (!ctx) throw new Error('No model selected. Open the Tessallite panel and select a project/model.');
  return { profileId: profile.id, serverUrl: profile.serverUrl, ...ctx, personaId: personaId ?? null };
}

function contextKeyPrefix(ctx: FullContext): string {
  return `${ctx.profileId}:${ctx.serverUrl}:${ctx.projectId}:${ctx.modelId}:${ctx.personaId ?? ''}:`;
}

function cacheKey(ctx: FullContext, entityId: string): string {
  return `${contextKeyPrefix(ctx)}${entityId}`;
}

/**
 * F-025-13: drop every cached entry that does not belong to the active context
 * before any cache hit can be returned. Because ``clearFunctionCaches()`` from
 * the pane cannot reach this runtime's Maps, this read-time purge is what
 * guarantees a TESS.* function evaluated after a profile/persona switch never
 * returns the previous context's data. Same-context entries (matching prefix)
 * are kept so the 60 s TTL still de-duplicates repeated reads.
 */
function purgeForeignProfileEntries(ctx: FullContext): void {
  const prefix = contextKeyPrefix(ctx);
  for (const key of kpiCache.keys()) {
    if (!key.startsWith(prefix)) kpiCache.delete(key);
  }
  for (const key of listCache.keys()) {
    if (!key.startsWith(prefix)) listCache.delete(key);
  }
}

/** Return a safe error string for display in Excel cells. Never surfaces raw backend details. */
const SAFE_ERRORS = [
  'Session expired',
  'Access denied',
  'Resource not found',
  'Server error',
  'Request failed',
  'Not signed in',
  'No active connection profile',
  'No model selected',
];

function safeErrorMessage(e: unknown): string {
  const msg = (e instanceof Error) ? e.message : 'Unknown error';
  for (const prefix of SAFE_ERRORS) {
    if (msg.startsWith(prefix)) return msg;
  }
  return 'An error occurred. Check the Tessallite panel for details.';
}

function tessListById(
  namedSetId: string,
  invocation: CustomFunctions.StreamingInvocation<unknown>,
): void {
  let cancelled = false;
  invocation.onCanceled = () => { cancelled = true; };

  (async () => {
    try {
      const ctx = await requireFullContext();
      // F-025-13: evict any other-profile/persona entries first so a hit below
      // can only ever be this context's data.
      purgeForeignProfileEntries(ctx);
      const key = cacheKey(ctx, namedSetId);
      const cached = listCache.get(key);
      if (cached && Date.now() - cached.ts < CACHE_TTL_MS) {
        invocation.setResult(cached.data);
        return;
      }

      const result = await apiRequest<PreviewResult>(
        `/api/v1/projects/${ctx.projectId}/models/${ctx.modelId}/named-sets/${namedSetId}/preview`,
        'POST',
      );

      if (cancelled) return;

      if (result.items.length === 0) {
        invocation.setResult([['(empty set)']]);
        return;
      }

      const grid = result.items.map((item) => [item.caption]);
      listCache.set(key, { data: grid, ts: Date.now() });
      invocation.setResult(grid);
    } catch (e) {
      if (!cancelled) invocation.setResult([[`#ERROR: ${safeErrorMessage(e)}`]]);
    }
  })();
}

async function tessKpiValue(kpiId: string): Promise<number | string> {
  try {
    const ev = await evalKpiCached(kpiId);
    return ev.value ?? '#N/A';
  } catch (e) {
    return `#ERROR: ${safeErrorMessage(e)}`;
  }
}

async function tessKpiGoal(kpiId: string): Promise<number | string> {
  try {
    const ev = await evalKpiCached(kpiId);
    return ev.goal ?? '#N/A';
  } catch (e) {
    return `#ERROR: ${safeErrorMessage(e)}`;
  }
}

async function tessKpiStatus(kpiId: string): Promise<number | string> {
  try {
    const ev = await evalKpiCached(kpiId);
    if (ev.status === null) return '#N/A';
    return ev.status;
  } catch (e) {
    return `#ERROR: ${safeErrorMessage(e)}`;
  }
}

async function evalKpiCached(kpiId: string): Promise<KpiEvalResult> {
  const ctx = await requireFullContext();
  // F-025-13: evict any other-profile/persona entries before a hit can return.
  purgeForeignProfileEntries(ctx);
  const key = cacheKey(ctx, kpiId);
  const cached = kpiCache.get(key);
  if (cached && Date.now() - cached.ts < CACHE_TTL_MS) return cached.data;

  // F-025-17: scope the evaluation to the active "Viewing as" persona so the
  // TESS.* functions return the same values the pane's KPI tab shows. The
  // backend accepts persona_id as a query param (kpis.py:1711); when absent it
  // auto-resolves the user's default persona (security is unchanged either way
  // — this only fixes the cross-tab numeric inconsistency).
  const personaQuery = ctx.personaId ? `?persona_id=${encodeURIComponent(ctx.personaId)}` : '';
  const data = await apiRequest<KpiEvalResult>(
    `/api/v1/projects/${ctx.projectId}/models/${ctx.modelId}/kpis/${kpiId}/evaluate${personaQuery}`,
    'POST',
  );

  kpiCache.set(key, { data, ts: Date.now() });
  return data;
}

if (typeof CustomFunctions !== 'undefined') {
  CustomFunctions.associate('LISTBYID', tessListById);
  CustomFunctions.associate('KPIVALUE', tessKpiValue);
  CustomFunctions.associate('KPIGOAL', tessKpiGoal);
  CustomFunctions.associate('KPISTATUS', tessKpiStatus);
}
