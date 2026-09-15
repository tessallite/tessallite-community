export interface ToastCall {
    message: string;
    severity: 'success' | 'error' | 'info' | 'warning';
}
/**
 * Bug-6737: decide the toast from the ACTUAL table insert outcome -- the
 * same pattern Bug-6733 introduced for chart inserts. The table write
 * (insertResultTable) is separated from post-insert steps (metadata
 * tagging, provenance footer), so a post-step failure after a successful
 * write shows a warning (stating the table was inserted) rather than a
 * false "Insert failed".
 */
export declare function describeTableInsertResult(tableInserted: boolean, rowCount: number, postStepWarning: boolean, blocked?: boolean): ToastCall | null;
/**
 * Bug-6733: a chart insert has two possible post-step issues:
 *
 *  1. The chart was created but axis formatting failed (cosmetic only -- the
 *     chart is visible and correct, just without custom axis titles).
 *  2. The chart was created but metadata tagging failed (invisible to the
 *     user -- provenance tracking only).
 *
 * A false FAILURE toast is as forbidden as a false success. This helper
 * decides the toast from the ACTUAL chart creation outcome. When the chart
 * was created, the toast says so -- even if a non-critical post-step failed
 * (noted with a brief qualifier). When nothing was created (busy guard
 * declined, user cancelled), no toast is emitted.
 */
export declare function describeChartInsertResult(chartInserted: boolean, postStepWarning: boolean): ToastCall | null;
/**
 * Decide the toast to show after `insertMeasureAsFormula` resolves.
 * `result` is the inserted cell address, or `null` when nothing was written
 * (the busy-guard declined the operation, or the user cancelled an overwrite
 * confirmation) -- in both cases there is nothing to report.
 */
export declare function describeMeasureFormulaInsertResult(result: string | null, connectionName: string): ToastCall | null;
/**
 * Bug-6709: decide the toast after a KPI insert (`handleInsertKpi`) resolves.
 * Same contract as `describeMeasureFormulaInsertResult`, extended for KPIs:
 *
 * - `result` null (busy-guard declined / overwrite cancelled) -> no toast.
 *   Crucially, the "Add KPI" reroute explanation for a custom-expression KPI
 *   must ALSO stay silent here -- the previous code toasted the reroute
 *   BEFORE the insert ran, announcing an insertion that may never happen.
 * - completed insert -> a warning carrying the workbook-connection
 *   requirement, never a bare success: every KPI insert mode emits
 *   CUBEVALUE/CUBEKPIMEMBER formulas that render #N/A without a connection
 *   named `connectionName` (F-025-10), exactly like the sigma measure path.
 * - `routedKpi` set (the Bug-6701 "Add KPI" reroute) -> the message
 *   additionally explains WHY a formula was inserted instead of a pivot
 *   value being staged.
 */
/**
 * Bug-6714: the executable action for a KPI insert mode, for ANY KPI shape.
 *
 * The previous dispatch guarded 'value_only' behind `if (valueMeasure)`,
 * leaving `result` null for a custom/expression KPI (value_measure_id null,
 * every KPI in the acme-demo seed) -- and since a null result correctly maps
 * to "no toast" (declined/cancelled inserts, Bug-6709), the menu entry was a
 * SILENT no-op: the exact defect class of Bug-6701. This planner is total
 * over the 7 published modes x both KPI shapes -- every combination resolves
 * to a concrete insert action, so no mode can ever fall through in silence:
 * a measure-less KPI's Value comes from its KPI-native CUBEKPIMEMBER Value
 * property (the construct the working "Formula Reference" insert uses).
 * Returns null ONLY for an unknown mode string, which the caller must
 * surface as an error toast, never swallow.
 */
export type KpiInsertAction = 'full_row' | 'measure_value_cell' | 'kpi_value_formula' | 'value_goal_rows' | 'status_cell';
export declare function planKpiInsertAction(mode: string, hasValueMeasure: boolean): KpiInsertAction | null;
/**
 * Bug-6721: the reroute reason is threaded from `planKpiZoneAdd` so the
 * post-insert toast states the TRUE cause -- a measure-less custom/expression
 * KPI vs a measure-backed KPI whose value measure is deleted or persona-hidden
 * (Bug-6719) need different explanations; telling a modeller the latter "is a
 * custom-expression KPI" sends them debugging the wrong thing.
 */
export type KpiFormulaRouteReason = 'no_measure' | 'unresolvable_measure';
export declare function describeKpiFormulaInsertResult(result: string | null, mode: string, connectionName: string, routedKpi?: {
    name: string;
    reason: KpiFormulaRouteReason;
} | null): ToastCall | null;
/**
 * The XMLA surface can only serve CUBE formulas for KPIs that meet BOTH:
 *  (a) measure-backed (value_measure_id set, not a composite expression), AND
 *  (b) deployed (is_deployed = true; MDSCHEMA_KPIS is deployed-only).
 *
 * A composite-expression KPI (Defect Rate %, Fulfilment Rate, Gross Margin %)
 * has no executable value member by design (Bug-6702: the gateway advertises
 * an empty KPI_VALUE). An undeployed measure-backed KPI is not in
 * MDSCHEMA_KPIS at all (F-017-05).
 *
 * This planner determines whether a KPI's CUBE formulas can resolve, and
 * when they cannot, provides a reason for the caller to surface as a toast
 * or to switch to a plugin-evaluated literal insert.
 */
export type KpiCubeEligibility = 'cube_eligible' | 'composite_expression' | 'undeployed';
/**
 * Determine whether a KPI's CUBE formulas can resolve on the XMLA surface.
 *
 * Decision order (kpi_type is authoritative, expression is a legacy fallback):
 *
 *  1. **kpi_type is present and authoritative.**
 *     - `simple_measure` -> cube-eligible (has value_measure_id by contract).
 *     - Any other type (ratio, variance, growth_rate, moving_window,
 *       composite) -> check expression fallback (step 2b).
 *  2. **kpi_type absent (legacy row before migration 0116).**
 *     a. Has `value_measure_id` -> cube-eligible (legacy measure-backed).
 *     b. Has `expression` matching the single-measure pattern -> cube-eligible
 *        (the gateway's `_kpi_single_measure_from_expression` serves this).
 *     c. Otherwise -> composite_expression.
 *  3. After eligible determination, if `is_deployed === false` -> undeployed.
 *  4. When `is_deployed` is absent (older API), assume deployed.
 */
export declare function planKpiCubeEligibility(kpi: {
    value_measure_id?: string | null;
    kpi_type?: string | null;
    expression?: string | null;
    is_deployed?: boolean;
}, 
/**
 * The set of resolvable measure ids (the loaded, persona-scoped list).
 * When provided, a KPI whose value_measure_id is not in this set is
 * treated as composite -- mirroring the gateway's
 * `if single and single in measure_names` guard (mdx_execute.py:164).
 * When absent, the id-existence check is skipped.
 */
knownMeasureIds?: readonly string[], 
/**
 * The set of resolvable measure NAMES (technical names from the loaded
 * persona-scoped list). When provided, a single-measure expression
 * like `measure("deleted_name")` whose extracted name is NOT in this
 * set is treated as composite -- mirroring the gateway's
 * `if single and single in measure_names` guard for the expression
 * fallback path. When absent, the name-existence check is skipped.
 */
knownMeasureNames?: readonly string[]): KpiCubeEligibility;
