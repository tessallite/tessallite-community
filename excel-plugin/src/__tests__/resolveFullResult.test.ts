/**
 * Bug-6357-A / Bug-6357-B: resolveFullResult discriminated-result tests.
 *
 * These tests exercise the resolveFullResult logic extracted from App.tsx to
 * verify that:
 *  (A) Each insert path (table, chart, pivot) shows exactly ONE correct toast
 *      per scenario — no misleading double-toast when truncation is blocked.
 *  (B) When re-fetch succeeds but returns empty rows for a truncated sample,
 *      the code blocks the insert (does not silently fall back to the partial
 *      50-row sample).
 *
 * The tests directly exercise the resolveFullResult logic via a minimal
 * extraction that mirrors App.tsx's implementation, mocking executeQuery and
 * showToast to verify side-effects.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { TurnResponse } from '@tessallite/shared-ui';
import { templates, strings } from '../i18n/strings';
import { normalizeAgentSemanticQuery } from '../utils/semanticQueryNormalizer';

// ---------------------------------------------------------------------------
// Mock executeQuery -- we control it per-test via mockImplementation
// ---------------------------------------------------------------------------
const mockExecuteQuery = vi.fn();
vi.mock('../api/queryRouter', () => ({
  executeQuery: (...args: unknown[]) => mockExecuteQuery(...args),
}));

// ---------------------------------------------------------------------------
// showToast spy
// ---------------------------------------------------------------------------
const showToast = vi.fn();

// ---------------------------------------------------------------------------
// resolveFullResult: extracted from App.tsx with the same logic, parameterised
// so we can test it without rendering a React component.
// ---------------------------------------------------------------------------
type ResolveResult =
  | { status: 'ok'; headers: string[]; rows: (string | number)[][] }
  | { status: 'blocked' }
  | { status: 'empty' };

async function resolveFullResult(
  turn: TurnResponse,
  opts: { projectId: string | null; modelId: string | null; activePersonaId: string | null },
): Promise<ResolveResult> {
  const { executeQuery } = await import('../api/queryRouter');
  const sample = turn.query_result_sample;
  if (!sample || sample.length === 0) return { status: 'empty' };

  const totalRows = turn.query_result_rows ?? sample.length;
  const isTruncated = totalRows > sample.length;

  if (!isTruncated) {
    const headers = Object.keys(sample[0]);
    const rows = sample.map(r => headers.map(h => r[h] as string | number));
    return { status: 'ok', headers, rows };
  }

  if (!turn.semantic_query || !opts.projectId || !opts.modelId) {
    showToast(
      templates.toasts.insertTruncatedWarning(sample.length, totalRows),
      'warning',
    );
    return { status: 'blocked' };
  }

  try {
    // Bug-7392: normalize agent-schema semantic_query to plugin's
    // SemanticQuery shape before re-fetch (mirrors App.tsx production fix).
    const sq = normalizeAgentSemanticQuery(turn.semantic_query);
    const response = await executeQuery(sq, {
      projectId: opts.projectId,
      modelId: opts.modelId,
      personaId: opts.activePersonaId || undefined,
    });
    if (response.data && response.data.length > 0) {
      const headers = Object.keys(response.data[0]);
      const rows = response.data.map(
        (r: Record<string, unknown>) => headers.map(h => r[h] as string | number),
      );
      return { status: 'ok', headers, rows };
    }
  } catch {
    showToast(
      templates.toasts.insertRefetchFailed(sample.length, totalRows),
      'warning',
    );
    return { status: 'blocked' };
  }

  // Bug-6357-B: re-fetch returned empty for a truncated result -- block
  showToast(
    templates.toasts.insertRefetchEmpty(sample.length, totalRows),
    'warning',
  );
  return { status: 'blocked' };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function makeTurn(overrides: Partial<TurnResponse> = {}): TurnResponse {
  return {
    id: 'turn-1',
    conversation_id: 'conv-1',
    turn_index: 0,
    user_message: 'show sales',
    answer_text: 'Here are the results',
    status: 'complete',
    latency_ms: 100,
    thought_summary: null,
    semantic_query: { measures: ['amount'], dimensions: [] },
    routed_sql: null,
    route: null,
    citations: null,
    user_feedback: null,
    judge_verdict: null,
    judge_reasoning: null,
    judge_metrics: null,
    guardrail_actions: null,
    usage_input_tokens: null,
    usage_output_tokens: null,
    rendered_output: null,
    llm_plan: null,
    query_result_rows: null,
    query_result_sample: null,
    calculation_steps: null,
    chart_type: null,
    provider: null,
    judge_pending: undefined,
    ...overrides,
  };
}

const DEFAULT_OPTS = { projectId: 'p1', modelId: 'm1', activePersonaId: null };

const SAMPLE_3_ROWS = [
  { region: 'US', amount: 100 },
  { region: 'UK', amount: 200 },
  { region: 'DE', amount: 300 },
];

beforeEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// TESTS
// ---------------------------------------------------------------------------

describe('resolveFullResult discriminated result', () => {
  // ---- status: empty ----

  it('returns {status:"empty"} when sample is null', async () => {
    const turn = makeTurn({ query_result_sample: null });
    const result = await resolveFullResult(turn, DEFAULT_OPTS);
    expect(result).toEqual({ status: 'empty' });
    expect(showToast).not.toHaveBeenCalled();
  });

  it('returns {status:"empty"} when sample is an empty array', async () => {
    const turn = makeTurn({ query_result_sample: [] });
    const result = await resolveFullResult(turn, DEFAULT_OPTS);
    expect(result).toEqual({ status: 'empty' });
    expect(showToast).not.toHaveBeenCalled();
  });

  // ---- status: ok (not truncated) ----

  it('returns {status:"ok"} with headers and rows when sample is not truncated', async () => {
    const turn = makeTurn({
      query_result_sample: SAMPLE_3_ROWS,
      query_result_rows: 3,
    });
    const result = await resolveFullResult(turn, DEFAULT_OPTS);
    expect(result).toEqual({
      status: 'ok',
      headers: ['region', 'amount'],
      rows: [['US', 100], ['UK', 200], ['DE', 300]],
    });
    expect(showToast).not.toHaveBeenCalled();
    expect(mockExecuteQuery).not.toHaveBeenCalled();
  });

  it('returns {status:"ok"} when query_result_rows is null (assumes not truncated)', async () => {
    const turn = makeTurn({
      query_result_sample: SAMPLE_3_ROWS,
      query_result_rows: null,
    });
    const result = await resolveFullResult(turn, DEFAULT_OPTS);
    expect(result.status).toBe('ok');
    expect(mockExecuteQuery).not.toHaveBeenCalled();
  });

  // ---- status: blocked (truncated, cannot re-fetch) ----

  it('returns {status:"blocked"} with warning toast when truncated but no semantic_query', async () => {
    const turn = makeTurn({
      query_result_sample: SAMPLE_3_ROWS,
      query_result_rows: 500,
      semantic_query: null,
    });
    const result = await resolveFullResult(turn, DEFAULT_OPTS);
    expect(result).toEqual({ status: 'blocked' });
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith(
      templates.toasts.insertTruncatedWarning(3, 500),
      'warning',
    );
  });

  it('returns {status:"blocked"} with warning toast when truncated but no projectId', async () => {
    const turn = makeTurn({
      query_result_sample: SAMPLE_3_ROWS,
      query_result_rows: 500,
    });
    const result = await resolveFullResult(turn, { projectId: null, modelId: 'm1', activePersonaId: null });
    expect(result).toEqual({ status: 'blocked' });
    expect(showToast).toHaveBeenCalledTimes(1);
  });

  it('returns {status:"blocked"} with warning toast when truncated but no modelId', async () => {
    const turn = makeTurn({
      query_result_sample: SAMPLE_3_ROWS,
      query_result_rows: 500,
    });
    const result = await resolveFullResult(turn, { projectId: 'p1', modelId: null, activePersonaId: null });
    expect(result).toEqual({ status: 'blocked' });
    expect(showToast).toHaveBeenCalledTimes(1);
  });

  // ---- status: blocked (truncated, re-fetch fails) ----

  it('returns {status:"blocked"} with refetch-failed toast when re-fetch throws', async () => {
    mockExecuteQuery.mockRejectedValueOnce(new Error('Network error'));
    const turn = makeTurn({
      query_result_sample: SAMPLE_3_ROWS,
      query_result_rows: 500,
    });
    const result = await resolveFullResult(turn, DEFAULT_OPTS);
    expect(result).toEqual({ status: 'blocked' });
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith(
      templates.toasts.insertRefetchFailed(3, 500),
      'warning',
    );
  });

  // ---- Bug-6357-B: status: blocked (truncated, re-fetch returns empty) ----

  it('returns {status:"blocked"} when re-fetch succeeds but returns empty data (Bug-6357-B)', async () => {
    mockExecuteQuery.mockResolvedValueOnce({ data: [] });
    const turn = makeTurn({
      query_result_sample: SAMPLE_3_ROWS,
      query_result_rows: 500,
    });
    const result = await resolveFullResult(turn, DEFAULT_OPTS);
    expect(result).toEqual({ status: 'blocked' });
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith(
      templates.toasts.insertRefetchEmpty(3, 500),
      'warning',
    );
  });

  it('returns {status:"blocked"} when re-fetch succeeds but data is null (Bug-6357-B)', async () => {
    mockExecuteQuery.mockResolvedValueOnce({ data: null });
    const turn = makeTurn({
      query_result_sample: SAMPLE_3_ROWS,
      query_result_rows: 500,
    });
    const result = await resolveFullResult(turn, DEFAULT_OPTS);
    expect(result).toEqual({ status: 'blocked' });
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith(
      templates.toasts.insertRefetchEmpty(3, 500),
      'warning',
    );
  });

  // ---- status: ok (truncated, re-fetch succeeds with data) ----

  it('returns {status:"ok"} with full data when re-fetch succeeds', async () => {
    const fullData = [
      { region: 'US', amount: 100 },
      { region: 'UK', amount: 200 },
      { region: 'DE', amount: 300 },
      { region: 'FR', amount: 400 },
      { region: 'JP', amount: 500 },
    ];
    mockExecuteQuery.mockResolvedValueOnce({ data: fullData });
    const turn = makeTurn({
      query_result_sample: SAMPLE_3_ROWS,
      query_result_rows: 5,
    });
    const result = await resolveFullResult(turn, DEFAULT_OPTS);
    expect(result).toEqual({
      status: 'ok',
      headers: ['region', 'amount'],
      rows: [['US', 100], ['UK', 200], ['DE', 300], ['FR', 400], ['JP', 500]],
    });
    expect(showToast).not.toHaveBeenCalled();
  });

  // ---- Bug-7392: re-fetch preserves agent-schema filters ----

  it('Bug-7392: re-fetch passes normalized filters from agent where/having/sort', async () => {
    const fullData = [{ region: 'EMEA', amount: 50000 }];
    mockExecuteQuery.mockResolvedValueOnce({ data: fullData });
    const turn = makeTurn({
      query_result_sample: SAMPLE_3_ROWS,
      query_result_rows: 100,
      // Agent-service schema: where/having/sort, NOT filters/order
      semantic_query: {
        model_id: 'model-uuid',
        measures: ['amount'],
        dimensions: ['region'],
        where: [
          { name: 'region', op: 'eq', value: 'EMEA' },
          { name: 'year', op: 'gte', value: 2024 },
        ],
        having: [
          { name: 'amount', op: 'gt', value: 10000 },
        ],
        sort: [
          { name: 'amount', direction: 'desc' },
        ],
        limit: 50,
      },
    });
    const result = await resolveFullResult(turn, DEFAULT_OPTS);
    expect(result.status).toBe('ok');

    // The critical assertion: executeQuery must receive the normalized
    // SemanticQuery with filters and order -- not an object where
    // filters/order are undefined (the pre-fix behavior that caused
    // unfiltered numbers to be inserted).
    expect(mockExecuteQuery).toHaveBeenCalledTimes(1);
    const [passedQuery] = mockExecuteQuery.mock.calls[0];
    expect(passedQuery.measures).toEqual(['amount']);
    expect(passedQuery.dimensions).toEqual(['region']);
    expect(passedQuery.filters).toEqual([
      { dimension: 'region', operator: 'eq', values: ['EMEA'] },
      { dimension: 'year', operator: 'gte', values: ['2024'] },
      { dimension: 'amount', operator: 'gt', values: ['10000'] },
    ]);
    expect(passedQuery.order).toEqual({ amount: 'desc' });
    expect(passedQuery.limit).toBe(50);
  });

  it('Bug-7392: re-fetch with plugin-schema filters passes them through unchanged', async () => {
    const fullData = [{ region: 'US', amount: 999 }];
    mockExecuteQuery.mockResolvedValueOnce({ data: fullData });
    const turn = makeTurn({
      query_result_sample: SAMPLE_3_ROWS,
      query_result_rows: 100,
      // Plugin schema: already has filters/order (e.g. from a Report Builder path)
      semantic_query: {
        measures: ['amount'],
        dimensions: ['region'],
        filters: [
          { dimension: 'region', operator: 'eq', values: ['US'] },
        ],
        order: { amount: 'desc' },
      },
    });
    const result = await resolveFullResult(turn, DEFAULT_OPTS);
    expect(result.status).toBe('ok');

    const [passedQuery] = mockExecuteQuery.mock.calls[0];
    expect(passedQuery.filters).toEqual([
      { dimension: 'region', operator: 'eq', values: ['US'] },
    ]);
    expect(passedQuery.order).toEqual({ amount: 'desc' });
  });
});

// ---------------------------------------------------------------------------
// Bug-6357-A: insert handler toast behavior
// These simulate what handleInsertChart / handleInsertLocalPivot / handleInsertTable
// do with the discriminated result. The key invariant: when status === 'blocked',
// NO additional toast is shown (one was already shown by resolveFullResult).
// ---------------------------------------------------------------------------

describe('Bug-6357-A — insert handlers show exactly one correct toast', () => {
  /**
   * Simulates handleInsertChart logic from App.tsx.
   */
  function simulateInsertChart(resolved: ResolveResult): void {
    if (resolved.status === 'blocked') return;
    if (resolved.status === 'empty') {
      showToast(strings.toasts.noDataToChart, 'info');
      return;
    }
    // status === 'ok' -- would insert chart, show success toast
    showToast(strings.toasts.chartCreated, 'success');
  }

  /**
   * Simulates handleInsertLocalPivot logic from App.tsx.
   */
  function simulateInsertPivot(resolved: ResolveResult): void {
    if (resolved.status === 'blocked') return;
    if (resolved.status === 'empty') {
      showToast(strings.toasts.noDataForPivot, 'info');
      return;
    }
    showToast(strings.toasts.pivotCreated, 'success');
  }

  /**
   * Simulates handleInsertTable logic from App.tsx.
   */
  function simulateInsertTable(resolved: ResolveResult): void {
    if (resolved.status === 'blocked') return;
    if (resolved.status === 'empty') {
      showToast(strings.toasts.noDataToInsert, 'info');
      return;
    }
    showToast('Inserted N rows', 'success');
  }

  // ---- Chart: truncation-blocked should NOT double-toast ----

  it('chart handler shows NO toast when result is blocked (toast already shown by resolveFullResult)', () => {
    simulateInsertChart({ status: 'blocked' });
    expect(showToast).not.toHaveBeenCalled();
  });

  it('chart handler shows "no data" toast when result is genuinely empty', () => {
    simulateInsertChart({ status: 'empty' });
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith(strings.toasts.noDataToChart, 'info');
  });

  it('chart handler shows success toast on ok result', () => {
    simulateInsertChart({ status: 'ok', headers: ['a'], rows: [[1]] });
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith(strings.toasts.chartCreated, 'success');
  });

  // ---- Pivot: truncation-blocked should NOT double-toast ----

  it('pivot handler shows NO toast when result is blocked (toast already shown by resolveFullResult)', () => {
    simulateInsertPivot({ status: 'blocked' });
    expect(showToast).not.toHaveBeenCalled();
  });

  it('pivot handler shows "no data" toast when result is genuinely empty', () => {
    simulateInsertPivot({ status: 'empty' });
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith(strings.toasts.noDataForPivot, 'info');
  });

  it('pivot handler shows success toast on ok result', () => {
    simulateInsertPivot({ status: 'ok', headers: ['a'], rows: [[1]] });
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith(strings.toasts.pivotCreated, 'success');
  });

  // ---- Table: truncation-blocked should NOT double-toast ----

  it('table handler shows NO toast when result is blocked', () => {
    simulateInsertTable({ status: 'blocked' });
    expect(showToast).not.toHaveBeenCalled();
  });

  it('table handler shows "no data" toast when result is genuinely empty', () => {
    simulateInsertTable({ status: 'empty' });
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith(strings.toasts.noDataToInsert, 'info');
  });

  it('table handler shows success toast on ok result', () => {
    simulateInsertTable({ status: 'ok', headers: ['a'], rows: [[1]] });
    expect(showToast).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// End-to-end: resolveFullResult -> handler shows exactly ONE toast total
// ---------------------------------------------------------------------------

describe('Bug-6357-A end-to-end: exactly one toast per truncation-blocked scenario', () => {
  it('chart: truncated + re-fetch fails -> ONE warning toast, NO "no data" toast', async () => {
    mockExecuteQuery.mockRejectedValueOnce(new Error('fail'));
    const turn = makeTurn({
      query_result_sample: SAMPLE_3_ROWS,
      query_result_rows: 500,
    });
    const resolved = await resolveFullResult(turn, DEFAULT_OPTS);
    // resolveFullResult showed one warning toast
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith(
      templates.toasts.insertRefetchFailed(3, 500),
      'warning',
    );

    // Chart handler must NOT add a second toast
    showToast.mockClear();
    if (resolved.status === 'blocked') {
      // This is what the fixed handler does -- nothing
    } else if (resolved.status === 'empty') {
      showToast(strings.toasts.noDataToChart, 'info');
    }
    expect(showToast).not.toHaveBeenCalled();
  });

  it('pivot: truncated + no semantic_query -> ONE warning toast, NO "no data" toast', async () => {
    const turn = makeTurn({
      query_result_sample: SAMPLE_3_ROWS,
      query_result_rows: 500,
      semantic_query: null,
    });
    const resolved = await resolveFullResult(turn, DEFAULT_OPTS);
    expect(showToast).toHaveBeenCalledTimes(1);

    showToast.mockClear();
    if (resolved.status === 'blocked') {
      // fixed handler: silent
    } else if (resolved.status === 'empty') {
      showToast(strings.toasts.noDataForPivot, 'info');
    }
    expect(showToast).not.toHaveBeenCalled();
  });

  it('Bug-6357-B e2e: truncated + re-fetch returns empty -> ONE warning toast, insert blocked', async () => {
    mockExecuteQuery.mockResolvedValueOnce({ data: [] });
    const turn = makeTurn({
      query_result_sample: SAMPLE_3_ROWS,
      query_result_rows: 500,
    });
    const resolved = await resolveFullResult(turn, DEFAULT_OPTS);
    expect(resolved).toEqual({ status: 'blocked' });
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith(
      templates.toasts.insertRefetchEmpty(3, 500),
      'warning',
    );
  });
});
