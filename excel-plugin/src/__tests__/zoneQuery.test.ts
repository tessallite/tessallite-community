/**
 * F-025-11 — named sets and hierarchy levels must produce BINDABLE queries.
 *
 * These payloads are checked against the shapes the query-router /plugin/execute
 * endpoint actually accepts (verified live on acme-demo/modely):
 *   - a hierarchy-level dimension `business_date_month` binds -> 200
 *   - an `in` filter over member keys (CREDIT, WALLET) binds -> 200
 * The previous code sent the named-set UUID / `uuid:level` token as a dimension
 * name, which the binder rejected with a 422.
 */
import { describe, it, expect } from 'vitest';
import { buildZoneQuery, buildLocalPivotFieldMapping, buildLocalPivotQuery, isMeasureSafeForLocalPivot, resolveZoneItemName, resolveNamedSetDimension, evaluateNamedSetZoneGate, resolveZoneAxes, pivotZoneResult, planKpiZoneAdd, planKpiZoneRemove, unsafeLocalPivotMeasures } from '../utils/zoneQuery';
import type { ZoneItem } from '../components/ReportBuilder/ZoneMappingGrid';
import type { Measure, Dimension, NamedSet, NamedSetPreviewResponse, Kpi } from '../types/tessallite';

const measures: Measure[] = [
  { id: 'm1', name: 'fee_amount', display_name: 'fee amount', default_agg: 'sum', measure_type: 'standard' },
];
const dimensions: Dimension[] = [
  { id: 'd1', name: 'account_type', display_name: 'account type', data_type: 'character varying', source_type: 'dim' },
  { id: 'd2', name: 'business_date_month', display_name: 'Month', data_type: 'integer', source_type: 'dim' },
];
const lists = { measures, dimensions };

describe('resolveZoneItemName', () => {
  it('resolves a plain measure/dimension UUID to its technical name', () => {
    expect(resolveZoneItemName({ id: 'm1', name: 'fee amount', zone: 'values' }, lists)).toBe('fee_amount');
    expect(resolveZoneItemName({ id: 'd1', name: 'account type', zone: 'rows' }, lists)).toBe('account_type');
  });

  it('prefers bindDimension for resolved named-set / hierarchy-level items', () => {
    const item: ZoneItem = { id: 'ns-uuid', name: 'Top Accounts', zone: 'rows', kind: 'named_set', bindDimension: 'account_type', memberKeys: ['CREDIT'] };
    expect(resolveZoneItemName(item, lists)).toBe('account_type');
  });
});

describe('resolveNamedSetDimension (Bug-6904 / Bug-1112)', () => {
  const base = { builder_definition: null, dimensions: null, expression: '' } as Pick<NamedSet, 'builder_definition' | 'dimensions' | 'expression'>;

  it('prefers the builder definition entity', () => {
    expect(resolveNamedSetDimension({ ...base, builder_definition: { entity: 'account_type' } })).toBe('account_type');
  });

  it('trims whitespace on the builder entity', () => {
    expect(resolveNamedSetDimension({ ...base, builder_definition: { entity: '  region  ' } })).toBe('region');
  });

  it('resolves an unambiguous single-value dimensions field', () => {
    expect(resolveNamedSetDimension({ ...base, dimensions: 'business_date_month' })).toBe('business_date_month');
  });

  it('does not resolve an ambiguous multi-value dimensions field', () => {
    expect(resolveNamedSetDimension({ ...base, dimensions: 'region,country' })).toBeNull();
  });

  it('Bug-6904: falls back to the MDX [dim].[dim].Members pattern (TopCount/Filter demo sets)', () => {
    // The demo seed's expression-only named sets carry null entity + null
    // dimensions — resolution MUST come from the expression, or the set is
    // undroppable (the exact Bug-6904 failure).
    expect(
      resolveNamedSetDimension({
        ...base,
        expression: 'TopCount([Region].[Region].Members, 5, [Measures].[fee_amount])',
      }),
    ).toBe('Region');
    expect(
      resolveNamedSetDimension({
        ...base,
        expression: 'Filter([Account Type].[Account Type].Members, [Measures].[fee_amount] > 0)',
      }),
    ).toBe('Account Type');
  });

  it('does not mis-read an unrelated [a].[b].Members as dimension "a"', () => {
    // The \1 back-reference pins level == dimension, so a cross-level member
    // reference is not accepted as the bound dimension.
    expect(resolveNamedSetDimension({ ...base, expression: '[Geography].[City].Members' })).toBeNull();
  });

  it('returns null when nothing resolves (steer to Insert as formulas)', () => {
    expect(resolveNamedSetDimension(base)).toBeNull();
    expect(resolveNamedSetDimension({ ...base, expression: 'SomeOpaqueMdx()' })).toBeNull();
  });
});

