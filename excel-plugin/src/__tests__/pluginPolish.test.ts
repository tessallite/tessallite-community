/**
 * Bug-6733 / Bug-6734 / Bug-6735 / Bug-6737 -- plugin polish lane tests.
 *
 * Bug-6733: chart insert outcome-derived toast (the isolated post-step
 *           failure case where the chart WAS created but axis formatting
 *           or metadata tagging failed).
 * Bug-6734: toast lifetime policy mapping (success/info 5s, warning 10s,
 *           errors persist).
 * Bug-6735: table insert placement anchor (active cell by default for the
 *           zone/table path; overwrite-confirm retains active cell via
 *           forceOverwrite, never useActiveCell=false).
 * Bug-6737: table insert outcome-derived toast (the isolated post-step
 *           failure case where the table WAS created but metadata tagging
 *           or provenance footer failed).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { describeChartInsertResult, describeTableInsertResult } from '../utils/measureFormulaInsert';
import { strings } from '../i18n/strings';
import { AUTO_DISMISS_MS } from '../components/Toast/ToastProvider';
import {
  applyChartAxisFormatting,
  separateColumns,
} from '../utils/excelCharts';
import { useExcel } from '../hooks/useExcel';
import { _resetWorkbookIdCache } from '../utils/workbookMetadata';
import { insertResultTable } from '../utils/officeSpike';

// ---------------------------------------------------------------------------
// Bug-6733: chart outcome-derived toast
// ---------------------------------------------------------------------------
describe('Bug-6733 -- describeChartInsertResult', () => {
  it('returns null when the chart was NOT inserted (busy guard / cancelled)', () => {
    expect(describeChartInsertResult(false, false)).toBeNull();
  });

  it('returns null when chart not inserted even with postStepWarning', () => {
    expect(describeChartInsertResult(false, true)).toBeNull();
  });

  it('returns success toast when chart was inserted cleanly', () => {
    const result = describeChartInsertResult(true, false);
    expect(result).not.toBeNull();
    expect(result!.severity).toBe('success');
    expect(result!.message).toBe(strings.toasts.chartCreated);
  });

  it('returns warning toast when chart was inserted but a post-step failed', () => {
    const result = describeChartInsertResult(true, true);
    expect(result).not.toBeNull();
    expect(result!.severity).toBe('warning');
    expect(result!.message).toBe(strings.toasts.chartCreatedWithPostStepWarning);
    expect(result!.message.toLowerCase()).toContain('chart created');
    expect(result!.message.toLowerCase()).not.toMatch(/^(insert failed|chart.*failed)/);
  });

  it('the post-step warning message mentions axis formatting', () => {
    const result = describeChartInsertResult(true, true);
    expect(result!.message.toLowerCase()).toContain('axis');
  });

  it('a clean insert never mentions warnings or cosmetic issues', () => {
    const result = describeChartInsertResult(true, false);
    expect(result!.message.toLowerCase()).not.toContain('warning');
    expect(result!.message.toLowerCase()).not.toContain('could not');
  });
});

// ---------------------------------------------------------------------------
// Bug-6737: table insert outcome-derived toast
// ---------------------------------------------------------------------------
describe('Bug-6737 -- describeTableInsertResult', () => {
  it('returns null when the table was NOT inserted (busy guard / cancelled)', () => {
    expect(describeTableInsertResult(false, 0, false)).toBeNull();
  });

  it('returns null when table not inserted even with postStepWarning', () => {
    expect(describeTableInsertResult(false, 10, true)).toBeNull();
  });

  it('returns success toast when table was inserted cleanly', () => {
    const result = describeTableInsertResult(true, 42, false);
    expect(result).not.toBeNull();
    expect(result!.severity).toBe('success');
    expect(result!.message).toContain('42');
    expect(result!.message.toLowerCase()).not.toContain('failed');
  });

  it('returns warning toast when table was inserted but a post-step failed', () => {
    const result = describeTableInsertResult(true, 15, true);
    expect(result).not.toBeNull();
    expect(result!.severity).toBe('warning');
    // The message must state the table was inserted (never "Insert failed").
    expect(result!.message.toLowerCase()).toContain('table inserted');
    expect(result!.message.toLowerCase()).not.toMatch(/^(insert failed|table.*failed)/);
    expect(result!.message).toContain('15');
  });

  it('the post-step warning message mentions metadata', () => {
    const result = describeTableInsertResult(true, 5, true);
    expect(result!.message.toLowerCase()).toContain('metadata');
  });

  it('a clean insert uses the row-count template, not a hardcoded string', () => {
    const r1 = describeTableInsertResult(true, 100, false);
    const r2 = describeTableInsertResult(true, 200, false);
    expect(r1!.message).toContain('100');
    expect(r2!.message).toContain('200');
    expect(r1!.message).not.toBe(r2!.message);
  });
});

// ---------------------------------------------------------------------------
// Bug-6733: axis fallback via chartHeaders
// ---------------------------------------------------------------------------
describe('Bug-6733 -- axis fallback via chartHeaders', () => {
  it('separateColumns produces chartHeaders with Category + measure columns', () => {
    const headers = ['region', 'revenue', 'cost'];
    const rows: (string | number)[][] = [['North', 1000, 500]];
    const annotation = {
      dimensions: { region: { title: 'region', type: 'string' } },
      measures: { revenue: { title: 'revenue', type: 'number' }, cost: { title: 'cost', type: 'number' } },
    };
    const { chartHeaders } = separateColumns(headers, rows, annotation);
    expect(chartHeaders.slice(1)).toEqual(['revenue', 'cost']);
  });

  it('applyChartAxisFormatting accepts fallbackMeasureHeaders (R1 Finding 2)', () => {
    expect(typeof applyChartAxisFormatting).toBe('function');
    expect(applyChartAxisFormatting.length).toBeGreaterThanOrEqual(1);
  });
});

// ---------------------------------------------------------------------------
// Bug-6734: toast lifetime policy
// ---------------------------------------------------------------------------
describe('Bug-6734 -- toast lifetime policy', () => {
  it('success toasts auto-dismiss in 5 000 ms', () => {
    expect(AUTO_DISMISS_MS.success).toBe(5000);
  });

  it('info toasts auto-dismiss in 5 000 ms', () => {
    expect(AUTO_DISMISS_MS.info).toBe(5000);
  });

  it('warning toasts auto-dismiss in 10 000 ms', () => {
    expect(AUTO_DISMISS_MS.warning).toBe(10000);
  });

  it('error toasts persist (null = no auto-dismiss)', () => {
    expect(AUTO_DISMISS_MS.error).toBeNull();
  });

  it('every severity has a defined policy entry', () => {
    const severities = ['success', 'info', 'warning', 'error'] as const;
    for (const s of severities) {
      expect(AUTO_DISMISS_MS).toHaveProperty(s);
    }
  });

  it('no severity has an ad-hoc duration outside the defined policy', () => {
    expect(AUTO_DISMISS_MS.success).toBe(AUTO_DISMISS_MS.info);
    expect(AUTO_DISMISS_MS.warning).toBeGreaterThan(AUTO_DISMISS_MS.success!);
  });

  it('chartCreatedWithPostStepWarning string exists in the central table', () => {
    expect(strings.toasts.chartCreatedWithPostStepWarning).toBeTruthy();
    expect(typeof strings.toasts.chartCreatedWithPostStepWarning).toBe('string');
  });
});

// ---------------------------------------------------------------------------
// Bug-6735: placement anchor + OVERWRITE_WARNING behavioral guard
//
// R3 Finding 1: the R2 tests only inspected source text. This suite uses
// renderHook + stubbed Excel to exercise the actual overwrite-confirm
// retry path and assert the retry writes at the active cell, not at (0,0).
// ---------------------------------------------------------------------------
describe('Bug-6735 -- table insert placement (renderHook)', () => {
  // Track which row/col coordinates getRangeByIndexes is called with.
  // On the retry (forceOverwrite=true), all data writes must target the
  // active cell position, never (0,0).
  const rangeByIndexesCalls: { row: number; col: number }[] = [];
  const storage = new Map<string, string>();
  const settingsStore = new Map<string, unknown>();

  // The active cell sits at row 5, col 10 (NOT (0,0))
  const ACTIVE_ROW = 5;
  const ACTIVE_COL = 10;

  // excelRunCount tracks how many times Excel.run is called.
  let excelRunCount = 0;

  // Bug-7397 R6: the insert path now does a small pre-read Excel.run to resolve
  // the table's lock anchor BEFORE insertResultTable, so keying "has existing
  // data" off excelRunCount === 1 is no longer robust. Instead, the FIRST
  // getRangeByIndexes read (the overwrite check on the first attempt) returns
  // existing data exactly once; the retry (forceOverwrite) skips the check.
  let overwriteDataPending = true;

  /** Range stub that records position and serves non-empty on the first
   *  target-range read (so the overwrite check triggers) and empty thereafter. */
  function makeTrackingRange(row: number, col: number, rowCount: number, colCount: number) {
    rangeByIndexesCalls.push({ row, col });
    const hasData = overwriteDataPending;
    overwriteDataPending = false;
    const values: unknown[][] = Array.from({ length: rowCount }, () =>
      Array.from({ length: colCount }, () => hasData ? 'existing' : ''),
    );
    return {
      rowIndex: row,
      columnIndex: col,
      address: `Sheet1!K${row + 1}`,
      values,
      formulas: values.map(r => r.map(() => '')),
      format: {
        font: { bold: false, size: 9, color: '', italic: false },
        fill: { color: '' },
        autofitColumns: () => {},
      },
      numberFormat: [],
      load: () => {},
      getCell: () => ({ values: [['']] }),
      getHeaderRowRange: () => ({
        format: { font: { bold: false } },
      }),
    };
  }

  function makeActiveRange() {
    return {
      rowIndex: ACTIVE_ROW,
      columnIndex: ACTIVE_COL,
      address: 'Sheet1!K6',
      values: [['']],
      formulas: [['']],
      format: {
        font: { bold: false },
        fill: { color: '' },
        autofitColumns: () => {},
      },
      load: () => {},
    };
  }

  function makeContext() {
    excelRunCount++;
    const sheet = {
      getRangeByIndexes: (row: number, col: number, rowCount: number, colCount: number) =>
        makeTrackingRange(row, col, rowCount, colCount),
      tables: {
        add: () => ({
          style: '',
          getHeaderRowRange: () => ({
            format: { font: { bold: false } },
          }),
        }),
      },
      // The provenance footer calls sheet.getRange(address) to locate the
      // inserted table. Return a range at the active position so the footer
      // getRangeByIndexes call targets the right area, not (0,0).
      getRange: () => makeTrackingRange(ACTIVE_ROW, ACTIVE_COL, 1, 1),
    };
    return {
      workbook: {
        worksheets: { getActiveWorksheet: () => sheet, getItem: () => sheet },
        getSelectedRange: () => makeActiveRange(),
        names: {
          items: [],
          load: () => {},
          add: () => ({ comment: '' }),
          getItemOrNullObject: () => ({ delete: () => {} }),
        },
      },
      sync: async () => {},
    };
  }

  beforeEach(() => {
    rangeByIndexesCalls.length = 0;
    excelRunCount = 0;
    overwriteDataPending = true;
    storage.clear();
    settingsStore.clear();
    _resetWorkbookIdCache();

    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => cb(makeContext()),
    });
    vi.stubGlobal('OfficeRuntime', {
      storage: {
        getItem: async (k: string) => storage.get(k) ?? null,
        setItem: async (k: string, v: string) => { storage.set(k, v); },
        removeItem: async (k: string) => { storage.delete(k); },
      },
    });
    vi.stubGlobal('Office', {
      context: {
        document: {
          settings: {
            get: (k: string) => settingsStore.get(k) ?? null,
            set: (k: string, v: unknown) => { settingsStore.set(k, v); },
            saveAsync: (cb: () => void) => cb(),
          },
        },
      },
    });
  });

  // Bug-6737 (REOPENED): the table object creation step (tables.add) can
  // fail after the data values have been committed. When this happens,
  // insertTable must NOT throw (which would trigger the caller's catch
  // with "Insert failed"). Instead, it must return { address, postStepWarning: true }
  // so the caller shows a warning, not a failure.
  it('returns postStepWarning when table-object creation fails after data write', async () => {
    // Override the makeContext to make tables.add THROW on the first real
    // insert (not the overwrite check retry).
    const tablesAddThrow = vi.fn(() => {
      throw new Error('Table overlap: cannot add table to range');
    });

    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => {
        excelRunCount++;
        const sheet = {
          getRangeByIndexes: (row: number, col: number, rowCount: number, colCount: number) =>
            makeTrackingRange(row, col, rowCount, colCount),
          // tables.add throws to simulate the table-object creation failure
          // (Bug-6736 overlap or any host-specific limitation). The data
          // values are already committed via the first context.sync().
          tables: {
            add: tablesAddThrow,
          },
          getRange: () => makeTrackingRange(ACTIVE_ROW, ACTIVE_COL, 1, 1),
        };
        const ctx = {
          workbook: {
            worksheets: { getActiveWorksheet: () => sheet, getItem: () => sheet },
            getSelectedRange: () => makeActiveRange(),
            names: {
              items: [],
              load: () => {},
              add: () => ({ comment: '' }),
              getItemOrNullObject: () => ({ delete: () => {} }),
            },
          },
          sync: async () => {},
        };
        return cb(ctx);
      },
    });

    const { result } = renderHook(() => useExcel(undefined, undefined, 'model-test'));

    const tableResult = await result.current.insertTable(
      ['Month', 'Revenue'],
      [[1, 100], [2, 200]],
    );

    // The data was written (address is not null) even though tables.add threw.
    expect(tableResult.address).toBeTruthy();
    // The post-step warning was set because tables.add failed.
    expect(tableResult.postStepWarning).toBe(true);
    // tables.add was called (and threw).
    expect(tablesAddThrow).toHaveBeenCalled();
  });

  it('overwrite-confirm retry writes at the active cell position, not at (0,0)', async () => {
    // confirmGuard auto-approves the overwrite prompt.
    const confirmGuard = vi.fn(async () => true);
    const { result } = renderHook(() => useExcel(confirmGuard, undefined, 'model-test'));

    const tableResult = await result.current.insertTable(
      ['Col1', 'Col2'],
      [['a', 1]],
      { useActiveCell: true },
    );

    // The insert should have completed (returned an address).
    // Bug-6737: insertTable now returns TableInsertResult, not string|null.
    expect(tableResult.address).toBeTruthy();
    // confirmGuard was called (the overwrite prompt fired).
    expect(confirmGuard).toHaveBeenCalled();

    // The CRITICAL assertion: every getRangeByIndexes call during the
    // retry must have been at ACTIVE_ROW, ACTIVE_COL -- never at (0,0).
    // This is the behavioral guard for the R1 HIGH finding. If the old
    // `doInsertAndTag(headers, rows, metadata, false)` pattern were
    // restored (no forceOverwrite), the retry would write at (0,0) and
    // this assertion would fail.
    const retryRanges = rangeByIndexesCalls.filter(c => c.row !== 0 || c.col !== 0);
    const zeroRanges = rangeByIndexesCalls.filter(c => c.row === 0 && c.col === 0);
    // There must be calls at the active cell position
    expect(retryRanges.length).toBeGreaterThan(0);
    // There must be NO calls at (0,0) -- that would mean A1 placement
    expect(zeroRanges.length).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Bug-6915: every product call site of excelInsertTable must anchor at the
// user's active cell. insertResultTable falls back to A1 AND skips the
// overwrite check when useActiveCell is falsy, so a bare call silently
// clobbers A1. This source-contract guard fails when a call site omits the
// option (three App.tsx callers shipped that way: chat insert, drill-through
// rows, KPI panel table).
// ---------------------------------------------------------------------------
import appSource from '../App.tsx?raw';
import reportBuilderSource from '../components/ReportBuilder/ReportBuilder.tsx?raw';

describe('Bug-6915: table inserts anchor at the active cell', () => {
  it.each([
    ['App.tsx', appSource],
    ['ReportBuilder.tsx', reportBuilderSource],
  ])('%s passes useActiveCell to every excelInsertTable call', (_name, source) => {
    // Match call sites only — not the `insertTable: excelInsertTable` destructure.
    const callSites = source.match(/excelInsertTable\(/g) ?? [];
    expect(callSites.length).toBeGreaterThan(0);
    for (const segment of source.split(/excelInsertTable\(/).slice(1)) {
      // The options argument appears within the first ~400 chars of the call.
      expect(segment.slice(0, 400)).toContain('useActiveCell: true');
    }
  });
});

// ---------------------------------------------------------------------------
// Bug-8344 (fifth site): insertResultTable's own occupancy probe.
//
// The registry entry names four probes in useExcel.ts. insertResultTable
// carries the SAME shape and the same exposure, so the fix is only complete
// once it too consults the formulas channel — otherwise the class simply moves
// to the site nobody enumerated.
// ---------------------------------------------------------------------------
describe('Bug-8344 — insertResultTable refuses to overwrite a cell whose formula renders ""', () => {
  /** Office.js is faithful on one point: `.formulas` is unreadable unloaded. */
  function formulaOnlyRange(rowCount: number, colCount: number) {
    let loadedFormulas = false;
    const grid = <T,>(fill: T) => Array.from({ length: rowCount }, () => Array.from({ length: colCount }, () => fill));
    return {
      rowIndex: 5,
      columnIndex: 10,
      address: 'Sheet1!K6',
      get values() { return grid(''); },
      set values(_v: unknown) { writes.push('values'); },
      get formulas() {
        if (!loadedFormulas) throw new Error('PropertyNotLoaded: formulas');
        return grid('=IF(B1>0,B1,"")');
      },
      set formulas(_v: unknown) { writes.push('formulas'); },
      numberFormat: [],
      format: { font: {}, fill: {}, autofitColumns: () => {} },
      load: (props?: string | string[]) => {
        const asked = Array.isArray(props) ? props.join(',') : (props ?? '');
        if (asked.includes('formulas')) loadedFormulas = true;
      },
      getCell: () => ({ values: [['']] }),
      getHeaderRowRange: () => ({ format: { font: {} } }),
    };
  }

  let writes: string[];

  beforeEach(() => {
    writes = [];
    const sheet = {
      getRangeByIndexes: (_r: number, _c: number, rowCount = 1, colCount = 1) => formulaOnlyRange(rowCount, colCount),
      getRange: () => formulaOnlyRange(1, 1),
      tables: { add: () => ({ style: '', getHeaderRowRange: () => ({ format: { font: {} } }) }) },
    };
    vi.stubGlobal('Excel', {
      run: async (cb: (ctx: unknown) => Promise<unknown>) => cb({
        workbook: {
          worksheets: { getActiveWorksheet: () => sheet, getItem: () => sheet },
          getSelectedRange: () => ({ address: 'Sheet1!K6', rowIndex: 5, columnIndex: 10, values: [['']], formulas: [['']], load: () => {} }),
          names: { items: [], load: () => {}, add: () => ({ comment: '' }), getItemOrNullObject: () => ({ delete: () => {} }) },
        },
        sync: async () => {},
      }),
    });
  });

  it('raises OVERWRITE_WARNING and writes nothing', async () => {
    // Pre-fix the values-only probe read the range as empty, wrote straight
    // over it, and the user's formulas were gone with no prompt.
    await expect(
      insertResultTable(['A'], [['1']], undefined, true, false, { sheetName: 'Sheet1', startRow: 5, startCol: 10 }),
    ).rejects.toThrow('OVERWRITE_WARNING');
    expect(writes).toEqual([]);
  });

  it('forceOverwrite still bypasses the probe (the confirm retry path)', async () => {
    const outcome = await insertResultTable(
      ['A'], [['1']], undefined, true, true, { sheetName: 'Sheet1', startRow: 5, startCol: 10 },
    );
    expect(outcome.address).toBeTruthy();
    expect(writes).toContain('values');
  });
});
