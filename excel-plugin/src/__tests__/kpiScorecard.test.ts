/**
 * F-025-10 / F-025-08 — KPI scorecard payload.
 *
 * The scorecard's Value/Goal cells are CUBEVALUE over the KPI's value/goal
 * MEASURES, so the payload must resolve measure ids to technical names. A KPI
 * with a static target (no goal measure) must carry the numeric target so the
 * Goal column is not blank. The status/trend cells use `kpi.name` (the gateway
 * publishes KPI_NAME = kpi.name, verified live: modely KPI 'aa').
 */
import { describe, it, expect } from 'vitest';
import { buildScorecardKpi, buildScorecardPayload } from '../utils/kpiScorecard';
import type { Kpi, Measure } from '../types/tessallite';

const measures: Measure[] = [
  { id: 'mv', name: 'fee_amount', display_name: 'fee amount', default_agg: 'sum', measure_type: 'standard' },
  { id: 'mg', name: 'fee_target', display_name: 'fee target', default_agg: 'sum', measure_type: 'standard' },
];
const byId = new Map(measures.map(m => [m.id, m]));

function kpi(partial: Partial<Kpi>): Kpi {
  return {
    id: 'k1', name: 'margin', display_name: 'Profit Margin', description: null,
    display_folder: null, value_measure_id: null, goal_measure_id: null,
    target_type: null, target_value: null,
    status_expression: null, trend_expression: null,
    status_graphic: 'Traffic Light', trend_graphic: 'Standard Arrow',
    weight: null, parent_kpi_id: null, certification_status: 'draft',
    replacement_id: null, owner_user_id: null, updated_at: '2026-01-01T00:00:00Z',
    ...partial,
  };
}

describe('buildScorecardKpi', () => {
  it('resolves value and goal measure ids to technical names', () => {
    const out = buildScorecardKpi(kpi({ value_measure_id: 'mv', goal_measure_id: 'mg' }), byId);
    expect(out.valueMeasureName).toBe('fee_amount');
    expect(out.goalMeasureName).toBe('fee_target');
    expect(out.goalLiteral).toBeNull();
  });

  it('emits the technical KPI name (not the display name) so status/trend bind', () => {
    const out = buildScorecardKpi(kpi({ name: 'margin', display_name: 'Profit Margin', value_measure_id: 'mv' }), byId);
    // F-025-08: the gateway publishes KPI_NAME = kpi.name. The payload carries
    // the technical name; useExcel emits CUBEKPIMEMBER over it.
    expect(out.name).toBe('margin');
  });

  it('carries the static target literal when a KPI has no goal measure (F-025-10)', () => {
    // mirrors live KPI 'aa': target_type=static, target_value=10000, no goal measure
    const out = buildScorecardKpi(kpi({ value_measure_id: 'mv', target_type: 'static', target_value: 10000 }), byId);
    expect(out.goalMeasureName).toBeNull();
    expect(out.goalLiteral).toBe(10000);
  });

  it('does NOT fabricate a goal literal for a measure-target KPI', () => {
    const out = buildScorecardKpi(kpi({ value_measure_id: 'mv', goal_measure_id: 'mg', target_type: 'measure' }), byId);
    expect(out.goalLiteral).toBeNull();
  });

  it('produces the correct contract for the individual-insert path when goal is a static literal (Bug-5294)', () => {
    // Bug-5294 lock-in: insertKpiFormulas consumes buildScorecardKpi's output.
    // When goal_measure_id is null and target_type is 'static', the insert path
    // writes goalLiteral directly into the Goal cell (not a CUBEVALUE formula).
    // This test guards the contract: goalMeasureName must be null (so the
    // formula branch is skipped) and goalLiteral must carry the numeric value.
    const out = buildScorecardKpi(
      kpi({ value_measure_id: 'mv', goal_measure_id: null, target_type: 'static', target_value: 42000 }),
      byId,
    );
    expect(out.goalMeasureName).toBeNull();
    expect(out.goalLiteral).toBe(42000);
    // valueMeasureName must still resolve normally
    expect(out.valueMeasureName).toBe('fee_amount');
  });

  it('leaves measure names null when the id is unknown (graceful, not blank-by-default)', () => {
    const out = buildScorecardKpi(kpi({ value_measure_id: 'missing' }), byId);
    expect(out.valueMeasureName).toBeNull();
  });
});

describe('buildScorecardPayload', () => {
  it('drops deprecated KPIs and resolves the rest', () => {
    const list = [
      kpi({ id: 'a', value_measure_id: 'mv', certification_status: 'certified' }),
      kpi({ id: 'b', certification_status: 'deprecated' }),
    ];
    const out = buildScorecardPayload(list, measures);
    expect(out.map(k => k.id)).toEqual(['a']);
    expect(out[0].valueMeasureName).toBe('fee_amount');
  });
});