describe('buildZoneQuery', () => {
  it('returns null when no measure is selected', () => {
    const items: ZoneItem[] = [{ id: 'd1', name: 'account type', zone: 'rows' }];
    expect(buildZoneQuery(items, lists)).toBeNull();
  });

  it('binds a hierarchy level via its underlying dimension, not the uuid:level token', () => {
    const items: ZoneItem[] = [
      { id: 'm1', name: 'fee amount', zone: 'values' },
      // hierarchy level dropped on rows — id is the uuid:level token, but it
      // resolves to the bindable dimension name.
      { id: 'hier-uuid:1', name: 'Calendar: Month', zone: 'rows', kind: 'hierarchy_level', bindDimension: 'business_date_month' },
    ];
    const q = buildZoneQuery(items, lists);
    expect(q).not.toBeNull();
    expect(q!.measures).toEqual(['fee_amount']);
    expect(q!.dimensions).toEqual(['business_date_month']);
    // crucially: no UUID / `uuid:level` token leaks into the dimensions list
    expect(q!.dimensions?.some(d => d.includes('hier-uuid'))).toBe(false);
  });

  it('translates a named set on rows into its dimension + an `in` filter over member keys', () => {
    const items: ZoneItem[] = [
      { id: 'm1', name: 'fee amount', zone: 'values' },
      { id: 'ns-uuid', name: 'Card Accounts', zone: 'rows', kind: 'named_set', bindDimension: 'account_type', memberKeys: ['CREDIT', 'WALLET'] },
    ];
    const q = buildZoneQuery(items, lists);
    expect(q!.dimensions).toEqual(['account_type']);
    expect(q!.filters).toEqual([
      { dimension: 'account_type', operator: 'in', values: ['CREDIT', 'WALLET'] },
    ]);
    expect(q!.dimensions?.some(d => d.includes('ns-uuid'))).toBe(false);
  });

  it('translates a named set on the filters zone into an `in` filter only (no axis)', () => {
    const items: ZoneItem[] = [
      { id: 'm1', name: 'fee amount', zone: 'values' },
      { id: 'ns-uuid', name: 'Card Accounts', zone: 'filters', kind: 'named_set', bindDimension: 'account_type', memberKeys: ['CREDIT'] },
    ];
    const q = buildZoneQuery(items, lists);
    expect(q!.dimensions).toEqual([]);
    expect(q!.filters).toEqual([
      { dimension: 'account_type', operator: 'in', values: ['CREDIT'] },
    ]);
  });

  it('omits an unconfigured filter chip (no operator / no values) from explicitFilters (Bug-5289)', () => {
    const items: ZoneItem[] = [
      { id: 'm1', name: 'fee amount', zone: 'values' },
      { id: 'd2', name: 'Month', zone: 'rows' },
      // This filter chip was dragged onto the Filters zone but never configured
      // (the user did not pick an operator or enter values). Previously it would
      // emit `operator: 'set'` with no values, which the query-router rejected.
      { id: 'd1', name: 'account type', zone: 'filters' },
    ];
    const q = buildZoneQuery(items, lists);
    expect(q).not.toBeNull();
    // The unconfigured chip must be completely absent from the filters array.
    expect(q!.filters).toBeUndefined();
  });

  it('keeps explicit dimension filters alongside named-set in-filters', () => {
    const items: ZoneItem[] = [
      { id: 'm1', name: 'fee amount', zone: 'values' },
      { id: 'd2', name: 'Month', zone: 'rows' },
      { id: 'd1', name: 'account type', zone: 'filters', operator: 'eq', values: ['CREDIT'] },
      { id: 'ns-uuid', name: 'Card Accounts', zone: 'filters', kind: 'named_set', bindDimension: 'business_date_month', memberKeys: ['1', '2'] },
    ];
    const q = buildZoneQuery(items, lists);
    expect(q!.filters).toEqual([
      { dimension: 'account_type', operator: 'eq', values: ['CREDIT'] },
      { dimension: 'business_date_month', operator: 'in', values: ['1', '2'] },
    ]);
  });
});

/**
 * Bug-1112 (H19 R2) — a zone drop must NOT silently bind a named set whose
 * preview was truncated (would under-count the aggregate) or whose membership
 * is dynamic (top-N / filtered; would be frozen at a point in time). The gate
 * blocks those drops so the caller can steer to "Insert as formulas" (CUBESET).
 */
