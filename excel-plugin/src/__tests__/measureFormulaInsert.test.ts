/**
 * Bug-6697 — the sigma "Insert as CUBEVALUE formula" path must never leave
 * the user to silently discover an unexplained #N/A. Office.js has no API to
 * detect or create the workbook's OLAP connection (F-025-18/21), so the
 * insert can never confirm the formula will resolve; the previous code
 * showed a bare "CUBEVALUE formula inserted" success toast that hid this.
 */
import { describe, it, expect } from 'vitest';
import { describeMeasureFormulaInsertResult, describeKpiFormulaInsertResult, planKpiInsertAction, planKpiCubeEligibility } from '../utils/measureFormulaInsert';
import { kpiValueCellFormula, kpiStatusCellFormula, buildCubeKpiValueFormula, CUBE_KPI_PROPERTIES } from '../utils/excelFormulas';

describe('describeMeasureFormulaInsertResult', () => {
  it('returns null when nothing was inserted (busy-guard declined or overwrite cancelled)', () => {
    expect(describeMeasureFormulaInsertResult(null, 'Tessallite')).toBeNull();
  });

  it('warns -- never a bare success -- when the formula was inserted, naming the required connection', () => {
    const outcome = describeMeasureFormulaInsertResult('Sheet1!A1', 'Tessallite');
    expect(outcome).not.toBeNull();
    expect(outcome!.severity).toBe('warning');
    expect(outcome!.message).toContain('Tessallite');
    expect(outcome!.message).toMatch(/#N\/A|resolve/i);
  });

  it('names whichever connection the caller configured, not a hardcoded literal', () => {
    const outcome = describeMeasureFormulaInsertResult('Sheet1!A1', 'CustomConnName');
    expect(outcome!.message).toContain('CustomConnName');
  });
});

/**
 * Bug-6709 — KPI inserts (all modes are CUBEVALUE/CUBEKPIMEMBER-based) must
 * follow the same contract as the sigma path: no toast when nothing was
 * inserted, and never a bare success afterwards. The Bug-6701 "Add KPI"
 * reroute additionally must explain itself only AFTER a completed insert --
 * the previous code announced the reroute before the insert ran, so a
 * declined/cancelled insert still toasted as if something had happened.
 */
describe('describeKpiFormulaInsertResult', () => {
  it('returns null when nothing was inserted -- including for the routed custom-KPI path', () => {
    expect(describeKpiFormulaInsertResult(null, 'formula_ref', 'Tessallite')).toBeNull();
    expect(describeKpiFormulaInsertResult(null, 'formula_ref', 'Tessallite', { name: 'Net Margin', reason: 'no_measure' })).toBeNull();
  });

  it('warns -- never a bare success -- after a completed insert, naming the mode and the required connection', () => {
    const outcome = describeKpiFormulaInsertResult('Sheet1!B2', 'full_row', 'Tessallite');
    expect(outcome).not.toBeNull();
    expect(outcome!.severity).toBe('warning');
    // Bug-6716: human phrasing, not the internal mode enum.
    expect(outcome!.message).toContain('Full KPI row inserted');
    expect(outcome!.message).toContain('Tessallite');
    expect(outcome!.message).toMatch(/resolve/i);
  });

  it('uses a human label for every published mode, never leaking internal enum tokens (Bug-6716)', () => {
    // Bug-6729: trend_only removed -- no longer a published mode.
    const modes = ['full_row', 'value_only', 'value_goal', 'status_only', 'kpi_card', 'formula_ref'];
    for (const mode of modes) {
      const outcome = describeKpiFormulaInsertResult('Sheet1!B2', mode, 'Tessallite');
      expect(outcome, mode).not.toBeNull();
      // No raw enum token and no underscore may appear in user copy.
      expect(outcome!.message, mode).not.toContain('_');
      expect(outcome!.message, mode).not.toContain(mode);
    }
  });

  it('explains the custom-KPI reroute (why a formula, not a pivot value) AND the connection requirement in one post-insert toast', () => {
    const outcome = describeKpiFormulaInsertResult('Sheet1!B2', 'formula_ref', 'Tessallite', { name: 'Net Margin', reason: 'no_measure' });
    expect(outcome).not.toBeNull();
    expect(outcome!.severity).toBe('warning');
    expect(outcome!.message).toContain('Net Margin');
    expect(outcome!.message).toMatch(/custom-expression KPI/i);
    expect(outcome!.message).toMatch(/cannot be added as a pivot value/i);
    expect(outcome!.message).toContain('Tessallite');
    expect(outcome!.message).toMatch(/resolve/i);
  });

  // Bug-6721: a measure-backed KPI whose value measure is deleted or
  // persona-hidden (Bug-6719) must state THAT reason -- calling it a
  // "custom-expression KPI" would send the user debugging the KPI type
  // instead of the missing measure.
  it('states the unresolvable-measure reason for the Bug-6719 edge, never the custom-KPI wording', () => {
    const outcome = describeKpiFormulaInsertResult('Sheet1!B2', 'formula_ref', 'Tessallite', { name: 'Net Margin', reason: 'unresolvable_measure' });
    expect(outcome).not.toBeNull();
    expect(outcome!.severity).toBe('warning');
    expect(outcome!.message).toContain('Net Margin');
    expect(outcome!.message).toMatch(/value measure is not available/i);
    expect(outcome!.message).toMatch(/deleted or hidden/i);
    expect(outcome!.message).not.toMatch(/custom-expression/i);
    expect(outcome!.message).toContain('Tessallite');
    expect(outcome!.message).toMatch(/resolve/i);
  });
});

/**
 * Bug-6714 — the NEVER-SILENT guard for KPI insert dispatch. The old
 * ReportBuilder switch skipped 'value_only' entirely for a custom/expression
 * KPI (`if (valueMeasure)`), the exact Bug-6701 silent-no-op class. The
 * planner must be TOTAL: every published mode, for BOTH KPI shapes, resolves
 * to a concrete insert action; only an unknown mode returns null, which the
 * caller surfaces as an error toast.
 */
describe('planKpiInsertAction (never-silent guard)', () => {
  // Bug-6729: trend_only removed -- the menu entry no longer exists.
  const PUBLISHED_MODES = [
    'full_row', 'value_only', 'value_goal', 'status_only', 'kpi_card', 'formula_ref',
  ];

  it('resolves EVERY published mode to an action for a measure-backed KPI', () => {
    for (const mode of PUBLISHED_MODES) {
      expect(planKpiInsertAction(mode, true), mode).not.toBeNull();
    }
  });

  it('resolves EVERY published mode to an action for a custom/expression KPI (no value measure)', () => {
    for (const mode of PUBLISHED_MODES) {
      expect(planKpiInsertAction(mode, false), mode).not.toBeNull();
    }
  });

  it('routes measure-less value_only to the KPI-native value formula instead of skipping it', () => {
    expect(planKpiInsertAction('value_only', false)).toBe('kpi_value_formula');
    expect(planKpiInsertAction('value_only', true)).toBe('measure_value_cell');
  });

  it('keeps formula_ref on the KPI-native path for both shapes', () => {
    expect(planKpiInsertAction('formula_ref', true)).toBe('kpi_value_formula');
    expect(planKpiInsertAction('formula_ref', false)).toBe('kpi_value_formula');
  });

  it('returns null for an unknown mode (the caller must toast an error)', () => {
    expect(planKpiInsertAction('no_such_mode', true)).toBeNull();
    expect(planKpiInsertAction('', false)).toBeNull();
  });

  // Bug-6729: trend_only is no longer a published mode (menu entry removed).
  // A stale caller sending 'trend_only' gets null -> error toast, which is
  // safer than the previous trend_unavailable -> warning toast on a dead
  // control (Bug-6701/6714 class).
  it('returns null for the removed trend_only mode (Bug-6729)', () => {
    expect(planKpiInsertAction('trend_only', true)).toBeNull();
    expect(planKpiInsertAction('trend_only', false)).toBeNull();
  });
});

/**
 * Bug-6714 / Bug-6729 -- the Value cell of every multi-cell KPI insert
 * must never be empty AND must use the CUBEVALUE wrap for numeric output.
 */
describe('kpiValueCellFormula', () => {
  it('uses CUBEVALUE on the value measure when one exists', () => {
    expect(kpiValueCellFormula('Tessallite', 'net_margin_kpi', 'net_sales'))
      .toBe('=CUBEVALUE("Tessallite","[Measures].[net_sales]")');
  });

  it('falls back to CUBEVALUE(CUBEKPIMEMBER Value) for a custom KPI with no value measure (Bug-6729)', () => {
    // Bug-6729: a bare CUBEKPIMEMBER shows the member CAPTION text, not the
    // number. The wrapped formula resolves to the numeric value.
    expect(kpiValueCellFormula('Tessallite', 'net_margin_kpi', null))
      .toBe('=CUBEVALUE("Tessallite",CUBEKPIMEMBER("Tessallite","net_margin_kpi",1))');
  });
});

/**
 * Bug-6729 -- the CUBEVALUE wrap for all KPI property formulas. A bare
 * CUBEKPIMEMBER shows the member CAPTION; the CUBEVALUE wrapper extracts
 * the numeric value Excel (and icon-set conditional formats) need.
 */
describe('buildCubeKpiValueFormula (Bug-6729 CUBEVALUE wrap)', () => {
  it('wraps Value (property 1) in CUBEVALUE', () => {
    const f = buildCubeKpiValueFormula('Tessallite', 'Shipping Cost', CUBE_KPI_PROPERTIES.Value);
    expect(f).toBe('=CUBEVALUE("Tessallite",CUBEKPIMEMBER("Tessallite","Shipping Cost",1))');
  });

  it('wraps Status (property 3) in CUBEVALUE', () => {
    const f = buildCubeKpiValueFormula('Tessallite', 'Shipping Cost', CUBE_KPI_PROPERTIES.Status);
    expect(f).toBe('=CUBEVALUE("Tessallite",CUBEKPIMEMBER("Tessallite","Shipping Cost",3))');
  });

  it('escapes connection name and KPI caption with embedded quotes', () => {
    const f = buildCubeKpiValueFormula('My "Conn"', 'Revenue "Growth"', CUBE_KPI_PROPERTIES.Value);
    expect(f).toBe('=CUBEVALUE("My ""Conn""",CUBEKPIMEMBER("My ""Conn""","Revenue ""Growth""",1))');
  });
});

/**
 * Bug-6729 -- kpiStatusCellFormula wraps Status in CUBEVALUE so the
 * icon-set conditional format receives the numeric -1/0/1, not the caption.
 */
describe('kpiStatusCellFormula (Bug-6729)', () => {
  it('produces CUBEVALUE(CUBEKPIMEMBER(...,3))', () => {
    expect(kpiStatusCellFormula('Tessallite', 'Shipping Cost'))
      .toBe('=CUBEVALUE("Tessallite",CUBEKPIMEMBER("Tessallite","Shipping Cost",3))');
  });
});

/**
 * Bug-6728 -- planKpiCubeEligibility routing. The XMLA surface cannot serve
 * CUBE formulas for composite-expression or undeployed KPIs.
 */
/**
 * Bug-6728 -- planKpiCubeEligibility routing, using REAL producer domain
 * values from shared/db/models.py:765:
 *   simple_measure | ratio | variance | growth_rate | moving_window | composite
 *
 * Migration 0116 backfills kpi_type='simple_measure' onto all v1 KPIs.
 * The gateway additionally serves single-measure expressions via
 * _kpi_single_measure_from_expression (Bug-6702).
 */
describe('planKpiCubeEligibility (Bug-6728)', () => {
  it('returns cube_eligible for a deployed simple_measure KPI', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: 'uuid-1',
      kpi_type: 'simple_measure',
      is_deployed: true,
    })).toBe('cube_eligible');
  });

  it('returns cube_eligible when kpi_type is null (v1 legacy, value_measure_id set)', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: 'uuid-1',
      kpi_type: null,
      is_deployed: true,
    })).toBe('cube_eligible');
  });

  it('returns composite_expression for a composite kpi_type', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: null,
      kpi_type: 'composite',
      is_deployed: true,
    })).toBe('composite_expression');
  });

  it('returns composite_expression for a ratio kpi_type even with a value_measure_id', () => {
    // A ratio KPI has a multi-term expression; value_measure_id may point
    // at the numerator but the gateway serves the expression result, not
    // the numerator measure.
    expect(planKpiCubeEligibility({
      value_measure_id: 'uuid-1',
      kpi_type: 'ratio',
      is_deployed: true,
    })).toBe('composite_expression');
  });

  it('returns composite_expression for variance/growth_rate/moving_window kpi_types', () => {
    for (const t of ['variance', 'growth_rate', 'moving_window']) {
      expect(planKpiCubeEligibility({
        value_measure_id: null,
        kpi_type: t,
        is_deployed: true,
      }), t).toBe('composite_expression');
    }
  });

  it('returns cube_eligible for a single-measure expression (gateway _kpi_single_measure_from_expression)', () => {
    // The gateway serves measure("net_amount") as [Measures].[net_amount].
    expect(planKpiCubeEligibility({
      value_measure_id: null,
      kpi_type: 'composite',
      expression: 'measure("net_amount")',
      is_deployed: true,
    })).toBe('cube_eligible');
  });

  it('returns cube_eligible for a single-measure expression with surrounding parens/whitespace', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: null,
      expression: "  ( measure('fee_amount') )  ",
      is_deployed: true,
    })).toBe('cube_eligible');
  });

  it('returns composite_expression for a multi-measure expression', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: null,
      kpi_type: 'composite',
      expression: 'safe_div(measure("revenue"), measure("cost"))',
      is_deployed: true,
    })).toBe('composite_expression');
  });

  it('returns undeployed for a deployed=false simple_measure KPI', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: 'uuid-1',
      kpi_type: 'simple_measure',
      is_deployed: false,
    })).toBe('undeployed');
  });

  it('returns undeployed for a deployed=false single-measure expression KPI', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: null,
      expression: 'measure("net_amount")',
      is_deployed: false,
    })).toBe('undeployed');
  });

  it('defaults to cube_eligible when is_deployed is undefined (older API)', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: 'uuid-1',
      kpi_type: 'simple_measure',
    })).toBe('cube_eligible');
  });

  it('defaults to composite_expression when both value_measure_id and kpi_type are absent', () => {
    expect(planKpiCubeEligibility({})).toBe('composite_expression');
  });

  // Tightened regex: conservative rejection of edge cases that the gateway
  // would also reject. composite_expression -> literal path, never a broken
  // CUBE formula.
  it('rejects unbalanced parens (conservative -> composite)', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: null,
      kpi_type: 'composite',
      expression: '(measure("x")',
      is_deployed: true,
    })).toBe('composite_expression');
  });

  it('rejects mixed quotes (conservative -> composite)', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: null,
      kpi_type: 'composite',
      expression: "measure(\"x')",
      is_deployed: true,
    })).toBe('composite_expression');
  });

  it('rejects extra content after the measure call', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: null,
      kpi_type: 'composite',
      expression: 'measure("x") + 1',
      is_deployed: true,
    })).toBe('composite_expression');
  });

  it('ratio with value_measure_id but non-measure kpi_type routes to expression fallback', () => {
    // A ratio KPI with a single-measure expression is still eligible.
    expect(planKpiCubeEligibility({
      value_measure_id: 'uuid-1',
      kpi_type: 'ratio',
      expression: 'measure("net_amount")',
      is_deployed: true,
    })).toBe('cube_eligible');
  });

  // R3 Finding 2: measure-existence check mirrors gateway's
  // `if single and single in measure_names` guard.
  it('returns composite_expression when value_measure_id is not in the known list', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: 'deleted-uuid',
      kpi_type: 'simple_measure',
      is_deployed: true,
    }, ['other-uuid'])).toBe('composite_expression');
  });

  it('returns cube_eligible when value_measure_id IS in the known list', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: 'uuid-1',
      kpi_type: 'simple_measure',
      is_deployed: true,
    }, ['uuid-1', 'uuid-2'])).toBe('cube_eligible');
  });

  it('skips measure-existence check when knownMeasureIds is not provided', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: 'any-uuid',
      kpi_type: 'simple_measure',
      is_deployed: true,
    })).toBe('cube_eligible');
  });

  // R4 Finding 2: expression-fallback must also verify the extracted measure
  // name exists in the loaded set (mirrors gateway mdx_execute.py:164).
  it('returns composite_expression for measure("deleted_name") when name is not in the known names list', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: null,
      kpi_type: 'composite',
      expression: 'measure("deleted_measure")',
      is_deployed: true,
    }, undefined, ['net_amount', 'fee_amount'])).toBe('composite_expression');
  });

  it('returns cube_eligible for measure("net_amount") when name IS in the known names list', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: null,
      kpi_type: 'composite',
      expression: 'measure("net_amount")',
      is_deployed: true,
    }, undefined, ['net_amount', 'fee_amount'])).toBe('cube_eligible');
  });

  it('skips name-existence check when knownMeasureNames is not provided', () => {
    expect(planKpiCubeEligibility({
      value_measure_id: null,
      kpi_type: 'composite',
      expression: 'measure("anything")',
      is_deployed: true,
    })).toBe('cube_eligible');
  });
});
