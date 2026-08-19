import { useCallback, useRef, useMemo } from 'react';
import { insertResultTable } from '../utils/officeSpike';
import { setTableMetadata, setTableMetadataWithinLock, withTableLocksKeys, blockKeysForRect, LockAcquireTimeoutError, type TargetRect, trackEntityUsage, invalidateMetadataCache } from '../utils/workbookMetadata';
import { InsertTracker, parseRangeAddress, rangeHasContent, type CellRange } from '../utils/insertGuard';
import {
  generateCubeSet,
  generateCubeValue,
  buildCubeRankedMemberFormula,
  CUBE_KPI_PROPERTIES,
  measureMemberRef,
  kpiValueCellFormula,
  kpiStatusCellFormula,
} from '../utils/excelFormulas';
import { SCORECARD_HEADERS, buildLiteralScorecardRows, buildFormulaScorecardRows, type EvaluatedScorecardKpi } from '../utils/kpiScorecard';
import { strings, templates } from '../i18n/strings';
import {
  recommendChartType,
  getChartTypeEnum,
  createChartOnSheet,
  applyChartAxisFormatting,
  type ChartTypeRecommendation,
} from '../utils/excelCharts';
import { resolvePivotFieldMapping, type PivotFieldMapping } from '../utils/excelPivotTables';
import {
  buildQueryProvenanceParts,
  formatProvenanceTimestamp,
  joinProvenanceFooter,
  PROVENANCE_FOOTER_ROWS,
  FOOTER_FONT_ITALIC,
  FOOTER_FONT_SIZE,
  FOOTER_FONT_COLOR,
} from '../utils/provenanceFooter';
import { resolveWriteTarget, withPinnedCellWrite } from '../utils/lockedCellWrite';

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
const LOCAL_PIVOT_DATA_TABLE_PREFIX = '_tsl_data_';

function safeExcelIdentifier(value: string): string {
  const normalised = value.replace(/[^A-Za-z0-9_]/g, '_').replace(/^([^A-Za-z_])/, '_$1');
  return normalised.slice(0, 180) || 'local_pivot';
}

function nextUniqueLocalPivotTableName(existingNames: Set<string>, sheetName: string): string {
  const base = `${LOCAL_PIVOT_DATA_TABLE_PREFIX}${safeExcelIdentifier(sheetName)}`;
  let name = base;
  let counter = 1;
  while (existingNames.has(name)) {
    name = `${base}_${counter}`;
    counter++;
  }
  existingNames.add(name);
  return name;
}

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

/**
 * Bug-6737: table insert result. The actual write (insertResultTable) is
 * separated from post-steps (metadata tagging, provenance footer) so the
 * toast can report the TRUE outcome -- a post-step failure that occurs
 * after a successful write must never become "Insert failed".
 */
export interface TableInsertResult {
  address: string | null;
  postStepWarning: boolean;
  // Bug-7397 R8-3: set when the insert FAILED CLOSED (a host-read failure, an
  // unresolvable target, or lock-retry exhaustion) rather than being a
  // deliberate no-op (busy guard / user decline). A blocked insert wrote
  // nothing but MUST surface a "try again" message -- silence looks like a
  // broken feature. A plain null address with blocked unset stays silent.
  blocked?: boolean;
}

/**
 * Bug-7397 R6/R7: resolve the insert's PINNED write target AND every table lock
 * key it must hold, from ONE host sample. Locking the exact rectangle that gets
 * written (not a re-derived location) closes the two-sample TOCTOU the
 * deep-review reproduced. The keys are those of ALL tracked tables the written
 * RECTANGLE intersects (rectangle-overlap, not start-cell containment), or the
 * target's own anchor key when it overlaps none. Returns null on headless mode,
 * a host-read failure, OR an unresolvable ('' -> no getItem target) sheet name;
 * the caller then FAILS CLOSED rather than doing an unprotected / wrong-target
 * write.
 */
async function resolveInsertTarget(
  useActiveCell: boolean | undefined,
  forceOverwrite: boolean | undefined,
): Promise<{ sheetName: string; startRow: number; startCol: number } | null> {
  if (typeof Excel === 'undefined') return null;
  try {
    const resolved = await Excel.run(async (context) => {
      const selected = context.workbook.getSelectedRange();
      selected.load('address, rowIndex, columnIndex');
      await context.sync();

      const addr = String(selected.address || '');
      const bang = addr.lastIndexOf('!');
      // Unquote a spaced/punctuated sheet name ('My Sheet'!A1 -> My Sheet) so
      // it matches the raw name insertResultTable's getItem() expects.
      const sheetName = bang >= 0
        ? addr.slice(0, bang).replace(/^'(.*)'$/, '$1').replace(/''/g, "'")
        : '';
      // Bug-7397 R7-4: a selection with no sheet qualifier is unresolvable --
      // getItem('') would throw. Treat as unresolved (fail closed) instead.
      if (!sheetName) return null;

      if (useActiveCell || forceOverwrite) {
        return { sheetName, startRow: selected.rowIndex, startCol: selected.columnIndex };
      }
      // Default path: the table goes to A1 of the active (selection's) sheet.
      return { sheetName, startRow: 0, startCol: 0 };
    });
    return resolved;
  } catch {
    return null;
  }
}

