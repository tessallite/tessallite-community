import { useCallback, useRef } from 'react';
import { insertResultTable } from '../utils/officeSpike';
import { setTableMetadata, trackEntityUsage, invalidateMetadataCache } from '../utils/workbookMetadata';
import {
  generateCubeSet,
  generateCubeValue,
  buildCubeRankedMemberFormula,
  buildCubeKpiFormula,
  CUBE_KPI_PROPERTIES,
  measureMemberRef,
} from '../utils/excelFormulas';
import {
  recommendChartType,
  getChartTypeEnum,
  insertChartFromRange,
  type ChartTypeRecommendation,
} from '../utils/excelCharts';
import { insertPivotTableWithMapping, type PivotFieldMapping } from '../utils/excelPivotTables';

interface InsertTableOptions {
  useActiveCell?: boolean;
  resetSheetsPerSession?: boolean;
}

interface InsertMetadata {
  projectId?: string;
  modelId?: string;
  personaId?: string;
  // F-025-23: human-readable model and persona labels for the provenance
  // footer. The footer used to show only "Source: Tessallite | <time>"; these
  // make the inserted data self-describing (which model, viewed as which
  // persona) as ISSUES.md item 10 required. Fall back to the IDs when a label
  // is not supplied.
  modelLabel?: string;
  personaLabel?: string;
  conversationId?: string;
  turnId?: string;
  semanticQuery?: string;
  formatTokens?: Record<string, string>;
  columnHeaders?: string[];
  measureColumns?: Record<string, string>;
  dimensionColumns?: Record<string, string>;
}

export interface ConfirmGuard {
  (message: string): Promise<boolean>;
}

const LARGE_ROW_THRESHOLD = 10000;

// F-025-27 / Bug-2860: an icon-set conditional format needs both a style AND a
// `criteria` array, otherwise Office.js no-ops or throws inside the batch.
// CUBEKPIMEMBER Status/Trend return a normalised value in [-1, 0, 1], so map the
// three icons to those thresholds. The first criterion of an N-icon set is a
// placeholder (Excel always assigns the lowest icon below the second threshold)
// but must still be present. A fresh array is built per call because each
// conditional format owns its own criteria assignment.
function kpiIconCriteria(): Excel.ConditionalIconCriterion[] {
  return [
    { type: Excel.ConditionalFormatIconRuleType.number, formula: '=-1', operator: Excel.ConditionalIconCriterionOperator.greaterThanOrEqual },
    { type: Excel.ConditionalFormatIconRuleType.number, formula: '=0', operator: Excel.ConditionalIconCriterionOperator.greaterThanOrEqual },
    { type: Excel.ConditionalFormatIconRuleType.number, formula: '=1', operator: Excel.ConditionalIconCriterionOperator.greaterThanOrEqual },
  ];
}

async function doInsertAndTag(
  headers: string[],
  rows: (string | number)[][],
  metadata?: InsertMetadata,
  useActiveCell?: boolean,
): Promise<string | null> {
  const resultRange = await insertResultTable(headers, rows, metadata?.formatTokens, useActiveCell);
  if (resultRange) {
    const timestamp = new Date().toISOString();
    await setTableMetadata(resultRange, {
      pluginVersion: '0.1.0',
      timestamp,
      projectId: metadata?.projectId,
      modelId: metadata?.modelId,
      personaId: metadata?.personaId,
      conversationId: metadata?.conversationId,
      turnId: metadata?.turnId,
      semanticQuery: metadata?.semanticQuery,
      columnHeaders: metadata?.columnHeaders ? JSON.stringify(metadata.columnHeaders) : undefined,
      measureColumns: metadata?.measureColumns ? JSON.stringify(metadata.measureColumns) : undefined,
      dimensionColumns: metadata?.dimensionColumns ? JSON.stringify(metadata.dimensionColumns) : undefined,
    });
    // Provenance footer: visible attribution row below the inserted table
    await insertProvenanceFooter(resultRange, headers.length, rows.length, timestamp, metadata);
  }
  return resultRange;
}

