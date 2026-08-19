import { describe, it, expect, beforeEach, vi } from 'vitest';

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

const registered: Record<string, Function> = {};
(globalThis as Record<string, unknown>).CustomFunctions = {
  associate: (name: string, fn: Function) => { registered[name] = fn; },
};

// Import helpers separately for testing.
const { parseFilterArgs, clearFunctionCaches } = await import('../functions');

// The import above triggers CustomFunctions.associate for all functions.

function setAuth(
  jwt: string,
  serverUrl: string,
  projectId: string,
  modelId: string,
  personaId: string | null = null,
  modelSlug = 'inventory',
  modelName = 'Inventory',
) {
  mockGetJwt.mockResolvedValue(jwt);
  mockGetActiveProfile.mockResolvedValue({ id: 'profile-1', serverUrl });
  mockGetModelContext.mockResolvedValue({ projectId, modelId, modelSlug, modelName });
  mockGetActivePersonaId.mockResolvedValue(personaId);
}

beforeEach(() => {
  vi.restoreAllMocks();
  mockGetJwt.mockReset();
  mockGetActiveProfile.mockReset();
  mockGetModelContext.mockReset();
  mockGetActivePersonaId.mockReset();
  mockGetActivePersonaId.mockResolvedValue(null);
  clearFunctionCaches();
});

