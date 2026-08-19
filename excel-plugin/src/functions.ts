/**
 * Tessallite custom Excel functions.
 *
 * F-025-02: there is exactly ONE published custom-functions namespace,
 * `TESSALLITE`, defined by the `Functions.Namespace` resource in every shipped
 * manifest (`manifest.xml`, `manifest.xml.template`, `sideload-catalog`, and
 * the pane-generated manifest). Office derives the workbook prefix solely from
 * that manifest string; `CustomFunctions.associate('<ID>', fn)` binds the ID,
 * not a second namespace. A separate `TESS.*` namespace was never registered,
 * so `=TESS.KPIVALUE(...)` would resolve to `#NAME?`. All functions below are
 * therefore reachable ONLY under `TESSALLITE.*`; do not reintroduce a `TESS.*`
 * promise in code, metadata, or tests without also publishing (and verifying
 * on a perpetual host) a second manifest namespace.
 *
 * Name-based functions:
 *   TESSALLITE.VALUE(model, measure, [filterCol, filterVal]...)
 *   TESSALLITE.KPI(model, kpiName, property)
 *   TESSALLITE.MEMBERVALUE(model, measure, dimension, member)
 *
 * ID-based functions (retained for backward compatibility, same namespace):
 *   TESSALLITE.LISTBYID, TESSALLITE.KPIVALUE, TESSALLITE.KPIGOAL,
 *   TESSALLITE.KPISTATUS
 */

import { rowSecurityDeniedAll } from './utils/rowSecurity';
import { getJwt, getActiveProfile, getModelContext, getActivePersonaId, getCacheGeneration, bumpCacheGeneration } from './utils/storage';
import { FunctionBatcher, computeResultKey, CollisionSafeResultMap, type BatchExecutor } from './utils/functionBatcher';

// ---------------------------------------------------------------------------
// Shared infrastructure
// ---------------------------------------------------------------------------

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
    credentials: 'omit',
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    if (res.status === 401) throw new Error('Session expired. Re-open the Tessallite panel and sign in.');
    if (res.status === 403) throw new Error('Access denied.');
    if (res.status === 404) throw new Error('Resource not found.');
    // Bug-8712: DEPLOYED_SNAPSHOT_INVALID. The published version of the model
    // cannot be read, so there is no definition this function is allowed to
    // serve. Fail closed with a plain message; never fall back to the live
    // draft, which is the leak this contract exists to close (Bug-8384).
    if (res.status === 409) throw new Error('Published model unavailable. Deploy the model again from the model builder.');
    if (res.status >= 500) throw new Error('Server error. Try again later.');
    throw new Error('Request failed.');
  }

  return res.json() as Promise<T>;
}

/**
 * Query string for a CONSUMPTION read from the custom-functions runtime
 * (Bug-8710 / Bug-8712).
 *
 * The functions runtime is a WWAHost AppContainer isolated from the task pane;
 * it shares only `OfficeRuntime.storage` and does NOT go through
 * `api/modelService.ts`. Threading `deployed_only` there therefore leaves this
 * transport open — it is a SECOND client of the same routes and needs the flag
 * in its own right. Same authority: the deployed snapshot is the contract, the
 * live state is editor-only (F-013-01).
 */
