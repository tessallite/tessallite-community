/**
 * Bug-6697 / Bug-6709 — post-insert feedback for direct CUBE-formula inserts
 * (the sigma "Insert as CUBEVALUE formula" path and the KPI insert modes).
 *
 * Office.js exposes no API to detect or create a workbook's OLAP connection
 * (F-025-18/21; see `useExcelConnections.ts`), so a direct CUBEVALUE insert
 * can never confirm the formula will actually resolve. The previous code
 * showed a plain "CUBEVALUE formula inserted" success toast regardless,
 * leaving the user to discover an unexplained #N/A on their own -- while the
 * (+) "Add to Values" path (a real REST query via the plugin protocol) always
 * works. `CubeFormulaWizard` already carries this exact warning
 * (`connectionHintWithName`) before the user commits to inserting there; this
 * module gives the one-click sigma path the same warning, every time, since
 * connection health is permanently unknowable from the add-in sandbox.
 *
 * Pure and side-effect-free so the outcome (and, crucially, that it is never
 * a bare "success" that hides the risk) is unit-testable without mounting
 * Report Builder's full hook tree.
 */
import { strings, templates } from '../i18n/strings';

export interface ToastCall {
  message: string;
  severity: 'success' | 'error' | 'info' | 'warning';
}

// ---------------------------------------------------------------------------
// Bug-6737: table insert outcome-derived toast
// ---------------------------------------------------------------------------

/**
 * Bug-6737: decide the toast from the ACTUAL table insert outcome -- the
 * same pattern Bug-6733 introduced for chart inserts. The table write
 * (insertResultTable) is separated from post-insert steps (metadata
 * tagging, provenance footer), so a post-step failure after a successful
 * write shows a warning (stating the table was inserted) rather than a
 * false "Insert failed".
 */
export function describeTableInsertResult(
  tableInserted: boolean,
  rowCount: number,
  postStepWarning: boolean,
  // Bug-7397 R8-3: the insert FAILED CLOSED (no write) and must surface a
  // "try again" message rather than silently doing nothing.
  blocked?: boolean,
): ToastCall | null {
  if (!tableInserted) {
    if (blocked) {
      return { severity: 'warning', message: strings.toasts.insertTableBusy };
    }
    return null;
  }
  if (postStepWarning) {
    return {
      severity: 'warning',
      message: templates.toasts.tableInsertedWithPostStepWarning(rowCount),
    };
  }
  return {
    severity: 'success',
    message: templates.toasts.insertedRows(rowCount),
  };
}

// ---------------------------------------------------------------------------
// Bug-6733: chart insert outcome-derived toast
// ---------------------------------------------------------------------------

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
export function describeChartInsertResult(
  chartInserted: boolean,
  postStepWarning: boolean,
): ToastCall | null {
  if (!chartInserted) return null;
  if (postStepWarning) {
    return {
      severity: 'warning',
      message: strings.toasts.chartCreatedWithPostStepWarning,
    };
  }
  return {
    severity: 'success',
    message: strings.toasts.chartCreated,
  };
}

/**
 * Decide the toast to show after `insertMeasureAsFormula` resolves.
 * `result` is the inserted cell address, or `null` when nothing was written
 * (the busy-guard declined the operation, or the user cancelled an overwrite
 * confirmation) -- in both cases there is nothing to report.
 */
export function describeMeasureFormulaInsertResult(
  result: string | null,
  connectionName: string,
): ToastCall | null {
  if (!result) return null;
  return {
    severity: 'warning',
    message: templates.toasts.cubeValueFormulaInsertedNeedsConnection(connectionName),
  };
}

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
export type KpiInsertAction =
  | 'full_row'            // label | value | goal | status row (Bug-6729: no Trend)
  | 'measure_value_cell'  // single CUBEVALUE cell on the value measure
  | 'kpi_value_formula'   // single CUBEVALUE(CUBEKPIMEMBER Value) cell on the KPI itself
  | 'value_goal_rows'     // label row + value row + goal row
  | 'status_cell';        // single CUBEVALUE(CUBEKPIMEMBER Status) cell
  // Bug-6729: 'trend_only' removed -- the gateway does not serve KPI Trend
  // members; the KpiCard menu entry no longer offers it. An unknown mode
  // (including 'trend_only' from a stale caller) returns null -> error toast.

export function planKpiInsertAction(
  mode: string,
  hasValueMeasure: boolean,
): KpiInsertAction | null {
  switch (mode) {
    case 'full_row':
    case 'kpi_card':
      return 'full_row';
    case 'value_only':
      return hasValueMeasure ? 'measure_value_cell' : 'kpi_value_formula';
    case 'value_goal':
      return 'value_goal_rows';
    case 'status_only':
      return 'status_cell';
    case 'formula_ref':
      return 'kpi_value_formula';
    default:
      return null;
  }
}

/**
 * Bug-6721: the reroute reason is threaded from `planKpiZoneAdd` so the
 * post-insert toast states the TRUE cause -- a measure-less custom/expression
 * KPI vs a measure-backed KPI whose value measure is deleted or persona-hidden
 * (Bug-6719) need different explanations; telling a modeller the latter "is a
 * custom-expression KPI" sends them debugging the wrong thing.
 */
