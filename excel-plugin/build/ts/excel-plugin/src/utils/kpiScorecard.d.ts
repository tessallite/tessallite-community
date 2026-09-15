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
import { type KpiCubeEligibility } from './measureFormulaInsert';
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
export declare const SCORECARD_HEADERS: readonly ["KPI", "Value", "Goal", "Status"];
export declare function buildLiteralScorecardRows(kpis: readonly EvaluatedScorecardKpi[]): (string | number | null)[][];
export declare function neutraliseLabelForFormulaChannel(label: string): string;
export declare function buildFormulaScorecardRows(kpis: readonly EvaluatedScorecardKpi[], modelSlug: string): string[][];
/**
 * Build the scorecard insert payload for one KPI given a measure id->name map.
 */
export declare function buildScorecardKpi(kpi: Kpi, measuresById: Map<string, Measure>, knownMeasureIds?: readonly string[], knownMeasureNames?: readonly string[]): ScorecardKpi;
/**
 * Build the scorecard payload for a list of KPIs, dropping deprecated ones.
 * Threads both measure ids and names for the eligibility check.
 */
export declare function buildScorecardPayload(kpis: Kpi[], measures: Measure[]): ScorecardKpi[];
