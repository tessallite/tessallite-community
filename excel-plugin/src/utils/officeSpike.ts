/// <reference types="office-js" />

/**
 * Office.js compatibility spike.
 * Phase 0: runCompatibilitySpike() validates platform support.
 *          Must be executed manually inside an Excel host.
 */

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

export async function insertResultTable(
  headers: string[],
  rows: (string | number)[][],
  formatTokens?: Record<string, string>,
  useActiveCell?: boolean,
): Promise<string | null> {
  return await Excel.run(async (context) => {
    const sheet = context.workbook.worksheets.getActiveWorksheet();

    let startRow = 0;
    let startCol = 0;

    if (useActiveCell) {
      const activeRange = context.workbook.getSelectedRange();
      activeRange.load(['rowIndex', 'columnIndex', 'values']);
      await context.sync();

      startRow = activeRange.rowIndex;
      startCol = activeRange.columnIndex;

      const targetRange = sheet.getRangeByIndexes(startRow, startCol, rows.length + 1, headers.length);
      targetRange.load('values');
      await context.sync();

      const hasExistingData = targetRange.values.some(
        (row: (string | number)[][]) => row.some((cell: unknown) => cell !== null && cell !== undefined && cell !== ''),
      );

      if (hasExistingData) {
        throw new Error('OVERWRITE_WARNING');
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

    await context.sync();

    const tableRange = sheet.getRangeByIndexes(startRow, startCol, rows.length + 1, colCount);
    const table = sheet.tables.add(tableRange, true);
    table.style = 'TableStyleMedium2';
    table.getHeaderRowRange().format.font.bold = true;
    await context.sync();

    return range.address;
  });
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
