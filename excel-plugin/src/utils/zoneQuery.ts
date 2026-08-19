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
import type { SemanticQuery, Measure, Dimension, NamedSet, NamedSetPreviewResponse, Kpi, ExecuteResponse } from '../types/tessallite';

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
 * Bug-6904 / Bug-1112: resolve the dimension a named set is bound to, so the
 * set can be dropped into a Report Builder zone as a bindable axis (+ member-key
 * `in` filter) rather than an unbindable UUID token.
 *
 * Resolution order, most-authoritative first:
 *  1. The builder definition's `entity` (structured sets store it explicitly).
 *  2. A single-value `dimensions` field (comma-joined; only unambiguous when it
 *     names exactly one dimension).
 *  3. The MDX expression's `[dim].[dim].Members` pattern that the
 *     TopCount / BottomCount / Filter builders emit — the ONLY signal the demo
 *     seed's expression-only named sets carry (both `entity` and `dimensions`
 *     are null for them, which is the exact Bug-6904 failure).
 *
 * Pure so the fallback chain is unit-testable without mounting the Report
 * Builder. Returns null when no dimension can be resolved (the caller then
 * steers the analyst to "Insert as formulas").
 */
export function resolveNamedSetDimension(
  ns: Pick<NamedSet, 'builder_definition' | 'dimensions' | 'expression'>,
): string | null {
  const bd = ns.builder_definition as { entity?: unknown } | null;
  const entity = bd && typeof bd.entity === 'string' ? bd.entity.trim() : '';
  if (entity) return entity;

  const dims = (ns.dimensions || '').split(',').map(s => s.trim()).filter(Boolean);
  if (dims.length === 1) return dims[0];

  if (ns.expression) {
    // TopCount/BottomCount/Filter axis expressions reference the set's
    // dimension as `[Region].[Region].Members`. The back-reference \1 pins
    // the level name to the dimension name so an unrelated `[a].[b].Members`
    // is not mis-read as dimension "a".
    const m = ns.expression.match(/\[([^\]]+)\]\.\[\1\]\.Members/);
    if (m) return m[1];
  }
  return null;
}

export interface KpiZoneAddPlan {
  /**
   * 'zone_stage': the KPI's value (and, if distinct, goal) measure can be
   * staged into the pivot "Values" zone for later Table/Chart/Pivot insert.
   * 'route_to_formula': the KPI has no backing measure (a custom/expression
   * KPI -- kpi_type "custom", value_measure_id null) and CANNOT be staged:
   * buildZoneQuery/SemanticQuery only resolve zone items to measure or
   * dimension names, never a KPI expression, and the query-router has no
   * field for one. The caller must fall back to a direct KPI-native insert
   * (e.g. the formula_ref/CUBEKPIMEMBER path) instead of doing nothing.
   */
  kind: 'zone_stage' | 'route_to_formula';
  valueMeasureId?: string;
  goalMeasureId?: string;
  /**
   * Bug-6721: WHY the KPI was rerouted (route_to_formula only), so the
   * post-insert toast can state the true reason -- a genuinely measure-less
   * custom/expression KPI reads very differently to a modeller than a
   * measure-backed KPI whose value measure was deleted or persona-hidden.
   */
  routeReason?: 'no_measure' | 'unresolvable_measure';
}

/**
 * Bug-6701: decide how "Add KPI" (the Report Builder KpiCard "+" action)
 * should behave for a given KPI. Pure so the always-some-action contract is
 * unit-testable without mounting the KPI library / Report Builder tree.
 * Previously the caller checked `kpi.value_measure_id` inline and did
 * NOTHING when it was null -- a guaranteed silent no-op for every
 * custom/expression KPI in the acme-demo seed.
 *
 * Bug-6719: `knownMeasureIds`, when provided, is the set of measures the
 * caller can actually resolve (the loaded, persona-scoped measure list). A
 * KPI whose value measure is NOT in it (deleted measure, or a persona that
 * exposes the KPI but hides its measure) must be treated like a measure-less
 * KPI and routed to the KPI-native formula insert -- staging the dangling
 * UUID would leak it through `resolveZoneItemName`'s id-fallback into the
 * semantic query as a "measure name" and fail as an uninterpretable 422. An
 * unresolvable goal measure is simply not staged (mirroring the caller's
 * existing goal guard).
 */
export function planKpiZoneAdd(
  kpi: Pick<Kpi, 'value_measure_id' | 'goal_measure_id'>,
  knownMeasureIds?: readonly string[],
): KpiZoneAddPlan {
  if (!kpi.value_measure_id) return { kind: 'route_to_formula', routeReason: 'no_measure' };
  if (knownMeasureIds && !knownMeasureIds.includes(kpi.value_measure_id)) {
    return { kind: 'route_to_formula', routeReason: 'unresolvable_measure' };
  }
  const goalMeasureId =
    kpi.goal_measure_id &&
    kpi.goal_measure_id !== kpi.value_measure_id &&
    (!knownMeasureIds || knownMeasureIds.includes(kpi.goal_measure_id))
      ? kpi.goal_measure_id
      : undefined;
  return { kind: 'zone_stage', valueMeasureId: kpi.value_measure_id, goalMeasureId };
}

