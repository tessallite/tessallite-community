/**
 * Cross-layer contract tests for the Excel add-in's three broken wire
 * contracts (B11 — F-025-01/02/03).
 *
 * These tests drive the REAL production API-client functions (executeQuery,
 * evaluateKpiBatch) against a mocked `fetch`, then assert the exact request
 * body the add-in puts on the wire and how it unwraps the response. The
 * response fixtures are GENERATED FROM THE REAL ENDPOINT SCHEMAS captured live
 * against the running stack on 2026-06-12 (not hand-rolled shapes):
 *   - /plugin/execute  -> { query, data, annotation } with the canonical
 *     filter-operator contract (src/api/filter_contract.py).
 *   - /kpis/evaluate-batch -> KPIBatchResponse { results: [...], evaluation_ms }
 *     where each result is the model-service KPIEvaluateResponse
 *     (governance_advanced.py), including the populated legacy goal aliases.
 *
 * The point of this suite is exactly what Bug-624 / the Fable review said was
 * missing: a test that the add-in's ACTUAL payloads match what the live
 * services accept, so the client and server can never silently drift again.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import type { MockInstance } from 'vitest';

const mockStorage: Record<string, string> = {};

beforeEach(() => {
  Object.keys(mockStorage).forEach(k => delete mockStorage[k]);
  mockStorage['tessallite_jwt'] = 'test-jwt';
  vi.restoreAllMocks();
  (globalThis as Record<string, unknown>).OfficeRuntime = {
    storage: {
      getItem: async (key: string) => mockStorage[key] ?? null,
      setItem: async (key: string, value: string) => { mockStorage[key] = value; },
      removeItem: async (key: string) => { delete mockStorage[key]; },
    },
  };
});

import { configureApiClient } from '../api/client';
import { executeQuery, getDrillOptions, drillThrough } from '../api/queryRouter';
import { evaluateKpiBatch, getFieldCompatibility, getHierarchyDetail } from '../api/modelService';
import { sendFeedback } from '../api/agentService';
import type { SemanticQuery } from '../types/tessallite';

beforeEach(() => {
  configureApiClient('https://test.example.com');
});

type FetchMock = MockInstance<Parameters<typeof fetch>, ReturnType<typeof fetch>>;

/** Capture the JSON body posted by the function under test. */
function mockPost(responseBody: unknown, status = 200): FetchMock {
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
    new Response(JSON.stringify(responseBody), { status }),
  );
  return fetchMock;
}

/**
 * Mock a 204 No Content response. jsdom's Response constructor rejects the
 * null-body status 204 directly, so we synthesise a minimal response object
 * that the api-client reads exactly like a real 204 (status 204, ok=true,
 * empty body via text()/json()).
 */
function mockNoContent(): FetchMock {
  const res = {
    status: 204,
    ok: true,
    statusText: 'No Content',
    text: async () => '',
    json: async () => { throw new Error('Unexpected end of JSON input'); },
  } as unknown as Response;
  return vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(res);
}

function lastBody(fetchMock: FetchMock): Record<string, unknown> {
  const call = fetchMock.mock.calls[fetchMock.mock.calls.length - 1];
  const init = call[1] as RequestInit;
  return JSON.parse(init.body as string);
}

// Real /plugin/execute success envelope (shape captured live).
const PLUGIN_EXECUTE_OK = {
  query: { measures: ['base_amount'], dimensions: [], limit: 50000, offset: 0 },
  data: [{ base_amount: '35730831.34' }],
  annotation: { measures: {}, dimensions: {}, timeDimensions: {} },
};