/** Insert a provenance attribution row below the table range. */
async function insertProvenanceFooter(
  rangeAddress: string,
  colCount: number,
  rowCount: number,
  timestamp: string,
  metadata?: InsertMetadata,
): Promise<void> {
  if (typeof Excel === 'undefined') return;
  try {
    await Excel.run(async (context) => {
      const sheet = context.workbook.worksheets.getActiveWorksheet();
      const tableRange = sheet.getRange(rangeAddress);
      tableRange.load(['rowIndex', 'columnIndex']);
      await context.sync();

      const footerRow = tableRange.rowIndex + rowCount + 1; // +1 for header
      const footerRange = sheet.getRangeByIndexes(footerRow, tableRange.columnIndex, 1, colCount);
      const dateStr = timestamp.slice(0, 16).replace('T', ' ') + ' UTC';
      // F-025-23: enrich the footer with model and, when active, persona so the
      // inserted data is self-describing. Labels fall back to ids; absent both,
      // the footer keeps its original minimal form.
      const parts = ['Source: Tessallite'];
      const model = metadata?.modelLabel || metadata?.modelId;
      if (model) parts.push(`Model: ${model}`);
      const persona = metadata?.personaLabel || metadata?.personaId;
      if (persona) parts.push(`Viewing as: ${persona}`);
      parts.push(dateStr);
      footerRange.getCell(0, 0).values = [[parts.join(' | ')]];
      footerRange.format.font.italic = true;
      footerRange.format.font.size = 9;
      footerRange.format.font.color = '#757575';
      await context.sync();
    });
  } catch {
    // Non-critical — silently ignore if footer insertion fails
  }
}

// F-33: Safe confirm helper for headless environments (e.g. tests, SSR,
// Office Scripts, Power Automate) where window.confirm may not exist.
function safeConfirm(msg: string, guard?: ConfirmGuard): boolean | Promise<boolean> {
  if (guard) return guard(msg);
  if (typeof window !== 'undefined' && typeof window.confirm === 'function') {
    try {
      return window.confirm(msg);
    } catch {
      // Office Scripts / Power Automate may throw on confirm(); auto-approve.
      return true;
    }
  }
  return true;
}

async function confirmLargeResult(
  rows: (string | number)[][],
  confirmGuard?: ConfirmGuard,
): Promise<boolean> {
  if (rows.length <= LARGE_ROW_THRESHOLD) return true;
  const msg = `This result contains ${rows.length.toLocaleString()} rows, which exceeds ${LARGE_ROW_THRESHOLD.toLocaleString()}. Inserting may cause Excel to become unresponsive. Continue?`;
  return safeConfirm(msg, confirmGuard);
}