describe('evaluateNamedSetZoneGate (Bug-1112)', () => {
  const fixedSet: Pick<NamedSet, 'builder_definition' | 'list_type'> = {
    builder_definition: { type: 'fixedMembers', dimension: 'account_type', members: ['CREDIT', 'WALLET'] },
    list_type: 'fixed',
  };
  const topNSet: Pick<NamedSet, 'builder_definition' | 'list_type'> = {
    builder_definition: { type: 'topN', entity: 'account_id', count: 200, measure: 'Revenue', direction: 'top' },
    list_type: 'dynamic_top_n',
  };
  const filterSet: Pick<NamedSet, 'builder_definition' | 'list_type'> = {
    builder_definition: { type: 'filter', entity: 'account_id', conditions: [{}] },
    list_type: 'filtered',
  };
  const fullPreview: Pick<NamedSetPreviewResponse, 'truncated'> = { truncated: false };
  const truncatedPreview: Pick<NamedSetPreviewResponse, 'truncated'> = { truncated: true };

  it('allows a fixed set whose full membership was previewed', () => {
    const gate = evaluateNamedSetZoneGate(fixedSet, fullPreview);
    expect(gate.safe).toBe(true);
    expect(gate.reason).toBe('ok');
    expect(gate.truncated).toBe(false);
    expect(gate.dynamic).toBe(false);
  });

  it('blocks a fixed set whose preview was TRUNCATED (would silently under-count)', () => {
    const gate = evaluateNamedSetZoneGate(fixedSet, truncatedPreview);
    expect(gate.safe).toBe(false);
    expect(gate.truncated).toBe(true);
    expect(gate.reason).toBe('truncated');
  });

  it('blocks a top-N (dynamic) set even when the preview was not truncated (would freeze it)', () => {
    const gate = evaluateNamedSetZoneGate(topNSet, fullPreview);
    expect(gate.safe).toBe(false);
    expect(gate.dynamic).toBe(true);
    expect(gate.reason).toBe('dynamic');
  });

  it('blocks a filtered (dynamic) set', () => {
    const gate = evaluateNamedSetZoneGate(filterSet, fullPreview);
    expect(gate.safe).toBe(false);
    expect(gate.dynamic).toBe(true);
  });

  it('reports both reasons when a dynamic set is also truncated', () => {
    const gate = evaluateNamedSetZoneGate(topNSet, truncatedPreview);
    expect(gate.safe).toBe(false);
    expect(gate.truncated).toBe(true);
    expect(gate.dynamic).toBe(true);
    expect(gate.reason).toBe('truncated_dynamic');
  });

  it('treats a missing/absent builder type as non-dynamic (only truncation gates it)', () => {
    const raw: Pick<NamedSet, 'builder_definition' | 'list_type'> = { builder_definition: null, list_type: 'advanced_mdx' };
    expect(evaluateNamedSetZoneGate(raw, fullPreview).safe).toBe(true);
    expect(evaluateNamedSetZoneGate(raw, truncatedPreview).safe).toBe(false);
  });

  it('the silent-truncation path is closed: a blocked set never reaches the in-filter bind', () => {
    // This is the regression guard for the original defect. Previously a >limit
    // set was previewed, truncated to the first N keys, and dropped straight
    // into a zone -> buildZoneQuery emitted an `in` filter over only N keys,
    // producing a silently under-counted aggregate. Now the gate blocks the
    // drop, so no truncated membership is ever handed to buildZoneQuery.
    const gate = evaluateNamedSetZoneGate(topNSet, truncatedPreview);
    expect(gate.safe).toBe(false);

    // Demonstrate the under-count that the gate prevents: had the truncated
    // keys been bound, the query would carry a partial `in` filter.
    const truncatedKeys = ['a1', 'a2', 'a3']; // stand-in for "first N of many"
    const items: ZoneItem[] = [
      { id: 'm1', name: 'fee amount', zone: 'values' },
      { id: 'ns-uuid', name: 'Top Accounts', zone: 'filters', kind: 'named_set', bindDimension: 'account_type', memberKeys: truncatedKeys },
    ];
    const q = buildZoneQuery(items, lists);
    // The under-counted shape (what we now refuse to produce via the zone path):
    expect(q!.filters).toEqual([{ dimension: 'account_type', operator: 'in', values: truncatedKeys }]);
    // Because the gate is unsafe, the ReportBuilder handler does not call
    // addResolvedToZone, so this partial-key item is never created in the first
    // place — the analyst is steered to "Insert as formulas" instead.
  });
});

