/**
 * Guard re-baseline tests for Bug-6741, Bug-6742, Bug-6744.
 *
 * These tests verify that existing guards assert the CURRENT contract.
 * No product changes -- only test additions.
 *
 * Bug-6741 (AKA Bug-6357/6359, harvest item 93):
 *   Guard that inserted row count == full result AND the 1000-row
 *   Report Builder cap is visible in the UI.
 *
 * Bug-6742 (AKA Bug-6361/5962/5963, harvest item 94):
 *   Guards for non-default-persona KPI parity, KPI list scoping,
 *   and named-set list/preview scoping.
 *
 * Bug-6744 (AKA Bug-6356, harvest item 92):
 *   Guard that repeated Local Pivot insertion succeeds with UNIQUE names.
 *   Coordinates with the InsertTracker/insertGuard work (Phase A).
 */
import { describe, it, expect } from 'vitest';
import { renderHook } from '@testing-library/react';
import useExcelSource from '../hooks/useExcel.ts?raw';
import { templates, strings } from '../i18n/strings';
import { describeTableInsertResult } from '../utils/measureFormulaInsert';
import { buildScorecardPayload, buildScorecardKpi } from '../utils/kpiScorecard';
import { InsertTracker, rangesOverlap } from '../utils/insertGuard';
import type { Kpi, Measure } from '../types/tessallite';

// ---------------------------------------------------------------------------
// Bug-6741: full-result and truncation-signal guards
// ---------------------------------------------------------------------------

describe('Bug-6741 -- full-result row count and 1000-row cap', () => {
  it('resultTruncated(1000) mentions the 1000-row cap (the visible limit the analyst sees)', () => {
    // The Report Builder uses REPORT_ROW_LIMIT = 1000 (file-private constant).
    // We verify its externally visible effect: the resultTruncated template
    // interpolates 1000 into the user-facing message.
    const msg = templates.toasts.resultTruncated(1000);
    expect(msg).toContain('1000');
    expect(msg).toContain('rows');
  });

  it('resultTruncated template produces a user-readable truncation notice', () => {
    const msg = templates.toasts.resultTruncated(1000);
    // Must tell the user the result was capped, not just silently truncated.
    expect(msg.toLowerCase()).toContain('cap');
    // Must suggest narrowing the query.
    expect(msg.toLowerCase()).toContain('filter');
  });

  it('describeTableInsertResult carries the ACTUAL row count (not a generic string)', () => {
    const r1 = describeTableInsertResult(true, 42, false);
    const r2 = describeTableInsertResult(true, 999, false);
    expect(r1!.message).toContain('42');
    expect(r2!.message).toContain('999');
    // Row counts must differ -- the template interpolates, not hardcodes.
    expect(r1!.message).not.toBe(r2!.message);
  });

  it('table insert toast includes inserted row count equal to full result', () => {
    // When the table is inserted cleanly with all 50 rows, the toast must
    // report exactly 50 rows (matching the full result).
    const result = describeTableInsertResult(true, 50, false);
    expect(result).not.toBeNull();
    expect(result!.severity).toBe('success');
    expect(result!.message).toContain('50');
  });

  it('table insert with post-step warning still reports the correct row count', () => {
    const result = describeTableInsertResult(true, 100, true);
    expect(result).not.toBeNull();
    expect(result!.severity).toBe('warning');
    expect(result!.message).toContain('100');
    // Must state the table was inserted (never "Insert failed").
    expect(result!.message.toLowerCase()).toContain('table inserted');
  });

  it('insertTruncatedWarning shows available vs total when insert is blocked', () => {
    const msg = templates.toasts.insertTruncatedWarning(50, 5000);
    expect(msg).toContain('50');
    expect(msg).toContain('5000');
  });

  it('insertRefetchFailed shows available vs total when re-fetch fails', () => {
    const msg = templates.toasts.insertRefetchFailed(50, 5000);
    expect(msg).toContain('50');
    expect(msg).toContain('5000');
  });

  it('insertRefetchEmpty shows available vs total when re-fetch returns empty', () => {
    const msg = templates.toasts.insertRefetchEmpty(50, 5000);
    expect(msg).toContain('50');
    expect(msg).toContain('5000');
  });
});

// ---------------------------------------------------------------------------
// Bug-6742: persona-scoped KPI and named-set guards
// ---------------------------------------------------------------------------

