/**
 * KPI scorecard payload construction.
 *
 * F-025-10: the KPI tab's "insert all as scorecard" originally emitted CUBE
 * formulas, so the payload still resolves value/goal measure names for the
 * explicit advanced formula paths. The default scorecard insert is now
 * connectionless: it writes plugin-evaluated literal values from the KPI batch
 * API so it does not depend on a workbook connection named "Tessallite".
 *
 * F-025-08: the KPI member emitted into status/trend CUBEKPIMEMBER cells must be
 * the technical `kpi.name` (the gateway publishes KPI_NAME = kpi.name), never
 * the display name.
 */
import type { Kpi, Measure } from '../types/tessallite';
import { planKpiCubeEligibility, type KpiCubeEligibility } from './measureFormulaInsert';

export interface ScorecardKpi {
  id: string;
  name: string;
  display_name: string | null;
  valueMeasureName: string | null;
  goalMeasureName: string | null;
  goalLiteral?: number | null;
  updated_at?: string;
  /**
   * Bug-6728: the XMLA eligibility of this KPI. Composite-expression KPIs
   * and undeployed KPIs cannot resolve CUBE formulas, so the scorecard
   * insert must route them to literal values or warnings.
   */
  cubeEligibility: KpiCubeEligibility;
}

export interface EvaluatedScorecardKpi extends ScorecardKpi {
  evaluatedValue?: number | null;
  evaluatedGoal?: number | null;
  evaluatedStatus?: number | null;
}

export const SCORECARD_HEADERS = ['KPI', 'Value', 'Goal', 'Status'] as const;

export function buildLiteralScorecardRows(
  kpis: readonly EvaluatedScorecardKpi[],
): (string | number | null)[][] {
  return kpis.map(kpi => [
    kpi.display_name || kpi.name,
    kpi.evaluatedValue ?? null,
    kpi.evaluatedGoal ?? kpi.goalLiteral ?? null,
    kpi.evaluatedStatus ?? null,
  ]);
}

function escapeFormulaString(s: string): string {
  return s.replace(/"/g, '""');
}

/**
 * Bug-7393 (adversarial R3): the formula-mode scorecard writes ALL four columns
 * of each row through the Excel FORMULA channel (`range.formulas`, useExcel.ts).
 * Columns 1-3 are `=TESSALLITE.KPI(...)` calls whose interpolated name is
 * quote-escaped and stays INSIDE a string literal. Column 0 is the KPI's raw
 * display label — model-author metadata that a modeller (lower privilege than
 * the analyst inserting the scorecard) or a malicious model import controls. If
 * that label begins with a formula trigger (`=`, `+`, `-`, `@`, or a leading
 * tab/CR/LF), the formula channel would parse it as a LIVE formula and execute
 * it on the analyst's machine (CWE-1236, same class as the static-insert path).
 *
 * Neutralise it with the OWASP apostrophe prefix (mirrors the frontend
 * `csvSafeCell` policy): a leading `'` makes Excel treat the cell as literal
 * text while the displayed label stays correct. Only the label is guarded; the
 * `=TESSALLITE.KPI(...)` formula cells are intentional formulas.
 */
const FORMULA_TRIGGERS = ['=', '+', '-', '@', '\t', '\r', '\n'];

export function neutraliseLabelForFormulaChannel(label: string): string {
  if (label.length > 0 && FORMULA_TRIGGERS.includes(label[0])) {
    return "'" + label;
  }
  return label;
}

export function buildFormulaScorecardRows(
  kpis: readonly EvaluatedScorecardKpi[],
  modelSlug: string,
): string[][] {
  const m = escapeFormulaString(modelSlug);
  return kpis.map(kpi => {
    const label = kpi.display_name || kpi.name;
    const kpiName = escapeFormulaString(label);
    return [
      // Bug-7393 (R3): the label is written to the formula channel, so a
      // formula-leading label must be neutralised before it can execute.
      neutraliseLabelForFormulaChannel(label),
      `=TESSALLITE.KPI("${m}","${kpiName}","value")`,
      `=TESSALLITE.KPI("${m}","${kpiName}","goal")`,
      `=TESSALLITE.KPI("${m}","${kpiName}","status")`,
    ];
  });
}

/**
 * Build the scorecard insert payload for one KPI given a measure id->name map.
 */
export function buildScorecardKpi(
  kpi: Kpi,
  measuresById: Map<string, Measure>,
  knownMeasureIds?: readonly string[],
  knownMeasureNames?: readonly string[],
): ScorecardKpi {
  const valueMeasureName = kpi.value_measure_id
    ? measuresById.get(kpi.value_measure_id)?.name ?? null
    : null;
  const goalMeasureName = kpi.goal_measure_id
    ? measuresById.get(kpi.goal_measure_id)?.name ?? null
    : null;
  const goalLiteral = (kpi.goal_measure_id == null && kpi.target_type === 'static')
    ? kpi.target_value
    : null;
  return {
    id: kpi.id,
    name: kpi.name,
    display_name: kpi.display_name,
    valueMeasureName,
    goalMeasureName,
    goalLiteral,
    updated_at: kpi.updated_at,
    cubeEligibility: planKpiCubeEligibility(kpi, knownMeasureIds, knownMeasureNames),
  };
}

/**
 * Build the scorecard payload for a list of KPIs, dropping deprecated ones.
 * Threads both measure ids and names for the eligibility check.
 */
export function buildScorecardPayload(kpis: Kpi[], measures: Measure[]): ScorecardKpi[] {
  const byId = new Map(measures.map(m => [m.id, m]));
  const measureIds = measures.map(m => m.id);
  const measureNames = measures.map(m => m.name);
  return kpis
    .filter(k => k.certification_status !== 'deprecated')
    .map(k => buildScorecardKpi(k, byId, measureIds, measureNames));
}
