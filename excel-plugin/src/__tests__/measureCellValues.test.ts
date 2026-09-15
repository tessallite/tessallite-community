/**
 * What a TESSALLITE.* formula actually puts in a cell.
 *
 * Bug-9876 and Bug-9910 were both invisible to the existing suite because it
 * asserted on the JSON, on the helper, or on the fact that something threw —
 * never on the value or the text a CELL ends up holding. These drive the
 * registered custom functions with real `/api/v1/plugin/execute` response
 * bodies, captured from the local stack, and assert on what comes back.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

import liveBodies from './fixtures/plugin-execute-live.json';

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

const { clearFunctionCaches } = await import('../functions');
const { normaliseMeasureRows } = await import('../utils/measureValues');
const { pivotZoneResult } = await import('../utils/zoneQuery');

function setAuth() {
  mockGetJwt.mockResolvedValue('jwt-token');
  mockGetActiveProfile.mockResolvedValue({ id: 'p1', serverUrl: 'https://tessallite.test' });
  mockGetModelContext.mockResolvedValue({
    projectId: 'proj-1', modelId: 'model-1', modelSlug: 'modely', modelName: 'modely',
  });
  mockGetActivePersonaId.mockResolvedValue(null);
}

function respondOk(body: unknown) {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: true, status: 200, json: () => Promise.resolve(body),
  } as Response);
}

function respond422(detail: string) {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: false, status: 422, json: () => Promise.resolve({ detail }),
  } as Response);
}

beforeEach(() => {
  vi.restoreAllMocks();
  [mockGetJwt, mockGetActiveProfile, mockGetModelContext, mockGetActivePersonaId]
    .forEach(m => m.mockReset());
  mockGetActivePersonaId.mockResolvedValue(null);
  setAuth();
  clearFunctionCaches();
});

// ---------------------------------------------------------------------------
// Bug-9876 / Bug-9910 — the cell holds a NUMBER
// ---------------------------------------------------------------------------

describe('a measure value reaches the cell as a number (Bug-9876)', () => {
  it('for the typed response the query-router now sends', async () => {
    // The producer types its own measure columns
    // (query-router src/api/measure_values.py), so the wire carries numbers.
    respondOk({
      data: [{ base_amount: 180442041.28 }],
      annotation: { measures: { base_amount: { title: 'base amount', type: 'sum' } } },
    });
    const result = await registered['VALUE']('modely', 'base_amount');
    expect(typeof result).toBe('number');
    expect(result).toBe(180442041.28);
  });

  it('for a count measure the source returns as an exact integer', async () => {
    respondOk({ data: [{ transaction_count: 100000 }] });
    const result = await registered['VALUE']('modely', 'transaction_count');
    expect(typeof result).toBe('number');
    expect(result).toBe(100000);
  });

  it('for the legacy string wire an older server still sends', async () => {
    // Compatibility shim: an Office host can hold a cached bundle while the
    // task pane points at a query-router that predates the boundary fix.
    respondOk({ data: [{ base_amount: '180442041.28' }] });
    const result = await registered['VALUE']('modely', 'base_amount');
    expect(typeof result).toBe('number');
    expect(result).toBe(180442041.28);
  });

  it('for the legacy scientific-notation string a count arrived as (Bug-9910)', async () => {
    // `str(Decimal('1.0E+5'))` is `"1.0E+5"`. That is the exact wire value
    // `transaction_count` carried on `modely` while `base_amount` carried
    // plain decimal text — the only difference between the two measures at
    // this boundary, and the reason one worked in Excel and one did not.
    respondOk({ data: [{ transaction_count: '1.0E+5' }] });
    const result = await registered['VALUE']('modely', 'transaction_count');
    expect(typeof result).toBe('number');
    expect(result).toBe(100000);
  });

  it('does not renumber a measure value that is genuinely text', async () => {
    // Now that the producer types its own columns, a STRING in a measure
    // column means the value really is text. Parsing it anyway is how a
    // client-side "fix" becomes the next wrong number: a cost centre or
    // product code of '0042' must not land in the cell as 42.
    respondOk({ data: [{ cost_centre_code: '0042' }] });
    const result = await registered['VALUE']('modely', 'cost_centre_code');
    expect(result).toBe('0042');
  });

  it('through MEMBERVALUE as well as VALUE', async () => {
    respondOk({ data: [{ card_entry_mode_name: 'Chip', base_amount: '1.0E+5' }] });
    const result = await registered['MEMBERVALUE'](
      'modely', 'base_amount', 'card_entry_mode_name', 'Chip',
    );
    expect(typeof result).toBe('number');
    expect(result).toBe(100000);
  });
});

// ---------------------------------------------------------------------------
// Bug-9876 — the PivotTable consequence
// ---------------------------------------------------------------------------

describe('the local PivotTable backing table holds numbers (Bug-9876)', () => {
  // Excel re-aggregates a local PivotTable's source column itself. It SUMS a
  // numeric column and COUNTS a text one, silently, so a numeric string in the
  // backing table turns a revenue total into a row count with no error
  // anywhere. This is the shape the Report Builder writes.
  const wire = [
    { country_code: 'GB', base_amount: '67969062.99', transaction_count: '1.0E+5' },
    { country_code: '007', base_amount: '1.5', transaction_count: '2803' },
  ];
  const measureKeys = ['base_amount', 'transaction_count'];

  it('every measure cell is a number, not one sampled cell', () => {
    const { rows } = pivotZoneResult(
      normaliseMeasureRows(wire, measureKeys), ['country_code'], [], measureKeys, {},
    );
    expect(rows).toHaveLength(2);
    const textCells: string[] = [];
    for (const row of rows) {
      // Column 0 is the row dimension; every column after it is a measure.
      for (let c = 1; c < row.length; c++) {
        if (typeof row[c] !== 'number') textCells.push(`row ${row[0]} col ${c}: ${JSON.stringify(row[c])}`);
      }
    }
    expect(textCells).toEqual([]);
  });

  it('leaves a dimension whose members look numeric as text', () => {
    // '007' is a country code, not the number seven. Renumbering it would
    // merge members and mislabel the pivot's row axis.
    const { rows } = pivotZoneResult(
      normaliseMeasureRows(wire, measureKeys), ['country_code'], [], measureKeys, {},
    );
    expect(rows.map(r => r[0])).toEqual(['GB', '007']);
    expect(typeof rows[1][0]).toBe('string');
  });
});

// ---------------------------------------------------------------------------
// Bug-9910 — a refused measure says which measure, and why
// ---------------------------------------------------------------------------

describe('a refused measure explains itself in the cell (Bug-9910)', () => {
  it('surfaces the router\'s reason instead of the generic text', async () => {
    // The live 422 body when a model does not expose the requested measure.
    respond422("Unknown column: 'transaction_count' in model 'modely'");
    const result = String(await registered['VALUE']('modely', 'transaction_count'));
    expect(result).toContain('transaction_count');
    expect(result).toContain('modely');
    expect(result).not.toContain('An error occurred. Check the Tessallite panel');
  });

  it('still withholds a 422 detail this client cannot vouch for', async () => {
    // A binder message about physical columns, join graphs or owner tables is
    // not the add-in's to show; it names schema the caller never supplied.
    respond422("Cannot resolve measure 'x' to a physical column in table public.fact_9c1");
    const result = String(await registered['VALUE']('modely', 'x'));
    expect(result).not.toContain('public.fact_9c1');
    expect(result).toContain('An error occurred. Check the Tessallite panel');
  });

  it('surfaces the date-variant reason (Bug-9759 regression)', async () => {
    respond422('Period-aware time variant requires a time dimension in the query grain; none was found.');
    const result = String(await registered['VALUE']('modely', 'base_amount_ytd'));
    expect(result).toContain('Period-aware time variant requires a time dimension');
  });
});

// ---------------------------------------------------------------------------
// Bug-9749 — a stalled request settles the cell with a readable reason
// ---------------------------------------------------------------------------

describe('a stalled request settles the cell (Bug-9749)', () => {
  it('does not leave the promise unsettled, and says the server never answered', async () => {
    vi.useFakeTimers();
    try {
      // A fetch that never settles: the documented AppContainer shape, where
      // the loopback exemption is missing and zero requests reach the server.
      vi.spyOn(globalThis, 'fetch').mockReturnValue(new Promise(() => {}) as Promise<Response>);
      const pending = registered['VALUE']('modely', 'base_amount');
      let settled = false;
      pending.then(() => { settled = true; }, () => { settled = true; });

      await vi.advanceTimersByTimeAsync(29_000);
      expect(settled).toBe(false);          // still #GETTING_DATA, correctly
      await vi.advanceTimersByTimeAsync(2_000);

      const result = String(await pending);
      expect(result).toContain('Request timed out');
      expect(result).toContain('did not respond');
      expect(result).not.toContain('An error occurred. Check the Tessallite panel');
    } finally {
      vi.useRealTimers();
    }
  });
});

// ---------------------------------------------------------------------------
// Bug-9880 — a row-security denial is a governed outcome and says so
// ---------------------------------------------------------------------------

describe('a row-security deny-all is explained in the cell (Bug-9880)', () => {
  it('through VALUE and MEMBERVALUE alike', async () => {
    for (const call of [
      () => registered['VALUE']('modely', 'transaction_count'),
      () => registered['MEMBERVALUE']('modely', 'transaction_count', 'card_entry_mode_name', 'Chip'),
    ]) {
      clearFunctionCaches();
      // A real deny-all: HTTP 200, a COUNT-shaped row carrying 0, the sentinel.
      respondOk({
        data: [{ card_entry_mode_name: 'Chip', transaction_count: 0 }],
        security_rules_applied: ['__deny_all__'],
      });
      const result = await call();
      expect(result).not.toBe(0);
      const text = String(result);
      expect(text).toContain('Row-level security');
      expect(text).toContain('not a value of zero');
      expect(text).not.toContain('An error occurred. Check the Tessallite panel');
    }
  });
});

// ---------------------------------------------------------------------------
// The same assertions against LIVE captured bodies
// ---------------------------------------------------------------------------

/**
 * `fixtures/plugin-execute-live.json` holds real `/api/v1/plugin/execute`
 * responses captured from the local docker stack against model `modely` — one
 * set as the tenant admin, one as a principal whose row security denies every
 * row. Hand-written fixtures are what let Bug-9876, Bug-9882 and Bug-9910 all
 * ship: each test asserted against a shape the server does not produce.
 *
 * The `route` block is stripped from the fixture: it carries the persona-
 * rewritten physical SQL, which is disclosure-gated and has no business in a
 * checked-in file.
 */
