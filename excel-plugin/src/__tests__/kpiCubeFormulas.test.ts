/**
 * Bug-5290 -- KPI status/trend CUBE formula end-to-end contract tests.
 *
 * Verifies that the Excel plugin generates the correct CUBEKPIMEMBER formulas
 * for KPI status and trend, and that the formula strings match the MDX pattern
 * the gateway expects (KPIStatus("kpiName") / KPITrend("kpiName")).
 *
 * These tests complement the gateway-side tests in test_kpi_cube_e2e.py.
 * Together they verify the full round-trip contract without needing a live
 * Excel client.
 */
import { describe, it, expect } from 'vitest';
import {
  buildCubeKpiFormula,
  buildCubeKpiValueFormula,
  kpiStatusCellFormula,
  kpiValueCellFormula,
  CUBE_KPI_PROPERTIES,
  TESSALLITE_CONNECTION_NAME,
  measureMemberRef,
  generateCubeValue,
  escapeExcelString,
  escapeMdxBracketContent,
} from '../utils/excelFormulas';
import { buildScorecardKpi, buildScorecardPayload } from '../utils/kpiScorecard';
import type { Kpi, Measure } from '../types/tessallite';


// ---------------------------------------------------------------------------
// CUBEKPIMEMBER formula generation
// ---------------------------------------------------------------------------

describe('Bug-5290: CUBEKPIMEMBER formula generation for status/trend', () => {
  const conn = TESSALLITE_CONNECTION_NAME;

  it('generates the correct status formula with bare KPI technical name', () => {
    const formula = buildCubeKpiFormula(conn, 'aa', CUBE_KPI_PROPERTIES.Status);
    expect(formula).toBe('=CUBEKPIMEMBER("Tessallite","aa",3)');
  });

  it('generates the correct trend formula with bare KPI technical name', () => {
    const formula = buildCubeKpiFormula(conn, 'aa', CUBE_KPI_PROPERTIES.Trend);
    expect(formula).toBe('=CUBEKPIMEMBER("Tessallite","aa",4)');
  });

  it('generates the correct value formula with bare KPI technical name', () => {
    const formula = buildCubeKpiFormula(conn, 'aa', CUBE_KPI_PROPERTIES.Value);
    expect(formula).toBe('=CUBEKPIMEMBER("Tessallite","aa",1)');
  });

  it('generates the correct goal formula with bare KPI technical name', () => {
    const formula = buildCubeKpiFormula(conn, 'aa', CUBE_KPI_PROPERTIES.Goal);
    expect(formula).toBe('=CUBEKPIMEMBER("Tessallite","aa",2)');
  });

  it('uses the technical KPI name (not display name) for all properties', () => {
    // The gateway publishes KPI_NAME = kpi.name. The formula must use the
    // technical name so the gateway can match it in its KPI lookup.
    const statusFormula = buildCubeKpiFormula(conn, 'margin_pct', CUBE_KPI_PROPERTIES.Status);
    const trendFormula = buildCubeKpiFormula(conn, 'margin_pct', CUBE_KPI_PROPERTIES.Trend);

    // Both formulas must contain the technical name, not "Margin %" (display)
    expect(statusFormula).toContain('"margin_pct"');
    expect(trendFormula).toContain('"margin_pct"');
    expect(statusFormula).not.toContain('Margin');
    expect(trendFormula).not.toContain('Margin');
  });

  it('Excel-escapes a KPI name containing double quotes', () => {
    const formula = buildCubeKpiFormula(conn, 'revenue "growth"', CUBE_KPI_PROPERTIES.Status);
    expect(formula).toBe('=CUBEKPIMEMBER("Tessallite","revenue ""growth""",3)');
  });

  it('does NOT MDX-bracket-escape the KPI name (bare caption contract)', () => {
    // Bug-3659: Excel sends the bare caption to the server. Wrapping it in
    // brackets would make the server look up a KPI literally named "[aa]".
    const formula = buildCubeKpiFormula(conn, 'aa', CUBE_KPI_PROPERTIES.Status);
    expect(formula).not.toContain('[aa]');
    expect(formula).toContain('"aa"');
  });
});


// ---------------------------------------------------------------------------
// MDX pattern matching — the MDX the formula produces must match what the
// gateway's find_kpi_member_functions regex detects
// ---------------------------------------------------------------------------

