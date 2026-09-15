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
export declare function evaluateNamedSetZoneGate(ns: Pick<NamedSet, 'builder_definition' | 'list_type'>, preview: Pick<NamedSetPreviewResponse, 'truncated'>): NamedSetZoneGate;
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
export declare function resolveNamedSetDimension(ns: Pick<NamedSet, 'builder_definition' | 'dimensions' | 'expression'>): string | null;
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
export declare function planKpiZoneAdd(kpi: Pick<Kpi, 'value_measure_id' | 'goal_measure_id'>, knownMeasureIds?: readonly string[]): KpiZoneAddPlan;
/**
 * Bug-6715: the measure ids to remove when a staged KPI is untoggled.
 * Un-staging must be the exact inverse of `planKpiZoneAdd`'s staging:
 * the previous code removed only `value_measure_id`, silently leaving the
 * goal-measure chip in the Values zone while the KPI card read as unstaged.
 * Only ids currently present in the zone are returned, so untoggling never
 * errors on (or double-removes) an item the user already deleted via its
 * zone chip. Pure for the same reason as `planKpiZoneAdd`.
 */
export declare function planKpiZoneRemove(kpi: Pick<Kpi, 'value_measure_id' | 'goal_measure_id'>, stagedValueIds: readonly string[]): string[];
/**
 * Resolve a single zone item to its bindable technical name.
 * A named-set / hierarchy-level item carries `bindDimension`; a plain field
 * resolves its UUID via the model's measure/dimension lists; anything else
 * falls back to its id (a deployed model name passed through directly).
 */
export declare function resolveZoneItemName(item: ZoneItem, lists: ZoneResolutionLists): string;
/**
 * Build the SemanticQuery from the current zone items.
 *
 * - Values -> measures.
 * - Rows + Columns -> dimensions (a named set / level binds via its dimension).
 * - Dimension filter items -> their configured operator/values.
 * - Measure filter items -> structured measure predicates with deployed agg.
 * - Named-set items (any zone) -> an `in` filter over their member keys on the
 *   bound dimension (F-025-11), in addition to any axis placement.
 *
 * Returns null when no measure is selected (the query is not executable).
 */
export declare function buildZoneQuery(zoneItems: ZoneItem[], lists: ZoneResolutionLists, limit?: number, order?: Record<string, 'asc' | 'desc'>): SemanticQuery | null;
/**
 * F-025-15: report which resolved dimension technical names are placed on the
 * Columns axis (as opposed to Rows). The query-router returns a flat grouped
 * result regardless of zone, so the Columns placement only becomes a real
 * cross-tab when the client pivots the flat rows before inserting. This pure
 * helper gives the inserter the row/column split it needs to do that.
 */
export declare function resolveZoneAxes(zoneItems: ZoneItem[], lists: ZoneResolutionLists): {
    rowDimNames: string[];
    colDimNames: string[];
};
export interface LocalPivotFieldMapping {
    rowFields: string[];
    columnFields: string[];
    dataFields: string[];
    filterFields: string[];
}
/**
 * Bug-6730: local PivotTables must receive the same zone placement the analyst
 * configured in Report Builder. The inserted source table is flat; Excel then
 * owns the row/column/filter/value layout through this field mapping.
 */
export declare function buildLocalPivotFieldMapping(zoneItems: ZoneItem[], lists: ZoneResolutionLists, annotation?: ExecuteResponse['annotation']): LocalPivotFieldMapping;
/**
 * Local PivotTables need filter-zone fields present as source-table columns so
 * Excel can place them in report filters. The normal table/chart query keeps
 * filters as predicates only; this variant appends their dimensions while
 * preserving predicates and row/column dimensions.
 */
export declare function buildLocalPivotQuery(baseQuery: SemanticQuery, zoneItems: ZoneItem[], lists: ZoneResolutionLists): SemanticQuery;
export declare function isMeasureSafeForLocalPivot(measure: Pick<Measure, 'default_agg' | 'measure_type' | 'semi_additive_behavior' | 'variant_of_measure_id'>): boolean;
/**
 * Excel can freely re-aggregate local PivotTable values. Block measures whose
 * model metadata says they are calculated, variant, semi-additive, or use a
 * non-additive default aggregation.
 */
export declare function unsafeLocalPivotMeasures(zoneItems: ZoneItem[], measures: Measure[] | undefined): Measure[];
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
export declare function pivotZoneResult(data: Record<string, unknown>[], rowDimKeys: string[], colDimKeys: string[], measureKeys: string[], titles: Record<string, string>): PivotedTable;
