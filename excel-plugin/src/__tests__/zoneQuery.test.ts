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
import { buildZoneQuery, resolveZoneItemName, evaluateNamedSetZoneGate, resolveZoneAxes, pivotZoneResult } from '../utils/zoneQuery';
import type { ZoneItem } from '../components/ReportBuilder/ZoneMappingGrid';
import type { Measure, Dimension, NamedSet, NamedSetPreviewResponse } from '../types/tessallite';

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
      { member: 'account_type', operator: 'in', values: ['CREDIT', 'WALLET'] },
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
      { member: 'account_type', operator: 'in', values: ['CREDIT'] },
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
      { member: 'account_type', operator: 'eq', values: ['CREDIT'] },
      { member: 'business_date_month', operator: 'in', values: ['1', '2'] },
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
    expect(q!.filters).toEqual([{ member: 'account_type', operator: 'in', values: truncatedKeys }]);
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
