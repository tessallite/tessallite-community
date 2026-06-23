import { describe, it, expect, beforeEach, vi } from 'vitest';

const mockGetJwt = vi.fn();
const mockGetActiveProfile = vi.fn();
const mockGetModelContext = vi.fn();
const mockGetActivePersonaId = vi.fn();

vi.mock('../utils/storage', () => ({
  getJwt: () => mockGetJwt(),
  getActiveProfile: () => mockGetActiveProfile(),
  getModelContext: () => mockGetModelContext(),
  // F-025-17: the CF runtime now reads the active persona to scope KPI evals.
  getActivePersonaId: () => mockGetActivePersonaId(),
}));

const registered: Record<string, Function> = {};
(globalThis as Record<string, unknown>).CustomFunctions = {
  associate: (name: string, fn: Function) => { registered[name] = fn; },
};

await import('../functions');

function setAuth(jwt: string, serverUrl: string, projectId: string, modelId: string, personaId: string | null = null) {
  mockGetJwt.mockResolvedValue(jwt);
  mockGetActiveProfile.mockResolvedValue({ id: 'profile-1', serverUrl });
  mockGetModelContext.mockResolvedValue({ projectId, modelId });
  mockGetActivePersonaId.mockResolvedValue(personaId);
}

beforeEach(() => {
  vi.restoreAllMocks();
  mockGetJwt.mockReset();
  mockGetActiveProfile.mockReset();
  mockGetModelContext.mockReset();
  mockGetActivePersonaId.mockReset();
  mockGetActivePersonaId.mockResolvedValue(null);
});

describe('CustomFunctions registration', () => {
  it('registers all four functions', () => {
    expect(registered['LISTBYID']).toBeDefined();
    expect(registered['KPIVALUE']).toBeDefined();
    expect(registered['KPIGOAL']).toBeDefined();
    expect(registered['KPISTATUS']).toBeDefined();
  });
});

describe('TESS.KPIVALUE', () => {
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

  it('returns #N/A when value is null', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1');
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ value: null, goal: null, status: null, trend: null, status_label: null, trend_label: null, formatted_value: null, formatted_goal: null }),
    } as Response);

    const result = await registered['KPIVALUE']('kpi-null');
    expect(result).toBe('#N/A');
  });

  it('returns error string when not signed in', async () => {
    mockGetJwt.mockResolvedValue(null);
    mockGetActiveProfile.mockResolvedValue({ id: 'profile-1', serverUrl: 'https://x.com' });
    mockGetModelContext.mockResolvedValue({ projectId: 'p', modelId: 'm' });

    const result = await registered['KPIVALUE']('kpi-1');
    expect(result).toContain('#ERROR');
    expect(result).toContain('Not signed in');
  });

  it('returns error string when no model context', async () => {
    mockGetJwt.mockResolvedValue('jwt');
    mockGetActiveProfile.mockResolvedValue({ id: 'profile-1', serverUrl: 'https://x.com' });
    mockGetModelContext.mockResolvedValue(null);
    mockGetActivePersonaId.mockResolvedValue(null);

    const result = await registered['KPIVALUE']('kpi-1');
    expect(result).toContain('#ERROR');
    expect(result).toContain('No model selected');
  });

  // F-025-17: when a persona is active in the pane, the CF runtime evaluates
  // the KPI under that persona (persona_id query param) so TESS.* values match
  // the KPI tab.
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

  // F-025-13: a KPI cached under one persona must not be returned after the
  // persona switches — the read-time purge drops the foreign-persona entry and
  // a fresh fetch (with the new persona) is issued.
  it('does not serve a cached value across a persona switch', async () => {
    // First eval under persona A.
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1', 'persona-A');
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ value: 1, goal: 10, status: 1, trend: 0, status_label: null, trend_label: null, formatted_value: null, formatted_goal: null }),
    } as Response);
    await registered['KPIVALUE']('kpi-x');
    const firstCallCount = (fetch as ReturnType<typeof vi.fn>).mock.calls.length;

    // Switch to persona B and re-evaluate the same KPI — must refetch.
    mockGetActivePersonaId.mockResolvedValue('persona-B');
    await registered['KPIVALUE']('kpi-x');
    expect((fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(firstCallCount + 1);
    expect(fetch).toHaveBeenLastCalledWith(
      expect.stringContaining('persona_id=persona-B'),
      expect.objectContaining({ method: 'POST' }),
    );
  });
});

describe('TESS.KPIGOAL', () => {
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

describe('TESS.KPISTATUS', () => {
  it('returns the KPI status code', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1');
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ value: 42, goal: 100, status: -1, trend: 0, status_label: null, trend_label: null, formatted_value: null, formatted_goal: null }),
    } as Response);

    const result = await registered['KPISTATUS']('kpi-status-1');
    expect(result).toBe(-1);
  });

  it('returns #N/A when status is null', async () => {
    setAuth('jwt-token', 'https://test.tessallite.com', 'proj-1', 'model-1');
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ value: 42, goal: 100, status: null, trend: null, status_label: null, trend_label: null, formatted_value: null, formatted_goal: null }),
    } as Response);

    const result = await registered['KPISTATUS']('kpi-status-null');
    expect(result).toBe('#N/A');
  });
});

describe('TESS.LISTBYID', () => {
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
    await new Promise((r) => setTimeout(r, 50));

    expect(resultValue).toEqual([['Alpha'], ['Beta'], ['Gamma']]);
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
    await new Promise((r) => setTimeout(r, 50));

    expect(resultValue).toEqual([['(empty set)']]);
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
    await new Promise((r) => setTimeout(r, 50));

    expect(resultValue).toEqual([['#ERROR: Server error. Try again later.']]);
  });
});
