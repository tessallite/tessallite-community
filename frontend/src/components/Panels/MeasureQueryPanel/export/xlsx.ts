import ExcelJS from "exceljs";
import type { Measure, MeasureFormatToken } from "../../../../api/types";
import { cellLookupKey } from "../pivot";
import type { CellCoord, PivotModel } from "../types";
import { NOT_ADDITIVE, type TotalsModel, type TotalValue } from "../totals";
import type { EmptyCellMode } from "../grid/PivotGrid";
import { csvSafeCell } from "../../../../utils/sanitize";

// Bug-7286: ExcelJS writes a string cell beginning with "=" (or "+"/"-"/"@") as
// a LIVE formula. Dimension row/column members are source-derived, so a planted
// value like `=WEBSERVICE(...)` would execute when the workbook is opened. Guard
// every string cell written to the sheet; numeric cells are left untouched so
// they stay real numbers. Data/total values are already coerced to number | null
// | "—" upstream, so this only rewrites genuine text cells.
function guardCell(v: string | number | null): string | number | null {
  return typeof v === "string" ? csvSafeCell(v) : v;
}

function guardCells(
  cells: (string | number | null)[],
): (string | number | null)[] {
  return cells.map(guardCell);
}

export interface XlsxExportLabels {
  worksheetName?: string;
  subtotalSuffix?: string;
  grandTotal?: string;
}

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
  // F-019-08: the grid's current sorted row/column order, so the workbook
  // honours the user's header-click sort.
  rowKeyOrder?: string[][];
  colKeyOrder?: string[][];
  // Localized labels for exported worksheet and totals.
  labels?: XlsxExportLabels;
}

