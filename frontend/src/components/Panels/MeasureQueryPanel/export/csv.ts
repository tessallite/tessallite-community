import type { Measure, MeasureFormatToken } from "../../../../api/types";
import { cellLookupKey } from "../pivot";
import type { CellCoord, PivotModel } from "../types";
import { NOT_ADDITIVE, type TotalsModel, type TotalValue } from "../totals";
import type { EmptyCellMode } from "../grid/PivotGrid";
import { csvSafeCell } from "../../../../utils/sanitize";

// Bug-7286: a cell that is a genuine number (optionally negative / decimal /
// scientific-notation) must keep its numeric form so spreadsheets treat it as a
// number, not text. F-015-04: raw measure values that are very small/large
// (e.g. 1e-7) serialise via String(num) in exponent form; those are still
// genuine numbers and must not be quoted as text. Every OTHER value — dimension
// members, labels, string-valued measures — is routed through csvSafeCell so a
// leading =/+/-/@/tab/CR/LF cannot execute as a formula. A scientific-notation
// token still begins with a digit or '-', so this stays injection-safe.
function isPlainNumber(s: string): boolean {
  return /^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(s);
}

function csvEscape(s: string): string {
  // Neutralise spreadsheet formula injection first, then apply RFC-4180 quoting
  // so the guard prefix ends up inside any surrounding quotes.
  const guarded = isPlainNumber(s) ? s : csvSafeCell(s);
  if (/[",\r\n]/.test(guarded)) return `"${guarded.replace(/"/g, '""')}"`;
  return guarded;
}

function rowToCsv(row: string[]): string {
  return row.map(csvEscape).join(",");
}

export interface CsvExportLabels {
  subtotalSuffix?: string;
  grandTotal?: string;
}

export interface CsvOptions {
  // F-019-08: extra column measures so the export carries EVERY measure on the
  // grid, not just the first. Each measure renders one value column per cell.
  extraMeasures?: Measure[];
  // Per-measure totals (keyed by measure.name) so the export carries the same
  // subtotals/grand totals the grid shows, not a single first-measure column.
  allTotals?: Map<string, TotalsModel | null> | null;
  showSubtotals?: boolean;
  showGrandTotals?: boolean;
  emptyCellMode?: EmptyCellMode;
  // F-019-08: explicit row/col key order so the export honours the user's
  // current header-click sort instead of the pivot's default order.
  rowKeyOrder?: string[][];
  colKeyOrder?: string[][];
  // Localized labels for export totals.
  labels?: CsvExportLabels;
}

type DisplayCol =
  | { kind: "data"; ck: string[]; ckIndex: number }
  | { kind: "subtotalCol"; head: string }
  | { kind: "grandCol" };

function emptyText(mode: EmptyCellMode): string {
  if (mode === "zero") return "0";
  if (mode === "dash") return "—";
  return "";
}

// F-015-04: CSV is a data interchange format, not a presentation surface.
// It must carry the RAW semantic number so downstream spreadsheets, scripts,
// and reconciliations receive the same value the query produced. The display
// formatter (formatMeasureValue) changes scale (percent ×100), precision
// (rounding), and type (grouping separators → text), so it must NOT touch CSV
// cells. A finite number is emitted verbatim via String(num); a non-numeric
// string measure value is passed through unchanged (csvEscape guards it).
// The `format` parameter is intentionally ignored for CSV.
function rawNumeric(v: unknown): string | null {
  if (v === null || v === undefined || v === "") return null;
  const num = typeof v === "number" ? v : Number(v);
  if (Number.isFinite(num)) return String(num);
  // Non-numeric measure value (e.g. a string measure): keep it as text.
  return String(v);
}

function rawCellText(
  v: unknown,
  _format: MeasureFormatToken | null,
  mode: EmptyCellMode,
): string {
  const raw = rawNumeric(v);
  return raw === null ? emptyText(mode) : raw;
}

function totalCellText(
  v: TotalValue,
  _format: MeasureFormatToken | null,
  mode: EmptyCellMode,
): string {
  if (v === NOT_ADDITIVE) return "—";
  if (v === null) return emptyText(mode);
  const raw = rawNumeric(v);
  return raw === null ? emptyText(mode) : raw;
}

function cellMeasureValue(cell: CellCoord | undefined, m: Measure, first: Measure): unknown {
  if (!cell) return undefined;
  return cell.measureValues?.[m.name] ?? (m.name === first.name ? cell.measureValue : undefined);
}

/**
 * Render the pivot grid as CSV.
 *
 * F-019-08: the export honours every measure column, the user's current sort
 * (via ``rowKeyOrder``/``colKeyOrder``), and subtotals/grand totals — the same
 * content the grid and the XLSX export carry. The old single-measure,
 * default-order, totals-free output is gone.
 */
export function pivotToCsv(
  pivot: PivotModel,
  measure: Measure,
  opts?: CsvOptions,
): string {
  const lb = opts?.labels ?? {};
  const lbSubtotal = lb.subtotalSuffix ?? "Total";
  const lbGrand = lb.grandTotal ?? "Grand Total";
  const allMeasures: Measure[] = [measure, ...(opts?.extraMeasures ?? [])];
  const multiMeasure = allMeasures.length > 1;
  const allTotals = opts?.allTotals ?? null;
  const showSubtotals = opts?.showSubtotals ?? false;
  const showGrandTotals = opts?.showGrandTotals ?? false;
  const emptyMode: EmptyCellMode = opts?.emptyCellMode ?? "blank";

  const { rowCols, colCols, rowLabels, byKey } = pivot;
  // Bug-6285: exported row-dimension headers use business display names.
  const rowHead = (i: number): string => rowLabels[i] ?? rowCols[i];
  const rowKeys = opts?.rowKeyOrder ?? pivot.rowKeys;
  const colKeys = opts?.colKeyOrder ?? pivot.colKeys;

  // Bug-6272: build a map from serialized row key to its ORIGINAL index in
  // pivot.rowKeys. The totals arrays (grandCol, colSubtotals) are indexed by
  // the original pivot order, but when the user sorts, rowKeys may be in a
  // different order. Without this map, the export picks up the wrong row's
  // grand total / column subtotal after a header-click sort.
  const originalRowIndex = new Map<string, number>();
  for (let i = 0; i < pivot.rowKeys.length; i++) {
    originalRowIndex.set(JSON.stringify(pivot.rowKeys[i]), i);
  }
  // Bug-6272 (column axis): same remap for column keys. The totals arrays
  // grandRow and rowSubtotals are indexed by the original pivot.colKeys order;
  // when the user sorts columns via colKeyOrder, the ckIndex stored in each
  // DisplayCol must be the ORIGINAL index, not the sorted position.
  const originalColIndex = new Map<string, number>();
  for (let i = 0; i < pivot.colKeys.length; i++) {
    originalColIndex.set(JSON.stringify(pivot.colKeys[i]), i);
  }
  const hasCols = colCols.length > 0;

  const totalsFor = (m: Measure): TotalsModel | null => allTotals?.get(m.name) ?? null;
  const fmtOf = (m: Measure) => (m.format ?? null) as MeasureFormatToken | null;

  const hasTotals = Boolean(allTotals && allTotals.size);
  const colSubtotalsActive = hasTotals && showSubtotals && colCols.length > 1;
  const grandActive = hasTotals && showGrandTotals;

  // Build the display-column model exactly as the grid/XLSX do.
  const displayCols: DisplayCol[] = [];
  if (hasCols) {
    const colHeads: string[] = [];
    const seen = new Set<string>();
    for (const ck of colKeys) {
      const h = ck[0] ?? "";
      if (!seen.has(h)) {
        seen.add(h);
        colHeads.push(h);
      }
    }
    for (const head of colHeads) {
      colKeys.forEach((ck) => {
        if ((ck[0] ?? "") === head) {
          const ckIndex = originalColIndex.get(JSON.stringify(ck)) ?? 0;
          displayCols.push({ kind: "data", ck, ckIndex });
        }
      });
      if (colSubtotalsActive) displayCols.push({ kind: "subtotalCol", head });
    }
    if (grandActive) displayCols.push({ kind: "grandCol" });
  } else {
    displayCols.push({ kind: "data", ck: [], ckIndex: 0 });
  }

  const lines: string[] = [];

  // Header rows: one per column level, then a measure sub-row when multi-measure.
  if (hasCols) {
    for (let lvl = 0; lvl < colCols.length; lvl++) {
      const labels: string[] = [];
      for (let i = 0; i < rowCols.length; i++) labels.push(lvl === 0 ? rowHead(i) : "");
      const cells: string[] = [...labels];
      for (const dc of displayCols) {
        const span = multiMeasure ? allMeasures.length : 1;
        if (dc.kind === "data") {
          cells.push(dc.ck[lvl] ?? "");
          for (let s = 1; s < span; s++) cells.push("");
        } else {
          const head = dc.kind === "subtotalCol" ? `${dc.head} ${lbSubtotal}` : lbGrand;
          cells.push(lvl === 0 ? head : "");
          for (let s = 1; s < span; s++) cells.push("");
        }
      }
      lines.push(rowToCsv(cells));
    }
    if (multiMeasure) {
      const cells: string[] = rowCols.map(() => "");
      for (const _dc of displayCols) {
        for (const m of allMeasures) cells.push(m.display_name || m.name);
      }
      lines.push(rowToCsv(cells));
    }
  } else if (multiMeasure) {
    lines.push(rowToCsv([...rowCols.map((_, i) => rowHead(i)), ...allMeasures.map((m) => m.display_name || m.name)]));
  } else {
    lines.push(rowToCsv([...rowCols.map((_, i) => rowHead(i)), measure.display_name]));
  }

  function dataCells(rk: string[]): string[] {
    // Bug-6272: resolve the row key back to its original pivot index so that
    // grandCol and colSubtotals look up the correct row's totals even after
    // the grid has been sorted.
    const origIdx = originalRowIndex.get(JSON.stringify(rk)) ?? 0;
    const out: string[] = [];
    for (const dc of displayCols) {
      for (const m of allMeasures) {
        const fmt = fmtOf(m);
        const totals = totalsFor(m);
        if (dc.kind === "data") {
          const cell = byKey.get(cellLookupKey(rk, dc.ck));
          out.push(rawCellText(cellMeasureValue(cell, m, measure), fmt, emptyMode));
        } else if (dc.kind === "subtotalCol") {
          const sub = totals?.colSubtotals.get(dc.head);
          out.push(totalCellText(sub?.[origIdx] ?? null, fmt, emptyMode));
        } else {
          out.push(totalCellText(totals?.grandCol[origIdx] ?? null, fmt, emptyMode));
        }
      }
    }
    return out;
  }

  // Group rows by first level for subtotal rows (matches the grid).
  const rowHeads: string[] = [];
  const rowGroups = new Map<string, string[][]>();
  const seenHeads = new Set<string>();
  for (const rk of rowKeys) {
    const h = rk[0] ?? "";
    if (!seenHeads.has(h)) {
      seenHeads.add(h);
      rowHeads.push(h);
    }
    const arr = rowGroups.get(h) ?? [];
    arr.push(rk);
    rowGroups.set(h, arr);
  }

  const rowSubtotalsActive = hasTotals && showSubtotals && rowCols.length >= 2;
  for (const head of rowHeads) {
    for (const rk of rowGroups.get(head) ?? []) {
      lines.push(rowToCsv([...rk, ...dataCells(rk)]));
    }
    if (rowSubtotalsActive) {
      const labels = rowCols.map((_, i) => (i === 0 ? `${head} ${lbSubtotal}` : ""));
      const cells: string[] = [...labels];
      for (const dc of displayCols) {
        for (const m of allMeasures) {
          const fmt = fmtOf(m);
          const totals = totalsFor(m);
          const subs = totals?.rowSubtotals.get(head);
          if (dc.kind === "data") {
            cells.push(totalCellText(subs?.[dc.ckIndex] ?? null, fmt, emptyMode));
          } else if (dc.kind === "subtotalCol") {
            cells.push(totalCellText(totals?.crossSubtotals.get(`${head}||${dc.head}`) ?? null, fmt, emptyMode));
          } else {
            cells.push(totalCellText(totals?.rowSubtotalGrand.get(head) ?? null, fmt, emptyMode));
          }
        }
      }
      lines.push(rowToCsv(cells));
    }
  }

  // Grand-total row.
  if (grandActive) {
    const labels = rowCols.map((_, i) => (i === 0 ? lbGrand : ""));
    const cells: string[] = [...labels];
    for (const dc of displayCols) {
      for (const m of allMeasures) {
        const fmt = fmtOf(m);
        const totals = totalsFor(m);
        if (dc.kind === "data") {
          cells.push(totalCellText(totals?.grandRow[dc.ckIndex] ?? null, fmt, emptyMode));
        } else if (dc.kind === "subtotalCol") {
          cells.push(totalCellText(totals?.colSubtotalGrand.get(dc.head) ?? null, fmt, emptyMode));
        } else {
          cells.push(totalCellText(totals?.grandGrand ?? null, fmt, emptyMode));
        }
      }
    }
    lines.push(rowToCsv(cells));
  }

  return lines.join("\r\n") + "\r\n";
}