export function useExcel(confirmGuard?: ConfirmGuard, onBusy?: () => void) {
  // F-19: Per-category busy flags so independent operation types don't block each other
  const busyTable = useRef(false);
  const busyChart = useRef(false);
  const busyPivot = useRef(false);
  const busyFormula = useRef(false);
  const largeGuardConfirmed = useRef(false);

  const insertTable = useCallback(async (
    headers: string[],
    rows: (string | number)[][],
    options?: InsertTableOptions,
    metadata?: InsertMetadata,
  ): Promise<string | null> => {
    if (busyTable.current) { onBusy?.(); return null; }
    busyTable.current = true;

    try {
      if (!largeGuardConfirmed.current && rows.length > LARGE_ROW_THRESHOLD) {
        const proceed = await confirmLargeResult(rows, confirmGuard);
        if (!proceed) { busyTable.current = false; return null; }
        largeGuardConfirmed.current = true;
      }

      try {
        const result = await doInsertAndTag(headers, rows, metadata, options?.useActiveCell);
        largeGuardConfirmed.current = false;
        invalidateMetadataCache();
        return result;
      } catch (e) {
        if (e instanceof Error && e.message === 'OVERWRITE_WARNING') {
          const msg = 'The active cell range already contains data. Overwrite existing data?';
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (confirmed) {
            const resultRange = await doInsertAndTag(headers, rows, metadata, false);
            largeGuardConfirmed.current = false;
            invalidateMetadataCache();
            return resultRange;
          }
          largeGuardConfirmed.current = false;
          return null;
        }
        largeGuardConfirmed.current = false;
        throw e;
      }
    } finally {
      busyTable.current = false;
    }
  }, [confirmGuard, onBusy]);

  const insertFormula = useCallback(async (formula: string, targetCell?: string): Promise<void> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    await Excel.run(async (context) => {
      const range = targetCell
        ? context.workbook.worksheets.getActiveWorksheet().getRange(targetCell)
        : context.workbook.getSelectedRange();
      range.load(['values', 'formulas']);
      await context.sync();
      const currentValue = range.values[0]?.[0];
      const currentFormula = (range.formulas[0]?.[0] as string) || '';
      if ((currentValue !== null && currentValue !== undefined && currentValue !== '') || currentFormula !== '') {
        const msg = `The target cell already contains data. Overwrite?`;
        const confirmed = await safeConfirm(msg, confirmGuard);
        if (!confirmed) return;
      }
      range.formulas = [[formula]];
      await context.sync();
    });
  }, [confirmGuard]);

  const getActiveCellAddress = useCallback(async (): Promise<string> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    return await Excel.run(async (context) => {
      const range = context.workbook.getSelectedRange();
      range.load('address');
      await context.sync();
      return range.address;
    });
  }, []);

  const readCellValue = useCallback(async (): Promise<{
    address: string;
    value: unknown;
    formula: string;
  }> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
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
  }, []);

  const createNewSheet = useCallback(async (baseName: string): Promise<string> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    return await Excel.run(async (context) => {
      const sheets = context.workbook.worksheets;
      sheets.load('items/name');
      await context.sync();

      const existingNames = new Set(sheets.items.map(s => s.name));
      let name = baseName;
      let counter = 1;
      while (existingNames.has(name)) {
        name = `${baseName} (${counter})`;
        counter++;
      }

      const newSheet = sheets.add(name);
      newSheet.activate();
      await context.sync();
      return name;
    });
  }, []);

  const insertChart = useCallback(async (
    headers: string[],
    rows: (string | number)[][],
    chartType?: ChartTypeRecommendation,
    annotation?: {
      measures?: Record<string, { title: string; type: string }>;
      dimensions?: Record<string, { title: string; type: string }>;
      timeDimensions?: Record<string, { title: string; type: string }>;
    },
    existingRangeAddress?: string,
  ): Promise<string | null> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (rows.length === 0) throw new Error('Cannot create chart from empty data');
    if (busyChart.current) { onBusy?.(); return null; }
    busyChart.current = true;

    try {
      if (!await confirmLargeResult(rows, confirmGuard)) return null;

      const recommendation = chartType
        ? { chartType, confidence: 'high' as const, reason: 'User selected' }
        : recommendChartType(headers, rows, annotation);
      const excelChartType = getChartTypeEnum(recommendation.chartType);

      const title = annotation?.measures
        ? Object.values(annotation.measures).map(m => m.title).join(' / ')
        : 'Tessallite Result';

      let savedRangeAddress: string | null = null;

      await Excel.run(async (context) => {
        let dataRange: Excel.Range;
        let sheet: Excel.Worksheet;

        if (existingRangeAddress) {
          const ws = context.workbook.worksheets.getActiveWorksheet();
          dataRange = ws.getRange(existingRangeAddress);
          sheet = ws;
          savedRangeAddress = existingRangeAddress;
        } else {
          const sheets = context.workbook.worksheets;
          sheets.load('items/name');
          await context.sync();
          const existingNames = new Set(sheets.items.map(s => s.name));
          let name = 'Chart Data';
          let counter = 1;
          while (existingNames.has(name)) {
            name = `Chart Data (${counter})`;
            counter++;
          }

          sheet = sheets.add(name);
          sheet.activate();

          const rowCount = rows.length + 1;
          const colCount = headers.length;
          dataRange = sheet.getRangeByIndexes(0, 0, rowCount, colCount);
          dataRange.values = [headers, ...rows];
          dataRange.format.autofitColumns();
          dataRange.load('address');
          await context.sync();

          const table = sheet.tables.add(dataRange, true);
          table.style = 'TableStyleMedium2';
          await context.sync();

          savedRangeAddress = dataRange.address;
        }

        await insertChartFromRange(excelChartType, sheet, headers, rows, annotation, title);
        await context.sync();
      });

      if (savedRangeAddress) {
        await setTableMetadata(savedRangeAddress, {
          pluginVersion: '0.1.0',
          timestamp: new Date().toISOString(),
        });
      }

      invalidateMetadataCache();
      return savedRangeAddress;
    } finally {
      busyChart.current = false;
    }
  }, [confirmGuard, onBusy]);

  const insertLocalPivot = useCallback(async (
    headers: string[],
    rows: (string | number)[][],
    fieldMapping?: PivotFieldMapping,
    annotation?: {
      measures?: Record<string, { title: string; type: string }>;
      dimensions?: Record<string, { title: string; type: string }>;
    },
  ): Promise<string | null> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (rows.length === 0) throw new Error('Cannot create PivotTable from empty data');
    if (busyPivot.current) { onBusy?.(); return null; }
    busyPivot.current = true;

    try {
      if (!await confirmLargeResult(rows, confirmGuard)) return null;

      const mapping = fieldMapping || buildDefaultFieldMapping(headers, annotation);
      let savedRangeAddress: string | null = null;

      await Excel.run(async (context) => {
        const sheets = context.workbook.worksheets;
        sheets.load('items/name');
        await context.sync();
        const existingNames = new Set(sheets.items.map(s => s.name));

        let dataName = 'Pivot Data';
        let counter = 1;
        while (existingNames.has(dataName)) {
          dataName = `Pivot Data (${counter})`;
          counter++;
        }

        const dataSheet = sheets.add(dataName);

        const rowCount = rows.length + 1;
        const colCount = headers.length;
        const range = dataSheet.getRangeByIndexes(0, 0, rowCount, colCount);
        range.values = [headers, ...rows];
        range.format.autofitColumns();
        range.load('address');
        await context.sync();

        const table = dataSheet.tables.add(range, true);
        table.style = 'TableStyleMedium2';
        table.load('name');
        await context.sync();

        const addr = range.address;
        const qualifiedAddr = addr.includes('!') ? addr : `'${dataName}'!${addr}`;
        savedRangeAddress = qualifiedAddr;

        let pivotName = 'Local Pivot';
        counter = 1;
        while (existingNames.has(pivotName)) {
          pivotName = `Local Pivot (${counter})`;
          counter++;
        }

        const pivotSheet = sheets.add(pivotName);
        pivotSheet.activate();

        const pivotRange = pivotSheet.getRange('A1');
        const pivotTable = pivotSheet.pivotTables.add(
          'TessalliteLocalPivot',
          qualifiedAddr,
          pivotRange,
        );
        await context.sync();

        pivotTable.load('hierarchies');
        await context.sync();

        const hierarchies = pivotTable.hierarchies;
        hierarchies.load('items/name');
        await context.sync();

        const hierarchyMap = new Map<string, Excel.PivotHierarchy>();
        for (const item of hierarchies.items) {
          hierarchyMap.set(item.name, item);
        }

        for (const field of mapping.rowFields) {
          const hier = hierarchyMap.get(field);
          if (hier) pivotTable.rowHierarchies.add(hier);
        }
        for (const field of mapping.dataFields) {
          const hier = hierarchyMap.get(field);
          if (hier) pivotTable.dataHierarchies.add(hier);
        }
        for (const field of mapping.columnFields) {
          const hier = hierarchyMap.get(field);
          if (hier) pivotTable.columnHierarchies.add(hier);
        }
        for (const field of mapping.filterFields) {
          const hier = hierarchyMap.get(field);
          if (hier) pivotTable.filterHierarchies.add(hier);
        }

        await context.sync();
      });

      if (savedRangeAddress) {
        await setTableMetadata(savedRangeAddress, {
          pluginVersion: '0.1.0',
          timestamp: new Date().toISOString(),
        });
        invalidateMetadataCache();
      }

      return savedRangeAddress;
    } finally {
      busyPivot.current = false;
    }
  }, [confirmGuard, onBusy]);

  const insertNamedSetAsFormulas = useCallback(async (
    namedSet: { id: string; name: string; display_name: string | null; expression: string; updated_at?: string },
    connectionName: string,
    memberCount: number = 10,
  ): Promise<string | null> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (busyFormula.current) { onBusy?.(); return null; }
    busyFormula.current = true;

    try {
      let startAddress: string | null = null;
      await Excel.run(async (context) => {
        const sheet = context.workbook.worksheets.getActiveWorksheet();
        const activeRange = context.workbook.getSelectedRange();
        activeRange.load('rowIndex,columnIndex');
        await context.sync();

        const row = activeRange.rowIndex;
        const col = activeRange.columnIndex;
        const totalRows = 1 + memberCount;

        const targetRange = sheet.getRangeByIndexes(row, col, totalRows, 1);
        targetRange.load('values');
        await context.sync();

        const hasContent = targetRange.values.some(r => r[0] !== null && r[0] !== undefined && r[0] !== '');
        if (hasContent) {
          const msg = `This will overwrite ${totalRows} cells. Continue?`;
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return;
        }

        const caption = namedSet.display_name || namedSet.name;
        const cubeSetFormula = generateCubeSet(connectionName, namedSet.expression, caption);
        const setCell = sheet.getRangeByIndexes(row, col, 1, 1);
        setCell.formulas = [[cubeSetFormula]];
        setCell.load('address');
        await context.sync();

        const setCellAddr = setCell.address.split('!')[1] || setCell.address;

        for (let i = 1; i <= memberCount; i++) {
          const memberCell = sheet.getRangeByIndexes(row + i, col, 1, 1);
          memberCell.formulas = [[buildCubeRankedMemberFormula(connectionName, setCellAddr, i)]];
        }

        await context.sync();
        startAddress = setCell.address;
      });

      if (startAddress) {
        await trackEntityUsage('named_set', namedSet.id, namedSet.display_name || namedSet.name, startAddress, undefined, namedSet.updated_at);
      }

      return startAddress;
    } finally {
      busyFormula.current = false;
    }
  }, [confirmGuard, onBusy]);

  const insertKpiFormulas = useCallback(async (
    kpi: { id: string; name: string; display_name: string | null; updated_at?: string },
    valueMeasureName: string | null,
    goalMeasureName: string | null,
    connectionName: string,
    goalLiteral?: number | null,
  ): Promise<string | null> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (busyFormula.current) { onBusy?.(); return null; }
    busyFormula.current = true;

    try {
      let startAddress: string | null = null;
      await Excel.run(async (context) => {
        const sheet = context.workbook.worksheets.getActiveWorksheet();
        const activeRange = context.workbook.getSelectedRange();
        activeRange.load('rowIndex,columnIndex');
        await context.sync();

        const row = activeRange.rowIndex;
        const col = activeRange.columnIndex;
        // Bug-5294: account for the goal row when a static literal is supplied
        const hasGoalRow = goalMeasureName || goalLiteral != null;
        const totalRows = 1 + (valueMeasureName ? 1 : 0) + (hasGoalRow ? 1 : 0);

        const targetRange = sheet.getRangeByIndexes(row, col, totalRows, 2);
        targetRange.load('values');
        await context.sync();

        const hasContent = targetRange.values.some(r => r.some(c => c !== null && c !== undefined && c !== ''));
        if (hasContent) {
          const msg = `This will overwrite ${totalRows} rows × 2 columns. Continue?`;
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return;
        }

        let r = row;

        const label = kpi.display_name || kpi.name;

        const labelCell = sheet.getRangeByIndexes(r, col, 1, 1);
        labelCell.values = [[label]];
        labelCell.format.font.bold = true;
        labelCell.load('address');
        await context.sync();
        startAddress = labelCell.address;

        r++;
        if (valueMeasureName) {
          sheet.getRangeByIndexes(r, col, 1, 1).values = [['Value']];
          sheet.getRangeByIndexes(r, col + 1, 1, 1).formulas = [[
            generateCubeValue(connectionName, measureMemberRef(valueMeasureName)),
          ]];
          r++;
        }

        if (goalMeasureName) {
          sheet.getRangeByIndexes(r, col, 1, 1).values = [['Goal']];
          sheet.getRangeByIndexes(r, col + 1, 1, 1).formulas = [[
            generateCubeValue(connectionName, measureMemberRef(goalMeasureName)),
          ]];
          r++;
        } else if (goalLiteral != null) {
          // Bug-5294: static-target KPI — write the numeric goal directly,
          // mirroring the scorecard path (F-025-10).
          sheet.getRangeByIndexes(r, col, 1, 1).values = [['Goal']];
          sheet.getRangeByIndexes(r, col + 1, 1, 1).values = [[goalLiteral]];
          r++;
        }

        await context.sync();
      });

      if (startAddress) {
        await trackEntityUsage('kpi', kpi.id, kpi.display_name || kpi.name, startAddress, undefined, kpi.updated_at);
      }

      return startAddress;
    } finally {
      busyFormula.current = false;
    }
  }, [confirmGuard, onBusy]);

  const insertKpiFullRow = useCallback(async (
    kpi: { id: string; name: string; display_name: string | null; updated_at?: string },
    valueMeasureName: string | null,
    goalMeasureName: string | null,
    connectionName: string,
    goalLiteral?: number | null,
  ): Promise<string | null> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (busyFormula.current) { onBusy?.(); return null; }
    busyFormula.current = true;

    try {
      let startAddress: string | null = null;
      await Excel.run(async (context) => {
        const sheet = context.workbook.worksheets.getActiveWorksheet();
        const activeRange = context.workbook.getSelectedRange();
        activeRange.load('rowIndex,columnIndex');
        await context.sync();

        const row = activeRange.rowIndex;
        const col = activeRange.columnIndex;
        const colCount = 5;

        const targetRange = sheet.getRangeByIndexes(row, col, 1, colCount);
        targetRange.load('values');
        await context.sync();

        const hasContent = targetRange.values[0].some(c => c !== null && c !== undefined && c !== '');
        if (hasContent) {
          const msg = `This will overwrite 1 row x ${colCount} columns. Continue?`;
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return;
        }

        const label = kpi.display_name || kpi.name;
        // F-025-08: the gateway publishes KPI_NAME = kpi.name (the technical
        // name), so a CUBEKPIMEMBER built from the display name never resolves.
        // Emit the technical name regardless of how the KPI is labelled in the UI.
        const kpiMemberName = kpi.name;

        sheet.getRangeByIndexes(row, col, 1, 1).values = [[label]];
        sheet.getRangeByIndexes(row, col, 1, 1).format.font.bold = true;

        if (valueMeasureName) {
          sheet.getRangeByIndexes(row, col + 1, 1, 1).formulas = [[
            generateCubeValue(connectionName, measureMemberRef(valueMeasureName)),
          ]];
        }

        if (goalMeasureName) {
          sheet.getRangeByIndexes(row, col + 2, 1, 1).formulas = [[
            generateCubeValue(connectionName, measureMemberRef(goalMeasureName)),
          ]];
        } else if (goalLiteral != null) {
          // Bug-5294: static-target KPI — write the numeric goal directly,
          // mirroring the scorecard path (F-025-10).
          sheet.getRangeByIndexes(row, col + 2, 1, 1).values = [[goalLiteral]];
        }

        sheet.getRangeByIndexes(row, col + 3, 1, 1).formulas = [[
          buildCubeKpiFormula(connectionName, kpiMemberName, CUBE_KPI_PROPERTIES.Status),
        ]];

        sheet.getRangeByIndexes(row, col + 4, 1, 1).formulas = [[
          buildCubeKpiFormula(connectionName, kpiMemberName, CUBE_KPI_PROPERTIES.Trend),
        ]];

        // F-025-27 / Bug-2860: icon-set formats require both a style AND a
        // criteria array (see kpiIconCriteria). All KPI icon-set sites use it.
        const statusCell = sheet.getRangeByIndexes(row, col + 3, 1, 1);
        const statusCf = statusCell.conditionalFormats.add(Excel.ConditionalFormatType.iconSet);
        statusCf.iconSetOrNullObject.style = Excel.IconSet.threeTrafficLights1;
        statusCf.iconSetOrNullObject.criteria = kpiIconCriteria();

        const trendCell = sheet.getRangeByIndexes(row, col + 4, 1, 1);
        const trendCf = trendCell.conditionalFormats.add(Excel.ConditionalFormatType.iconSet);
        trendCf.iconSetOrNullObject.style = Excel.IconSet.threeArrows;
        trendCf.iconSetOrNullObject.criteria = kpiIconCriteria();

        const labelCell = sheet.getRangeByIndexes(row, col, 1, 1);
        labelCell.load('address');
        await context.sync();
        startAddress = labelCell.address;
      });

      if (startAddress) {
        await trackEntityUsage('kpi', kpi.id, kpi.display_name || kpi.name, startAddress, undefined, kpi.updated_at);
      }

      return startAddress;
    } finally {
      busyFormula.current = false;
    }
  }, [confirmGuard, onBusy]);

  const insertKpiValueOnly = useCallback(async (
    valueMeasureName: string,
    connectionName: string,
  ): Promise<string | null> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (busyFormula.current) { onBusy?.(); return null; }
    busyFormula.current = true;

    try {
      let startAddress: string | null = null;
      await Excel.run(async (context) => {
        const activeRange = context.workbook.getSelectedRange();
        activeRange.load('values,formulas');
        await context.sync();

        const currentValue = activeRange.values[0]?.[0];
        const currentFormula = (activeRange.formulas[0]?.[0] as string) || '';
        if ((currentValue !== null && currentValue !== undefined && currentValue !== '') || currentFormula !== '') {
          const msg = 'The target cell already contains data. Overwrite?';
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return;
        }

        activeRange.formulas = [[generateCubeValue(connectionName, measureMemberRef(valueMeasureName))]];
        activeRange.load('address');
        await context.sync();
        startAddress = activeRange.address;
      });

      return startAddress;
    } finally {
      busyFormula.current = false;
    }
  }, [confirmGuard, onBusy]);

  const insertKpiStatusOnly = useCallback(async (
    kpiName: string,
    connectionName: string,
  ): Promise<string | null> => {
    // Bug-5290: the parameter was previously named kpiDisplayName, but the
    // gateway publishes KPI_NAME = kpi.name (the technical name). All callers
    // already pass the technical name; the rename makes the contract explicit
    // so future call sites cannot accidentally pass the display name.
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (busyFormula.current) { onBusy?.(); return null; }
    busyFormula.current = true;

    try {
      let startAddress: string | null = null;
      await Excel.run(async (context) => {
        const activeRange = context.workbook.getSelectedRange();
        activeRange.load('rowIndex,columnIndex,values,formulas');
        await context.sync();

        const currentValue = activeRange.values[0]?.[0];
        const currentFormula = (activeRange.formulas[0]?.[0] as string) || '';
        if ((currentValue !== null && currentValue !== undefined && currentValue !== '') || currentFormula !== '') {
          const msg = 'The target cell already contains data. Overwrite?';
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return;
        }

        const sheet = context.workbook.worksheets.getActiveWorksheet();
        const cell = sheet.getRangeByIndexes(activeRange.rowIndex, activeRange.columnIndex, 1, 1);
        cell.formulas = [[buildCubeKpiFormula(connectionName, kpiName, CUBE_KPI_PROPERTIES.Status)]];

        const cf = cell.conditionalFormats.add(Excel.ConditionalFormatType.iconSet);
        cf.iconSetOrNullObject.style = Excel.IconSet.threeTrafficLights1;
        cf.iconSetOrNullObject.criteria = kpiIconCriteria();

        cell.load('address');
        await context.sync();
        startAddress = cell.address;
      });

      return startAddress;
    } finally {
      busyFormula.current = false;
    }
  }, [confirmGuard, onBusy]);

  const insertKpiTrendOnly = useCallback(async (
    kpiName: string,
    connectionName: string,
  ): Promise<string | null> => {
    // Bug-5290: see insertKpiStatusOnly — same rename rationale.
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (busyFormula.current) { onBusy?.(); return null; }
    busyFormula.current = true;

    try {
      let startAddress: string | null = null;
      await Excel.run(async (context) => {
        const activeRange = context.workbook.getSelectedRange();
        activeRange.load('rowIndex,columnIndex,values,formulas');
        await context.sync();

        const currentValue = activeRange.values[0]?.[0];
        const currentFormula = (activeRange.formulas[0]?.[0] as string) || '';
        if ((currentValue !== null && currentValue !== undefined && currentValue !== '') || currentFormula !== '') {
          const msg = 'The target cell already contains data. Overwrite?';
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return;
        }

        const sheet = context.workbook.worksheets.getActiveWorksheet();
        const cell = sheet.getRangeByIndexes(activeRange.rowIndex, activeRange.columnIndex, 1, 1);
        cell.formulas = [[buildCubeKpiFormula(connectionName, kpiName, CUBE_KPI_PROPERTIES.Trend)]];

        const cf = cell.conditionalFormats.add(Excel.ConditionalFormatType.iconSet);
        cf.iconSetOrNullObject.style = Excel.IconSet.threeArrows;
        cf.iconSetOrNullObject.criteria = kpiIconCriteria();

        cell.load('address');
        await context.sync();
        startAddress = cell.address;
      });

      return startAddress;
    } finally {
      busyFormula.current = false;
    }
  }, [confirmGuard, onBusy]);

  const insertKpiValueFormula = useCallback(async (
    kpiName: string,
    connectionName: string,
  ): Promise<string | null> => {
    // Bug-5290: see insertKpiStatusOnly — same rename rationale.
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (busyFormula.current) { onBusy?.(); return null; }
    busyFormula.current = true;

    try {
      let startAddress: string | null = null;
      await Excel.run(async (context) => {
        const activeRange = context.workbook.getSelectedRange();
        activeRange.load('rowIndex,columnIndex,values,formulas');
        await context.sync();

        const currentValue = activeRange.values[0]?.[0];
        const currentFormula = (activeRange.formulas[0]?.[0] as string) || '';
        if ((currentValue !== null && currentValue !== undefined && currentValue !== '') || currentFormula !== '') {
          const msg = 'The target cell already contains data. Overwrite?';
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return;
        }

        const sheet = context.workbook.worksheets.getActiveWorksheet();
        const cell = sheet.getRangeByIndexes(activeRange.rowIndex, activeRange.columnIndex, 1, 1);
        cell.formulas = [[buildCubeKpiFormula(connectionName, kpiName, CUBE_KPI_PROPERTIES.Value)]];
        cell.load('address');
        await context.sync();
        startAddress = cell.address;
      });

      return startAddress;
    } finally {
      busyFormula.current = false;
    }
  }, [confirmGuard, onBusy]);

  const insertMeasureAsFormula = useCallback(async (
    measureName: string,
    connectionName: string,
  ): Promise<string | null> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (busyFormula.current) { onBusy?.(); return null; }
    busyFormula.current = true;

    try {
      let startAddress: string | null = null;
      await Excel.run(async (context) => {
        const sheet = context.workbook.worksheets.getActiveWorksheet();
        const activeRange = context.workbook.getSelectedRange();
        activeRange.load('rowIndex,columnIndex,values,formulas');
        await context.sync();

        const currentValue = activeRange.values[0]?.[0];
        const currentFormula = (activeRange.formulas[0]?.[0] as string) || '';
        if ((currentValue !== null && currentValue !== undefined && currentValue !== '') || currentFormula !== '') {
          const msg = 'The target cell already contains data. Overwrite?';
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return;
        }

        const formula = generateCubeValue(connectionName, measureMemberRef(measureName));
        activeRange.formulas = [[formula]];
        activeRange.load('address');
        await context.sync();
        startAddress = activeRange.address;
      });

      return startAddress;
    } finally {
      busyFormula.current = false;
    }
  }, [confirmGuard, onBusy]);

  const insertKpiScorecard = useCallback(async (
    kpis: { id: string; name: string; display_name: string | null; valueMeasureName: string | null; goalMeasureName: string | null; goalLiteral?: number | null; updated_at?: string }[],
    connectionName: string,
  ): Promise<string | null> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (busyFormula.current) { onBusy?.(); return null; }
    if (kpis.length === 0) return null;
    busyFormula.current = true;

    try {
      let startAddress: string | null = null;
      await Excel.run(async (context) => {
        const sheet = context.workbook.worksheets.getActiveWorksheet();
        const activeRange = context.workbook.getSelectedRange();
        activeRange.load('rowIndex,columnIndex');
        await context.sync();

        const startRow = activeRange.rowIndex;
        const startCol = activeRange.columnIndex;
        const colCount = 5;
        const totalRows = 1 + kpis.length;

        const targetRange = sheet.getRangeByIndexes(startRow, startCol, totalRows, colCount);
        targetRange.load('values');
        await context.sync();

        const hasContent = targetRange.values.some(row => row.some(c => c !== null && c !== undefined && c !== ''));
        if (hasContent) {
          const msg = `This will overwrite ${totalRows} rows x ${colCount} columns. Continue?`;
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return;
        }

        const headers = ['KPI', 'Value', 'Goal', 'Status', 'Trend'];
        const headerRange = sheet.getRangeByIndexes(startRow, startCol, 1, colCount);
        headerRange.values = [headers];
        headerRange.format.font.bold = true;
        headerRange.format.fill.color = '#f5f5f5';

        for (let i = 0; i < kpis.length; i++) {
          const kpi = kpis[i];
          const row = startRow + 1 + i;
          const label = kpi.display_name || kpi.name;

          sheet.getRangeByIndexes(row, startCol, 1, 1).values = [[label]];
          sheet.getRangeByIndexes(row, startCol, 1, 1).format.font.bold = true;

          if (kpi.valueMeasureName) {
            sheet.getRangeByIndexes(row, startCol + 1, 1, 1).formulas = [[
              generateCubeValue(connectionName, measureMemberRef(kpi.valueMeasureName)),
            ]];
          }

          if (kpi.goalMeasureName) {
            sheet.getRangeByIndexes(row, startCol + 2, 1, 1).formulas = [[
              generateCubeValue(connectionName, measureMemberRef(kpi.goalMeasureName)),
            ]];
          } else if (kpi.goalLiteral != null) {
            // F-025-10: a static-target KPI has no goal measure; write the
            // numeric target directly so the Goal column is not blank.
            sheet.getRangeByIndexes(row, startCol + 2, 1, 1).values = [[kpi.goalLiteral]];
          }

          // F-025-08: KPI member must be the technical KPI_NAME, not the label.
          const kpiMemberName = kpi.name;
          sheet.getRangeByIndexes(row, startCol + 3, 1, 1).formulas = [[
            buildCubeKpiFormula(connectionName, kpiMemberName, CUBE_KPI_PROPERTIES.Status),
          ]];
          sheet.getRangeByIndexes(row, startCol + 4, 1, 1).formulas = [[
            buildCubeKpiFormula(connectionName, kpiMemberName, CUBE_KPI_PROPERTIES.Trend),
          ]];

          const statusCell = sheet.getRangeByIndexes(row, startCol + 3, 1, 1);
          const statusCf = statusCell.conditionalFormats.add(Excel.ConditionalFormatType.iconSet);
          statusCf.iconSetOrNullObject.style = Excel.IconSet.threeTrafficLights1;
          statusCf.iconSetOrNullObject.criteria = kpiIconCriteria();

          const trendCell = sheet.getRangeByIndexes(row, startCol + 4, 1, 1);
          const trendCf = trendCell.conditionalFormats.add(Excel.ConditionalFormatType.iconSet);
          trendCf.iconSetOrNullObject.style = Excel.IconSet.threeArrows;
          trendCf.iconSetOrNullObject.criteria = kpiIconCriteria();
        }

        const firstCell = sheet.getRangeByIndexes(startRow, startCol, 1, 1);
        firstCell.load('address');
        await context.sync();
        startAddress = firstCell.address;
      });

      if (startAddress) {
        for (const kpi of kpis) {
          await trackEntityUsage('kpi', kpi.id, kpi.display_name || kpi.name, startAddress, undefined, kpi.updated_at);
        }
      }

      return startAddress;
    } finally {
      busyFormula.current = false;
    }
  }, [confirmGuard, onBusy]);

  return {
    insertTable,
    insertChart,
    insertLocalPivot,
    insertFormula,
    insertNamedSetAsFormulas,
    insertKpiFormulas,
    insertKpiFullRow,
    insertKpiValueOnly,
    insertKpiStatusOnly,
    insertKpiTrendOnly,
    insertKpiValueFormula,
    insertMeasureAsFormula,
    insertKpiScorecard,
    getActiveCellAddress,
    readCellValue,
    createNewSheet,
  };
}

function buildDefaultFieldMapping(
  headers: string[],
  annotation?: {
    measures?: Record<string, { title: string; type: string }>;
    dimensions?: Record<string, { title: string; type: string }>;
  },
): PivotFieldMapping {
  if (annotation) {
    const measureTitles = Object.values(annotation.measures || {}).map(m => m.title);
    const dimensionTitles = Object.values(annotation.dimensions || {}).map(d => d.title);
    return {
      rowFields: dimensionTitles.length > 0 ? dimensionTitles : headers.slice(0, 1),
      columnFields: [],
      dataFields: measureTitles.length > 0 ? measureTitles : [],
      filterFields: [],
    };
  }

  return {
    rowFields: headers.slice(0, Math.min(1, headers.length > 1 ? 1 : 0)),
    columnFields: [],
    dataFields: headers.length > 1 ? [headers[headers.length - 1]] : [],
    filterFields: [],
  };
}