// F-025-15: the Columns zone must produce a real cross-tab, not a flat table
// identical to Rows. resolveZoneAxes splits row vs column dims; pivotZoneResult
// reshapes the flat /plugin/execute rows into side-by-side column members.
describe('F-025-15 — Columns zone cross-tab pivot', () => {
  const lists = {
    measures: [
      { id: 'm1', name: 'revenue', display_name: 'Revenue', default_agg: 'sum', measure_type: 'standard' } as Measure,
    ],
    dimensions: [
      { id: 'd1', name: 'region', display_name: 'Region', data_type: 'text', source_type: 'dim' } as Dimension,
      { id: 'd2', name: 'quarter', display_name: 'Quarter', data_type: 'text', source_type: 'dim' } as Dimension,
    ],
  };

  it('resolveZoneAxes separates row and column dimension names', () => {
    const items: ZoneItem[] = [
      { id: 'm1', name: 'Revenue', zone: 'values' },
      { id: 'd1', name: 'Region', zone: 'rows' },
      { id: 'd2', name: 'Quarter', zone: 'columns' },
    ];
    expect(resolveZoneAxes(items, lists)).toEqual({
      rowDimNames: ['region'],
      colDimNames: ['quarter'],
    });
  });

  it('pivots flat rows into a cross-tab with column members as headers', () => {
    const flat = [
      { region: 'US', quarter: 'Q1', revenue: 100 },
      { region: 'US', quarter: 'Q2', revenue: 150 },
      { region: 'UK', quarter: 'Q1', revenue: 80 },
      { region: 'UK', quarter: 'Q2', revenue: 90 },
    ];
    const titles = { region: 'Region', quarter: 'Quarter', revenue: 'Revenue' };
    const out = pivotZoneResult(flat, ['region'], ['quarter'], ['revenue'], titles);
    // Single measure -> column header is just the column-member value.
    expect(out.headers).toEqual(['Region', 'Q1', 'Q2']);
    expect(out.rows).toEqual([
      ['US', 100, 150],
      ['UK', 80, 90],
    ]);
  });

  it('with no column dims returns the flat shape (dimensions first, then measures)', () => {
    const flat = [
      { region: 'US', revenue: 250 },
      { region: 'UK', revenue: 170 },
    ];
    const titles = { region: 'Region', revenue: 'Revenue' };
    const out = pivotZoneResult(flat, ['region'], [], ['revenue'], titles);
    expect(out.headers).toEqual(['Region', 'Revenue']);
    expect(out.rows).toEqual([
      ['US', 250],
      ['UK', 170],
    ]);
  });

  it('suffixes the measure name when more than one measure is pivoted', () => {
    const flat = [
      { region: 'US', quarter: 'Q1', revenue: 100, units: 5 },
      { region: 'US', quarter: 'Q2', revenue: 150, units: 7 },
    ];
    const titles = { region: 'Region', quarter: 'Quarter', revenue: 'Revenue', units: 'Units' };
    const out = pivotZoneResult(flat, ['region'], ['quarter'], ['revenue', 'units'], titles);
    expect(out.headers).toEqual(['Region', 'Q1 — Revenue', 'Q1 — Units', 'Q2 — Revenue', 'Q2 — Units']);
    expect(out.rows).toEqual([
      ['US', 100, 5, 150, 7],
    ]);
  });

  it('fills missing cells with empty string', () => {
    const flat = [
      { region: 'US', quarter: 'Q1', revenue: 100 },
      { region: 'UK', quarter: 'Q2', revenue: 90 },
    ];
    const titles = { region: 'Region', quarter: 'Quarter', revenue: 'Revenue' };
    const out = pivotZoneResult(flat, ['region'], ['quarter'], ['revenue'], titles);
    expect(out.headers).toEqual(['Region', 'Q1', 'Q2']);
    // US has no Q2; UK has no Q1.
    expect(out.rows).toEqual([
      ['US', 100, ''],
      ['UK', '', 90],
    ]);
  });
});

/**
 * Bug-5785 — composite row/column identity keys must be unambiguous.
 *
 * Previously the pivot built its identity key by concatenating field values
 * with a fixed delimiter, so two distinct value tuples that reconstitute the
 * same string (e.g. ["a","b c"] vs ["a b","c"], or a value that itself contains
 * the delimiter) collapsed to one key and silently merged distinct pivot rows
 * or columns. The identity key is now JSON-encoded, so distinct tuples can
 * never collide regardless of their contents. Display headers still use the
 * friendly delimiter-joined label.
 */
