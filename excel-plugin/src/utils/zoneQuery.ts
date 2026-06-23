/**
 * Report Builder zone -> SemanticQuery translation.
 *
 * Pure, side-effect-free so the binding contract is unit-testable against the
 * payloads the query-router actually accepts. Extracted as part of F-025-11:
 * named-set and hierarchy-level zone items are NOT bindable by their UUID
 * token, so each carries a pre-resolved `bindDimension` (and, for named sets,
 * a `memberKeys` list). This builder turns those into bindable dimension/axis
 * placements plus `in` filters over the member keys.
 */
import type { ZoneItem } from '../components/ReportBuilder/ZoneMappingGrid';
import type { SemanticQuery, Measure, Dimension, NamedSet, NamedSetPreviewResponse } from '../types/tessallite';

export interface ZoneResolutionLists {
  measures?: Measure[];
  dimensions?: Dimension[];
}

/**
 * Builder types whose membership is computed at evaluation time (top-N,
 * filtered). When such a set is dropped into a zone it is frozen to a
 * point-in-time snapshot of the keys returned by the preview; it does not
 * re-evaluate as the source data changes. "Insert as formulas" (CUBESET)
 * preserves the live, dynamic membership instead.
 */
const DYNAMIC_BUILDER_TYPES = new Set(['topN', 'dynamic_top_n', 'filter', 'filtered']);

export interface NamedSetZoneGate {
  /** True when the set can be safely bound to the zone as an exact `in` filter. */
  safe: boolean;
  /** True when the preview truncated the membership (more keys than were returned). */
  truncated: boolean;
  /** True when the set's membership is dynamic (top-N / filtered) and would be frozen. */
  dynamic: boolean;
  /** Reason code for the caller; 'ok' when safe. */
  reason: 'ok' | 'truncated' | 'dynamic' | 'truncated_dynamic';
}

/**
 * Bug-1112: decide whether a named set can be dropped into a zone as an exact
 * `in`-filter membership, or whether the binding would silently under-count
 * (preview truncated at the server limit) or freeze a dynamic set at a
 * point-in-time snapshot. Pure so the gate is unit-testable.
 *
 * A set is UNSAFE to silently bind when either:
 *  - the preview was truncated (`preview.truncated`) — the inserted aggregate
 *    would reflect only the first N keys, under-counting the real total; or
 *  - the set's builder type is dynamic (top-N / filtered) — the membership is
 *    computed live and binding it as a static key list freezes it.
 * In both cases the caller must warn and steer the analyst to
 * "Insert as formulas" (CUBESET), which preserves full / dynamic membership.
 */
export function evaluateNamedSetZoneGate(
  ns: Pick<NamedSet, 'builder_definition' | 'list_type'>,
  preview: Pick<NamedSetPreviewResponse, 'truncated'>,
): NamedSetZoneGate {
  const truncated = preview.truncated === true;
  const builderType =
    ns.builder_definition && typeof ns.builder_definition.type === 'string'
      ? (ns.builder_definition.type as string)
      : '';
  const dynamic =
    DYNAMIC_BUILDER_TYPES.has(builderType) ||
    DYNAMIC_BUILDER_TYPES.has(ns.list_type ?? '');

  let reason: NamedSetZoneGate['reason'] = 'ok';
  if (truncated && dynamic) reason = 'truncated_dynamic';
  else if (truncated) reason = 'truncated';
  else if (dynamic) reason = 'dynamic';

  return { safe: !truncated && !dynamic, truncated, dynamic, reason };
}

/**
 * Resolve a single zone item to its bindable technical name.
 * A named-set / hierarchy-level item carries `bindDimension`; a plain field
 * resolves its UUID via the model's measure/dimension lists; anything else
 * falls back to its id (a deployed model name passed through directly).
 */
export function resolveZoneItemName(item: ZoneItem, lists: ZoneResolutionLists): string {
  if (item.bindDimension) return item.bindDimension;
  const m = lists.measures?.find(x => x.id === item.id);
  if (m) return m.name;
  const d = lists.dimensions?.find(x => x.id === item.id);
  if (d) return d.name;
  return item.id;
}

/**
 * Build the SemanticQuery from the current zone items.
 *
 * - Values -> measures.
 * - Rows + Columns -> dimensions (a named set / level binds via its dimension).
 * - Explicit filter items -> their configured operator/values.
 * - Named-set items (any zone) -> an `in` filter over their member keys on the
 *   bound dimension (F-025-11), in addition to any axis placement.
 *
 * Returns null when no measure is selected (the query is not executable).
 */
