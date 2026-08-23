/// <reference types="office-js" />

/**
 * Office.js compatibility spike.
 * Phase 0: runCompatibilitySpike() validates platform support.
 *          Must be executed manually inside an Excel host.
 */

import { rangeHasContent, rangesOverlap, type CellRange } from './insertGuard';

export interface CompatibilityMatrix {
  host: string;
  platform: string;
  insertTable: boolean | string;
  insertChart: boolean | string;
  localPivotTable: boolean | string;
  cubeFormulas: boolean | string;
  createXmlaConnection: boolean | string;
  readSelectedCubeFormula: boolean | string;
  readActiveCell: boolean | string;
  getActiveCellAddress: boolean | string;
  detectWorkbookConnections: boolean | string;
  notes: string;
}

const defaultMatrix: Omit<CompatibilityMatrix, 'host' | 'platform'> = {
  insertTable: 'untested',
  insertChart: 'untested',
  localPivotTable: 'untested',
  cubeFormulas: 'untested',
  createXmlaConnection: 'untested',
  readSelectedCubeFormula: 'untested',
  readActiveCell: 'untested',
  getActiveCellAddress: 'untested',
  detectWorkbookConnections: 'untested',
  notes: '',
};

export function detectHost(): { host: string; platform: string } {
  const host = String(Office.context?.host || 'unknown');
  const platform = String(Office.context?.platform || 'unknown');
  return { host, platform };
}

/**
 * Bug-6737 (REOPENED): the table insert has TWO commit points inside a single
 * Excel.run. The first sync commits cell data + formatting; the second sync
 * commits the Excel table object (filter dropdowns, alternating row colors,
 * header styling). If the second sync fails (e.g., the range overlaps an
 * existing table object -- Bug-6736), the data is already visible but the
 * function threw, propagating as "Insert failed" to the caller.
 *
 * Fix: return { address, tableObjectFailed } so the caller knows the data
 * was written even if the table object step failed. The table object is
 * cosmetic (filter dropdowns, alternating rows); the data values are the
 * user's primary concern.
 */
export interface InsertResultTableOutcome {
  address: string | null;
  tableObjectFailed: boolean;
}

/**
 * Bug-7397 R6: a PINNED write target. When supplied, insertResultTable writes
 * to exactly this sheet + start cell and NEVER re-reads the selection or the
 * active worksheet. The caller (doInsertAndTag) resolves this target and the
 * table lock key from ONE host sample, so the location that is locked is the
 * location that is written -- closing the two-sample TOCTOU the deep-review
 * reproduced (a selection move, or a concurrent chart/pivot insert activating
 * a different sheet, between the anchor pre-read and the write).
 */
export interface PinnedInsertTarget {
  sheetName: string;
  startRow: number;
  startCol: number;
}

