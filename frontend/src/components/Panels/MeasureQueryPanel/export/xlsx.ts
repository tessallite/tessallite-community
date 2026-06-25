import ExcelJS from "exceljs";
import type { Measure, MeasureFormatToken } from "../../../../api/types";
import { cellLookupKey } from "../pivot";
import type { CellCoord, PivotModel } from "../types";
import { NOT_ADDITIVE, type TotalsModel, type TotalValue } from "../totals";
import type { EmptyCellMode } from "../grid/PivotGrid";

export interface XlsxOptions {
  // F-019-08: extra column measures, so the workbook carries every measure
  // on the grid (one value column per measure per cell), not just the first.
  extraMeasures?: Measure[];
  // Totals for the first measure (kept for back-compat).
  totals?: TotalsModel | null;
  // F-019-08: per-measure totals keyed by measure.name.
  allTotals?: Map<string, TotalsModel | null> | null;
  showSubtotals?: boolean;
  showGrandTotals?: boolean;
  emptyCellMode?: EmptyCellMode;
  // F-019-08: the grid's current sorted row order, so the workbook honours
  // the user's header-click sort.
  rowKeyOrder?: string[][];
}

function isPercentToken(token: MeasureFormatToken | null | undefined): boolean {
  return token === "percent" || token === "percent_2dp";
}

function formatTokenToExcel(token: MeasureFormatToken | null | undefined): string {
  if (!token) return "#,##0.00";
  switch (token) {
    case "currency":
      return "$#,##0.00";
    case "percent":
      return '#,##0"%"';
    case "percent_2dp":
      return '#,##0.00"%"';
    case "integer":
    case "decimal_0":
      return "#,##0";
    case "decimal_1":
      return "#,##0.0";
    case "decimal_2dp":
      return "#,##0.00";
    case "decimal_3":
      return "#,##0.000";
    case "decimal_4":
      return "#,##0.0000";
    case "decimal_5":
      return "#,##0.00000";
    case "decimal_6":
      return "#,##0.000000";
    default:
      return "#,##0.00";
  }
}

function normalizePercent(v: number): number {
  return Math.abs(v) < 1 ? v * 100 : v;
}

function totalToCell(
  v: TotalValue,
  emptyMode: EmptyCellMode,
  pct: boolean,
): string | number | null {
  if (v === NOT_ADDITIVE) return "—";
  if (v === null) {
    if (emptyMode === "zero") return 0;
    if (emptyMode === "dash") return "—";
    return null;
  }
  return pct ? normalizePercent(v) : v;
}

function rawToCell(
  v: unknown,
  emptyMode: EmptyCellMode,
  pct: boolean,
): string | number | null {
  if (v === null || v === undefined) {
    if (emptyMode === "zero") return 0;
    if (emptyMode === "dash") return "—";
    return null;
  }
  const n = Number(v);
  return pct ? normalizePercent(n) : n;
}

function cellMeasureValue(cell: CellCoord | undefined, m: Measure, first: Measure): unknown {
  if (!cell) return undefined;
  return cell.measureValues?.[m.name] ?? (m.name === first.name ? cell.measureValue : undefined);
}

type DisplayCol =
  | { kind: "data"; ck: string[]; ckIndex: number }
  | { kind: "subtotalCol"; head: string }
  | { kind: "grandCol" };