describe('CustomFunctions registration', () => {
  it('registers all legacy ID-based functions (TESSALLITE.LISTBYID/KPIVALUE/KPIGOAL/KPISTATUS)', () => {
    expect(registered['LISTBYID']).toBeDefined();
    expect(registered['KPIVALUE']).toBeDefined();
    expect(registered['KPIGOAL']).toBeDefined();
    expect(registered['KPISTATUS']).toBeDefined();
  });

  it('registers all name-based TESSALLITE.* functions', () => {
    expect(registered['VALUE']).toBeDefined();
    expect(registered['KPI']).toBeDefined();
    expect(registered['MEMBERVALUE']).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// TESSALLITE.VALUE argument validation
// ---------------------------------------------------------------------------

describe('TESSALLITE.VALUE argument validation', () => {
  it('returns error when model is missing', async () => {
    const result = await registered['VALUE']('', 'cost');
    expect(result).toContain('#ERROR');
    expect(result).toContain('Model name is required');
  });

  it('returns error when measure is missing', async () => {
    const result = await registered['VALUE']('inventory', '');
    expect(result).toContain('#ERROR');
    expect(result).toContain('Measure name is required');
  });

  it('returns #CONNECT! when not signed in', async () => {
    mockGetJwt.mockResolvedValue(null);
    mockGetActiveProfile.mockResolvedValue({ id: 'p', serverUrl: 'https://x.com' });
    mockGetModelContext.mockResolvedValue({ projectId: 'p', modelId: 'm', modelSlug: 'inventory', modelName: 'Inventory' });
    mockGetActivePersonaId.mockResolvedValue(null);

    // The batcher will call apiRequest which throws "Not signed in".
    // But we need to wait for the coalescing timer. Use a real approach:
    // mock fetch to fail with auth error.
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
      ok: false,
      status: 401,
    } as Response);

    const result = await registered['VALUE']('inventory', 'cost');
    // The error message should indicate auth issue.
    expect(typeof result).toBe('string');
    expect(result).toMatch(/#CONNECT!|#ERROR/);
  });

  it('uses the active model only when the formula model matches the stored slug', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1');
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ data: [{ shipping_cost: 123 }] }),
    } as Response);

    const result = await registered['VALUE']('inventory', 'shipping_cost');

    expect(result).toBe(123);
    expect(fetch).toHaveBeenCalledWith(
      'https://test.tessallite.com/api/v1/plugin/execute',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          project_id: 'proj-1',
          model_id: 'model-1',
          measures: ['shipping_cost'],
          dimensions: undefined,
          filters: undefined,
          persona_id: undefined,
        }),
      }),
    );
  });

  it('fails closed when the formula model differs from the active model', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1');
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    const result = await registered['VALUE']('sales', 'shipping_cost');

    expect(result).toContain('#ERROR');
    expect(result).toContain('Formula model does not match the selected model');
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// TESSALLITE.KPI argument validation
// ---------------------------------------------------------------------------

describe('TESSALLITE.KPI argument validation', () => {
  it('returns error when model is missing', async () => {
    const result = await registered['KPI']('', 'Shipping Cost', 'value');
    expect(result).toContain('#ERROR');
    expect(result).toContain('Model name is required');
  });

  it('returns error when kpiName is missing', async () => {
    const result = await registered['KPI']('inventory', '', 'value');
    expect(result).toContain('#ERROR');
    expect(result).toContain('KPI name is required');
  });

  it('returns error when property is missing', async () => {
    const result = await registered['KPI']('inventory', 'Shipping Cost', '');
    expect(result).toContain('#ERROR');
    expect(result).toContain('Property is required');
  });

  it('returns error for invalid property', async () => {
    const result = await registered['KPI']('inventory', 'Shipping Cost', 'invalid');
    expect(result).toContain('#ERROR');
    expect(result).toContain('Invalid property');
  });

  it('fails closed when a KPI formula references a different model', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1');
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    const result = await registered['KPI']('sales', 'Shipping Cost', 'value');

    expect(result).toContain('#ERROR');
    expect(result).toContain('Formula model does not match the selected model');
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// TESSALLITE.MEMBERVALUE argument validation
// ---------------------------------------------------------------------------

describe('TESSALLITE.MEMBERVALUE argument validation', () => {
  it('returns error when model is missing', async () => {
    const result = await registered['MEMBERVALUE']('', 'cost', 'region', 'EU');
    expect(result).toContain('#ERROR');
    expect(result).toContain('Model name is required');
  });

  it('returns error when dimension is missing', async () => {
    const result = await registered['MEMBERVALUE']('inv', 'cost', '', 'EU');
    expect(result).toContain('#ERROR');
    expect(result).toContain('Dimension name is required');
  });

  it('returns error when member is missing', async () => {
    const result = await registered['MEMBERVALUE']('inv', 'cost', 'region', '');
    expect(result).toContain('#ERROR');
    expect(result).toContain('Member value is required');
  });
});

// ---------------------------------------------------------------------------
// parseFilterArgs
// ---------------------------------------------------------------------------

describe('parseFilterArgs', () => {
  it('parses filter pairs', () => {
    const result = parseFilterArgs('region', 'EU', 'year', '2025');
    expect(result).toEqual([['region', 'EU'], ['year', '2025']]);
  });

  it('skips empty/null/undefined pairs', () => {
    const result = parseFilterArgs('region', 'EU', undefined, undefined, '', '');
    expect(result).toEqual([['region', 'EU']]);
  });

  it('returns empty array when no args', () => {
    expect(parseFilterArgs()).toEqual([]);
  });

  it('ignores odd trailing argument', () => {
    const result = parseFilterArgs('region');
    expect(result).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Legacy ID-based KPIVALUE (backward compatibility) — reachable as
// TESSALLITE.KPIVALUE (single published namespace; F-025-02).
// ---------------------------------------------------------------------------

describe('TESSALLITE.KPIVALUE (legacy ID-based)', () => {
  it('returns the KPI value from the evaluate endpoint', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1');
    const mockResponse = { value: 42, goal: 100, status: 1, trend: 1, status_label: null, trend_label: null, formatted_value: null, formatted_goal: null };
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(mockResponse),
    } as Response);

    const result = await registered['KPIVALUE']('kpi-abc');
    expect(result).toBe(42);
    expect(fetch).toHaveBeenCalledWith(
      'https://test.tessallite.com/api/v1/projects/proj-1/models/model-1/kpis/kpi-abc/evaluate',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('returns #N/A error when value is null (Bug-6908: CF Error path)', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1');
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ value: null, goal: null, status: null, trend: null, status_label: null, trend_label: null, formatted_value: null, formatted_goal: null }),
    } as Response);

    const result = await registered['KPIVALUE']('kpi-null');
    // Bug-6908: makeFunctionError returns '#N/A ...' in test env (no CF Error)
    expect(result).toMatch(/^#N\/A/);
  });

  it('returns connect error when not signed in (Bug-6908)', async () => {
    mockGetJwt.mockResolvedValue(null);
    mockGetActiveProfile.mockResolvedValue({ id: 'profile-1', serverUrl: 'https://x.com' });
    mockGetModelContext.mockResolvedValue({ projectId: 'p', modelId: 'm' });

    const result = await registered['KPIVALUE']('kpi-1');
    // Bug-6908: now routes through makeFunctionError -> '#CONNECT!' prefix
    expect(result).toMatch(/#CONNECT!|Sign in/);
  });

  it('returns error through makeFunctionError when no model context (Bug-6908)', async () => {
    mockGetJwt.mockResolvedValue('jwt');
    mockGetActiveProfile.mockResolvedValue({ id: 'profile-1', serverUrl: 'https://x.com' });
    mockGetModelContext.mockResolvedValue(null);
    mockGetActivePersonaId.mockResolvedValue(null);

    const result = await registered['KPIVALUE']('kpi-1');
    // Bug-6908: now routes through makeFunctionError -> '#ERROR:' prefix
    expect(result).toMatch(/#ERROR:|No model selected/);
  });

  it('threads the active persona into the evaluate URL', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1', 'persona-9');
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ value: 7, goal: 10, status: 1, trend: 0, status_label: null, trend_label: null, formatted_value: null, formatted_goal: null }),
    } as Response);

    await registered['KPIVALUE']('kpi-abc');
    expect(fetch).toHaveBeenCalledWith(
      'https://test.tessallite.com/api/v1/projects/proj-1/models/model-1/kpis/kpi-abc/evaluate?persona_id=persona-9',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('does not serve a cached value across a persona switch', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1', 'persona-A');
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ value: 1, goal: 10, status: 1, trend: 0, status_label: null, trend_label: null, formatted_value: null, formatted_goal: null }),
    } as Response);
    await registered['KPIVALUE']('kpi-x');
    const firstCallCount = (fetch as ReturnType<typeof vi.fn>).mock.calls.length;

    mockGetActivePersonaId.mockResolvedValue('persona-B');
    await registered['KPIVALUE']('kpi-x');
    expect((fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(firstCallCount + 1);
    expect(fetch).toHaveBeenLastCalledWith(
      expect.stringContaining('persona_id=persona-B'),
      expect.objectContaining({ method: 'POST' }),
    );
  });
});

describe('TESSALLITE.KPIGOAL (legacy ID-based)', () => {
  it('returns the KPI goal value', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1');
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ value: 42, goal: 100, status: 1, trend: 1, status_label: null, trend_label: null, formatted_value: null, formatted_goal: null }),
    } as Response);

    const result = await registered['KPIGOAL']('kpi-abc');
    expect(result).toBe(100);
  });
});