describe('Bug-6730 local PivotTable zone mapping and additive safety', () => {
  const localLists = {
    measures: [
      { id: 'm-revenue', name: 'revenue', display_name: 'Revenue', default_agg: 'sum', measure_type: 'standard' } as Measure,
      { id: 'm-margin', name: 'margin_rate', display_name: 'Margin Rate', default_agg: 'avg', measure_type: 'calculated' } as Measure,
      { id: 'm-inventory', name: 'ending_inventory', display_name: 'Ending Inventory', default_agg: 'sum', measure_type: 'standard', semi_additive_behavior: 'last_non_empty' } as Measure,
    ],
    dimensions: [
      { id: 'd-region', name: 'region', display_name: 'Region', data_type: 'text', source_type: 'dim' } as Dimension,
      { id: 'd-quarter', name: 'quarter', display_name: 'Quarter', data_type: 'text', source_type: 'dim' } as Dimension,
      { id: 'd-channel', name: 'channel', display_name: 'Channel', data_type: 'text', source_type: 'dim' } as Dimension,
    ],
  };

  const rowColumnFilterItems: ZoneItem[] = [
    { id: 'm-revenue', name: 'Revenue', zone: 'values' },
    { id: 'd-region', name: 'Region', zone: 'rows' },
    { id: 'd-quarter', name: 'Quarter', zone: 'columns' },
    { id: 'd-channel', name: 'Channel', zone: 'filters', operator: 'eq', values: ['Direct'] },
  ];

  it('builds native PivotTable field mapping from row, column, filter and value zones', () => {
    const annotation = {
      measures: { revenue: { title: 'Revenue', type: 'number' } },
      dimensions: {
        region: { title: 'Region', type: 'string' },
        quarter: { title: 'Quarter', type: 'string' },
        channel: { title: 'Channel', type: 'string' },
      },
      timeDimensions: {},
    };

    expect(buildLocalPivotFieldMapping(rowColumnFilterItems, localLists, annotation)).toEqual({
      rowFields: ['Region'],
      columnFields: ['Quarter'],
      dataFields: ['Revenue'],
      filterFields: ['Channel'],
    });
  });

  it('keeps filter-zone fields in the flat local pivot source query', () => {
    const baseQuery = buildZoneQuery(rowColumnFilterItems, localLists);
    expect(baseQuery).not.toBeNull();

    const localQuery = buildLocalPivotQuery(baseQuery!, rowColumnFilterItems, localLists);

    expect(localQuery.measures).toEqual(['revenue']);
    expect(localQuery.dimensions).toEqual(['region', 'quarter', 'channel']);
    expect(localQuery.filters).toEqual([{ dimension: 'channel', operator: 'eq', values: ['Direct'] }]);
  });

  it('classifies only additive standard measures as safe for free Excel re-aggregation', () => {
    expect(isMeasureSafeForLocalPivot(localLists.measures[0])).toBe(true);
    expect(isMeasureSafeForLocalPivot(localLists.measures[1])).toBe(false);
    expect(isMeasureSafeForLocalPivot(localLists.measures[2])).toBe(false);
  });

  it('reports unsafe staged value measures before local PivotTable insertion', () => {
    const items: ZoneItem[] = [
      { id: 'm-revenue', name: 'Revenue', zone: 'values' },
      { id: 'm-margin', name: 'Margin Rate', zone: 'values' },
      { id: 'm-inventory', name: 'Ending Inventory', zone: 'values' },
      { id: 'd-region', name: 'Region', zone: 'rows' },
    ];

    expect(unsafeLocalPivotMeasures(items, localLists.measures).map(m => m.name)).toEqual([
      'margin_rate',
      'ending_inventory',
    ]);
  });
});