export type KpiFormulaRouteReason = 'no_measure' | 'unresolvable_measure';

export function describeKpiFormulaInsertResult(
  result: string | null,
  mode: string,
  connectionName: string,
  routedKpi?: { name: string; reason: KpiFormulaRouteReason } | null,
): ToastCall | null {
  if (!result) return null;
  if (routedKpi) {
    return {
      severity: 'warning',
      message: routedKpi.reason === 'unresolvable_measure'
        ? templates.toasts.kpiUnresolvableMeasureRoutedToFormulaInserted(routedKpi.name, connectionName)
        : templates.toasts.customKpiRoutedToFormulaInserted(routedKpi.name, connectionName),
    };
  }
  return {
    severity: 'warning',
    message: templates.toasts.kpiModeInsertedNeedsConnection(mode, connectionName),
  };
}

// ---------------------------------------------------------------------------
// Bug-6728: KPI CUBE-formula eligibility routing
// ---------------------------------------------------------------------------

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
export type KpiCubeEligibility =
  | 'cube_eligible'           // measure-backed + deployed: CUBE formulas resolve
  | 'composite_expression'    // composite: gateway advertises no executable member
  | 'undeployed';             // measure-backed but not deployed: #N/A until deploy

/**
 * The set of kpi_type values that represent a single-measure-backed KPI in
 * the producer domain (shared/db/models.py:765). These have a value_measure_id
 * and their XMLA Value member is [Measures].[<that measure>].
 *
 * The producer domain is:
 *   simple_measure | ratio | variance | growth_rate | moving_window | composite
 *
 * Only 'simple_measure' is directly measure-backed. The others (ratio,
 * variance, growth_rate, moving_window, composite) are expression-based and
 * MIGHT or might not have a `value_measure_id`. Migration 0116 backfills
 * existing legacy KPIs as 'simple_measure'.
 *
 * Additionally, the gateway (Bug-6702) serves a single-measure expression
 * KPI (one whose expression is exactly `measure("X")`) as if it were
 * measure-backed — via `_kpi_single_measure_from_expression`. The plugin
 * mirrors this by checking the `expression` field when `value_measure_id`
 * is absent.
 */
const MEASURE_BACKED_KPI_TYPES = new Set(['simple_measure']);

/**
 * Mirror the gateway's `_kpi_single_measure_from_expression` (mdx_execute.py)
 * exactly: strip whitespace, peel balanced wrapping parens one layer at a
 * time, then fullmatch `measure("X")` or `measure('X')` with consistent
 * quote style. Returns the extracted measure NAME, or null when the
 * expression is not a single bare measure reference. Conservative:
 * unbalanced parens, mixed quotes, or any extra content -> null (composite,
 * literal path, never a broken formula).
 */
function extractSingleMeasureName(expression: string): string | null {
  let text = expression.trim();
  if (!text) return null;
  // Peel one layer of balanced wrapping parens at a time.
  while (text.startsWith('(') && text.endsWith(')')) {
    const inner = text.slice(1, -1).trim();
    if (!inner) break;
    text = inner;
  }
  // Try double-quoted: measure("name")
  const dq = /^measure\(\s*"([^"]+)"\s*\)$/i;
  let m = dq.exec(text);
  if (m) return m[1];
  // Try single-quoted: measure('name')
  const sq = /^measure\(\s*'([^']+)'\s*\)$/i;
  m = sq.exec(text);
  return m ? m[1] : null;
}

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
export function planKpiCubeEligibility(
  kpi: {
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
  knownMeasureNames?: readonly string[],
): KpiCubeEligibility {
  let eligible = false;

  if (kpi.kpi_type != null) {
    // kpi_type is authoritative when present.
    if (MEASURE_BACKED_KPI_TYPES.has(kpi.kpi_type)) {
      // Verify the value measure is resolvable when a measure list is
      // provided (mirror the gateway's measure-existence check).
      eligible = !knownMeasureIds ||
        Boolean(kpi.value_measure_id && knownMeasureIds.includes(kpi.value_measure_id));
    } else {
      // Expression-based type -- extract the single measure name and
      // verify it exists in the loaded measure set.
      eligible = _expressionIsEligible(kpi.expression, knownMeasureNames);
    }
  } else {
    // Legacy row (no kpi_type) -- use value_measure_id or expression.
    const measureResolvable = Boolean(kpi.value_measure_id) &&
      (!knownMeasureIds || knownMeasureIds.includes(kpi.value_measure_id!));
    eligible = measureResolvable ||
      _expressionIsEligible(kpi.expression, knownMeasureNames);
  }

  if (!eligible) return 'composite_expression';

  if (kpi.is_deployed === false) return 'undeployed';

  return 'cube_eligible';
}

/** Check if the expression is a single-measure expression whose measure exists. */
function _expressionIsEligible(
  expression: string | null | undefined,
  knownMeasureNames?: readonly string[],
): boolean {
  if (!expression) return false;
  const name = extractSingleMeasureName(expression);
  if (!name) return false;
  // When a names list is provided, verify the extracted name exists
  // (mirror mdx_execute.py:164: `if single and single in measure_names`).
  if (knownMeasureNames && !knownMeasureNames.includes(name)) return false;
  return true;
}