describe('F-025-01 — Report Builder filter contract (client payload)', () => {
  // Each row is exactly what the ZoneMappingGrid operator dropdown can emit
  // (values-array only — the add-in never sends a scalar `value`). The
  // canonical contract (filter_contract.py) accepts all of these verbatim.
  const OPERATORS = ['equals', 'notEquals', 'contains', 'notContains', 'gt', 'lt', 'inDateRange', 'set'];

  it.each(OPERATORS)('emits {dimension, operator, values} for %s', async (operator) => {
    const fetchMock = mockPost(PLUGIN_EXECUTE_OK);
    const query: SemanticQuery = {
      measures: ['base_amount'],
      dimensions: [],
      filters: [{ member: 'account_type', operator, values: ['CREDIT'] }],
      limit: 1000,
    };
    await executeQuery(query, { projectId: 'p1', modelId: 'm1' });

    const body = lastBody(fetchMock);
    expect(body.project_id).toBe('p1');
    expect(body.model_id).toBe('m1');
    const filters = body.filters as Array<Record<string, unknown>>;
    // The server's canonical contract reads `dimension` (not `member`) and
    // normalizes the Cube-style operator server-side; the client must send the
    // raw operator + a values array, never a `member` key.
    expect(filters[0]).toEqual({
      dimension: 'account_type',
      operator,
      values: ['CREDIT'],
    });
    expect(filters[0]).not.toHaveProperty('member');
  });

  it('surfaces the 422 contract detail as an ApiError message', async () => {
    // An unknown operator is rejected by the contract with the accepted list.
    // (notContains is now ACCEPTED — Bug-3609 added NOT LIKE rewriter support.)
    mockPost(
      { detail: "Unsupported filter operator: 'regex'. Accepted operators: between, contains, eq, ..." },
      422,
    );
    const query: SemanticQuery = {
      measures: ['base_amount'],
      filters: [{ member: 'account_type', operator: 'regex', values: ['x'] }],
      dimensions: [],
    };
    await expect(executeQuery(query, { projectId: 'p1', modelId: 'm1' }))
      .rejects.toThrowError(/Unsupported filter operator/);
  });

  it('threads persona_id when supplied', async () => {
    const fetchMock = mockPost(PLUGIN_EXECUTE_OK);
    await executeQuery(
      { measures: ['base_amount'], dimensions: [] },
      { projectId: 'p1', modelId: 'm1', personaId: 'persona-9' },
    );
    expect(lastBody(fetchMock).persona_id).toBe('persona-9');
  });
});