/**
 * Bug-6715: the measure ids to remove when a staged KPI is untoggled.
 * Un-staging must be the exact inverse of `planKpiZoneAdd`'s staging:
 * the previous code removed only `value_measure_id`, silently leaving the
 * goal-measure chip in the Values zone while the KPI card read as unstaged.
 * Only ids currently present in the zone are returned, so untoggling never
 * errors on (or double-removes) an item the user already deleted via its
 * zone chip. Pure for the same reason as `planKpiZoneAdd`.
 */
export function planKpiZoneRemove(
  kpi: Pick<Kpi, 'value_measure_id' | 'goal_measure_id'>,
  stagedValueIds: readonly string[],
): string[] {
  const plan = planKpiZoneAdd(kpi);
  if (plan.kind !== 'zone_stage') return [];
  const ids: string[] = [];
  if (stagedValueIds.includes(plan.valueMeasureId!)) ids.push(plan.valueMeasureId!);
  if (plan.goalMeasureId && stagedValueIds.includes(plan.goalMeasureId)) ids.push(plan.goalMeasureId);
  return ids;
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
      dimension: resolve(f),
      operator: f.operator!,
      values: f.values!,
    }));

  const namedSetFilters = zoneItems
    .filter(i => i.kind === 'named_set' && i.bindDimension && i.memberKeys?.length)
    .map(i => ({
      dimension: i.bindDimension as string,
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

  // Bug-6739: when no explicit user sort is set, add a deterministic default
  // ORDER BY on dimension columns (coarse to fine, ascending). Without this,
  // tables render in arbitrary result order (e.g. months 9,4,2,10,...).
  // Dimension columns are already in coarse-to-fine order from the zone items
  // (hierarchy levels are added that way by handleAddHierarchyToZone).
  // Measures are NOT ordered (they aggregate, so sort order is meaningless).
  const allDims = [...rowDims, ...colDims];
  let finalOrder: Record<string, 'asc' | 'desc'> | undefined;
  if (resolvedOrder && Object.keys(resolvedOrder).length > 0) {
    finalOrder = resolvedOrder as Record<string, 'asc' | 'desc'>;
  } else if (allDims.length > 0) {
    // Default: all dimensions ascending (natural key order).
    const defaultOrder: Record<string, 'asc' | 'desc'> = {};
    for (const dim of allDims) {
      defaultOrder[dim] = 'asc';
    }
    finalOrder = defaultOrder;
  }

  return {
    measures,
    dimensions: allDims,
    filters: filters.length > 0 ? filters : undefined,
    order: finalOrder,
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

export interface LocalPivotFieldMapping {
  rowFields: string[];
  columnFields: string[];
  dataFields: string[];
  filterFields: string[];
}

function resolvedFieldTitle(
  technicalName: string,
  item: ZoneItem,
  kind: 'measure' | 'dimension',
  lists: ZoneResolutionLists,
  annotation?: ExecuteResponse['annotation'],
): string {
  const annotated =
    kind === 'measure'
      ? annotation?.measures?.[technicalName]?.title
      : annotation?.dimensions?.[technicalName]?.title;
  if (annotated) return annotated;

  if (kind === 'measure') {
    const measure = lists.measures?.find(x => x.name === technicalName || x.id === item.id);
    return measure?.display_name || measure?.name || item.name || technicalName;
  }

  const dimension = lists.dimensions?.find(x => x.name === technicalName || x.id === item.id);
  return dimension?.display_name || dimension?.name || item.name || technicalName;
}

function uniqueInOrder(values: string[]): string[] {
  return Array.from(new Set(values));
}

/**
 * Bug-6730: local PivotTables must receive the same zone placement the analyst
 * configured in Report Builder. The inserted source table is flat; Excel then
 * owns the row/column/filter/value layout through this field mapping.
 */
export function buildLocalPivotFieldMapping(
  zoneItems: ZoneItem[],
  lists: ZoneResolutionLists,
  annotation?: ExecuteResponse['annotation'],
): LocalPivotFieldMapping {
  const titleFor = (item: ZoneItem, kind: 'measure' | 'dimension') => {
    const technicalName = resolveZoneItemName(item, lists);
    return resolvedFieldTitle(technicalName, item, kind, lists, annotation);
  };

  return {
    rowFields: uniqueInOrder(zoneItems.filter(i => i.zone === 'rows').map(i => titleFor(i, 'dimension'))),
    columnFields: uniqueInOrder(zoneItems.filter(i => i.zone === 'columns').map(i => titleFor(i, 'dimension'))),
    dataFields: uniqueInOrder(zoneItems.filter(i => i.zone === 'values').map(i => titleFor(i, 'measure'))),
    filterFields: uniqueInOrder(zoneItems.filter(i => i.zone === 'filters').map(i => titleFor(i, 'dimension'))),
  };
}

/**
 * Local PivotTables need filter-zone fields present as source-table columns so
 * Excel can place them in report filters. The normal table/chart query keeps
 * filters as predicates only; this variant appends their dimensions while
 * preserving predicates and row/column dimensions.
 */
export function buildLocalPivotQuery(
  baseQuery: SemanticQuery,
  zoneItems: ZoneItem[],
  lists: ZoneResolutionLists,
): SemanticQuery {
  const filterDimensions = zoneItems
    .filter(i => i.zone === 'filters')
    .map(i => resolveZoneItemName(i, lists));
  const dimensions = uniqueInOrder([...(baseQuery.dimensions || []), ...filterDimensions]);

  return {
    ...baseQuery,
    dimensions,
  };
}

const LOCAL_PIVOT_ADDITIVE_AGGS = new Set(['sum', 'count', 'count_star']);
const LOCAL_PIVOT_ADDITIVE_SEMI_BEHAVIORS = new Set(['', 'none', 'sum', 'additive']);

export function isMeasureSafeForLocalPivot(measure: Pick<Measure, 'default_agg' | 'measure_type' | 'semi_additive_behavior'>): boolean {
  const agg = (measure.default_agg || '').trim().toLowerCase();
  const semi = (measure.semi_additive_behavior || '').trim().toLowerCase();
  return (
    measure.measure_type === 'standard' &&
    LOCAL_PIVOT_ADDITIVE_AGGS.has(agg) &&
    LOCAL_PIVOT_ADDITIVE_SEMI_BEHAVIORS.has(semi)
  );
}

/**
 * Excel can freely re-aggregate local PivotTable values. Block measures whose
 * model metadata says they are calculated, variant, semi-additive, or use a
 * non-additive default aggregation.
 */
export function unsafeLocalPivotMeasures(zoneItems: ZoneItem[], measures: Measure[] | undefined): Measure[] {
  if (!measures?.length) return [];
  const measureIds = zoneItems.filter(i => i.zone === 'values').map(i => i.id);
  return uniqueInOrder(measureIds)
    .map(id => measures.find(m => m.id === id))
    .filter((m): m is Measure => Boolean(m))
    .filter(m => !isMeasureSafeForLocalPivot(m));
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

  // Bug-5785: build the composite identity key with an unambiguous encoding.
  // A plain `.join(delimiter)` collapses distinct value tuples that reconstitute
  // the same string (e.g. ["a","b|c"] and ["a|b","c"], or a value that itself
  // contains the delimiter), silently merging distinct pivot rows/columns.
  // JSON.stringify of the normalised value array escapes embedded quotes and
  // preserves segment boundaries, so two different tuples can never encode to
  // one key regardless of the characters the values contain.
  const encodeKey = (parts: string[]) => JSON.stringify(parts);
  const rowValsOf = (r: Record<string, unknown>) => rowDimKeys.map(k => String(r[k] ?? ''));
  const colValsOf = (r: Record<string, unknown>) => colDimKeys.map(k => String(r[k] ?? ''));
  const rowKeyOf = (r: Record<string, unknown>) => encodeKey(rowValsOf(r));
  // The column identity key is not human-readable; the header still shows the
  // friendly ' / '-joined member combo, tracked separately per identity key.
  const colLabelOf = (vals: string[]) => vals.join(' / ');

  // Preserve first-seen order for both row groups and column-member combos.
  const rowOrder: string[] = [];
  const rowSeen = new Map<string, Record<string, unknown>>();
  const colOrder: string[] = [];
  const colSeen = new Set<string>();
  const colLabels = new Map<string, string>();
  // value lookup: rowKey -> colKey -> measureKey -> value
  const values = new Map<string, Map<string, Record<string, unknown>>>();

  for (const r of data) {
    const rk = rowKeyOf(r);
    if (!rowSeen.has(rk)) { rowSeen.set(rk, r); rowOrder.push(rk); }
    const cvals = colValsOf(r);
    const ck = encodeKey(cvals);
    if (!colSeen.has(ck)) { colSeen.add(ck); colOrder.push(ck); colLabels.set(ck, colLabelOf(cvals)); }
    if (!values.has(rk)) values.set(rk, new Map());
    values.get(rk)!.set(ck, r);
  }

  // Headers: row-dim titles, then one column per (colMember × measure).
  const headers: string[] = rowDimKeys.map(title);
  for (const ck of colOrder) {
    const label = colLabels.get(ck) ?? ck;
    for (const mk of measureKeys) {
      // When there is a single measure, drop the redundant measure suffix.
      headers.push(measureKeys.length > 1 ? `${label} — ${title(mk)}` : label);
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