export async function insertResultTable(
  headers: string[],
  rows: (string | number)[][],
  formatTokens?: Record<string, string>,
  useActiveCell?: boolean,
  /**
   * Bug-6735 / R1 Finding 1: when the user confirms the overwrite prompt,
   * the retry must position at the same active cell WITHOUT re-checking
   * for existing data (which would throw OVERWRITE_WARNING again in an
   * infinite loop). `forceOverwrite` = true means "use active cell, skip
   * the data check". Previously the retry passed `useActiveCell=false`,
   * which silently relocated the table to (0,0) / A1.
   */
  forceOverwrite?: boolean,
  // Bug-7397 R6: the pinned write location (see PinnedInsertTarget). Required
  // for the exclusion guarantee; the legacy self-resolving path is kept only
  // for the Excel-undefined / no-target degraded case.
  pinnedTarget?: PinnedInsertTarget,
): Promise<InsertResultTableOutcome> {
  let savedAddress: string | null = null;
  let tableObjectFailed = false;

  await Excel.run(async (context) => {
    // Bug-7397 R6: target the PINNED sheet by name (not getActiveWorksheet), so
    // a concurrent op that changes the active sheet after the caller resolved
    // the lock key cannot redirect this write to a different sheet than the
    // one the lock protects.
    const sheet = pinnedTarget
      ? context.workbook.worksheets.getItem(pinnedTarget.sheetName)
      : context.workbook.worksheets.getActiveWorksheet();

    let startRow = pinnedTarget ? pinnedTarget.startRow : 0;
    let startCol = pinnedTarget ? pinnedTarget.startCol : 0;

    if (useActiveCell || forceOverwrite) {
      if (!pinnedTarget) {
        // Legacy self-resolving path (no pinned target available).
        const activeRange = context.workbook.getSelectedRange();
        activeRange.load(['rowIndex', 'columnIndex', 'values']);
        await context.sync();
        startRow = activeRange.rowIndex;
        startCol = activeRange.columnIndex;
      }

      if (!forceOverwrite) {
        const targetRange = sheet.getRangeByIndexes(startRow, startCol, rows.length + 1, headers.length);
        // Bug-8344: the fifth site of the same occupancy class. A values-only
        // probe reads a cell holding `=IF(B1>0,B1,"")` as empty and drops the
        // OVERWRITE_WARNING, destroying the formula. Both channels, through the
        // one shared helper — the reason the helper exists rather than a
        // per-site `.some()`.
        targetRange.load('values,formulas');
        await context.sync();

        const hasExistingData = rangeHasContent(
          targetRange.values as unknown[][], targetRange.formulas as unknown[][],
        );

        if (hasExistingData) {
          throw new Error('OVERWRITE_WARNING');
        }
      }
    }

    const colCount = headers.length;
    const range = sheet.getRangeByIndexes(startRow, startCol, rows.length + 1, colCount);
    const allData = [headers, ...rows];
    range.values = allData;
    range.format.autofitColumns();
    range.load('address');

    if (formatTokens) {
      for (let col = 0; col < colCount; col++) {
        const header = headers[col];
        const formatToken = formatTokens[header];
        if (formatToken) {
          const dataRange = sheet.getRangeByIndexes(startRow + 1, startCol + col, rows.length, 1);
          dataRange.numberFormat = Array.from({ length: rows.length }, () => [formatToExcelFormat(formatToken)]);
        }
      }
    }

    // Phase 1: commit cell data + number formatting (critical).
    await context.sync();
    savedAddress = range.address;

    // Phase 2: create the Excel table object (non-critical cosmetic step).
    // Bug-6737 (REOPENED): if tables.add fails (e.g., overlapping an
    // existing table -- Bug-6736, or any host-specific limitation), the
    // data values are already committed. The table object adds filter
    // dropdowns and alternating row styling but is not required for the
    // data to be correct and usable.
    //
    // Bug-6736: detect and remove any existing table object overlapping the
    // target range before calling tables.add, so a re-insert onto the same
    // region cleanly replaces the previous table instead of leaving a
    // half-mutated sheet. sheet.tables.items is loaded and checked for
    // overlap; any overlapping table is deleted before the new one is
    // created. If deletion or detection itself fails, fall through to the
    // original catch (cosmetic-only failure).
    try {
      const tableRange = sheet.getRangeByIndexes(startRow, startCol, rows.length + 1, colCount);

      // Attempt to clear any overlapping table objects first.
      // Bug-6736 R1 Finding 5: reuse the pure, unit-tested rangesOverlap()
      // from insertGuard.ts instead of hand-rolling rectangle math inline.
      try {
        sheet.tables.load('items');
        await context.sync();
        const targetCellRange: CellRange = {
          sheet: '',
          rowStart: startRow,
          colStart: startCol,
          rowCount: rows.length + 1,
          colCount,
        };
        for (const existingTable of sheet.tables.items) {
          const existingRange = existingTable.getRange();
          existingRange.load(['rowIndex', 'columnIndex', 'rowCount', 'columnCount']);
          await context.sync();
          const existingCellRange: CellRange = {
            sheet: '',
            rowStart: existingRange.rowIndex,
            colStart: existingRange.columnIndex,
            rowCount: existingRange.rowCount,
            colCount: existingRange.columnCount,
          };
          if (rangesOverlap(targetCellRange, existingCellRange)) {
            // Bug-6736 R1 Finding 1: Table.delete() removes the table AND
            // clears its underlying cell data. Since Phase 1 already wrote
            // new values into this range, delete() would destroy them.
            // convertToRange() removes only the table object (filters,
            // alternating row styling) while preserving cell values.
            existingTable.convertToRange();
            await context.sync();
          }
        }
      } catch {
        // Detection/deletion failed — proceed with the tables.add attempt.
        // The worst case is the original Bug-6736 behavior (caught below).
      }

      const table = sheet.tables.add(tableRange, true);
      table.style = 'TableStyleMedium2';
      table.getHeaderRowRange().format.font.bold = true;
      await context.sync();
    } catch {
      tableObjectFailed = true;
    }
  });

  return { address: savedAddress, tableObjectFailed };
}

const FORMAT_MAP: Record<string, string> = {
  currency: '$#,##0.00',
  currency_eur: '#,##0.00 €',
  currency_gbp: '£#,##0.00',
  percent: '0.00%',
  percent_2dp: '0.00%',
  percent_1dp: '0.0%',
  number: '#,##0',
  decimal: '#,##0.00',
  decimal_1dp: '#,##0.0',
  decimal_3dp: '#,##0.000',
  date: 'yyyy-mm-dd',
  datetime: 'yyyy-mm-dd hh:mm:ss',
  time: 'hh:mm:ss',
  quantity: '#,##0',
  integer: '0',
  '': '#,##0.00',
};

function formatToExcelFormat(token: string): string {
  return FORMAT_MAP[token] || FORMAT_MAP[''];
}

