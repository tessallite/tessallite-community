/**
 * Bug-8710 / Bug-8712 — the Excel add-in reads PUBLISHED definitions.
 *
 * The add-in is an end-user BI transport, not an authoring surface. The
 * governing contract is not new: "the deployed snapshot is the contract; the
 * live state is editor-only" (F-013-01), stated operationally for these routes
 * by F-017-05 — BI catalogue surfaces send `deployed_only`, and only the model
 * builder omits it.
 *
 * There are TWO clients inside this add-in, not one. The task pane goes through
 * `api/modelService.ts`; the custom-functions runtime is a separate WWAHost
 * AppContainer with its own `apiRequest`, sharing only `OfficeRuntime.storage`.
 * A fix applied to `modelService.ts` alone leaves every `TESSALLITE.*` cell
 * formula reading live drafts, so both are pinned here.
 *
 * What each half proves: on the CLIENT side the parameter IS the mechanism, so
 * asserting the request is the right assertion. That the parameter changes which
 * definition is computed is proved server-side, on known values, by
 * `model-service/tests/test_bug_8712_named_set_preview_deploy_authority.py`.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';

const mockStorage: Record<string, string> = {};

const mockGetJwt = vi.fn();
const mockGetActiveProfile = vi.fn();
const mockGetModelContext = vi.fn();
const mockGetActivePersonaId = vi.fn();

vi.mock('../utils/storage', () => ({
  getJwt: () => mockGetJwt(),
  getActiveProfile: () => mockGetActiveProfile(),
  getModelContext: () => mockGetModelContext(),
  getActivePersonaId: () => mockGetActivePersonaId(),
  getCacheGeneration: () => Promise.resolve(null),
  bumpCacheGeneration: () => Promise.resolve(),
}));

(globalThis as Record<string, unknown>).OfficeRuntime = {
  storage: {
    getItem: async (key: string) => mockStorage[key] ?? null,
    setItem: async (key: string, value: string) => { mockStorage[key] = value; },
    removeItem: async (key: string) => { delete mockStorage[key]; },
  },
};

const registered: Record<string, Function> = {};
(globalThis as Record<string, unknown>).CustomFunctions = {
  associate: (name: string, fn: Function) => { registered[name] = fn; },
};

import { configureApiClient, ApiError, formatApiError } from '../api/client';
import { getKpis, getNamedSets, previewNamedSet } from '../api/modelService';

const { clearFunctionCaches } = await import('../functions');

const PROJECT = 'proj-1';
const MODEL = 'model-1';
const SET = 'set-1';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

beforeEach(() => {
  vi.restoreAllMocks();
  mockStorage['tessallite_jwt'] = 'test-jwt';
  configureApiClient('https://test.example.com');
  mockGetJwt.mockReset();
  mockGetActiveProfile.mockReset();
  mockGetModelContext.mockReset();
  mockGetActivePersonaId.mockReset();
  mockGetJwt.mockResolvedValue('test-jwt');
  mockGetActiveProfile.mockResolvedValue({ id: 'p1', serverUrl: 'https://test.example.com' });
  mockGetModelContext.mockResolvedValue({
    projectId: PROJECT, modelId: MODEL, modelSlug: 'inventory', modelName: 'Inventory',
  });
  mockGetActivePersonaId.mockResolvedValue(null);
  clearFunctionCaches();
});

function requestedUrl(fetchMock: ReturnType<typeof vi.spyOn>): string {
  return String(fetchMock.mock.calls[0][0]);
}

// ---------------------------------------------------------------------------
// Transport 1 — the task pane (Bug-8710)
// ---------------------------------------------------------------------------

describe('task pane catalogue reads the deployed snapshot (Bug-8710)', () => {
  it('getKpis requests deployed_only', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(jsonResponse([]));
    await getKpis(PROJECT, MODEL);
    expect(requestedUrl(fetchMock)).toContain('deployed_only=true');
  });

  it('getNamedSets requests deployed_only', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(jsonResponse([]));
    await getNamedSets(PROJECT, MODEL);
    expect(requestedUrl(fetchMock)).toContain('deployed_only=true');
  });

  it('keeps the persona alongside the deploy authority', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(jsonResponse([]));
    await getKpis(PROJECT, MODEL, 'persona-9');
    const url = requestedUrl(fetchMock);
    expect(url).toContain('deployed_only=true');
    expect(url).toContain('persona_id=persona-9');
  });
});

// ---------------------------------------------------------------------------
// Transport 2 — the named-set preview, which writes members into cells
// (Bug-8712)
// ---------------------------------------------------------------------------

describe('named-set preview reads the deployed snapshot (Bug-8712)', () => {
  it('previewNamedSet requests deployed_only', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      jsonResponse({ items: [], total_count: 0, truncated: false }),
    );
    await previewNamedSet(PROJECT, MODEL, SET);
    const url = requestedUrl(fetchMock);
    expect(url).toContain(`/named-sets/${SET}/preview`);
    expect(url).toContain('deployed_only=true');
  });
});

// ---------------------------------------------------------------------------
// Transport 3 — the custom-functions runtime, a SEPARATE client (Bug-8710 /
// Bug-8712). Pinning `modelService.ts` does not reach it.
// ---------------------------------------------------------------------------

describe('custom-functions runtime reads the deployed snapshot', () => {
  it('TESSALLITE.KPI name lookup requests deployed_only', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse([{ id: 'k1', name: 'net_revenue', display_name: 'Net Revenue' }]),
    );
    await registered['KPI']('inventory', 'Net Revenue', 'value');
    const kpiListCall = fetchMock.mock.calls
      .map(call => String(call[0]))
      .find(url => /\/kpis\?/.test(url));
    expect(kpiListCall).toBeDefined();
    expect(kpiListCall).toContain('deployed_only=true');
  });

  it('TESSALLITE.LISTBYID requests deployed_only', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ items: [{ ordinal: 1, caption: 'North', key: 'North' }], total_count: 1 }),
    );
    const invocation = { setResult: vi.fn(), onCanceled: null as unknown };
    registered['LISTBYID'](SET, invocation);
    await vi.waitFor(() => expect(invocation.setResult).toHaveBeenCalled());
    expect(requestedUrl(fetchMock)).toContain('deployed_only=true');
  });
});

// ---------------------------------------------------------------------------
// Fail-closed 409 (Bug-8384 / Bug-8712): never fall back to live, and say what
// to do about it.
// ---------------------------------------------------------------------------

describe('DEPLOYED_SNAPSHOT_INVALID fails closed with a usable message', () => {
  it('the task pane maps 409 to a deploy instruction, not the raw detail', () => {
    const message = formatApiError(
      new ApiError(409, { detail: 'DEPLOYED_SNAPSHOT_INVALID: snapshot is empty or malformed.' }),
    );
    expect(message).toContain('published version');
    expect(message).toContain('deploy');
    expect(message).not.toContain('DEPLOYED_SNAPSHOT_INVALID');
  });

  it('a cell formula shows the fail-closed message rather than a draft value', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ detail: 'DEPLOYED_SNAPSHOT_INVALID: bad snapshot' }, 409),
    );
    const invocation = { setResult: vi.fn(), onCanceled: null as unknown };
    registered['LISTBYID'](SET, invocation);
    await vi.waitFor(() => expect(invocation.setResult).toHaveBeenCalled());
    const rendered = JSON.stringify(invocation.setResult.mock.calls[0][0]);
    expect(rendered).toContain('Published model unavailable');
  });
});