describe('F-025-03 — KPI batch-evaluate request and response shapes', () => {
  // Real KPIBatchResponse envelope (model-service governance_advanced.py),
  // values captured live from acme-demo/modely on 2026-06-12.
  const BATCH_RESPONSE = {
    results: [
      {
        kpi_id: 'c50cbb0c-a620-4443-af34-cd92fde1c5c9',
        value: 2753735.21, value_str: '2,753,735.21',
        target: 10000.0, status: 1, status_label: 'On Track',
        status_color: '#388E3C', trend: null, trend_label: 'Insufficient Data',
        trend_pct: null, formatted_value: '2,753,735.21',
        formatted_target: '10,000.00', formatted_variance: null,
        trend_series: null, evaluation_ms: 12,
        // Backend's populated legacy aliases the KPI tab reads:
        goal: 10000.0, formatted_goal: '10,000.00',
      },
    ],
    evaluation_ms: 2307,
  };

  it('posts {kpi_ids:[...]} (not a body-less POST that 422s)', async () => {
    const fetchMock = mockPost(BATCH_RESPONSE);
    await evaluateKpiBatch('p1', 'm1', ['k1', 'k2']);
    const call = fetchMock.mock.calls[0];
    expect(call[0]).toContain('/kpis/evaluate-batch');
    expect(lastBody(fetchMock)).toEqual({ kpi_ids: ['k1', 'k2'] });
  });

  it('threads persona_id as a query param', async () => {
    const fetchMock = mockPost(BATCH_RESPONSE);
    await evaluateKpiBatch('p1', 'm1', ['k1'], 'persona-7');
    expect(fetchMock.mock.calls[0][0]).toContain('persona_id=persona-7');
  });

  it('unwraps the .results envelope (not a bare array)', async () => {
    mockPost(BATCH_RESPONSE);
    const results = await evaluateKpiBatch('p1', 'm1', ['k1']);
    expect(Array.isArray(results)).toBe(true);
    expect(results).toHaveLength(1);
    const r = results[0];
    expect(r.kpi_id).toBe('c50cbb0c-a620-4443-af34-cd92fde1c5c9');
    expect(r.value).toBe(2753735.21);
    expect(r.formatted_value).toBe('2,753,735.21');
    // goal/formatted_goal are the backend's populated legacy aliases.
    expect(r.goal).toBe(10000.0);
    expect(r.formatted_goal).toBe('10,000.00');
    expect(r.status).toBe(1);
    expect(r.status_label).toBe('On Track');
  });

  it('short-circuits to [] without a network call when no ids', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch');
    const results = await evaluateKpiBatch('p1', 'm1', []);
    expect(results).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('F-025-04 — chat feedback hits the real turn-feedback route', () => {
  // Real route (agent-service conversations.py:746):
  //   POST .../conversations/{id}/turns/{turn_id}/feedback
  //   body FeedbackBody{ vote: "up"|"down", comment?: string }
  // The previous client posted .../messages/feedback {message_id, rating},
  // a route that does not exist (404 on every vote).
  it('POSTs to /turns/{turnId}/feedback with {vote}', async () => {
    const fetchMock = mockNoContent();
    await sendFeedback('p1', 'conv-1', 'turn-42', 'up');
    const call = fetchMock.mock.calls[0];
    const url = call[0] as string;
    expect(url).toContain('/projects/p1/agent/conversations/conv-1/turns/turn-42/feedback');
    // Must NOT use the dead messages/feedback route.
    expect(url).not.toContain('/messages/feedback');
    // Body matches FeedbackBody — `vote`, not `rating`/`message_id`.
    expect(lastBody(fetchMock)).toEqual({ vote: 'up' });
  });

  it('sends vote=down verbatim (matches the ^(up|down)$ pattern)', async () => {
    const fetchMock = mockNoContent();
    await sendFeedback('p1', 'conv-1', 'turn-9', 'down');
    expect(lastBody(fetchMock).vote).toBe('down');
    expect(lastBody(fetchMock)).not.toHaveProperty('message_id');
    expect(lastBody(fetchMock)).not.toHaveProperty('rating');
  });
});

describe('F-025-06 — drill-through request carries id + coordinates', () => {
  // Real DrillThroughRequest (query-router drill_routes.py:54):
  //   { filters: [{column, op, value}], grouping_levels: [{column, op, value}],
  //     cursor?, limit?, persona_id?, hierarchy_id? }
  // Unknown flat keys (measure_name, project_id, ...) are dropped by Pydantic,
  // so the cell coordinates MUST ride in grouping_levels — not flat keys.
  const DRILL_OPTIONS_OK = {
    hierarchies: [
      { hierarchy_id: 'h1', hierarchy_name: 'Geography', current_level_name: 'Country', next_level_name: 'City' },
    ],
  };
  const DRILL_THROUGH_OK = {
    columns: ['country_code', 'base_amount'],
    rows: [{ country_code: 'GB', base_amount: 12749 }],
    page: { cursor: 'c0', next_cursor: null, has_more: false },
    drill_mode: 'leaf',
    route_type: 'source',
  };

  it('drill-options path param is the measure UUID (not a name)', async () => {
    const measureUuid = 'c50cbb0c-a620-4443-af34-cd92fde1c5c9';
    const fetchMock = mockPost(DRILL_OPTIONS_OK);
    await getDrillOptions(measureUuid, {
      grouping_levels: [{ column: 'country_code', op: 'eq', value: 'GB' }],
      filters: [],
      persona_id: 'persona-2',
    });
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain(`/measures/${measureUuid}/drill-options`);
    // The coordinates ride in grouping_levels, in the {column,op,value} shape.
    const body = lastBody(fetchMock);
    expect(body.grouping_levels).toEqual([{ column: 'country_code', op: 'eq', value: 'GB' }]);
    expect(body.persona_id).toBe('persona-2');
  });

  it('drill-through sends grouping_levels + hierarchy_id + cursor', async () => {
    const measureUuid = 'c50cbb0c-a620-4443-af34-cd92fde1c5c9';
    const fetchMock = mockPost(DRILL_THROUGH_OK);
    await drillThrough(
      measureUuid,
      {
        grouping_levels: [{ column: 'country_code', op: 'eq', value: 'GB' }],
        filters: [],
        persona_id: 'persona-2',
        hierarchy_id: 'h1',
      },
      'cursor-abc',
    );
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain(`/measures/${measureUuid}/drill-through`);
    const body = lastBody(fetchMock);
    expect(body.grouping_levels).toEqual([{ column: 'country_code', op: 'eq', value: 'GB' }]);
    expect(body.hierarchy_id).toBe('h1');
    expect(body.cursor).toBe('cursor-abc');
    // Coordinates are real cell keys, never measure_name / flat keys that drop.
    expect(body).not.toHaveProperty('measure_name');
    expect(body).not.toHaveProperty('measure_id');
  });
});

// F-025-11 — hierarchy detail level -> bindable dimension mapping.
// Fixture shape captured live from the hierarchy detail endpoint
// (acme-demo/modely "Calendar"): each level carries a key_attribute whose
// `name` is the technical dimension the level maps to. The summary LIST
// endpoint omits this, which is why the level zone token was unbindable.
const HIERARCHY_DETAIL_OK = {
  id: 'a2bc5bd7-8072-4891-b8c5-11cbb34280d2',
  name: 'Calendar',
  levels: [
    { name: 'Year', ordinal: 0, time_unit: 'year', key_attribute: { name: 'business_date_year' } },
    { name: 'Month', ordinal: 1, time_unit: 'month', key_attribute: { name: 'business_date_month' } },
    { name: 'Day', ordinal: 2, time_unit: 'day', key_attribute: { name: 'business_date' } },
  ],
};

describe('F-025-11 — getHierarchyDetail maps levels to bindable dimensions', () => {
  it('maps key_attribute.name onto HierarchyLevel.dimensionName', async () => {
    const fetchMock = mockPost(HIERARCHY_DETAIL_OK);
    const levels = await getHierarchyDetail('p1', 'm1', 'h1', 'persona-1');
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain('/projects/p1/models/m1/hierarchies/h1');
    expect(url).toContain('persona_id=persona-1');
    expect(levels).toEqual([
      { name: 'Year', level_number: 0, time_unit: 'year', dimensionName: 'business_date_year' },
      { name: 'Month', level_number: 1, time_unit: 'month', dimensionName: 'business_date_month' },
      { name: 'Day', level_number: 2, time_unit: 'day', dimensionName: 'business_date' },
    ]);
  });

  it('tolerates a level without a key attribute (dimensionName undefined)', async () => {
    mockPost({ levels: [{ name: 'X', ordinal: 0 }] });
    const levels = await getHierarchyDetail('p1', 'm1', 'h1');
    expect(levels[0].dimensionName).toBeUndefined();
    expect(levels[0].level_number).toBe(0);
  });
});

describe('Phase 5 — field compatibility API wrapper', () => {
  it('calls the model-service compatibility endpoint with persona and compact sorted ids', async () => {
    const fetchMock = mockPost({
      model_id: 'm1',
      version_id: 'v1',
      generated_at: '2026-06-14T00:00:00Z',
      status: 'compatible',
      measures: {},
      multi_measure: null,
    });

    await getFieldCompatibility('p1', 'm1', {
      personaId: 'persona-1',
      measureIds: ['measure-b', 'measure-a', 'measure-a'],
      dimensionIds: ['dim-b', 'dim-a', 'dim-b'],
    });

    const url = new URL(fetchMock.mock.calls[0][0] as string);
    expect(url.pathname).toBe('/api/v1/projects/p1/models/m1/field-compatibility');
    expect(url.searchParams.get('persona_id')).toBe('persona-1');
    expect(url.searchParams.getAll('measure_ids')).toEqual(['measure-a', 'measure-b']);
    expect(url.searchParams.getAll('dimension_ids')).toEqual(['dim-a', 'dim-b']);
  });
});
