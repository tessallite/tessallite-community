/**
 * KPI scorecard payload construction.
 *
 * F-025-10: the KPI tab's "insert all as scorecard" previously hardcoded
 * null value/goal measure names, leaving the Value/Goal columns blank. The
 * scorecard CUBEVALUE cells bind by the value/goal MEASURE's technical name, so
 * each KPI's `value_measure_id`/`goal_measure_id` must be resolved to a name,
 * and a static-target KPI (no goal measure) must carry its numeric target so
 * the Goal column is populated.
 *
 * F-025-08: the KPI member emitted into status/trend CUBEKPIMEMBER cells must be
 * the technical `kpi.name` (the gateway publishes KPI_NAME = kpi.name), never
 * the display name.
 */
import type { Kpi, Measure } from '../types/tessallite';

export interface ScorecardKpi {
  id: string;
  name: string;
  display_name: string | null;
  valueMeasureName: string | null;
  goalMeasureName: string | null;
  goalLiteral?: number | null;
  updated_at?: string;
}

/**
 * Build the scorecard insert payload for one KPI given a measure id->name map.
 */
export function buildScorecardKpi(kpi: Kpi, measuresById: Map<string, Measure>): ScorecardKpi {
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
  };
}

/**
 * Build the scorecard payload for a list of KPIs, dropping deprecated ones.
 */
export function buildScorecardPayload(kpis: Kpi[], measures: Measure[]): ScorecardKpi[] {
  const byId = new Map(measures.map(m => [m.id, m]));
  return kpis
    .filter(k => k.certification_status !== 'deprecated')
    .map(k => buildScorecardKpi(k, byId));
}