async function doInsertAndTag(
  headers: string[],
  rows: (string | number)[][],
  metadata?: InsertMetadata,
  useActiveCell?: boolean,
  forceOverwrite?: boolean,
): Promise<TableInsertResult> {
  // Bug-7397 R9: resolve the PINNED write location from ONE host sample, then
  // acquire the SPATIAL BLOCK LOCKS covering the cells it will write. Because
  // lock keys derive from cell coordinates, any concurrent refresh/insert
  // touching these cells shares a block key by construction and is excluded --
  // no re-check, no publish/read race. FAIL CLOSED if the target can't be
  // resolved (do not write blindly).
  const target = await resolveInsertTarget(useActiveCell, forceOverwrite);
  if (!target) return { address: null, postStepWarning: false, blocked: true };

  // The locked rectangle covers header + data (rows.length + 1) AND the
  // provenance footer row one below (R8-4), so a table immediately beneath is
  // excluded from the footer write too. insertResultTable writes only
  // rows.length + 1 tall; this wider count governs ONLY the lock coverage.
  const rect: TargetRect = {
    startRow: target.startRow,
    startCol: target.startCol,
    // header + data + the provenance footer row (PROVENANCE_FOOTER_ROWS). The
    // footer row MUST be inside the lock: it is a cell this operation writes,
    // and dropping it would leave a neighbouring table's refresh free to mutate
    // the very cell the footer lands on. Pinned to the shared constant so the
    // insert's footprint and the refresh's footprint cannot drift apart.
    rowCount: rows.length + 1 + PROVENANCE_FOOTER_ROWS,
    colCount: headers.length,
  };
  const blockKeys = blockKeysForRect(target.sheetName, rect);

  // Bug-7397 R12-3: a wedged holder of these blocks must not hang the insert
  // silently. On acquisition timeout NOTHING was written, so report the same
  // fail-closed `blocked` outcome an unresolvable target produces -- the UI
  // turns it into a "busy, try again" message instead of a frozen button.
  try {
    return await withTableLocksKeys(blockKeys, async (): Promise<TableInsertResult> => {
      // Phase 1: the actual data write + table object (insertResultTable). It
      // writes to the PINNED target, never re-reading the selection / active
      // sheet (so the write cannot drift off the locked cells).
      const writeResult = await insertResultTable(
        headers, rows, metadata?.formatTokens, useActiveCell, forceOverwrite,
        { sheetName: target.sheetName, startRow: target.startRow, startCol: target.startCol },
      );
      if (!writeResult.address) return { address: null, postStepWarning: false };

      // Phase 2: post-insert metadata tagging + provenance (non-critical). A
      // throw here does NOT propagate as "Insert failed" -- the table data is
      // already written and visible.
      let postStepWarning = writeResult.tableObjectFailed;
      try {
        const timestamp = new Date().toISOString();
        // setTableMetadataWithinLock (NOT the self-locking setTableMetadata):
        // we already hold the blocks covering these cells, so re-acquiring
        // would deadlock.
        await setTableMetadataWithinLock(writeResult.address, {
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
        // Provenance footer on the SAME pinned sheet as the data (R7-3).
        await insertProvenanceFooter(writeResult.address, headers.length, rows.length, timestamp, metadata, target.sheetName);
      } catch {
        // Bug-6737: metadata tagging failed -- the table data is already committed.
        postStepWarning = true;
      }

      return { address: writeResult.address, postStepWarning };
    });
  } catch (err) {
    if (err instanceof LockAcquireTimeoutError) {
      return { address: null, postStepWarning: false, blocked: true };
    }
    throw err;
  }
}

/** Insert a provenance attribution row below the table range. */
async function insertProvenanceFooter(
  rangeAddress: string,
  colCount: number,
  rowCount: number,
  timestamp: string,
  metadata?: InsertMetadata,
  // Bug-7397 R7-3: the PINNED sheet the data was written to. The footer must
  // target the SAME sheet as the data, not getActiveWorksheet() -- a concurrent
  // chart/pivot insert calling sheets.add().activate() could otherwise send the
  // footer to a different (active) sheet than the pinned data write.
  pinnedSheetName?: string,
): Promise<void> {
  if (typeof Excel === 'undefined') return;
  try {
    await Excel.run(async (context) => {
      const sheet = pinnedSheetName
        ? context.workbook.worksheets.getItem(pinnedSheetName)
        : context.workbook.worksheets.getActiveWorksheet();
      const tableRange = sheet.getRange(rangeAddress);
      tableRange.load(['rowIndex', 'columnIndex']);
      await context.sync();

      const footerRow = tableRange.rowIndex + rowCount + 1; // +1 for header
      const footerRange = sheet.getRangeByIndexes(footerRow, tableRange.columnIndex, 1, colCount);
      // Bug-7397 R12-2: one shared formatter for the trailing date segment, so a
      // refresh that RESTAMPS this footer produces byte-identical formatting.
      const dateStr = formatProvenanceTimestamp(timestamp);
      // F-025-23: enrich the footer with model and, when active, persona so the
      // inserted data is self-describing. Labels fall back to ids; absent both,
      // the footer keeps its original minimal form.
      const parts: string[] = [strings.provenance.source];
      const model = metadata?.modelLabel || metadata?.modelId;
      if (model) parts.push(`${strings.provenance.model}: ${model}`);
      const persona = metadata?.personaLabel || metadata?.personaId;
      if (persona) parts.push(`${strings.provenance.viewingAs}: ${persona}`);
      // Bug-7417: surface the filters and ordering that produced the numbers so
      // two tables from the same measures but different slices are
      // distinguishable. Derived from the executed SemanticQuery captured in
      // metadata.semanticQuery; degrades silently when absent/malformed.
      for (const segment of buildQueryProvenanceParts(metadata?.semanticQuery)) {
        parts.push(segment);
      }
      parts.push(dateStr);
      footerRange.getCell(0, 0).values = [[joinProvenanceFooter(parts)]];
      // Bug-7397 R12-2: shared constants -- the refresh path re-applies exactly
      // these at the footer's new row and reverts them on the row it vacated.
      footerRange.format.font.italic = FOOTER_FONT_ITALIC;
      footerRange.format.font.size = FOOTER_FONT_SIZE;
      footerRange.format.font.color = FOOTER_FONT_COLOR;
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
  const msg = templates.confirm.largeResult(rows.length.toLocaleString(), LARGE_ROW_THRESHOLD.toLocaleString());
  return safeConfirm(msg, confirmGuard);
}

// F-025-16 / Bug-6363: the model currently loaded in this hook instance. It is
// threaded into every internal trackEntityUsage call so manifest entries are
// scoped to their model (matching the ReportBuilder direct-insert path). Without
// it, entity-formula and scorecard inserts wrote UNSCOPED manifest entries,
// which the staleness check treats as legacy/global and then flags as "Deleted
// from source" the moment a different model is loaded.
export function useExcel(confirmGuard?: ConfirmGuard, onBusy?: () => void, modelId?: string) {
  // F-19: Per-category busy flags so independent operation types don't block each other
  const busyTable = useRef(false);
  const busyChart = useRef(false);
  const busyPivot = useRef(false);
  const busyFormula = useRef(false);
  const largeGuardConfirmed = useRef(false);
  // Bug-6740: anchor-collision tracker records recently-written ranges.
  // After each successful table write, the range is committed to the tracker.
  // Future inserts can use checkCollision/findSafeAnchor to detect and avoid
  // overlapping writes. The existing per-category busy flags prevent
  // concurrent same-type writes; this tracker adds post-write range history.
  const insertTracker = useMemo(() => new InsertTracker(), []);

  const insertTable = useCallback(async (
    headers: string[],
    rows: (string | number)[][],
    options?: InsertTableOptions,
    metadata?: InsertMetadata,
  ): Promise<TableInsertResult> => {
    if (busyTable.current) { onBusy?.(); return { address: null, postStepWarning: false }; }
    busyTable.current = true;

    try {
      if (!largeGuardConfirmed.current && rows.length > LARGE_ROW_THRESHOLD) {
        const proceed = await confirmLargeResult(rows, confirmGuard);
        if (!proceed) { busyTable.current = false; return { address: null, postStepWarning: false }; }
        largeGuardConfirmed.current = true;
      }

      try {
        const result = await doInsertAndTag(headers, rows, metadata, options?.useActiveCell);
        largeGuardConfirmed.current = false;
        invalidateMetadataCache();
        // Bug-6740: record the written range so subsequent inserts detect it.
        if (result.address) {
          const parsed = parseRangeAddress(result.address);
          if (parsed) {
            const opKey = InsertTracker.operationKey('table', result.address);
            insertTracker.tryStart(opKey);
            insertTracker.registerInFlight(opKey, {
              ...parsed,
              rowCount: rows.length + 1,
              colCount: headers.length,
            });
            insertTracker.commitRange(opKey);
            insertTracker.complete(opKey);
          }
        }
        return result;
      } catch (e) {
        if (e instanceof Error && e.message === 'OVERWRITE_WARNING') {
          const msg = strings.confirm.overwriteActiveCell;
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (confirmed) {
            const result = await doInsertAndTag(headers, rows, metadata, false, true);
            largeGuardConfirmed.current = false;
            invalidateMetadataCache();
            if (result.address) {
              const parsed = parseRangeAddress(result.address);
              if (parsed) {
                const opKey = InsertTracker.operationKey('table', result.address);
                insertTracker.tryStart(opKey);
                insertTracker.registerInFlight(opKey, {
                  ...parsed,
                  rowCount: rows.length + 1,
                  colCount: headers.length,
                });
                insertTracker.commitRange(opKey);
                insertTracker.complete(opKey);
              }
            }
            return result;
          }
          largeGuardConfirmed.current = false;
          return { address: null, postStepWarning: false };
        }
        largeGuardConfirmed.current = false;
        throw e;
      }
    } finally {
      busyTable.current = false;
    }
  }, [confirmGuard, onBusy, insertTracker]);

  /**
   * Bug-7397 R12-1: the single-cell formula write now runs under the block lock
   * covering the cell it writes, against a target pinned from ONE host sample.
   * Before this, it opened its own Excel.run and wrote with no lock at all --
   * the external gate reproduced a formula insert overwriting a cell WHILE a
   * covering refresh lock was held (the headline wrong-numbers class, different
   * producer).
   *
   * Returns TRUE only when a formula was actually written. False means no write
   * happened: the user declined the overwrite confirm (deliberately silent, per
   * the Bug-6709 no-toast-on-decline invariant) or the covering blocks were busy
   * (already surfaced through `onBusy`). Callers must not report success on
   * false -- that would be the false-success class this lane exists to remove.
   */
  const insertFormula = useCallback(async (formula: string, targetCell?: string): Promise<boolean> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    const target = await resolveWriteTarget(targetCell);
    // Fail closed: never write to an undetermined (therefore unlockable) cell.
    if (!target) throw new Error('Excel write target could not be resolved');
    const outcome = await withPinnedCellWrite(target, { rowCount: 1, colCount: 1 }, async ({ context, sheet }) => {
      const range = sheet.getRangeByIndexes(target.startRow, target.startCol, 1, 1);
      range.load(['values', 'formulas']);
      await context.sync();
      const currentValue = range.values[0]?.[0];
      const currentFormula = (range.formulas[0]?.[0] as string) || '';
      if ((currentValue !== null && currentValue !== undefined && currentValue !== '') || currentFormula !== '') {
        const msg = strings.confirm.overwriteTargetCell;
        const confirmed = await safeConfirm(msg, confirmGuard);
        if (!confirmed) return false;
      }
      range.formulas = [[formula]];
      await context.sync();
      return true;
    });
    if (!outcome.ok) { onBusy?.(); return false; }
    return outcome.value;
  }, [confirmGuard, onBusy]);

  /**
   * Bug-7393: write a literal value to the selected (or targeted) cell using the
   * values channel. Unlike insertFormula, this NEVER interprets the value as a
   * formula -- a string starting with '=' is written as text, closing the
   * formula-injection path for source-derived static inserts (CWE-1236).
   *
   * Bug-7397 R12-1: locked + pinned, and returns whether the write happened
   * (see insertFormula for the full rationale).
   */
  const insertLiteral = useCallback(async (value: string | number | null, targetCell?: string): Promise<boolean> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    const target = await resolveWriteTarget(targetCell);
    if (!target) throw new Error('Excel write target could not be resolved');
    const outcome = await withPinnedCellWrite(target, { rowCount: 1, colCount: 1 }, async ({ context, sheet }) => {
      const range = sheet.getRangeByIndexes(target.startRow, target.startCol, 1, 1);
      range.load(['values', 'formulas']);
      await context.sync();
      const currentValue = range.values[0]?.[0];
      const currentFormula = (range.formulas[0]?.[0] as string) || '';
      if ((currentValue !== null && currentValue !== undefined && currentValue !== '') || currentFormula !== '') {
        const msg = strings.confirm.overwriteTargetCell;
        const confirmed = await safeConfirm(msg, confirmGuard);
        if (!confirmed) return false;
      }
      range.values = [[value === null ? '' : value]];
      await context.sync();
      return true;
    });
    if (!outcome.ok) { onBusy?.(); return false; }
    return outcome.value;
  }, [confirmGuard, onBusy]);

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
  ): Promise<{ address: string | null; postStepWarning: boolean }> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (rows.length === 0) throw new Error('Cannot create chart from empty data');
    if (busyChart.current) { onBusy?.(); return { address: null, postStepWarning: false }; }
    busyChart.current = true;

    try {
      if (!await confirmLargeResult(rows, confirmGuard)) return { address: null, postStepWarning: false };

      const recommendation = chartType
        ? { chartType, confidence: 'high' as const, reason: 'User selected' }
        : recommendChartType(headers, rows, annotation);
      const excelChartType = getChartTypeEnum(recommendation.chartType);

      const title = annotation?.measures
        ? Object.values(annotation.measures).map(m => m.title).join(' / ')
        : 'Tessallite Result';

      let savedRangeAddress: string | null = null;
      let postStepWarning = false;

      // Bug-7397 R12-1: NOT routed through withPinnedCellWrite, deliberately --
      // and the exemption is now unconditional. EVERY cell write in this
      // operation (the data range here, and the chart's own source block that
      // `createChartOnSheet` writes) goes into a worksheet this operation
      // CREATES, with a freshly de-duplicated name. A brand-new sheet holds no
      // content and no other operation can hold blocks on it, so there is
      // nothing to exclude.
      //
      // Bug-7397 R12 review finding 2: an `existingRangeAddress` parameter used
      // to route BOTH writes onto `getActiveWorksheet()` with no lock -- a
      // multi-cell unlocked write into the user's live sheet at column A, a
      // wider blast radius than any writer this round locked down. It had no
      // caller anywhere in the app, so the branch is deleted rather than
      // wrapped: dead code that silently violates the lock contract is exactly
      // what a future caller would trip over.
      //
      // Bug-6733: two-phase chart creation. Phase 1 creates the chart
      // (critical -- must succeed for the user to see anything). Phase 2
      // applies axis formatting (non-critical -- the chart is visible and
      // correct without it; axis titles default instead of showing field
      // names). If phase 2 fails, the chart was still created.
      await Excel.run(async (context) => {
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

        const sheet = sheets.add(name);
        sheet.activate();

        const rowCount = rows.length + 1;
        const colCount = headers.length;
        const dataRange = sheet.getRangeByIndexes(0, 0, rowCount, colCount);
        dataRange.values = [headers, ...rows];
        dataRange.format.autofitColumns();
        dataRange.load('address');
        await context.sync();

        const table = sheet.tables.add(dataRange, true);
        table.style = 'TableStyleMedium2';
        await context.sync();

        savedRangeAddress = dataRange.address;

        // Phase 1: create the chart (critical)
        const { chart, chartHeaders } = createChartOnSheet(excelChartType, sheet, headers, rows, annotation, title);
        await context.sync();

        // Phase 2: axis formatting (non-critical)
        // R1 Finding 2: pass chartHeaders as fallback so the value-axis
        // title shows the real measure column names when annotation is
        // absent (Ask-Tessallite / KPI-panel chart paths).
        try {
          applyChartAxisFormatting(chart, annotation, chartHeaders.slice(1));
          await context.sync();
        } catch {
          // Bug-6733: axis formatting failed (e.g. pie charts on some hosts
          // do not support category axes). The chart itself is already
          // persisted; note the cosmetic gap so the caller can warn.
          postStepWarning = true;
        }
      });

      // Post-insert metadata tagging (non-critical -- provenance tracking
      // only; must never propagate a failure toast for an inserted chart).
      try {
        if (savedRangeAddress) {
          await setTableMetadata(savedRangeAddress, {
            pluginVersion: '0.1.0',
            timestamp: new Date().toISOString(),
          });
        }
        invalidateMetadataCache();
      } catch {
        // Bug-6733: metadata tagging failure is invisible to the user.
        postStepWarning = true;
      }

      return { address: savedRangeAddress, postStepWarning };
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
      // Bug-7397 R12-1: NOT routed through withPinnedCellWrite, deliberately --
      // the source data is written into a worksheet this operation creates
      // (`Pivot Data (n)`), which no other operation can be touching. See the
      // matching note in insertChart.
      let savedRangeAddress: string | null = null;

      await Excel.run(async (context) => {
        const sheets = context.workbook.worksheets;
        sheets.load('items/name');
        await context.sync();
        const existingNames = new Set(sheets.items.map(s => s.name));

        // Bug-6356: collect existing PivotTable names across ALL sheets so
        // the new PivotTable gets a workbook-unique name (Excel requires
        // PivotTable names to be unique per workbook, not per sheet).
        const existingPivotNames = new Set<string>();
        for (const ws of sheets.items) {
          ws.pivotTables.load('items/name');
          ws.tables.load('items/name');
        }
        await context.sync();
        const existingTableNames = new Set<string>();
        for (const ws of sheets.items) {
          for (const pt of ws.pivotTables.items) {
            existingPivotNames.add(pt.name);
          }
          for (const table of ws.tables.items) {
            existingTableNames.add(table.name);
          }
        }

        let dataName = 'Pivot Data';
        let counter = 1;
        while (existingNames.has(dataName)) {
          dataName = `Pivot Data (${counter})`;
          counter++;
        }

        const dataSheet = sheets.add(dataName);
        let pivotSheet: Excel.Worksheet | null = null;
        let table: Excel.Table | null = null;

        try {
          const rowCount = rows.length + 1;
          const colCount = headers.length;
          const range = dataSheet.getRangeByIndexes(0, 0, rowCount, colCount);
          range.values = [headers, ...rows];
          range.format.autofitColumns();
          range.load('address');
          await context.sync();

          table = dataSheet.tables.add(range, true);
          table.name = nextUniqueLocalPivotTableName(existingTableNames, dataName);
          table.style = 'TableStyleMedium2';
          table.load('name');
          await context.sync();

          const addr = range.address;
          const qualifiedAddr = addr.includes('!') ? addr : `'${dataName}'!${addr}`;

          let pivotName = 'Local Pivot';
          counter = 1;
          while (existingNames.has(pivotName)) {
            pivotName = `Local Pivot (${counter})`;
            counter++;
          }

          pivotSheet = sheets.add(pivotName);
          pivotSheet.activate();

          // Bug-6356: generate a unique PivotTable name using the same counter
          // pattern applied to the sheet name. The first pivot is
          // "TessalliteLocalPivot"; subsequent ones are suffixed _2, _3, etc.
          let pivotTableName = 'TessalliteLocalPivot';
          let ptCounter = 2;
          while (existingPivotNames.has(pivotTableName)) {
            pivotTableName = `TessalliteLocalPivot_${ptCounter}`;
            ptCounter++;
          }

          const pivotRange = pivotSheet.getRange('A1');
          const pivotTable = pivotSheet.pivotTables.add(
            pivotTableName,
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

          const resolved = resolvePivotFieldMapping(hierarchyMap, mapping);
          for (const hier of resolved.rowFields) {
            pivotTable.rowHierarchies.add(hier);
          }
          for (const hier of resolved.dataFields) {
            pivotTable.dataHierarchies.add(hier);
          }
          for (const hier of resolved.columnFields) {
            pivotTable.columnHierarchies.add(hier);
          }
          for (const hier of resolved.filterFields) {
            pivotTable.filterHierarchies.add(hier);
          }

          dataSheet.visibility = Excel.SheetVisibility.hidden;
          savedRangeAddress = qualifiedAddr;
          await context.sync();
        } catch (error) {
          if (table) {
            table.delete();
          }
          if (pivotSheet) {
            pivotSheet.delete();
          }
          dataSheet.delete();
          await context.sync();
          savedRangeAddress = null;
          throw error;
        }
      });

      // Bug-7397 fix #4: setTableMetadata surfaces errors (re-throws) instead
      // of swallowing them silently. Metadata tagging is non-critical here --
      // the pivot data is already committed -- but a failure must NOT be fully
      // silent (Bug-7397 R6 MEDIUM): log it so the lost-provenance case is
      // observable rather than a silent continue-as-success. setTableMetadata
      // (self-locking) is correct: this is a standalone write, not nested in a
      // held per-table critical section.
      try {
        if (savedRangeAddress) {
          await setTableMetadata(savedRangeAddress, {
            pluginVersion: '0.1.0',
            timestamp: new Date().toISOString(),
          });
          invalidateMetadataCache();
        }
      } catch (err) {
        if (typeof console !== 'undefined' && console.warn) {
          console.warn('[tessallite] insertLocalPivot: provenance tagging failed (pivot data is committed):', err);
        }
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
      // Bug-7397 R12-1: pinned target + block locks over the whole written
      // column (set cell + `memberCount` member cells).
      const target = await resolveWriteTarget();
      if (!target) throw new Error('Excel write target could not be resolved');
      const totalRows = 1 + memberCount;
      const outcome = await withPinnedCellWrite(target, { rowCount: totalRows, colCount: 1 }, async ({ context, sheet }) => {
        const row = target.startRow;
        const col = target.startCol;

        const targetRange = sheet.getRangeByIndexes(row, col, totalRows, 1);
        // Bug-8344: BOTH channels. A formula rendering "" is an empty VALUE but
        // an occupied cell; a values-only probe destroys it without asking.
        targetRange.load('values,formulas');
        await context.sync();

        const hasContent = rangeHasContent(
          targetRange.values as unknown[][], targetRange.formulas as unknown[][],
        );
        if (hasContent) {
          const msg = templates.confirm.overwriteCells(totalRows);
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return null;
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
        return setCell.address as string | null;
      });
      if (!outcome.ok) { onBusy?.(); return null; }
      const startAddress = outcome.value;

      if (startAddress) {
        await trackEntityUsage('named_set', namedSet.id, namedSet.display_name || namedSet.name, startAddress, undefined, namedSet.updated_at, modelId);
      }

      return startAddress;
    } finally {
      busyFormula.current = false;
    }
  }, [confirmGuard, onBusy, modelId]);

  const insertKpiFormulas = useCallback(async (
    kpi: { id: string; name: string; display_name: string | null; updated_at?: string },
    valueMeasureName: string | null,
    goalMeasureName: string | null,
    connectionName: string,
    goalLiteral?: number | null,
    /**
     * Bug-6728: composite-expression KPI literal insert. When `forceLiteral`
     * is true, the Value cell receives `valueLiteral` as a plain number
     * (or blank if null -- e.g. evaluation returned null for div-by-zero /
     * no data). This prevents the formula fallback to
     * CUBEVALUE(CUBEKPIMEMBER Value) which is permanently #N/A for
     * composite KPIs (Bug-6702: no executable value member).
     */
    valueLiteral?: number | null,
    forceLiteral?: boolean,
  ): Promise<string | null> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (busyFormula.current) { onBusy?.(); return null; }
    busyFormula.current = true;

    try {
      // Bug-7397 R12-1: pinned target + block locks over the label/value/goal
      // block this writes.
      const target = await resolveWriteTarget();
      if (!target) throw new Error('Excel write target could not be resolved');
      // Bug-5294: account for the goal row when a static literal is supplied.
      // Bug-6714: the Value row is ALWAYS present -- a custom/expression KPI
      // (no value measure) gets its Value from CUBEKPIMEMBER instead of an
      // empty cell (see kpiValueCellFormula).
      const hasGoalRow = goalMeasureName || goalLiteral != null;
      const totalRows = 2 + (hasGoalRow ? 1 : 0);
      const outcome = await withPinnedCellWrite(target, { rowCount: totalRows, colCount: 2 }, async ({ context, sheet }) => {
        const row = target.startRow;
        const col = target.startCol;

        const targetRange = sheet.getRangeByIndexes(row, col, totalRows, 2);
        // Bug-8344: BOTH channels (see rangeHasContent).
        targetRange.load('values,formulas');
        await context.sync();

        const hasContent = rangeHasContent(
          targetRange.values as unknown[][], targetRange.formulas as unknown[][],
        );
        if (hasContent) {
          const msg = templates.confirm.overwriteRowsCols(totalRows, 2);
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return null;
        }

        let r = row;

        const label = kpi.display_name || kpi.name;

        const labelCell = sheet.getRangeByIndexes(r, col, 1, 1);
        labelCell.values = [[label]];
        labelCell.format.font.bold = true;
        // Bug-8345: the claimed rectangle is `totalRows x 2` and the overwrite
        // prompt named 2 columns, so the label row's second cell belongs to this
        // block too. Leaving it unwritten kept a previous occupant's content
        // sitting inside the KPI block, where it reads as part of it.
        sheet.getRangeByIndexes(r, col + 1, 1, 1).values = [['']];
        labelCell.load('address');
        await context.sync();
        const labelAddress = labelCell.address as string | null;

        r++;
        // Bug-6714 / Bug-6728: the Value row is always present. When
        // forceLiteral is set (composite-expression KPI), write the
        // evaluated value (or blank if evaluation returned null) -- never
        // fall through to a CUBE formula that is permanently #N/A for
        // composites (Bug-6702).
        sheet.getRangeByIndexes(r, col, 1, 1).values = [['Value']];
        if (forceLiteral) {
          // Bug-8345: ALWAYS write the Value cell. The old guard skipped the
          // write entirely when the evaluation returned null (div-by-zero / no
          // data), so a Value cell that already held a previous KPI's number
          // kept showing it while the label and goal around it were replaced --
          // a stale number presented as the current KPI's value. `''` is the
          // blank convention this hook already uses for a null literal
          // (insertKpiStatusOnly, below).
          sheet.getRangeByIndexes(r, col + 1, 1, 1).values = [[valueLiteral ?? '']];
        } else {
          sheet.getRangeByIndexes(r, col + 1, 1, 1).formulas = [[
            kpiValueCellFormula(connectionName, kpi.name, valueMeasureName),
          ]];
        }
        r++;

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
        return labelAddress;
      });
      if (!outcome.ok) { onBusy?.(); return null; }
      const startAddress = outcome.value;

      if (startAddress) {
        await trackEntityUsage('kpi', kpi.id, kpi.display_name || kpi.name, startAddress, undefined, kpi.updated_at, modelId);
      }

      return startAddress;
    } finally {
      busyFormula.current = false;
    }
  }, [confirmGuard, onBusy, modelId]);

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
      // Bug-7397 R12-1: pinned target + block locks over the 1 x 4 KPI row.
      const target = await resolveWriteTarget();
      if (!target) throw new Error('Excel write target could not be resolved');
      // Bug-6729: Trend column dropped -- the gateway does not serve a KPI
      // Trend member, so a CUBEKPIMEMBER Trend formula is permanently #N/A.
      // Layout: Label | Value | Goal | Status (4 columns).
      const colCount = 4;
      const outcome = await withPinnedCellWrite(target, { rowCount: 1, colCount }, async ({ context, sheet }) => {
        const row = target.startRow;
        const col = target.startCol;

        const targetRange = sheet.getRangeByIndexes(row, col, 1, colCount);
        // Bug-8344: BOTH channels (see rangeHasContent).
        targetRange.load('values,formulas');
        await context.sync();

        const hasContent = rangeHasContent(
          targetRange.values as unknown[][], targetRange.formulas as unknown[][],
        );
        if (hasContent) {
          const msg = templates.confirm.overwriteRowByColsLabel(1, colCount);
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return null;
        }

        const label = kpi.display_name || kpi.name;
        // F-025-08: the gateway publishes KPI_NAME = kpi.name (the technical
        // name), so a CUBEKPIMEMBER built from the display name never resolves.
        // Emit the technical name regardless of how the KPI is labelled in the UI.
        const kpiMemberName = kpi.name;

        sheet.getRangeByIndexes(row, col, 1, 1).values = [[label]];
        sheet.getRangeByIndexes(row, col, 1, 1).format.font.bold = true;

        // Bug-6714: the Value cell is never left empty -- a custom KPI (no
        // value measure) gets CUBEVALUE(CUBEKPIMEMBER Value) (kpiValueCellFormula).
        sheet.getRangeByIndexes(row, col + 1, 1, 1).formulas = [[
          kpiValueCellFormula(connectionName, kpiMemberName, valueMeasureName),
        ]];

        if (goalMeasureName) {
          sheet.getRangeByIndexes(row, col + 2, 1, 1).formulas = [[
            generateCubeValue(connectionName, measureMemberRef(goalMeasureName)),
          ]];
        } else {
          // Bug-5294: static-target KPI — write the numeric goal directly,
          // mirroring the scorecard path (F-025-10).
          // Bug-8345 (same class): with neither a goal measure nor a static
          // target -- reachable from ReportBuilder whenever `target_type` is not
          // 'static' -- the Goal cell used to be skipped even though it sits
          // inside the 1x4 rectangle this writer locked and the user confirmed
          // overwriting. A previous KPI's goal survived there as a stale number.
          sheet.getRangeByIndexes(row, col + 2, 1, 1).values = [[goalLiteral ?? '']];
        }

        // Bug-6729: Status uses CUBEVALUE(CUBEKPIMEMBER) to get the numeric
        // value (-1/0/1) the icon-set conditional format needs, not the caption.
        sheet.getRangeByIndexes(row, col + 3, 1, 1).formulas = [[
          kpiStatusCellFormula(connectionName, kpiMemberName),
        ]];

        // F-025-27 / Bug-2860: icon-set formats require both a style AND a
        // criteria array (see kpiIconCriteria). All KPI icon-set sites use it.
        const statusCell = sheet.getRangeByIndexes(row, col + 3, 1, 1);
        const statusCf = statusCell.conditionalFormats.add(Excel.ConditionalFormatType.iconSet);
        statusCf.iconSetOrNullObject.style = Excel.IconSet.threeTrafficLights1;
        statusCf.iconSetOrNullObject.criteria = kpiIconCriteria();

        const labelCell = sheet.getRangeByIndexes(row, col, 1, 1);
        labelCell.load('address');
        await context.sync();
        return labelCell.address as string | null;
      });
      if (!outcome.ok) { onBusy?.(); return null; }
      const startAddress = outcome.value;

      if (startAddress) {
        await trackEntityUsage('kpi', kpi.id, kpi.display_name || kpi.name, startAddress, undefined, kpi.updated_at, modelId);
      }

      return startAddress;
    } finally {
      busyFormula.current = false;
    }
  }, [confirmGuard, onBusy, modelId]);

  const insertKpiValueOnly = useCallback(async (
    valueMeasureName: string,
    connectionName: string,
  ): Promise<string | null> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (busyFormula.current) { onBusy?.(); return null; }
    busyFormula.current = true;

    try {
      // Bug-7397 R12-1: pinned target + block lock over the single written cell.
      const target = await resolveWriteTarget();
      if (!target) throw new Error('Excel write target could not be resolved');
      const outcome = await withPinnedCellWrite(target, { rowCount: 1, colCount: 1 }, async ({ context, sheet }) => {
        const cell = sheet.getRangeByIndexes(target.startRow, target.startCol, 1, 1);
        cell.load('values,formulas');
        await context.sync();

        const currentValue = cell.values[0]?.[0];
        const currentFormula = (cell.formulas[0]?.[0] as string) || '';
        if ((currentValue !== null && currentValue !== undefined && currentValue !== '') || currentFormula !== '') {
          const msg = strings.confirm.overwriteTargetCell;
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return null;
        }

        cell.formulas = [[generateCubeValue(connectionName, measureMemberRef(valueMeasureName))]];
        cell.load('address');
        await context.sync();
        return cell.address as string | null;
      });
      if (!outcome.ok) { onBusy?.(); return null; }
      return outcome.value;
    } finally {
      busyFormula.current = false;
    }
  }, [confirmGuard, onBusy]);

  const insertKpiStatusOnly = useCallback(async (
    kpiName: string,
    connectionName: string,
    /**
     * Bug-7397 R12 review finding 1 (BLOCKING): a composite-expression KPI has
     * no resolvable CUBE status member, so ReportBuilder used to call this to
     * write the formula and then overwrite the same cell with the evaluated
     * literal from its OWN raw `Excel.run` -- completely outside the block-lock
     * contract, AND as a second acquisition after this one released (the exact
     * two-acquisition TOCTOU R6 closed for the insert path).
     *
     * Passing the literal here collapses both writes into the ONE critical
     * section this function already holds: the cell ends up with the literal,
     * never transiently with a formula another operation could observe or
     * interleave with. `undefined` keeps the CUBE-formula behaviour for every
     * ordinary KPI.
     * Passed as an OPTIONS OBJECT rather than a bare value (review round 2,
     * finding 7): with a bare parameter, a caller writing `ev?.status` instead
     * of `ev?.status ?? ''` would hand in `undefined` on a failed evaluation
     * and silently fall back to a CUBE formula that is permanently `#N/A` for
     * a composite KPI. Constructing `{ statusLiteral }` makes the override
     * explicit and un-collapsible. Omitting the object entirely keeps the
     * CUBE-formula behaviour for every ordinary KPI.
     */
    literal?: { statusLiteral: string | number | null },
  ): Promise<string | null> => {
    // Bug-5290: the parameter was previously named kpiDisplayName, but the
    // gateway publishes KPI_NAME = kpi.name (the technical name). All callers
    // already pass the technical name; the rename makes the contract explicit
    // so future call sites cannot accidentally pass the display name.
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (busyFormula.current) { onBusy?.(); return null; }
    busyFormula.current = true;

    try {
      // Bug-7397 R12-1: pinned target + block lock over the single written cell.
      const target = await resolveWriteTarget();
      if (!target) throw new Error('Excel write target could not be resolved');
      const outcome = await withPinnedCellWrite(target, { rowCount: 1, colCount: 1 }, async ({ context, sheet }) => {
        const cell = sheet.getRangeByIndexes(target.startRow, target.startCol, 1, 1);
        cell.load('values,formulas');
        await context.sync();

        const currentValue = cell.values[0]?.[0];
        const currentFormula = (cell.formulas[0]?.[0] as string) || '';
        if ((currentValue !== null && currentValue !== undefined && currentValue !== '') || currentFormula !== '') {
          const msg = strings.confirm.overwriteTargetCell;
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return null;
        }

        if (literal) {
          // Composite-expression KPI: the evaluated status goes in through the
          // VALUES channel (never `formulas` -- Bug-7393 formula injection) as
          // the cell's ONLY write, inside this held critical section.
          cell.values = [[literal.statusLiteral === null ? '' : literal.statusLiteral]];
        } else {
          // Bug-6729: wrap in CUBEVALUE so Excel shows the numeric -1/0/1
          // value, not the member caption text.
          cell.formulas = [[kpiStatusCellFormula(connectionName, kpiName)]];
        }

        const cf = cell.conditionalFormats.add(Excel.ConditionalFormatType.iconSet);
        cf.iconSetOrNullObject.style = Excel.IconSet.threeTrafficLights1;
        cf.iconSetOrNullObject.criteria = kpiIconCriteria();

        cell.load('address');
        await context.sync();
        return cell.address as string | null;
      });
      if (!outcome.ok) { onBusy?.(); return null; }
      return outcome.value;
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
      // Bug-7397 R12-1: pinned target + block lock over the single written cell.
      const target = await resolveWriteTarget();
      if (!target) throw new Error('Excel write target could not be resolved');
      const outcome = await withPinnedCellWrite(target, { rowCount: 1, colCount: 1 }, async ({ context, sheet }) => {
        const cell = sheet.getRangeByIndexes(target.startRow, target.startCol, 1, 1);
        cell.load('values,formulas');
        await context.sync();

        const currentValue = cell.values[0]?.[0];
        const currentFormula = (cell.formulas[0]?.[0] as string) || '';
        if ((currentValue !== null && currentValue !== undefined && currentValue !== '') || currentFormula !== '') {
          const msg = strings.confirm.overwriteTargetCell;
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return null;
        }

        // Bug-6729: wrap in CUBEVALUE so Excel shows the numeric value, not
        // the member caption text.
        cell.formulas = [[kpiValueCellFormula(connectionName, kpiName, null)]];
        cell.load('address');
        await context.sync();
        return cell.address as string | null;
      });
      if (!outcome.ok) { onBusy?.(); return null; }
      return outcome.value;
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
      // Bug-7397 R12-1: pinned target + block lock over the single written cell.
      const target = await resolveWriteTarget();
      if (!target) throw new Error('Excel write target could not be resolved');
      const outcome = await withPinnedCellWrite(target, { rowCount: 1, colCount: 1 }, async ({ context, sheet }) => {
        const cell = sheet.getRangeByIndexes(target.startRow, target.startCol, 1, 1);
        cell.load('values,formulas');
        await context.sync();

        const currentValue = cell.values[0]?.[0];
        const currentFormula = (cell.formulas[0]?.[0] as string) || '';
        if ((currentValue !== null && currentValue !== undefined && currentValue !== '') || currentFormula !== '') {
          const msg = strings.confirm.overwriteTargetCell;
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return null;
        }

        const formula = generateCubeValue(connectionName, measureMemberRef(measureName));
        cell.formulas = [[formula]];
        cell.load('address');
        await context.sync();
        return cell.address as string | null;
      });
      if (!outcome.ok) { onBusy?.(); return null; }
      return outcome.value;
    } finally {
      busyFormula.current = false;
    }
  }, [confirmGuard, onBusy]);

  const insertKpiScorecard = useCallback(async (
    kpis: EvaluatedScorecardKpi[],
    _connectionName: string,
    modelSlug?: string,
  ): Promise<string | null> => {
    if (typeof Excel === 'undefined') throw new Error('Excel API not available');
    if (busyFormula.current) { onBusy?.(); return null; }
    if (kpis.length === 0) return null;
    busyFormula.current = true;

    try {
      // Bug-7397 R12-1: pinned target + block locks over the whole scorecard
      // rectangle (header row + one row per KPI).
      const target = await resolveWriteTarget();
      if (!target) throw new Error('Excel write target could not be resolved');
      const colCount = SCORECARD_HEADERS.length;
      const totalRows = 1 + kpis.length;
      const outcome = await withPinnedCellWrite(target, { rowCount: totalRows, colCount }, async ({ context, sheet }) => {
        const startRow = target.startRow;
        const startCol = target.startCol;

        const targetRange = sheet.getRangeByIndexes(startRow, startCol, totalRows, colCount);
        // Bug-8344: BOTH channels (see rangeHasContent).
        targetRange.load('values,formulas');
        await context.sync();

        const hasContent = rangeHasContent(
          targetRange.values as unknown[][], targetRange.formulas as unknown[][],
        );
        if (hasContent) {
          const msg = templates.confirm.overwriteRowsCols(totalRows, colCount);
          const confirmed = await safeConfirm(msg, confirmGuard);
          if (!confirmed) return null;
        }

        const headers = [...SCORECARD_HEADERS];
        const headerRange = sheet.getRangeByIndexes(startRow, startCol, 1, colCount);
        headerRange.values = [headers];
        headerRange.format.font.bold = true;
        headerRange.format.fill.color = '#f5f5f5';

        // Bug-6903: when model slug is available, write TESSALLITE.KPI formulas
        // for live refresh (connectionless custom functions). Fall back to
        // literal values when slug is absent.
        if (modelSlug) {
          const formulaRows = buildFormulaScorecardRows(kpis, modelSlug);
          if (formulaRows.length > 0) {
            sheet.getRangeByIndexes(startRow + 1, startCol, formulaRows.length, colCount).formulas = formulaRows;
          }
        } else {
          const rows = buildLiteralScorecardRows(kpis);
          if (rows.length > 0) {
            sheet.getRangeByIndexes(startRow + 1, startCol, rows.length, colCount).values = rows;
          }
        }

        for (let i = 0; i < kpis.length; i++) {
          const row = startRow + 1 + i;
          sheet.getRangeByIndexes(row, startCol, 1, 1).format.font.bold = true;

          const statusCell = sheet.getRangeByIndexes(row, startCol + 3, 1, 1);
          const statusCf = statusCell.conditionalFormats.add(Excel.ConditionalFormatType.iconSet);
          statusCf.iconSetOrNullObject.style = Excel.IconSet.threeTrafficLights1;
          statusCf.iconSetOrNullObject.criteria = kpiIconCriteria();
        }

        const firstCell = sheet.getRangeByIndexes(startRow, startCol, 1, 1);
        firstCell.load('address');
        await context.sync();
        return firstCell.address as string | null;
      });
      if (!outcome.ok) { onBusy?.(); return null; }
      const startAddress = outcome.value;

      if (startAddress) {
        for (const kpi of kpis) {
          await trackEntityUsage('kpi', kpi.id, kpi.display_name || kpi.name, startAddress, undefined, kpi.updated_at, modelId);
        }
      }

      return startAddress;
    } finally {
      busyFormula.current = false;
    }
  }, [confirmGuard, onBusy, modelId]);

  return {
    insertTable,
    insertChart,
    insertLocalPivot,
    insertFormula,
    insertLiteral,
    insertNamedSetAsFormulas,
    insertKpiFormulas,
    insertKpiFullRow,
    insertKpiValueOnly,
    insertKpiStatusOnly,
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