describe('pivotZoneResult composite-key identity (Bug-5785)', () => {
  // The raw NUL byte the pre-fix row key used as its join delimiter. Two tuples
  // collide under that join only when a value itself contains the delimiter, so
  // the row-axis guards below embed it via fromCharCode (keeps a control byte
  // out of the source text) to reproduce the exact pre-fix collision.
  const NUL = String.fromCharCode(0);

  it('keeps distinct composite ROW tuples separate when the pre-fix NUL join would collide', () => {
    // The pre-fix row key joined values with a raw NUL byte, so the tuples
    // ["x\0y","z"] and ["x","y\0z"] BOTH collapsed to "x\0y\0z" and merged into
    // one row. These values are chosen to collide under that exact NUL join, so
    // this test fails if the fix is reverted; JSON encoding keeps them distinct.
    const data = [
      { country: `x${NUL}y`, region: 'z', period: 'Q1', rev: 1 },
      { country: 'x', region: `y${NUL}z`, period: 'Q1', rev: 2 },
    ];
    const out = pivotZoneResult(data, ['country', 'region'], ['period'], ['rev'], {});

    // Two distinct row groups must survive — not one merged row.
    expect(out.rows).toHaveLength(2);
    const first = out.rows.find(r => r[0] === `x${NUL}y` && r[1] === 'z');
    const second = out.rows.find(r => r[0] === 'x' && r[1] === `y${NUL}z`);
    expect(first).toBeDefined();
    expect(second).toBeDefined();
    // Each row keeps its own measure value (no overwrite/merge).
    expect(first![2]).toBe(1);
    expect(second![2]).toBe(2);
  });

  it('keeps distinct composite COLUMN tuples separate when a naive join would collide', () => {
    // Column members ["a","b / c"] and ["a / b","c"] both flatten to "a / b / c".
    const data = [
      { country: 'X', c1: 'a', c2: 'b / c', rev: 10 },
      { country: 'X', c1: 'a / b', c2: 'c', rev: 20 },
    ];
    const out = pivotZoneResult(data, ['country'], ['c1', 'c2'], ['rev'], {});

    // One row (country X) with two separate measure columns, not a merged one.
    expect(out.rows).toHaveLength(1);
    // country label + one column per (colMember x measure) => 3 headers.
    expect(out.headers).toHaveLength(3);
    const [, ...cells] = out.rows[0];
    // Both column cells present and distinct — data was not collapsed into one.
    expect(cells).toContain(10);
    expect(cells).toContain(20);
  });

  it('keeps distinct rows apart when a value contains the raw key delimiter', () => {
    // Same NUL hazard, boundary shifted: ["North"+NUL+"East", "x"] and
    // ["North", "East"+NUL+"x"] both join to one pre-fix key; JSON keeps them apart.
    const data = [
      { d1: `North${NUL}East`, d2: 'x', period: 'Q1', rev: 5 },
      { d1: 'North', d2: `East${NUL}x`, period: 'Q1', rev: 6 },
    ];
    const out = pivotZoneResult(data, ['d1', 'd2'], ['period'], ['rev'], {});
    expect(out.rows).toHaveLength(2);
    const merged = out.rows.map(r => r[2]);
    expect(merged).toContain(5);
    expect(merged).toContain(6);
  });
});

describe('planKpiZoneAdd', () => {
  const baseKpi: Kpi = {
    id: 'kpi-1',
    name: 'net_margin_kpi',
    display_name: 'Net Margin',
    description: null,
    display_folder: null,
    value_measure_id: null,
    goal_measure_id: null,
    target_type: null,
    target_value: null,
    status_expression: null,
    trend_expression: null,
    status_graphic: 'Traffic Light',
    trend_graphic: 'Standard Arrow',
    weight: null,
    parent_kpi_id: null,
    certification_status: 'draft',
    replacement_id: null,
    owner_user_id: null,
    updated_at: '2026-01-01T00:00:00Z',
  };

  // Bug-6701: every KPI in the acme-demo seed is kpi_type "custom" -- a
  // computed expression with NO value_measure_id. The old inline check
  // (`if (kpi.value_measure_id) { ... }`) fell through to nothing for this
  // exact shape, making "Add KPI" a guaranteed silent no-op. The plan must
  // NEVER be silent: it always resolves to a concrete action.
  it('routes a custom/expression KPI (no value_measure_id) to the formula path, never silently', () => {
    const plan = planKpiZoneAdd(baseKpi);
    expect(plan.kind).toBe('route_to_formula');
    expect(plan.valueMeasureId).toBeUndefined();
    // Bug-6721: measure-less is the 'no_measure' reason (custom-KPI wording).
    expect(plan.routeReason).toBe('no_measure');
  });

  it('stages a measure-backed KPI into the Values zone', () => {
    const kpi: Kpi = { ...baseKpi, value_measure_id: 'm-value', goal_measure_id: null };
    const plan = planKpiZoneAdd(kpi);
    expect(plan.kind).toBe('zone_stage');
    expect(plan.valueMeasureId).toBe('m-value');
    expect(plan.goalMeasureId).toBeUndefined();
  });

  it('stages both value and goal measures when they differ', () => {
    const kpi: Kpi = { ...baseKpi, value_measure_id: 'm-value', goal_measure_id: 'm-goal' };
    const plan = planKpiZoneAdd(kpi);
    expect(plan.kind).toBe('zone_stage');
    expect(plan.valueMeasureId).toBe('m-value');
    expect(plan.goalMeasureId).toBe('m-goal');
  });

  it('omits a goal measure identical to the value measure (no duplicate zone item)', () => {
    const kpi: Kpi = { ...baseKpi, value_measure_id: 'm-shared', goal_measure_id: 'm-shared' };
    const plan = planKpiZoneAdd(kpi);
    expect(plan.kind).toBe('zone_stage');
    expect(plan.goalMeasureId).toBeUndefined();
  });
});