export async function readActiveCell(): Promise<{
  address: string; value: unknown; formula: string;
}> {
  return await Excel.run(async (context) => {
    const range = context.workbook.getSelectedRange();
    range.load(['address', 'values', 'formulas']);
    await context.sync();
    return {
      address: range.address,
      value: range.values[0]?.[0] ?? null,
      formula: (range.formulas[0]?.[0] as string) || '',
    };
  });
}

// F-025-14: the prefix of the disposable worksheet the spike writes into. A
// unique suffix is appended per run so a re-run never collides with leftover
// state, and the sheet is always deleted in a finally block.
const SPIKE_SHEET_PREFIX = '__TessalliteSpike';

/**
 * Probe table/chart/formula support inside a single throwaway worksheet, then
 * delete it. F-025-14: the spike used to write a TestCol1/TestCol2 table at A1
 * of the ACTIVE sheet, drop a chart, add a "Local Pivot" sheet, and overwrite
 * the selected cell — destroying real user data with no warning or cleanup.
 * Every probe now targets a dedicated hidden sheet that is removed afterwards,
 * so the user's workbook content and selection are never touched.
 */
async function probeInTempSheet(): Promise<Partial<CompatibilityMatrix>> {
  const sheetName = `${SPIKE_SHEET_PREFIX}_${Date.now()}`;
  const partial: Partial<CompatibilityMatrix> = {};

  await Excel.run(async (context) => {
    const sheet = context.workbook.worksheets.add(sheetName);
    try {
      sheet.visibility = Excel.SheetVisibility.hidden;
      await context.sync();

      // insertTable — write headers + rows into the temp sheet and add a table.
      try {
        const range = sheet.getRangeByIndexes(0, 0, 3, 2);
        range.values = [['TestCol1', 'TestCol2'], ['a', 1], ['b', 2]];
        const table = sheet.tables.add(sheet.getRangeByIndexes(0, 0, 3, 2), true);
        table.style = 'TableStyleMedium2';
        await context.sync();
        partial.insertTable = true;
      } catch (e) { partial.insertTable = `failed: ${(e as Error).message}`; }

      // insertChart — chart off the temp data range.
      try {
        const dataRange = sheet.getRangeByIndexes(0, 0, 3, 2);
        sheet.charts.add(Excel.ChartType.columnClustered, dataRange, Excel.ChartSeriesBy.auto);
        await context.sync();
        partial.insertChart = true;
      } catch (e) { partial.insertChart = `failed: ${(e as Error).message}`; }

      // cubeFormulas — write a CUBEVALUE into a temp-sheet cell.
      try {
        sheet.getRangeByIndexes(5, 0, 1, 1).formulas = [['=CUBEVALUE("Connection","[Measures].[Test]")']];
        await context.sync();
        partial.cubeFormulas = true;
      } catch (e) { partial.cubeFormulas = `failed: ${(e as Error).message}`; }
    } finally {
      // Always remove the disposable sheet, even if a probe threw.
      sheet.delete();
      await context.sync();
    }
  });

  return partial;
}

export async function runCompatibilitySpike(): Promise<CompatibilityMatrix> {
  const { host, platform } = detectHost();
  const result: CompatibilityMatrix = { host, platform, ...defaultMatrix };

  // F-025-14: all workbook-mutating probes run inside a throwaway sheet that is
  // deleted afterwards — they no longer touch the active sheet or selection.
  try {
    const tempResults = await probeInTempSheet();
    Object.assign(result, tempResults);
  } catch (e) {
    const msg = `failed: ${(e as Error).message}`;
    result.insertTable = result.insertTable === 'untested' ? msg : result.insertTable;
    result.insertChart = result.insertChart === 'untested' ? msg : result.insertChart;
    result.cubeFormulas = result.cubeFormulas === 'untested' ? msg : result.cubeFormulas;
  }

  // localPivotTable requires a real source range; probing it would either need
  // a persisted source or create a "Local Pivot" sheet in the user's workbook.
  // Reported as untested rather than mutating the workbook to find out.
  result.localPivotTable = 'untested (skipped: would add a worksheet)';

  // Read-only probes are safe against the active selection.
  try {
    const cell = await readActiveCell();
    result.readActiveCell = true;
    result.getActiveCellAddress = true;
    result.readSelectedCubeFormula = typeof cell.formula === 'string';
  } catch (e) {
    result.readActiveCell = `failed: ${(e as Error).message}`;
    result.getActiveCellAddress = `failed: ${(e as Error).message}`;
    result.readSelectedCubeFormula = `failed: ${(e as Error).message}`;
  }

  // F-025-18: workbook.connections is not a released Office.js API; do not probe
  // it by mutating the workbook. Report as unsupported in this host.
  result.createXmlaConnection = 'unsupported (no Office.js workbook.connections API)';
  result.detectWorkbookConnections = 'unsupported (no Office.js workbook.connections API)';

  return result;
}