function formatTokenToExcel(token: MeasureFormatToken | null | undefined): string {
  if (!token) return "#,##0.00";
  switch (token) {
    case "currency":
      return "$#,##0.00";
    // F-015-01: use Excel's NATIVE percent format. A native `0%` / `0.00%`
    // format multiplies by 100 for DISPLAY only — the stored cell value stays
    // the raw engine ratio (0.125, not 12.5). The previous `#,##0"%"` was a
    // literal-suffix format that forced the value itself to be pre-scaled,
    // corrupting every exported percent cell (a 100x error on re-import, sum,
    // or chart). CSV already keeps the raw ratio; this aligns XLSX with it and
    // with the XMLA FORMAT_STRING tokens.
    case "percent":
      return "0%";
    case "percent_2dp":
      return "0.00%";
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

// F-015-01: the exported cell holds the RAW engine value (a percent measure is
// a decimal ratio, e.g. 0.125). Display scaling to "12.5%" is delegated to the
// native Excel `0%` / `0.00%` number format applied in `applyValueFormats`, so
// the stored value is never mutated. Pre-scaling the value here (the old
// `v * 100`) produced a 100x error the moment anyone summed, charted, or
// re-imported the column. CSV keeps the raw ratio too; the two exports now
// agree.
function totalToCell(
  v: TotalValue,
  emptyMode: EmptyCellMode,
): string | number | null {
  if (v === NOT_ADDITIVE) return "—";
  if (v === null) {
    if (emptyMode === "zero") return 0;
    if (emptyMode === "dash") return "—";
    return null;
  }
  return v;
}

function rawToCell(
  v: unknown,
  emptyMode: EmptyCellMode,
): string | number | null {
  if (v === null || v === undefined) {
    if (emptyMode === "zero") return 0;
    if (emptyMode === "dash") return "—";
    return null;
  }
  return Number(v);
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
  const lb = opts?.labels ?? {};
  const lbSubtotal = lb.subtotalSuffix ?? "Total";
  const lbGrand = lb.grandTotal ?? "Grand Total";
  const wb = new ExcelJS.Workbook();
  const ws = wb.addWorksheet(lb.worksheetName ?? "Pivot");

  const allMeasures: Measure[] = [measure, ...(opts?.extraMeasures ?? [])];
  const measureCount = allMeasures.length;
  const multiMeasure = measureCount > 1;

  const { rowCols, colCols, rowLabels, byKey } = pivot;
  const colKeys = opts?.colKeyOrder ?? pivot.colKeys;
  const rowKeys = opts?.rowKeyOrder ?? pivot.rowKeys;
  const hasCols = colCols.length > 0;
  // Bug-6285: exported row-dimension headers use business display names.
  const rowHead = (i: number): string => rowLabels[i] ?? rowCols[i];

  // Bug-6272: build maps from serialized row/col key to the ORIGINAL index in
  // pivot.rowKeys / pivot.colKeys. The totals arrays are indexed by the
  // original pivot order; after a header-click sort, the output order differs.
  // Without these maps the export writes the wrong row's/column's totals.
  const originalRowIndex = new Map<string, number>();
  for (let i = 0; i < pivot.rowKeys.length; i++) {
    originalRowIndex.set(JSON.stringify(pivot.rowKeys[i]), i);
  }
  const originalColIndex = new Map<string, number>();
  for (let i = 0; i < pivot.colKeys.length; i++) {
    originalColIndex.set(JSON.stringify(pivot.colKeys[i]), i);
  }
  // F-019-08: per-measure totals; fall back to the legacy single-measure map.
  const allTotals: Map<string, TotalsModel | null> | null =
    opts?.allTotals ?? (opts?.totals ? new Map([[measure.name, opts.totals]]) : null);
  const showSubtotals = opts?.showSubtotals ?? false;
  const showGrandTotals = opts?.showGrandTotals ?? false;
  const emptyMode: EmptyCellMode = opts?.emptyCellMode ?? "blank";

  const totalsFor = (m: Measure): TotalsModel | null => allTotals?.get(m.name) ?? null;
  const fmtOf = (m: Measure) => (m.format ?? null) as MeasureFormatToken | null;
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

  // Number of header rows: one per col level, plus one measure row when multi.
  const headerRowCount = hasCols
    ? colCols.length + (multiMeasure ? 1 : 0)
    : 1;

  // Column headers.
  if (hasCols) {
    for (let lvl = 0; lvl < colCols.length; lvl++) {
      const cells: string[] = [];
      for (let i = 0; i < rowCols.length; i++) cells.push(lvl === 0 ? rowHead(i) : "");
      for (const dc of displayCols) {
        const label =
          dc.kind === "data"
            ? (dc.ck[lvl] ?? "")
            : lvl === 0
              ? dc.kind === "subtotalCol"
                ? `${dc.head} ${lbSubtotal}`
                : lbGrand
              : "";
        cells.push(label);
        for (let s = 1; s < measureCount; s++) cells.push("");
      }
      const row = ws.addRow(guardCells(cells));
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
      const row = ws.addRow(guardCells(cells));
      row.eachCell((cell) => {
        cell.fill = headerFill;
        cell.font = headerFont;
        cell.alignment = { horizontal: "center" };
      });
    }
  } else {
    const cells = multiMeasure
      ? [...rowCols.map((_, i) => rowHead(i)), ...allMeasures.map((m) => m.display_name || m.name)]
      : [...rowCols.map((_, i) => rowHead(i)), measure.display_name];
    const row = ws.addRow(guardCells(cells));
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
  function addDataRow(rk: string[]) {
    // Bug-6272: resolve the row key back to its original pivot index so that
    // grandCol and colSubtotals look up the correct row's totals after sorting.
    const origIdx = originalRowIndex.get(JSON.stringify(rk)) ?? 0;
    const cells: (string | number | null)[] = [...rk];
    for (const dc of displayCols) {
      for (const m of allMeasures) {
        const totals = totalsFor(m);
        if (dc.kind === "data") {
          const cell = byKey.get(cellLookupKey(rk, dc.ck));
          cells.push(rawToCell(cellMeasureValue(cell, m, measure), emptyMode));
        } else if (dc.kind === "subtotalCol") {
          cells.push(totalToCell(totals?.colSubtotals.get(dc.head)?.[origIdx] ?? null, emptyMode));
        } else {
          cells.push(totalToCell(totals?.grandCol[origIdx] ?? null, emptyMode));
        }
      }
    }
    const row = ws.addRow(guardCells(cells));
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
      i === 0 ? `${head} ${lbSubtotal}` : "",
    );
    for (const dc of displayCols) {
      for (const m of allMeasures) {
        const totals = totalsFor(m);
        const rowSubs = totals?.rowSubtotals.get(head);
        if (dc.kind === "data") {
          cells.push(totalToCell(rowSubs?.[dc.ckIndex] ?? null, emptyMode));
        } else if (dc.kind === "subtotalCol") {
          cells.push(totalToCell(totals?.crossSubtotals.get(`${head}||${dc.head}`) ?? null, emptyMode));
        } else {
          cells.push(totalToCell(totals?.rowSubtotalGrand.get(head) ?? null, emptyMode));
        }
      }
    }
    const row = ws.addRow(guardCells(cells));
    row.eachCell((cell) => {
      cell.fill = totalFill;
      cell.font = totalFont;
    });
    applyValueFormats(row);
  }

  const rowSubtotalsActive = showSubtotals && hasTotals && rowCols.length >= 2;
  for (const head of rowHeads) {
    for (const rk of rowGroupMap.get(head) ?? []) {
      addDataRow(rk);
    }
    if (rowSubtotalsActive) addRowSubtotalRow(head);
  }

  // Grand total row.
  if (grandActive) {
    const cells: (string | number | null)[] = rowCols.map((_, i) =>
      i === 0 ? lbGrand : "",
    );
    for (const dc of displayCols) {
      for (const m of allMeasures) {
        const totals = totalsFor(m);
        if (dc.kind === "data") {
          cells.push(totalToCell(totals?.grandRow[dc.ckIndex] ?? null, emptyMode));
        } else if (dc.kind === "subtotalCol") {
          cells.push(totalToCell(totals?.colSubtotalGrand.get(dc.head) ?? null, emptyMode));
        } else {
          cells.push(totalToCell(totals?.grandGrand ?? null, emptyMode));
        }
      }
    }
    const row = ws.addRow(guardCells(cells));
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