describe('planKpiZoneRemove (Bug-6715)', () => {
  const kpiWith = (value: string | null, goal: string | null): Pick<Kpi, 'value_measure_id' | 'goal_measure_id'> => ({
    value_measure_id: value,
    goal_measure_id: goal,
  });

  // Bug-6715: untoggle must be the exact inverse of Add. The previous code
  // removed only value_measure_id, leaving the goal-measure chip staged
  // while the KPI card read as unstaged.
  it('removes BOTH the value and the distinct goal measure that Add staged', () => {
    expect(planKpiZoneRemove(kpiWith('m-value', 'm-goal'), ['m-value', 'm-goal']))
      .toEqual(['m-value', 'm-goal']);
  });

  it('removes only the value measure when the goal is identical (Add staged it once)', () => {
    expect(planKpiZoneRemove(kpiWith('m-shared', 'm-shared'), ['m-shared']))
      .toEqual(['m-shared']);
  });

  it('skips ids the user already removed via their zone chips (no error, no double-remove)', () => {
    expect(planKpiZoneRemove(kpiWith('m-value', 'm-goal'), ['m-goal']))
      .toEqual(['m-goal']);
    expect(planKpiZoneRemove(kpiWith('m-value', 'm-goal'), []))
      .toEqual([]);
  });

  it('returns nothing for a custom/expression KPI (nothing was ever staged)', () => {
    expect(planKpiZoneRemove(kpiWith(null, null), ['m-anything'])).toEqual([]);
  });
});

describe('planKpiZoneAdd resolvability guard (Bug-6719)', () => {
  const kpi: Pick<Kpi, 'value_measure_id' | 'goal_measure_id'> = {
    value_measure_id: 'm-value',
    goal_measure_id: 'm-goal',
  };

  // Bug-6719: a KPI whose value measure is not in the loaded (persona-scoped)
  // measure list -- deleted measure, or a persona exposing the KPI but hiding
  // its measure -- must NOT stage the dangling UUID (it would leak through
  // resolveZoneItemName's id-fallback into the semantic query as a "measure
  // name" and 422). It routes to the KPI-native formula insert instead, the
  // same treatment as a measure-less custom KPI.
  it('routes to the formula path when the value measure is not resolvable', () => {
    const plan = planKpiZoneAdd(kpi, ['m-other', 'm-goal']);
    expect(plan.kind).toBe('route_to_formula');
    // Bug-6721: the reason distinguishes this edge from a measure-less
    // custom KPI so the toast can state the true cause.
    expect(plan.routeReason).toBe('unresolvable_measure');
  });

  it('stages normally when both measures resolve', () => {
    const plan = planKpiZoneAdd(kpi, ['m-value', 'm-goal']);
    expect(plan).toEqual({ kind: 'zone_stage', valueMeasureId: 'm-value', goalMeasureId: 'm-goal' });
  });

  it('stages the value but drops an unresolvable goal measure', () => {
    const plan = planKpiZoneAdd(kpi, ['m-value']);
    expect(plan.kind).toBe('zone_stage');
    expect(plan.valueMeasureId).toBe('m-value');
    expect(plan.goalMeasureId).toBeUndefined();
  });

  it('applies no filtering when the caller has no measure list (backwards-compatible)', () => {
    const plan = planKpiZoneAdd(kpi);
    expect(plan).toEqual({ kind: 'zone_stage', valueMeasureId: 'm-value', goalMeasureId: 'm-goal' });
  });
});