export function buildZoneQuery(
  zoneItems: ZoneItem[],
  lists: ZoneResolutionLists,
  limit = 1000,
  // F-025-27: optional sort. `order` maps a resolved technical field name to a
  // direction; the query-router accepts it as order_by ([{field, direction}])
  // with injection-hardened validation. Fields not present in the query's
  // measures/dimensions are dropped so we never ask the backend to sort by an
  // unselected column.
  order?: Record<string, 'asc' | 'desc'>,
): SemanticQuery | null {
  const resolve = (i: ZoneItem) => resolveZoneItemName(i, lists);

  const measures = zoneItems.filter(i => i.zone === 'values').map(resolve);
  const rowDims = zoneItems.filter(i => i.zone === 'rows').map(resolve);
  const colDims = zoneItems.filter(i => i.zone === 'columns').map(resolve);
  // A named set carries its own `in` constraint below; it must not also be
  // emitted as a plain filter item (which would double-constrain or, worse,
  // bind its UUID as a `set` operator on a non-existent member).
  const filterItems = zoneItems.filter(i => i.zone === 'filters' && i.kind !== 'named_set');

  // Bug-5289: an unedited filter chip (no operator, no values) must be omitted
  // entirely — emitting `operator: 'set'` with no values is invalid and causes
  // the query-router to reject the request. Only emit filters that have a
  // configured operator AND at least one value.
  const explicitFilters = filterItems
    .filter(f => f.operator && f.values?.length)
    .map(f => ({
      member: resolve(f),
      operator: f.operator!,
      values: f.values!,
    }));

  const namedSetFilters = zoneItems
    .filter(i => i.kind === 'named_set' && i.bindDimension && i.memberKeys?.length)
    .map(i => ({
      member: i.bindDimension as string,
      operator: 'in',
      values: i.memberKeys as string[],
    }));

  const filters = [...explicitFilters, ...namedSetFilters];

  if (measures.length === 0) return null;

  const selectable = new Set<string>([...measures, ...rowDims, ...colDims]);
  const resolvedOrder = order
    ? Object.fromEntries(
        Object.entries(order).filter(([field]) => selectable.has(field)),
      )
    : undefined;

  return {
    measures,
    dimensions: [...rowDims, ...colDims],
    filters: filters.length > 0 ? filters : undefined,
    order:
      resolvedOrder && Object.keys(resolvedOrder).length > 0
        ? (resolvedOrder as Record<string, 'asc' | 'desc'>)
        : undefined,
    limit,
  };
}

/**
 * F-025-15: report which resolved dimension technical names are placed on the
 * Columns axis (as opposed to Rows). The query-router returns a flat grouped
 * result regardless of zone, so the Columns placement only becomes a real
 * cross-tab when the client pivots the flat rows before inserting. This pure
 * helper gives the inserter the row/column split it needs to do that.
 */
export function resolveZoneAxes(
  zoneItems: ZoneItem[],
  lists: ZoneResolutionLists,
): { rowDimNames: string[]; colDimNames: string[] } {
  const resolve = (i: ZoneItem) => resolveZoneItemName(i, lists);
  return {
    rowDimNames: zoneItems.filter(i => i.zone === 'rows').map(resolve),
    colDimNames: zoneItems.filter(i => i.zone === 'columns').map(resolve),
  };
}

export interface PivotedTable {
  headers: string[];
  rows: (string | number)[][];
}

/**
 * F-025-15: reshape a flat grouped result into a cross-tab when one or more
 * dimensions are on the Columns axis.
 *
 * Input is the raw `/plugin/execute` rows (records keyed by technical column
 * name) plus the row-dim / column-dim / measure key lists and a title map for
 * friendly headers. Output is `{ headers, rows }` where:
 *  - the leftmost columns are the row dimensions (one column each), and
 *  - each subsequent column is a (column-member-combo × measure) pair, e.g.
 *    "Q1 — Revenue", "Q2 — Revenue", so periods sit side by side as the
 *    period-comparison template promises.
 *
 * When there are no column dims this returns the flat shape unchanged (row
 * dims first, then measures) so the non-cross-tab path is unaffected.
 * Missing cells are rendered as an empty string. Column-member order follows
 * first appearance in the source rows (stable, deterministic for a sorted
 * result).
 */
export function pivotZoneResult(
  data: Record<string, unknown>[],
  rowDimKeys: string[],
  colDimKeys: string[],
  measureKeys: string[],
  titles: Record<string, string>,
): PivotedTable {
  const cell = (v: unknown): string | number =>
    v == null ? '' : (typeof v === 'string' || typeof v === 'number') ? v : JSON.stringify(v);
  const title = (k: string) => titles[k] ?? k;

  // No column axis: flat table (row dims, then measures), unchanged behaviour.
  if (colDimKeys.length === 0) {
    const keys = [...rowDimKeys, ...measureKeys];
    return {
      headers: keys.map(title),
      rows: data.map(r => keys.map(k => cell(r[k]))),
    };
  }

  const rowKeyOf = (r: Record<string, unknown>) => rowDimKeys.map(k => String(r[k] ?? '')).join(' ');
  const colKeyOf = (r: Record<string, unknown>) => colDimKeys.map(k => String(r[k] ?? '')).join(' / ');

  // Preserve first-seen order for both row groups and column-member combos.
  const rowOrder: string[] = [];
  const rowSeen = new Map<string, Record<string, unknown>>();
  const colOrder: string[] = [];
  const colSeen = new Set<string>();
  // value lookup: rowKey -> colKey -> measureKey -> value
  const values = new Map<string, Map<string, Record<string, unknown>>>();

  for (const r of data) {
    const rk = rowKeyOf(r);
    if (!rowSeen.has(rk)) { rowSeen.set(rk, r); rowOrder.push(rk); }
    const ck = colKeyOf(r);
    if (!colSeen.has(ck)) { colSeen.add(ck); colOrder.push(ck); }
    if (!values.has(rk)) values.set(rk, new Map());
    values.get(rk)!.set(ck, r);
  }

  // Headers: row-dim titles, then one column per (colMember × measure).
  const headers: string[] = rowDimKeys.map(title);
  for (const ck of colOrder) {
    for (const mk of measureKeys) {
      // When there is a single measure, drop the redundant measure suffix.
      headers.push(measureKeys.length > 1 ? `${ck} — ${title(mk)}` : ck);
    }
  }

  const rows: (string | number)[][] = rowOrder.map(rk => {
    const sampleRow = rowSeen.get(rk)!;
    const out: (string | number)[] = rowDimKeys.map(k => cell(sampleRow[k]));
    const byCol = values.get(rk)!;
    for (const ck of colOrder) {
      const src = byCol.get(ck);
      for (const mk of measureKeys) {
        out.push(src ? cell(src[mk]) : '');
      }
    }
    return out;
  });

  return { headers, rows };
}
