import type { Measure, MeasureFormatToken } from "../../../../api/types";
import { formatMeasureValue } from "../../../../api/measureFormat";
import { cellLookupKey } from "../pivot";
import type { CellCoord, PivotModel } from "../types";
import { NOT_ADDITIVE, type TotalsModel, type TotalValue } from "../totals";
import type { EmptyCellMode } from "../grid/PivotGrid";

function csvEscape(s: string): string {
  if (/[",\r\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

function rowToCsv(row: string[]): string {
  return row.map(csvEscape).join(",");
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

function rawCellText(
  v: unknown,
  format: MeasureFormatToken | null,
  mode: EmptyCellMode,
): string {
  if (v === null || v === undefined) return emptyText(mode);
  return formatMeasureValue(v, format);
}

function totalCellText(
  v: TotalValue,
  format: MeasureFormatToken | null,
  mode: EmptyCellMode,
): string {
  if (v === NOT_ADDITIVE) return "—";
  if (v === null) return emptyText(mode);
  return formatMeasureValue(v, format);
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
  const allMeasures: Measure[] = [measure, ...(opts?.extraMeasures ?? [])];
  const multiMeasure = allMeasures.length > 1;
  const allTotals = opts?.allTotals ?? null;
  const showSubtotals = opts?.showSubtotals ?? false;
  const showGrandTotals = opts?.showGrandTotals ?? false;
  const emptyMode: EmptyCellMode = opts?.emptyCellMode ?? "blank";

  const { rowCols, colCols, byKey } = pivot;
  const rowKeys = opts?.rowKeyOrder ?? pivot.rowKeys;
  const colKeys = opts?.colKeyOrder ?? pivot.colKeys;
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
      colKeys.forEach((ck, ckIndex) => {
        if ((ck[0] ?? "") === head) displayCols.push({ kind: "data", ck, ckIndex });
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
      for (let i = 0; i < rowCols.length; i++) labels.push(lvl === 0 ? rowCols[i] : "");
      const cells: string[] = [...labels];
      for (const dc of displayCols) {
        const span = multiMeasure ? allMeasures.length : 1;
        if (dc.kind === "data") {
          cells.push(dc.ck[lvl] ?? "");
          for (let s = 1; s < span; s++) cells.push("");
        } else {
          const head = dc.kind === "subtotalCol" ? `${dc.head} Total` : "Grand Total";
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
    lines.push(rowToCsv([...rowCols, ...allMeasures.map((m) => m.display_name || m.name)]));
  } else {
    lines.push(rowToCsv([...rowCols, measure.display_name]));
  }

  function dataCells(rk: string[], rkIndex: number): string[] {
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
          out.push(totalCellText(sub?.[rkIndex] ?? null, fmt, emptyMode));
        } else {
          out.push(totalCellText(totals?.grandCol[rkIndex] ?? null, fmt, emptyMode));
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
  let globalRkIdx = 0;
  for (const head of rowHeads) {
    for (const rk of rowGroups.get(head) ?? []) {
      lines.push(rowToCsv([...rk, ...dataCells(rk, globalRkIdx)]));
      globalRkIdx++;
    }
    if (rowSubtotalsActive) {
      const labels = rowCols.map((_, i) => (i === 0 ? `${head} Total` : ""));
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
    const labels = rowCols.map((_, i) => (i === 0 ? "Grand Total" : ""));
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