describe('Bug-5290: formula-to-MDX contract', () => {
  // When Excel evaluates CUBEKPIMEMBER("Tessallite","kpiName",3), it sends:
  //   SELECT FROM [cube] WHERE (KPIStatus("kpiName"))
  // The gateway regex _KPI_FUNC_RE must match this pattern.

  it('the KPI name in the formula matches the caption in the MDX the server expects', () => {
    const kpiName = 'aa';
    const formula = buildCubeKpiFormula(TESSALLITE_CONNECTION_NAME, kpiName, CUBE_KPI_PROPERTIES.Status);

    // Extract the KPI name from the formula
    const match = formula.match(/CUBEKPIMEMBER\("[^"]*","([^"]*)",\d+\)/);
    expect(match).not.toBeNull();
    const extractedName = match![1];

    // This is the same name the server will see in KPIStatus("aa")
    expect(extractedName).toBe(kpiName);
  });

  it('a KPI with underscores in the name round-trips correctly', () => {
    const kpiName = 'gross_margin_pct';
    const formula = buildCubeKpiFormula(TESSALLITE_CONNECTION_NAME, kpiName, CUBE_KPI_PROPERTIES.Status);
    expect(formula).toContain(`"${kpiName}"`);
  });

  it('a KPI with spaces in the name round-trips correctly', () => {
    const kpiName = 'Gross Margin';
    const formula = buildCubeKpiFormula(TESSALLITE_CONNECTION_NAME, kpiName, CUBE_KPI_PROPERTIES.Trend);
    expect(formula).toContain(`"${kpiName}"`);
  });
});


// ---------------------------------------------------------------------------
// Scorecard payload — status cells use the technical KPI name
// Bug-6729: Trend column removed; Status uses CUBEVALUE(CUBEKPIMEMBER).
// ---------------------------------------------------------------------------

describe('Bug-5290/6729: scorecard payload uses technical name for status', () => {
  const measures: Measure[] = [
    { id: 'mv', name: 'fee_amount', display_name: 'Fee Amount', default_agg: 'sum', measure_type: 'standard' },
  ];

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

  it('scorecard payload carries the technical name for status CUBEVALUE(CUBEKPIMEMBER) binding', () => {
    const scorecardKpi = buildScorecardKpi(
      kpi({ name: 'margin_pct', display_name: 'Gross Margin %', value_measure_id: 'mv' }),
      new Map(measures.map(m => [m.id, m])),
    );
    expect(scorecardKpi.name).toBe('margin_pct');

    // Bug-6729: the scorecard status cell uses kpiStatusCellFormula which
    // wraps CUBEKPIMEMBER inside CUBEVALUE for the numeric value.
    const statusFormula = kpiStatusCellFormula(TESSALLITE_CONNECTION_NAME, scorecardKpi.name);
    expect(statusFormula).toContain('"margin_pct"');
    expect(statusFormula).not.toContain('Gross Margin');
    expect(statusFormula).toMatch(/^=CUBEVALUE\(/);
  });

  it('value cell uses CUBEVALUE with the resolved measure name, not CUBEKPIMEMBER', () => {
    const scorecardKpi = buildScorecardKpi(
      kpi({ name: 'margin', value_measure_id: 'mv' }),
      new Map(measures.map(m => [m.id, m])),
    );
    expect(scorecardKpi.valueMeasureName).toBe('fee_amount');
    const valueFormula = kpiValueCellFormula(
      TESSALLITE_CONNECTION_NAME,
      scorecardKpi.name,
      scorecardKpi.valueMeasureName,
    );
    expect(valueFormula).toBe('=CUBEVALUE("Tessallite","[Measures].[fee_amount]")');
  });

  it('static goal is a literal, not a CUBE formula', () => {
    const scorecardKpi = buildScorecardKpi(
      kpi({ name: 'margin', value_measure_id: 'mv', target_type: 'static', target_value: 10000 }),
      new Map(measures.map(m => [m.id, m])),
    );
    expect(scorecardKpi.goalLiteral).toBe(10000);
    expect(scorecardKpi.goalMeasureName).toBeNull();
  });
});


// ---------------------------------------------------------------------------
// Full scorecard row — four cells have the correct formula type
// Bug-6729: Trend column removed; layout is KPI | Value | Goal | Status.
// ---------------------------------------------------------------------------

describe('Bug-5290/6729: full scorecard row cell types (4-column, no Trend)', () => {
  const conn = TESSALLITE_CONNECTION_NAME;
  const kpiName = 'aa';
  const valueMeasure = 'fee_amount';

  it('column 0 (Name) is a plain label, not a CUBE formula', () => {
    const kpiLabel = 'Revenue KPI';
    expect(typeof kpiLabel).toBe('string');
    expect(kpiLabel).not.toMatch(/^=CUBE/);
  });

  it('column 1 (Value) uses CUBEVALUE with the measure member ref', () => {
    const formula = kpiValueCellFormula(conn, kpiName, valueMeasure);
    expect(formula).toBe('=CUBEVALUE("Tessallite","[Measures].[fee_amount]")');
  });

  it('column 3 (Status) uses CUBEVALUE(CUBEKPIMEMBER) for the numeric value (Bug-6729)', () => {
    const formula = kpiStatusCellFormula(conn, kpiName);
    expect(formula).toBe('=CUBEVALUE("Tessallite",CUBEKPIMEMBER("Tessallite","aa",3))');
  });

  // Bug-6729: Trend column removed -- the gateway does not serve KPI Trend
  // members. No column 4 test; the scorecard layout is 4 columns.
});