export async function pivotToXlsx(
  pivot: PivotModel,
  measure: Measure,
  opts?: XlsxOptions,
): Promise<Blob> {
  const wb = new ExcelJS.Workbook();
  const ws = wb.addWorksheet("Pivot");

  const allMeasures: Measure[] = [measure, ...(opts?.extraMeasures ?? [])];
  const measureCount = allMeasures.length;
  const multiMeasure = measureCount > 1;

  const { rowCols, colCols, byKey } = pivot;
  const colKeys = opts?.rowKeyOrder ? pivot.colKeys : pivot.colKeys;
  const rowKeys = opts?.rowKeyOrder ?? pivot.rowKeys;
  const hasCols = colCols.length > 0;
  // F-019-08: per-measure totals; fall back to the legacy single-measure map.
  const allTotals: Map<string, TotalsModel | null> | null =
    opts?.allTotals ?? (opts?.totals ? new Map([[measure.name, opts.totals]]) : null);
  const showSubtotals = opts?.showSubtotals ?? false;
  const showGrandTotals = opts?.showGrandTotals ?? false;
  const emptyMode: EmptyCellMode = opts?.emptyCellMode ?? "blank";

  const totalsFor = (m: Measure): TotalsModel | null => allTotals?.get(m.name) ?? null;
  const fmtOf = (m: Measure) => (m.format ?? null) as MeasureFormatToken | null;
  const pctOf = (m: Measure) => isPercentToken(fmtOf(m));
  const numFmtOf = (m: Measure) => formatTokenToExcel(fmtOf(m));

  const headerFill: ExcelJS.FillPattern = {
    type: "pattern",
    pattern: "solid",
    fgColor: { argb: "FF4472C4" },
  };
  const headerFont: Partial<ExcelJS.Font> = {
    bold: true,
    color: { argb: "FFFFFFFF" },
    size: 11,
  };
  const totalFill: ExcelJS.FillPattern = {
    type: "pattern",
    pattern: "solid",
    fgColor: { argb: "FFE2EFDA" },
  };
  const totalFont: Partial<ExcelJS.Font> = { bold: true, size: 11 };

  // Display columns matching the grid's column model.
  const displayCols: DisplayCol[] = [];
  const hasTotals = Boolean(allTotals && allTotals.size);
  const colSubtotalsActive = showSubtotals && hasTotals && colCols.length > 1;
  const grandActive = showGrandTotals && hasTotals;

  if (hasCols) {
    const colHeads: string[] = [];
    const seenColHeads = new Set<string>();
    for (const ck of colKeys) {
      const h = ck[0] ?? "";
      if (!seenColHeads.has(h)) {
        seenColHeads.add(h);
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

  // Number of header rows: one per col level, plus one measure row when multi.
  const headerRowCount = hasCols
    ? colCols.length + (multiMeasure ? 1 : 0)
    : 1;

  // Column headers.
  if (hasCols) {
    for (let lvl = 0; lvl < colCols.length; lvl++) {
      const cells: string[] = [];
      for (let i = 0; i < rowCols.length; i++) cells.push(lvl === 0 ? rowCols[i] : "");
      for (const dc of displayCols) {
        const label =
          dc.kind === "data"
            ? (dc.ck[lvl] ?? "")
            : lvl === 0
              ? dc.kind === "subtotalCol"
                ? `${dc.head} Total`
                : "Grand Total"
              : "";
        cells.push(label);
        for (let s = 1; s < measureCount; s++) cells.push("");
      }
      const row = ws.addRow(cells);
      row.eachCell((cell) => {
        cell.fill = headerFill;
        cell.font = headerFont;
        cell.alignment = { horizontal: "center" };
      });
    }
    if (multiMeasure) {
      const cells: string[] = rowCols.map(() => "");
      for (const _dc of displayCols) {
        for (const m of allMeasures) cells.push(m.display_name || m.name);
      }
      const row = ws.addRow(cells);
      row.eachCell((cell) => {
        cell.fill = headerFill;
        cell.font = headerFont;
        cell.alignment = { horizontal: "center" };
      });
    }
  } else {
    const cells = multiMeasure
      ? [...rowCols, ...allMeasures.map((m) => m.display_name || m.name)]
      : [...rowCols, measure.display_name];
    const row = ws.addRow(cells);
    row.eachCell((cell) => {
      cell.fill = headerFill;
      cell.font = headerFont;
      cell.alignment = { horizontal: "center" };
    });
  }

  // Apply per-measure number formats to a data/total value row.
  function applyValueFormats(row: ExcelJS.Row) {
    let col = rowCols.length + 1;
    for (const _dc of displayCols) {
      for (const m of allMeasures) {
        const cell = row.getCell(col);
        if (typeof cell.value === "number") cell.numFmt = numFmtOf(m);
        col++;
      }
    }
  }

  // Group rows by first element for row subtotals.
  const rowHeads: string[] = [];
  const rowGroupMap = new Map<string, string[][]>();
  const seenHeads = new Set<string>();
  for (const rk of rowKeys) {
    const h = rk[0] ?? "";
    if (!seenHeads.has(h)) {
      seenHeads.add(h);
      rowHeads.push(h);
    }
    const arr = rowGroupMap.get(h) ?? [];
    arr.push(rk);
    rowGroupMap.set(h, arr);
  }

  let dataRowOrdinal = 0;
  function addDataRow(rk: string[], rkIndex: number) {
    const cells: (string | number | null)[] = [...rk];
    for (const dc of displayCols) {
      for (const m of allMeasures) {
        const totals = totalsFor(m);
        const pct = pctOf(m);
        if (dc.kind === "data") {
          const cell = byKey.get(cellLookupKey(rk, dc.ck));
          cells.push(rawToCell(cellMeasureValue(cell, m, measure), emptyMode, pct));
        } else if (dc.kind === "subtotalCol") {
          cells.push(totalToCell(totals?.colSubtotals.get(dc.head)?.[rkIndex] ?? null, emptyMode, pct));
        } else {
          cells.push(totalToCell(totals?.grandCol[rkIndex] ?? null, emptyMode, pct));
        }
      }
    }
    const row = ws.addRow(cells);
    applyValueFormats(row);
    if (dataRowOrdinal % 2 === 0) {
      const banded: ExcelJS.FillPattern = {
        type: "pattern",
        pattern: "solid",
        fgColor: { argb: "FFD9E2F3" },
      };
      row.eachCell((cell) => {
        cell.fill = banded;
      });
    }
    dataRowOrdinal++;
  }

  function addRowSubtotalRow(head: string) {
    const cells: (string | number | null)[] = rowCols.map((_, i) =>
      i === 0 ? `${head} Total` : "",
    );
    for (const dc of displayCols) {
      for (const m of allMeasures) {
        const totals = totalsFor(m);
        const pct = pctOf(m);
        const rowSubs = totals?.rowSubtotals.get(head);
        if (dc.kind === "data") {
          cells.push(totalToCell(rowSubs?.[dc.ckIndex] ?? null, emptyMode, pct));
        } else if (dc.kind === "subtotalCol") {
          cells.push(totalToCell(totals?.crossSubtotals.get(`${head}||${dc.head}`) ?? null, emptyMode, pct));
        } else {
          cells.push(totalToCell(totals?.rowSubtotalGrand.get(head) ?? null, emptyMode, pct));
        }
      }
    }
    const row = ws.addRow(cells);
    row.eachCell((cell) => {
      cell.fill = totalFill;
      cell.font = totalFont;
    });
    applyValueFormats(row);
  }

  const rowSubtotalsActive = showSubtotals && hasTotals && rowCols.length >= 2;
  let globalRkIdx = 0;
  for (const head of rowHeads) {
    for (const rk of rowGroupMap.get(head) ?? []) {
      addDataRow(rk, globalRkIdx);
      globalRkIdx++;
    }
    if (rowSubtotalsActive) addRowSubtotalRow(head);
  }

  // Grand total row.
  if (grandActive) {
    const cells: (string | number | null)[] = rowCols.map((_, i) =>
      i === 0 ? "Grand Total" : "",
    );
    for (const dc of displayCols) {
      for (const m of allMeasures) {
        const totals = totalsFor(m);
        const pct = pctOf(m);
        if (dc.kind === "data") {
          cells.push(totalToCell(totals?.grandRow[dc.ckIndex] ?? null, emptyMode, pct));
        } else if (dc.kind === "subtotalCol") {
          cells.push(totalToCell(totals?.colSubtotalGrand.get(dc.head) ?? null, emptyMode, pct));
        } else {
          cells.push(totalToCell(totals?.grandGrand ?? null, emptyMode, pct));
        }
      }
    }
    const row = ws.addRow(cells);
    row.eachCell((cell) => {
      cell.fill = totalFill;
      cell.font = totalFont;
    });
    applyValueFormats(row);
  }

  const totalColCount = rowCols.length + displayCols.length * measureCount;
  for (let c = 1; c <= totalColCount; c++) {
    const col = ws.getColumn(c);
    col.width = Math.max(12, (col.header?.toString().length ?? 0) + 4);
  }

  ws.views = [{ state: "frozen", ySplit: headerRowCount, xSplit: rowCols.length }];

  const buffer = await wb.xlsx.writeBuffer();
  return new Blob([buffer], {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
}