describe('live /plugin/execute bodies reach the cell correctly', () => {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const live = liveBodies as Record<string, { status: number; body: any }>;

  function respondLive(key: string) {
    const captured = live[key];
    expect(captured, `fixture has no case ${key}`).toBeTruthy();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: captured.status < 400,
      status: captured.status,
      json: () => Promise.resolve(captured.body),
    } as Response);
    return captured;
  }

  it('a sum and a count measure both arrive as numbers on the wire', () => {
    const { body } = live.admin_sum_and_count;
    expect(typeof body.data[0].base_amount).toBe('number');
    expect(typeof body.data[0].transaction_count).toBe('number');
    expect(body.data[0].transaction_count).toBe(100000);
  });

  it('a sum measure lands in the cell as a number', async () => {
    respondLive('admin_sum_and_count');
    const result = await registered['VALUE']('modely', 'base_amount');
    expect(typeof result).toBe('number');
    expect(result).toBe(live.admin_sum_and_count.body.data[0].base_amount);
  });

  it('a count measure lands in the cell as a number', async () => {
    respondLive('admin_sum_and_count');
    const result = await registered['VALUE']('modely', 'transaction_count');
    expect(typeof result).toBe('number');
    expect(result).toBe(100000);
  });

  it('a count_distinct measure lands in the cell as a number', async () => {
    respondLive('admin_count_distinct');
    const result = await registered['VALUE']('modely', 'unique_customers');
    expect(typeof result).toBe('number');
    expect(result).toBe(99993);
  });

  it('a time-variant measure lands in the cell as a number', async () => {
    respondLive('admin_variant_with_time_grain');
    const rows = live.admin_variant_with_time_grain.body.data as Record<string, unknown>[];
    const keys = Object.keys(live.admin_variant_with_time_grain.body.annotation.measures);
    const parsed = normaliseMeasureRows(rows, keys);
    expect(typeof parsed[0].base_amount_ytd).toBe('number');
    // The year on the axis is a member label, not a measure: it stays text.
    expect(typeof parsed[0].business_date_calendar_year).toBe('string');
  });

  it('a deny-all explains itself instead of writing the fabricated 0', async () => {
    // The live body: HTTP 200, `{"unique_customers": 0}`, and the
    // `__deny_all__` sentinel. A COUNT-shaped measure really does return 0
    // under a `WHERE 0 = 1` rewrite, which is the wrong number a user would
    // format, chart and forward.
    const { body } = respondLive('deny_all_count_distinct');
    expect(body.data[0].unique_customers).toBe(0);
    expect(body.security_rules_applied).toContain('__deny_all__');

    const result = await registered['VALUE']('modely', 'unique_customers');
    expect(result).not.toBe(0);
    expect(String(result)).toContain('Row-level security');
    expect(String(result)).toContain('not a value of zero');
    expect(String(result)).not.toContain('An error occurred. Check the Tessallite panel');
  });

  it('a deny-all on a sum measure explains itself too, not #N/A', async () => {
    // A SUM under `WHERE 0 = 1` returns NULL, which would otherwise fan out to
    // a bare #N/A — indistinguishable from "this slice happens to be empty".
    const { body } = respondLive('deny_all_sum');
    expect(body.data[0].base_amount).toBeNull();
    const result = await registered['VALUE']('modely', 'base_amount');
    expect(String(result)).toContain('Row-level security');
  });

  it('a refused measure names the measure and the model in the cell', async () => {
    const { body } = respondLive('refused_measure_422');
    expect(body.detail).toContain('Unknown column');
    const result = String(await registered['VALUE']('modely', 'not_a_measure'));
    expect(result).toContain('not_a_measure');
    expect(result).toContain('modely');
    expect(result).not.toContain('An error occurred. Check the Tessallite panel');
  });

  it('a time-variant measure called with no date context says why', async () => {
    respondLive('variant_without_time_grain_422');
    const result = String(await registered['VALUE']('modely', 'base_amount_ytd'));
    expect(result).toContain('Period-aware time variant requires a time dimension');
  });
});