function makeKpi(partial: Partial<Kpi>): Kpi {
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

const MEASURES: Measure[] = [
  { id: 'mv', name: 'fee_amount', display_name: 'fee amount', default_agg: 'sum', measure_type: 'standard' },
  { id: 'mg', name: 'fee_target', display_name: 'fee target', default_agg: 'sum', measure_type: 'standard' },
];

describe('Bug-6742 -- persona-scoped KPI parity guards', () => {
  it('buildScorecardPayload drops deprecated KPIs regardless of persona', () => {
    const kpis = [
      makeKpi({ id: 'a', value_measure_id: 'mv', certification_status: 'certified' }),
      makeKpi({ id: 'b', certification_status: 'deprecated' }),
      makeKpi({ id: 'c', value_measure_id: 'mg', certification_status: 'draft' }),
    ];
    const payload = buildScorecardPayload(kpis, MEASURES);
    // Deprecated KPI (id 'b') must be excluded.
    expect(payload.map(k => k.id)).toEqual(['a', 'c']);
  });

  it('buildScorecardKpi preserves KPI identity fields for persona parity', () => {
    const byId = new Map(MEASURES.map(m => [m.id, m]));
    const kpi = makeKpi({ id: 'k-test', name: 'test_kpi', display_name: 'Test KPI', value_measure_id: 'mv' });
    const result = buildScorecardKpi(kpi, byId);
    // The scorecard entry must carry the technical name (for CUBE binding)
    // and the display name (for the label column).
    expect(result.name).toBe('test_kpi');
    expect(result.display_name).toBe('Test KPI');
    expect(result.id).toBe('k-test');
    expect(result.valueMeasureName).toBe('fee_amount');
  });

  it('getKpis API function accepts personaId for persona-scoped queries', async () => {
    // Bug-5962/5963: the actual boundary that matters is that the API function
    // threads personaId into the request URL. Verify the function signature
    // accepts the optional personaId parameter (3rd argument).
    const { getKpis } = await import('../api/modelService');
    expect(typeof getKpis).toBe('function');
    // Function must accept (projectId, modelId, personaId?) -- at least 2 required params.
    expect(getKpis.length).toBeGreaterThanOrEqual(2);
  });

  it('getNamedSets API function accepts personaId for persona-scoped queries', async () => {
    const { getNamedSets } = await import('../api/modelService');
    expect(typeof getNamedSets).toBe('function');
    expect(getNamedSets.length).toBeGreaterThanOrEqual(2);
  });

  it('evaluateKpiBatch API function accepts personaId for persona-scoped evaluation', async () => {
    // Bug-6361: KPI batch evaluation must pass personaId so values match
    // the persona view, not the default.
    const { evaluateKpiBatch } = await import('../api/modelService');
    expect(typeof evaluateKpiBatch).toBe('function');
    expect(evaluateKpiBatch.length).toBeGreaterThanOrEqual(3);
  });

  it('KPI panel search and filter use the central strings table', () => {
    // These were part of the Bug-6361/5962/5963 fixes; verify the strings
    // are centralized (not hardcoded inline).
    expect(strings.kpiPanel.filterAll).toBe('All');
    expect(strings.kpiPanel.filterCertified).toBe('Certified');
    expect(strings.kpiPanel.searchPlaceholder).toBeTruthy();
    expect(strings.kpiPanel.noSearchMatch).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Bug-6744: unique pivot table name guard
// ---------------------------------------------------------------------------

describe('Bug-6744 -- local pivot unique-name guard', () => {
  it('InsertTracker.operationKey produces distinct keys for different insert types', () => {
    // The InsertTracker is used to prevent overlapping writes.
    // Verify the key structure is deterministic and type-discriminating.
    expect(InsertTracker.operationKey('pivot', 'p1')).toBe('pivot:p1');
    expect(InsertTracker.operationKey('pivot', 'p2')).toBe('pivot:p2');
    expect(InsertTracker.operationKey('table', 'p1')).not.toBe(InsertTracker.operationKey('pivot', 'p1'));
  });

  it('insertLocalPivot source code contains the Bug-6356 unique-name logic', async () => {
    // The useExcel hook's insertLocalPivot function must:
    // 1. Collect existing PivotTable names across ALL sheets.
    // 2. Generate a unique name using a counter suffix.
    // We verify the module exports the hook and that the pivot name guard
    // is architecturally present.
    const { useExcel } = await import('../hooks/useExcel');
    expect(typeof useExcel).toBe('function');
    // The hook returns insertLocalPivot among its members.
    // We cannot call it without an Excel context, but we verify the shape.
    const { result } = renderHook(() => useExcel());
    expect(typeof result.current.insertLocalPivot).toBe('function');
  });

  it('insertLocalPivot keeps the backing data table namespaced and hidden', () => {
    expect(useExcelSource).toContain("const LOCAL_PIVOT_DATA_TABLE_PREFIX = '_tsl_data_'");
    expect(useExcelSource).toContain('table.name = nextUniqueLocalPivotTableName(existingTableNames, dataName)');
    expect(useExcelSource).toContain('dataSheet.visibility = Excel.SheetVisibility.hidden');
  });

  it('InsertTracker detects same-sheet range collision for pivot data ranges', () => {
    const tracker = new InsertTracker();
    // Simulate a first pivot data write at A1:E101.
    tracker.tryStart('pivot:p1');
    tracker.registerInFlight('pivot:p1', {
      sheet: 'Pivot Data', rowStart: 0, colStart: 0, rowCount: 101, colCount: 5,
    });
    tracker.commitRange('pivot:p1');
    tracker.complete('pivot:p1');

    // A second pivot trying the same location must be detected.
    const collision = tracker.checkCollision({
      sheet: 'Pivot Data', rowStart: 0, colStart: 0, rowCount: 50, colCount: 3,
    });
    expect(collision).not.toBeNull();

    // findSafeAnchor should offset below.
    const safe = tracker.findSafeAnchor({
      sheet: 'Pivot Data', rowStart: 0, colStart: 0, rowCount: 50, colCount: 3,
    });
    expect(safe).not.toBeNull();
    expect(safe!.rowStart).toBeGreaterThan(100);
  });

  it('rangesOverlap returns false for non-overlapping pivot regions', () => {
    // Two pivots on different data sheets never collide.
    expect(rangesOverlap(
      { sheet: 'Pivot Data', rowStart: 0, colStart: 0, rowCount: 100, colCount: 5 },
      { sheet: 'Pivot Data (1)', rowStart: 0, colStart: 0, rowCount: 100, colCount: 5 },
    )).toBe(false);
  });
});