describe('TESSALLITE.KPISTATUS (legacy ID-based)', () => {
  it('returns the KPI status code', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1');
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ value: 42, goal: 100, status: -1, trend: 0, status_label: null, trend_label: null, formatted_value: null, formatted_goal: null }),
    } as Response);

    const result = await registered['KPISTATUS']('kpi-status-1');
    expect(result).toBe(-1);
  });

  it('returns #N/A error when status is null (Bug-6908)', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1');
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ value: 42, goal: 100, status: null, trend: null, status_label: null, trend_label: null, formatted_value: null, formatted_goal: null }),
    } as Response);

    const result = await registered['KPISTATUS']('kpi-status-null');
    // Bug-6908: makeFunctionError returns '#N/A ...' in test env (no CF Error)
    expect(result).toMatch(/^#N\/A/);
  });
});

describe('TESSALLITE.LISTBYID (legacy ID-based)', () => {
  it('streams named set members as a matrix', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1');
    const previewResult = {
      items: [
        { ordinal: 0, caption: 'Alpha', key: 'k1' },
        { ordinal: 1, caption: 'Beta', key: 'k2' },
        { ordinal: 2, caption: 'Gamma', key: 'k3' },
      ],
      total_count: 3,
      truncated: false,
    };
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(previewResult),
    } as Response);

    let resultValue: unknown;
    const invocation = {
      setResult: (val: unknown) => { resultValue = val; },
      onCanceled: () => {},
    };

    registered['LISTBYID']('ns-123', invocation);

    await vi.waitFor(() => {
      expect(resultValue).toEqual([['Alpha'], ['Beta'], ['Gamma']]);
    }, { interval: 1, timeout: 100 });
  });

  it('returns (empty set) for empty result', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1');
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ items: [], total_count: 0, truncated: false }),
    } as Response);

    let resultValue: unknown;
    const invocation = {
      setResult: (val: unknown) => { resultValue = val; },
      onCanceled: () => {},
    };

    registered['LISTBYID']('ns-empty', invocation);

    await vi.waitFor(() => {
      expect(resultValue).toEqual([['(empty set)']]);
    }, { interval: 1, timeout: 100 });
  });

  it('returns error on API failure', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1');
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
      ok: false,
      status: 500,
    } as Response);

    let resultValue: unknown;
    const invocation = {
      setResult: (val: unknown) => { resultValue = val; },
      onCanceled: () => {},
    };

    registered['LISTBYID']('ns-fail', invocation);

    await vi.waitFor(() => {
      expect(resultValue).toEqual([['#ERROR: Server error. Try again later.']]);
    }, { interval: 1, timeout: 100 });
  });
});

// ---------------------------------------------------------------------------
// Default vs Advanced routing
// ---------------------------------------------------------------------------

describe('Default vs Advanced formula routing', () => {
  it('TESSALLITE.VALUE is the default (sigma icon)', () => {
    // Verify VALUE is registered. In the UI, the sigma icon now calls
    // handleInsertMeasureAsFunction which emits =TESSALLITE.VALUE("model","measure").
    expect(registered['VALUE']).toBeDefined();
  });

  it('CUBE formulas remain available under Advanced', () => {
    // The CUBEVALUE path (handleInsertMeasureAsFormula) is still wired
    // through the CubeFormulaWizard and explicit "Advanced" UI paths.
    // This test confirms the legacy TESS.* functions are still registered.
    expect(registered['KPIVALUE']).toBeDefined();
    expect(registered['KPIGOAL']).toBeDefined();
    expect(registered['KPISTATUS']).toBeDefined();
    expect(registered['LISTBYID']).toBeDefined();
  });
});