function consumptionQuery(personaId?: string | null): string {
  const params = new URLSearchParams({ deployed_only: 'true' });
  if (personaId) params.set('persona_id', personaId);
  return `?${params.toString()}`;
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
/** F11: cached KPI list for TESSALLITE.KPI name lookups. */
const kpiListCache = new Map<string, { data: { id: string; name: string; display_name: string }[]; ts: number }>();
const CACHE_TTL_MS = 60_000;

/**
 * Clear the local TTL caches and batcher of THIS runtime synchronously.
 * Does NOT bump the cross-runtime generation token — callers that need the
 * separate functions runtime to invalidate must use the awaitable
 * `bumpFunctionCacheGeneration` (or `clearFunctionCaches`, which fires it
 * fire-and-forget for backward compatibility).
 */
function clearLocalFunctionCaches(): void {
  kpiCache.clear();
  listCache.clear();
  kpiListCache.clear();
  valueBatcher.invalidate();
}

/**
 * Clear all custom function caches AND invalidate the batcher.
 * Phase A: with shared runtime, this is callable from the taskpane's
 * "Refresh values" action, persona switch, profile switch, and logout.
 *
 * Bug-6912: additionally bumps the cache generation token in
 * OfficeRuntime.storage so the functions runtime (if running in a separate
 * JS context on perpetual Office) detects the invalidation on its next eval.
 *
 * F-025-03: this fire-and-forget form is retained only for teardown paths
 * (logout) where ordering against a persisted governed scope does not matter.
 * For persona/profile/model transitions use `applyContextTransition`, which
 * persists the new scope BEFORE bumping the generation so the separate
 * functions runtime can never observe a new generation with a stale scope.
 */
export function clearFunctionCaches(): void {
  clearLocalFunctionCaches();
  // Bug-6912: fire-and-forget; catch prevents unhandled-rejection in tests
  // or hosts where storage is unavailable.
  bumpCacheGeneration().catch(() => {});
}

/**
 * Trigger a full workbook rebuild so every existing TESSALLITE.* cell
 * re-evaluates against the freshly invalidated caches. Resolves when the
 * recalc request has been submitted (or immediately if the Excel host is
 * unavailable), so callers can sequence it after cache invalidation.
 */
function requestFullWorkbookRecalc(): Promise<void> {
  if (typeof Excel === 'undefined') return Promise.resolve();
  try {
    return Excel.run(async (context) => {
      context.workbook.application.calculate(Excel.CalculationType.fullRebuild);
      await context.sync();
    }).catch(() => {
      // Recalc not available on all hosts — the cache invalidation alone
      // ensures the next evaluation fetches fresh data.
    });
  } catch {
    // Excel API not available — silent.
    return Promise.resolve();
  }
}

/**
 * F-025-03: single awaited context-transition operation for persona/profile/
 * model switches. The CALLER must persist the new governed scope (persona,
 * model context, active profile) into OfficeRuntime.storage BEFORE invoking
 * this. This function then, in order:
 *   1. clears this runtime's local caches,
 *   2. awaits the cross-runtime generation bump (so the separate functions
 *      runtime, which reads persona + generation atomically, can only ever
 *      see the NEW generation alongside the already-persisted NEW scope —
 *      never a new generation with the old persona), and
 *   3. requests a full workbook rebuild so already-inserted cells recompute
 *      under the new scope instead of retaining the previous persona's values.
 */
export async function applyContextTransition(): Promise<void> {
  clearLocalFunctionCaches();
  // Await the generation bump so recalc happens strictly after invalidation.
  await bumpCacheGeneration().catch(() => {});
  await requestFullWorkbookRecalc();
}

/**
 * Refresh values: invalidate caches and trigger a full recalc of all
 * TESSALLITE.* functions. Called by the taskpane's "Refresh values" button.
 * Now awaitable so the caller can sequence UI feedback.
 */
export async function refreshCustomFunctionValues(): Promise<void> {
  clearLocalFunctionCaches();
  await bumpCacheGeneration().catch(() => {});
  await requestFullWorkbookRecalc();
}

interface FullContext {
  profileId: string;
  serverUrl: string;
  projectId: string;
  modelId: string;
  modelSlug?: string;
  modelName?: string;
  personaId: string | null;
}

/**
 * Bug-6912: module-level cache generation token last seen by this runtime.
 * When the stored token differs, the pane (or another runtime) bumped it —
 * clear all local caches before serving anything.
 */
let lastSeenGeneration: string | null = null;

/** Bug-6912: exported for testing. Resets the last-seen state. */
export function _resetLastSeenGeneration(): void {
  lastSeenGeneration = null;
}

async function requireFullContext(): Promise<FullContext> {
  // Bug-6912: piggyback the cache-generation check onto the existing
  // parallel storage reads so no extra round-trip is added per evaluation.
  const [profile, ctx, personaId, generation] = await Promise.all([
    getActiveProfile(),
    getModelContext(),
    getActivePersonaId(),
    getCacheGeneration(),
  ]);
  if (!profile) throw new Error('No active connection profile.');
  if (!ctx) throw new Error('No model selected. Open the Tessallite panel and select a project/model.');

  // Bug-6912: if the generation token differs from the last value this runtime
  // saw, the pane signalled a cache invalidation (Refresh, persona switch,
  // profile switch, or logout). Clear all local caches immediately. On a true
  // cold start the caches are empty so the extra clear is free — but skipping
  // it (the old "seed without clearing" logic) would let stale entries survive
  // if formulas evaluated before the pane opened (token null, caches filled,
  // then the first-ever bump from null to a value was missed).
  //
  // Bug-6914: clear ONLY the TTL caches here — never the batcher. This code
  // runs INSIDE the batcher's execute path; invalidating the batcher from here
  // killed the very batch that detected the bump, leaving every recalculated
  // cell stuck at #GETTING_DATA. The in-flight queries are already fresh
  // (they execute after the bump), so the batcher holds nothing stale.
  if (generation !== lastSeenGeneration) {
    kpiCache.clear();
    listCache.clear();
    kpiListCache.clear();
    lastSeenGeneration = generation;
  }

  return { profileId: profile.id, serverUrl: profile.serverUrl, ...ctx, personaId: personaId ?? null };
}

function normaliseModelKey(value: string): string {
  return value.trim().toLowerCase();
}

function assertFormulaModelMatchesActiveContext(formulaModel: string, ctx: FullContext): void {
  const requested = normaliseModelKey(formulaModel);
  const accepted = [ctx.modelSlug, ctx.modelName, ctx.modelId]
    .filter((value): value is string => Boolean(value))
    .map(normaliseModelKey);
  if (!accepted.includes(requested)) {
    throw new Error('Formula model does not match the selected model. Open the Tessallite panel and select the model named in the formula.');
  }
}

function contextKeyPrefix(ctx: FullContext): string {
  return `${ctx.profileId}:${ctx.serverUrl}:${ctx.projectId}:${ctx.modelId}:${ctx.personaId ?? ''}:`;
}

function cacheKey(ctx: FullContext, entityId: string): string {
  return `${contextKeyPrefix(ctx)}${entityId}`;
}

/**
 * F-025-13: drop every cached entry that does not belong to the active context
 * before any cache hit can be returned. With shared runtime this is less
 * critical (pane and functions share the same Maps), but the guard remains
 * for the profile/persona switch path.
 */
function purgeForeignProfileEntries(ctx: FullContext): void {
  const prefix = contextKeyPrefix(ctx);
  for (const key of kpiCache.keys()) {
    if (!key.startsWith(prefix)) kpiCache.delete(key);
  }
  for (const key of listCache.keys()) {
    if (!key.startsWith(prefix)) listCache.delete(key);
  }
  for (const key of kpiListCache.keys()) {
    if (!key.startsWith(prefix)) kpiListCache.delete(key);
  }
}

/**
 * F11: cached KPI list for TESSALLITE.KPI name lookups. Uses the shared
 * kpiListCache so repeated evaluations within the TTL do not refetch.
 * Invalidated by clearFunctionCaches() (Refresh action).
 */
async function getKpiListCached(ctx: FullContext): Promise<{ id: string; name: string; display_name: string }[]> {
  const key = cacheKey(ctx, '_kpi_list');
  const cached = kpiListCache.get(key);
  if (cached && Date.now() - cached.ts < CACHE_TTL_MS) return cached.data;

  const data = await apiRequest<{ id: string; name: string; display_name: string }[]>(
    `/api/v1/projects/${ctx.projectId}/models/${ctx.modelId}/kpis${consumptionQuery(ctx.personaId)}`,
  );
  kpiListCache.set(key, { data, ts: Date.now() });
  return data;
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
  'Formula model does not match the selected model',
  'Unknown measure',
  'Unknown KPI',
  'Batcher invalidated',
  // Bug-8712: the fail-closed DEPLOYED_SNAPSHOT_INVALID message. It must be
  // allow-listed here or the user sees the generic "check the panel" text and
  // has no way to learn that the fix is to redeploy the model.
  'Published model unavailable',
];

function safeErrorMessage(e: unknown): string {
  const msg = (e instanceof Error) ? e.message : 'Unknown error';
  for (const prefix of SAFE_ERRORS) {
    if (msg.startsWith(prefix)) return msg;
  }
  return 'An error occurred. Check the Tessallite panel for details.';
}

/**
 * F6: Build a proper CustomFunctions.Error when available (desktop Excel),
 * falling back to a string for hosts where the constructor is absent.
 * The Error's `code` maps to Excel's error indicator (#CONNECT!, #N/A, etc.)
 * and `message` appears in the cell's tooltip on hover.
 */
/**
 * F6: Build a proper CustomFunctions.Error when the ErrorCode enum is
 * available (desktop Excel), falling back to an error string for other hosts.
 * The Error's `code` maps to Excel's cell error indicator (#CONNECT!, #N/A, etc.)
 * and `message` appears in the cell's tooltip on hover.
 *
 * Unit-testable: in the test environment CustomFunctions.Error is not defined,
 * so the fallback string is returned. Live rendering goes on the manual checklist.
 */
/**
 * F6: Build an error value for a custom function cell.
 *
 * On desktop Excel with CustomFunctions.ErrorCode available, this THROWS a
 * CustomFunctions.Error which Excel catches and renders as a cell error
 * indicator (#N/A, #VALUE!) with the message in the tooltip. The caller
 * must NOT wrap this call in a try/catch that would swallow the CF Error.
 *
 * On hosts without CF Error support (web, tests), returns an error string
 * that Excel renders as the cell text.
 *
 * `code` maps to:
 *   'connect'      -> #N/A + "#CONNECT!" prefix (auth issues)
 *   'notAvailable' -> #N/A (no data)
 *   'invalidValue' -> #VALUE! (bad args)
 *
 * Unit-testable: in the test environment CustomFunctions.Error/ErrorCode are
 * not defined, so the fallback string is returned. Live CF Error rendering
 * goes on the manual checklist.
 */
export function makeFunctionError(
  code: 'connect' | 'notAvailable' | 'invalidValue',
  message: string,
): never | string {
  if (typeof CustomFunctions !== 'undefined' && CustomFunctions.ErrorCode && CustomFunctions.Error) {
    const errorCode = code === 'invalidValue'
      ? CustomFunctions.ErrorCode.invalidValue
      : CustomFunctions.ErrorCode.notAvailable;
    throw new CustomFunctions.Error(errorCode, message);
  }
  // Fallback: return a string that Excel renders as the cell value.
  const prefixMap: Record<string, string> = {
    connect: '#CONNECT!',
    notAvailable: '#N/A',
    invalidValue: '#ERROR:',
  };
  return `${prefixMap[code] || '#ERROR:'} ${message}`;
}

// ---------------------------------------------------------------------------
// Phase A: Coalescing batcher for TESSALLITE.VALUE / TESSALLITE.MEMBERVALUE
// ---------------------------------------------------------------------------

/**
 * The batch executor: issues one plugin-protocol query per (model, filter-shape)
 * group and returns a map of resultKey -> value.
 *
 * Uses the correct /api/v1/plugin/execute contract:
 *   POST body: { project_id, model_id, measures, dimensions, filters, persona_id }
 *   where filters is an array of { dimension, operator, values }.
 *
 * WRONG-NUMBERS GUARD: when `dimensionColumns` is non-empty, those columns
 * are included as dimensions (GROUP BY) in the query. The response returns
 * one row per member-value tuple. Each invocation's promise receives its own
 * row's value — never another member's number. A member absent from the
 * result gets null (the caller maps this to #N/A with reason).
 */
const batchExecute: BatchExecutor = async (
  model: string,
  measures: string[],
  singleValueFilters: [string, string][],
  dimensionColumns: string[],
  filterValueSets: Map<string, Set<string>>,
): Promise<Map<string, number | string | null>> => {
  const ctx = await requireFullContext();
  assertFormulaModelMatchesActiveContext(model, ctx);

  // The query's dimensions = the GROUP BY columns (multi-value filter
  // columns that become dimension axes in the result).
  const allDimensions = [...dimensionColumns];

  // Build filters: single-value columns get an equality filter;
  // multi-value columns get an IN filter (all requested values).
  const queryFilters: { dimension: string; operator: string; values: string[] }[] = [];
  for (const [col, val] of singleValueFilters) {
    queryFilters.push({ dimension: col, operator: 'eq', values: [val] });
  }
  for (const col of dimensionColumns) {
    const vals = filterValueSets.get(col);
    if (vals && vals.size > 0) {
      queryFilters.push({ dimension: col, operator: 'in', values: [...vals] });
    }
  }

  const body: Record<string, unknown> = {
    project_id: ctx.projectId,
    model_id: ctx.modelId,
    measures,
    dimensions: allDimensions.length > 0 ? allDimensions : undefined,
    filters: queryFilters.length > 0 ? queryFilters : undefined,
    persona_id: ctx.personaId || undefined,
  };

  const result = await apiRequest<{
    data: Record<string, unknown>[];
    annotation?: unknown;
    security_rules_applied?: string[];
  }>(
    '/api/v1/plugin/execute',
    'POST',
    body,
  );

  // Bug-8453 / R3 finding S-1 [worst-case wrong number]. A row-security
  // deny-all returns HTTP 200. For a COUNT/COALESCE-shaped measure it returns a
  // row containing 0, which would land a fabricated business figure in a
  // spreadsheet cell that the user then formats, charts and forwards. Throwing
  // puts every cell in this batch into an Excel error state with a readable
  // message instead — the same convention the auth and transport failures above
  // use. Branch on the sentinel, never on data.length.
  if (rowSecurityDeniedAll(result)) {
    throw new Error(
      'Row-level security: your permissions grant you access to no rows for '
      + 'this query. This is a permissions restriction, not a value of zero. '
      + 'Contact your administrator if you believe you should have access.',
    );
  }

  // Bug-7394 (adversarial R1): accumulate collision-safe. Two DISTINCT server
  // members that normalize to the same fan-out key (e.g. "EU"/"eu", "5"/"5.0",
  // a bare-date row and a separate T00:00:00 row) must NOT overwrite each other
  // — that would deliver one member's number to the other's cell. The
  // accumulator poisons such keys so they fan out to #N/A (safe) instead.
  const resultMap = new CollisionSafeResultMap();

  const coerce = (val: unknown): number | string | null =>
    (val === null || val === undefined)
      ? null
      : (typeof val === 'number' ? val : String(val));

  if (!result.data || result.data.length === 0) return resultMap.finalize();

  if (dimensionColumns.length === 0) {
    // No GROUP BY — single-row result. Every invocation in this group
    // shares the same filter values; the raw signature is the single-value
    // filter tuple (identical for all, so no collision is possible here).
    const row = result.data[0];
    const rawSig = CollisionSafeResultMap.rawSignature(singleValueFilters);
    for (const measure of measures) {
      const key = computeResultKey(measure, singleValueFilters);
      resultMap.set(key, rawSig, coerce(row[measure]));
    }
  } else {
    // Multi-row result (GROUP BY). Each row has dimension columns that
    // identify which member-value tuple it represents. Build a resultKey
    // for each measure x row combination.
    for (const row of result.data) {
      // Reconstruct the full filter set for this row: single-value
      // filters (shared across all rows) + dimension-column values
      // from this specific row.
      const rowFilters: [string, string][] = [...singleValueFilters];
      for (const col of dimensionColumns) {
        const memberVal = row[col];
        if (memberVal !== null && memberVal !== undefined) {
          // NOTE (Bug-7394): String() coercion assumes a single dimension
          // column returns ONE JS type across all its member rows (true for a
          // SQL GROUP BY column). If a column could ever return mixed types
          // (number 5 vs string "5") for distinct members, their raw
          // signatures would collapse and the collision poison would not fire.
          // This does not occur for a typed source column, so it is safe here.
          rowFilters.push([col, String(memberVal)]);
        }
      }
      // The RAW signature distinguishes distinct members that would collide on
      // the normalized key, so a genuine collision poisons the key instead of
      // silently overwriting one member's value with another's.
      const rawSig = CollisionSafeResultMap.rawSignature(rowFilters);

      for (const measure of measures) {
        const key = computeResultKey(measure, rowFilters);
        resultMap.set(key, rawSig, coerce(row[measure]));
      }
    }
  }

  return resultMap.finalize();
};

const valueBatcher = new FunctionBatcher(batchExecute);

// Export the batcher for testing.
export { valueBatcher as _valueBatcher };

// ---------------------------------------------------------------------------
// TESSALLITE.VALUE — connectionless measure value with optional filters
// ---------------------------------------------------------------------------

/**
 * Parse the variadic filter arguments into [column, value] pairs.
 * Filters come in pairs: (filterColumn1, filterValue1, filterColumn2, ...).
 * Missing or empty pairs are skipped.
 */
export function parseFilterArgs(...args: (string | undefined | null | boolean)[]): [string, string][] {
  const filters: [string, string][] = [];
  for (let i = 0; i + 1 < args.length; i += 2) {
    const col = args[i];
    const val = args[i + 1];
    if (col && val && typeof col === 'string' && typeof val === 'string') {
      filters.push([col, val]);
    }
  }
  return filters;
}

async function tessalliteValue(
  model: string,
  measure: string,
  filterColumn1?: string,
  filterValue1?: string,
  filterColumn2?: string,
  filterValue2?: string,
  filterColumn3?: string,
  filterValue3?: string,
  filterColumn4?: string,
  filterValue4?: string,
): Promise<number | string> {
  // Arg validation (returns strings; CF Error throw propagates directly).
  if (!model || typeof model !== 'string') {
    return makeFunctionError('invalidValue', 'Model name is required.');
  }
  if (!measure || typeof measure !== 'string') {
    return makeFunctionError('invalidValue', 'Measure name is required.');
  }
  try {
    const filters = parseFilterArgs(
      filterColumn1, filterValue1,
      filterColumn2, filterValue2,
      filterColumn3, filterValue3,
      filterColumn4, filterValue4,
    );
    const result = await valueBatcher.enqueue(model, measure, filters);
    if (result === null) return makeFunctionError('notAvailable', `No data for measure "${measure}".`);
    return result;
  } catch (e) {
    // R2-F2: rethrow CF Errors unchanged so Excel renders them correctly.
    if (typeof CustomFunctions !== 'undefined' && CustomFunctions.Error &&
        e instanceof CustomFunctions.Error) {
      throw e;
    }
    const msg = safeErrorMessage(e);
    if (msg.startsWith('Session expired') || msg.startsWith('Not signed in')) {
      return makeFunctionError('connect', 'Sign in via the Tessallite panel.');
    }
    return makeFunctionError('invalidValue', msg);
  }
}

// ---------------------------------------------------------------------------
// TESSALLITE.KPI — connectionless KPI property by name
// ---------------------------------------------------------------------------

async function tessalliteKpi(
  model: string,
  kpiName: string,
  property: string,
): Promise<number | string> {
  // Arg validation outside try so CF Error throws propagate directly.
  if (!model || typeof model !== 'string') {
    return makeFunctionError('invalidValue', 'Model name is required.');
  }
  if (!kpiName || typeof kpiName !== 'string') {
    return makeFunctionError('invalidValue', 'KPI name is required.');
  }
  if (!property || typeof property !== 'string') {
    return makeFunctionError('invalidValue', 'Property is required (value, goal, or status).');
  }
  const prop = property.toLowerCase().trim();
  if (!['value', 'goal', 'status'].includes(prop)) {
    return makeFunctionError('invalidValue', 'Invalid property. Use "value", "goal", or "status".');
  }
  try {
    const ctx = await requireFullContext();
    assertFormulaModelMatchesActiveContext(model, ctx);
    purgeForeignProfileEntries(ctx);

    const kpisResponse = await getKpiListCached(ctx);
    const kpi = kpisResponse.find(
      k => k.name.toLowerCase() === kpiName.toLowerCase() ||
           k.display_name?.toLowerCase() === kpiName.toLowerCase(),
    );
    if (!kpi) {
      return makeFunctionError('notAvailable', `Unknown KPI "${kpiName}". Check the KPI name in the Tessallite panel.`);
    }

    const ev = await evalKpiCached(kpi.id);
    switch (prop) {
      case 'value': return ev.value ?? makeFunctionError('notAvailable', `KPI "${kpiName}" value is not available.`);
      case 'goal': return ev.goal ?? makeFunctionError('notAvailable', `KPI "${kpiName}" goal is not available.`);
      case 'status':
        if (ev.status === null) return makeFunctionError('notAvailable', `KPI "${kpiName}" status is not available.`);
        return ev.status;
      default: return makeFunctionError('notAvailable', `KPI "${kpiName}" ${prop} is not available.`);
    }
  } catch (e) {
    if (typeof CustomFunctions !== 'undefined' && CustomFunctions.Error &&
        e instanceof CustomFunctions.Error) {
      throw e;
    }
    const msg = safeErrorMessage(e);
    if (msg.startsWith('Session expired') || msg.startsWith('Not signed in')) {
      return makeFunctionError('connect', 'Sign in via the Tessallite panel.');
    }
    return makeFunctionError('invalidValue', msg);
  }
}

// ---------------------------------------------------------------------------
// TESSALLITE.MEMBERVALUE — value for a specific dimension member
// ---------------------------------------------------------------------------

async function tessalliteMemberValue(
  model: string,
  measure: string,
  dimension: string,
  member: string,
): Promise<number | string> {
  if (!model || typeof model !== 'string') {
    return makeFunctionError('invalidValue', 'Model name is required.');
  }
  if (!measure || typeof measure !== 'string') {
    return makeFunctionError('invalidValue', 'Measure name is required.');
  }
  if (!dimension || typeof dimension !== 'string') {
    return makeFunctionError('invalidValue', 'Dimension name is required.');
  }
  if (!member || typeof member !== 'string') {
    return makeFunctionError('invalidValue', 'Member value is required.');
  }
  try {
    const filters: [string, string][] = [[dimension, member]];
    const result = await valueBatcher.enqueue(model, measure, filters);
    if (result === null) return makeFunctionError('notAvailable', `No data for "${measure}" where ${dimension}="${member}".`);
    return result;
  } catch (e) {
    if (typeof CustomFunctions !== 'undefined' && CustomFunctions.Error &&
        e instanceof CustomFunctions.Error) {
      throw e;
    }
    const msg = safeErrorMessage(e);
    if (msg.startsWith('Session expired') || msg.startsWith('Not signed in')) {
      return makeFunctionError('connect', 'Sign in via the Tessallite panel.');
    }
    return makeFunctionError('invalidValue', msg);
  }
}

// ---------------------------------------------------------------------------
// Legacy ID-based functions (TESSALLITE.LISTBYID / KPIVALUE / KPIGOAL /
// KPISTATUS) — retained for backward compatibility, same TESSALLITE namespace.
// ---------------------------------------------------------------------------

function tessListById(
  namedSetId: string,
  invocation: CustomFunctions.StreamingInvocation<unknown>,
): void {
  let cancelled = false;
  invocation.onCanceled = () => { cancelled = true; };

  (async () => {
    try {
      const ctx = await requireFullContext();
      purgeForeignProfileEntries(ctx);
      const key = cacheKey(ctx, namedSetId);
      const cached = listCache.get(key);
      if (cached && Date.now() - cached.ts < CACHE_TTL_MS) {
        invocation.setResult(cached.data);
        return;
      }

      // Persona is deliberately NOT threaded here, unchanged from before: this
      // function has always relied on the caller's EFFECTIVE persona resolved
      // server-side, while the task pane sends its actively-selected one. That
      // difference is a real cross-surface inconsistency, but it is not this
      // change's business — logged separately rather than altered in passing.
      const result = await apiRequest<PreviewResult>(
        `/api/v1/projects/${ctx.projectId}/models/${ctx.modelId}/named-sets/${namedSetId}/preview`
        + consumptionQuery(),
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

// Bug-6908: the ID-based KPI functions (TESSALLITE.KPIVALUE / KPIGOAL /
// KPISTATUS) route through makeFunctionError so Excel treats errors as
// structured cell errors (ISNA/IFERROR/sorting) instead of literal text.
async function tessKpiValue(kpiId: string): Promise<number | string> {
  try {
    const ev = await evalKpiCached(kpiId);
    if (ev.value === null) return makeFunctionError('notAvailable', 'KPI value is not available.');
    return ev.value;
  } catch (e) {
    if (typeof CustomFunctions !== 'undefined' && CustomFunctions.Error &&
        e instanceof CustomFunctions.Error) {
      throw e;
    }
    const msg = safeErrorMessage(e);
    if (msg.startsWith('Session expired') || msg.startsWith('Not signed in')) {
      return makeFunctionError('connect', 'Sign in via the Tessallite panel.');
    }
    return makeFunctionError('invalidValue', msg);
  }
}

async function tessKpiGoal(kpiId: string): Promise<number | string> {
  try {
    const ev = await evalKpiCached(kpiId);
    if (ev.goal === null) return makeFunctionError('notAvailable', 'KPI goal is not available.');
    return ev.goal;
  } catch (e) {
    if (typeof CustomFunctions !== 'undefined' && CustomFunctions.Error &&
        e instanceof CustomFunctions.Error) {
      throw e;
    }
    const msg = safeErrorMessage(e);
    if (msg.startsWith('Session expired') || msg.startsWith('Not signed in')) {
      return makeFunctionError('connect', 'Sign in via the Tessallite panel.');
    }
    return makeFunctionError('invalidValue', msg);
  }
}

async function tessKpiStatus(kpiId: string): Promise<number | string> {
  try {
    const ev = await evalKpiCached(kpiId);
    if (ev.status === null) return makeFunctionError('notAvailable', 'KPI status is not available.');
    return ev.status;
  } catch (e) {
    if (typeof CustomFunctions !== 'undefined' && CustomFunctions.Error &&
        e instanceof CustomFunctions.Error) {
      throw e;
    }
    const msg = safeErrorMessage(e);
    if (msg.startsWith('Session expired') || msg.startsWith('Not signed in')) {
      return makeFunctionError('connect', 'Sign in via the Tessallite panel.');
    }
    return makeFunctionError('invalidValue', msg);
  }
}

async function evalKpiCached(kpiId: string): Promise<KpiEvalResult> {
  const ctx = await requireFullContext();
  purgeForeignProfileEntries(ctx);
  const key = cacheKey(ctx, kpiId);
  const cached = kpiCache.get(key);
  if (cached && Date.now() - cached.ts < CACHE_TTL_MS) return cached.data;

  const personaQuery = ctx.personaId ? `?persona_id=${encodeURIComponent(ctx.personaId)}` : '';
  const data = await apiRequest<KpiEvalResult>(
    `/api/v1/projects/${ctx.projectId}/models/${ctx.modelId}/kpis/${kpiId}/evaluate${personaQuery}`,
    'POST',
  );

  kpiCache.set(key, { data, ts: Date.now() });
  return data;
}

// ---------------------------------------------------------------------------
// TESSALLITE.DIAG — diagnostic function to debug storage/runtime state
// ---------------------------------------------------------------------------

async function tessalliteDiag(): Promise<string> {
  const parts: string[] = [];

  // 1. Check OfficeRuntime availability
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const global = globalThis as any;
  if (typeof global.OfficeRuntime === 'undefined') {
    parts.push('RT=NO');
    return parts.join(' | ');
  }

  const storageApi = global.OfficeRuntime.storage;
  if (!storageApi) {
    parts.push('STOR=NO');
    return parts.join(' | ');
  }
  parts.push('STOR=OK');

  // 2. Check model slug (needed for formula matching)
  try {
    const slug: string | null = await withTimeout(storageApi.getItem('tessallite_model_slug'), 3000);
    parts.push(`SLUG=${slug || 'NULL'}`);
  } catch {
    parts.push('SLUG=ERR');
  }

  // 3. Check model name
  try {
    const name: string | null = await withTimeout(storageApi.getItem('tessallite_model_name'), 3000);
    parts.push(`MNAME=${name || 'NULL'}`);
  } catch {
    parts.push('MNAME=ERR');
  }

  // 4. Get profile serverUrl
  let serverUrl = '';
  try {
    const profileJson: string | null = await withTimeout(storageApi.getItem('tessallite_session_profile'), 3000);
    if (profileJson) {
      const profile = JSON.parse(profileJson);
      serverUrl = profile.serverUrl || '';
      parts.push(`URL=${serverUrl}`);
    } else {
      parts.push('URL=NO_PROFILE');
    }
  } catch {
    parts.push('URL=ERR');
  }

  // 5. Test fetch to health endpoint
  if (serverUrl) {
    try {
      const res = await withTimeout(
        fetch(`${serverUrl.replace(/\/$/, '')}/health`, {
          method: 'GET',
          credentials: 'omit',
        }),
        5000,
      );
      parts.push(`FETCH=${res.status}`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'unknown';
      parts.push(`FETCH=FAIL(${msg.substring(0, 40)})`);
    }
  }

  return parts.join(' | ');
}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timeout_${ms}ms`)), ms);
    promise.then(
      (val) => { clearTimeout(timer); resolve(val); },
      (err) => { clearTimeout(timer); reject(err); },
    );
  });
}

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------

if (typeof CustomFunctions !== 'undefined') {
  // All IDs resolve under the single published TESSALLITE namespace (see the
  // F-025-02 note in the file header). associate() binds the ID, not a
  // namespace — there is no separate TESS.* namespace.

  // Name-based functions: TESSALLITE.VALUE / KPI / MEMBERVALUE
  CustomFunctions.associate('VALUE', tessalliteValue);
  CustomFunctions.associate('KPI', tessalliteKpi);
  CustomFunctions.associate('MEMBERVALUE', tessalliteMemberValue);

  // ID-based functions (backward-compatible): TESSALLITE.LISTBYID / KPIVALUE /
  // KPIGOAL / KPISTATUS
  CustomFunctions.associate('LISTBYID', tessListById);
  CustomFunctions.associate('KPIVALUE', tessKpiValue);
  CustomFunctions.associate('KPIGOAL', tessKpiGoal);
  CustomFunctions.associate('KPISTATUS', tessKpiStatus);

  // Diagnostic: TESSALLITE.DIAG
  CustomFunctions.associate('DIAG', tessalliteDiag);
}