// ---------------------------------------------------------------------------
// Bug-6738: whole hierarchy expands to ALL levels (coarse to fine)
// ---------------------------------------------------------------------------
describe('Bug-6738 -- whole hierarchy expands to all levels', () => {
  it('multiple hierarchy levels added coarse-to-fine all appear in the query dimensions', () => {
    // This is the expected zone state after handleAddHierarchyToZone adds
    // all levels of a date hierarchy (Year, Quarter, Month, Day).
    const items: ZoneItem[] = [
      { id: 'm1', name: 'fee amount', zone: 'values' },
      { id: 'h1:0', name: 'Calendar: Year', zone: 'rows', kind: 'hierarchy_level', bindDimension: 'order_date_year' },
      { id: 'h1:1', name: 'Calendar: Quarter', zone: 'rows', kind: 'hierarchy_level', bindDimension: 'order_date_quarter' },
      { id: 'h1:2', name: 'Calendar: Month', zone: 'rows', kind: 'hierarchy_level', bindDimension: 'order_date_month' },
      { id: 'h1:3', name: 'Calendar: Day', zone: 'rows', kind: 'hierarchy_level', bindDimension: 'order_date_day' },
    ];
    const extendedLists = {
      ...lists,
      dimensions: [
        ...lists.dimensions,
        { id: 'dy', name: 'order_date_year', display_name: 'Year', data_type: 'integer', source_type: 'dim' as const },
        { id: 'dq', name: 'order_date_quarter', display_name: 'Quarter', data_type: 'integer', source_type: 'dim' as const },
        { id: 'dm', name: 'order_date_month', display_name: 'Month', data_type: 'integer', source_type: 'dim' as const },
        { id: 'dd', name: 'order_date_day', display_name: 'date', data_type: 'date', source_type: 'dim' as const },
      ],
    };
    const q = buildZoneQuery(items, extendedLists);
    expect(q).not.toBeNull();
    // All four hierarchy levels must be in the dimensions list, coarse to fine.
    expect(q!.dimensions).toEqual([
      'order_date_year', 'order_date_quarter', 'order_date_month', 'order_date_day',
    ]);
    // No hierarchy UUID leaked into the dimensions.
    expect(q!.dimensions?.some(d => d.includes('h1'))).toBe(false);
  });

  it('a single hierarchy level only adds that one dimension (not the whole hierarchy)', () => {
    // When a single level is added (not whole hierarchy), only that level appears.
    const items: ZoneItem[] = [
      { id: 'm1', name: 'fee amount', zone: 'values' },
      { id: 'h1:2', name: 'Calendar: Month', zone: 'rows', kind: 'hierarchy_level', bindDimension: 'order_date_month' },
    ];
    const q = buildZoneQuery(items, lists);
    expect(q).not.toBeNull();
    expect(q!.dimensions).toEqual(['order_date_month']);
  });
});

// ---------------------------------------------------------------------------
// Bug-6739: zone query default ordering
// ---------------------------------------------------------------------------
describe('Bug-6739 -- zone query default ordering', () => {
  it('adds ascending ORDER BY for all dimension columns when no explicit order is given', () => {
    const items: ZoneItem[] = [
      { id: 'm1', name: 'fee amount', zone: 'values' },
      { id: 'd1', name: 'account type', zone: 'rows' },
      { id: 'd2', name: 'Month', zone: 'rows' },
    ];
    const q = buildZoneQuery(items, lists);
    expect(q).not.toBeNull();
    expect(q!.order).toEqual({ account_type: 'asc', business_date_month: 'asc' });
  });

  it('preserves an explicit user sort and does not add dimension defaults', () => {
    const items: ZoneItem[] = [
      { id: 'm1', name: 'fee amount', zone: 'values' },
      { id: 'd1', name: 'account type', zone: 'rows' },
    ];
    const q = buildZoneQuery(items, lists, 1000, { fee_amount: 'desc' });
    expect(q).not.toBeNull();
    expect(q!.order).toEqual({ fee_amount: 'desc' });
  });

  it('includes column dimensions in the default ordering', () => {
    const items: ZoneItem[] = [
      { id: 'm1', name: 'fee amount', zone: 'values' },
      { id: 'd1', name: 'account type', zone: 'rows' },
      { id: 'd2', name: 'Month', zone: 'columns' },
    ];
    const q = buildZoneQuery(items, lists);
    expect(q).not.toBeNull();
    // Both row and column dimensions get default ascending order.
    expect(q!.order).toEqual({ account_type: 'asc', business_date_month: 'asc' });
  });

  it('does not add order when there are no dimensions (measures only)', () => {
    const items: ZoneItem[] = [
      { id: 'm1', name: 'fee amount', zone: 'values' },
    ];
    const q = buildZoneQuery(items, lists);
    expect(q).not.toBeNull();
    expect(q!.order).toBeUndefined();
  });

  it('hierarchy level zone items get ordered by their bound dimension name', () => {
    const items: ZoneItem[] = [
      { id: 'm1', name: 'fee amount', zone: 'values' },
      { id: 'hier:0', name: 'Calendar: Year', zone: 'rows', kind: 'hierarchy_level', bindDimension: 'order_date_year' },
      { id: 'hier:1', name: 'Calendar: Month', zone: 'rows', kind: 'hierarchy_level', bindDimension: 'order_date_month' },
    ];
    const extendedLists = {
      ...lists,
      dimensions: [
        ...lists.dimensions,
        { id: 'd3', name: 'order_date_year', display_name: 'Year', data_type: 'integer', source_type: 'dim' as const },
        { id: 'd4', name: 'order_date_month', display_name: 'Month', data_type: 'integer', source_type: 'dim' as const },
      ],
    };
    const q = buildZoneQuery(items, extendedLists);
    expect(q).not.toBeNull();
    // Coarse-to-fine ordering: year first, then month.
    expect(q!.order).toEqual({ order_date_year: 'asc', order_date_month: 'asc' });
  });
});
