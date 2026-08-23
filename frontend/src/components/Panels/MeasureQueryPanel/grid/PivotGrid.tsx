import { useEffect, useMemo } from "react";
import {
  Box,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tooltip,
} from "@mui/material";
import { useT } from "../../../../i18n";
import ArrowUpwardIcon from "@mui/icons-material/ArrowUpward";
import ArrowDownwardIcon from "@mui/icons-material/ArrowDownward";
import UnfoldMoreIcon from "@mui/icons-material/UnfoldMore";
import { ui } from "../../../../theme/tokens";
import type { Measure, MeasureFormatToken } from "../../../../api/types";
import type { PivotSort } from "../../../../api/client";
import { formatMeasureValue } from "../../../../api/measureFormat";
import { cellLookupKey } from "../pivot";
import type { CellCoord, PivotModel } from "../types";
import { NOT_ADDITIVE, type TotalValue, type TotalsModel } from "../totals";
import { pivotSortMeasureIdentity, resolvePivotSort } from "./sortState";

export type EmptyCellMode = "blank" | "zero" | "dash";
export type ConditionalFormat =
  | { kind: "none" }
  | { kind: "color-scale"; low: string; high: string }
  | { kind: "data-bars"; color: string }
  | { kind: "threshold"; below: string; above: string; threshold: number };

type SortDir = "asc" | "desc";
function interpolateColor(low: string, high: string, t: number): string {
  const parseHex = (h: string) => {
    const c = h.replace("#", "");
    return [parseInt(c.slice(0, 2), 16), parseInt(c.slice(2, 4), 16), parseInt(c.slice(4, 6), 16)];
  };
  const [lr, lg, lb] = parseHex(low);
  const [hr, hg, hb] = parseHex(high);
  const r = Math.round(lr + (hr - lr) * t);
  const g = Math.round(lg + (hg - lg) * t);
  const b = Math.round(lb + (hb - lb) * t);
  return `rgb(${r},${g},${b})`;
}

function compareForSort(a: unknown, b: unknown): number {
  const aMissing = a === null || a === undefined;
  const bMissing = b === null || b === undefined;
  if (aMissing && bMissing) return 0;
  if (aMissing) return 1;
  if (bMissing) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b));
}

function tupleKey(parts: string[]): string {
  return JSON.stringify(parts);
}

// Always-visible sort affordance: a muted neutral glyph when the column is
// unsorted, and a directional arrow when it is the active sort column.
function SortGlyph({ active, dir }: { active: boolean; dir?: SortDir }) {
  if (active && dir === "asc") return <ArrowUpwardIcon fontSize="inherit" />;
  if (active && dir === "desc") return <ArrowDownwardIcon fontSize="inherit" />;
  return <UnfoldMoreIcon fontSize="inherit" sx={{ color: "text.disabled" }} />;
}

type Props = {
  model: PivotModel;
  measure: Measure;
  extraMeasures?: Measure[];
  // Per-measure totals map, keyed by measure.name.
  allTotals?: Map<string, TotalsModel | null>;
  showSubtotals: boolean;
  showGrandTotals: boolean;
  emptyCellMode: EmptyCellMode;
  conditionalFormat: ConditionalFormat;
  sort: PivotSort | null;
  onSortChange: (sort: PivotSort | null) => void;
  onSortInvalid?: () => void;
  // measure is the one whose column was clicked.
  onCellClick?: (coord: CellCoord, measure: Measure) => void;
  drillableRowDims?: Set<string>;
  // dimName -> hierarchy name, used to hint which hierarchy a drillable row goes through.
  drillHierarchyNames?: Map<string, string>;
  // F-019-11: ``rawValue`` is the underlying cell value (number/null/string),
  // not the rendered label, so the drill filter pins the real value rather
  // than "(null)" or a stringified number.
  onRowDrill?: (dimName: string, value: string, rawValue: unknown) => void;
  // F-019-08: report the grid's current sorted row order so the export honours
  // the user's header-click sort instead of the pivot's default order.
  onRowOrderChange?: (rowKeys: string[][]) => void;
};

type DisplayCol =
  | { kind: "data"; ck: string[]; ckIndex: number }
  | { kind: "subtotalCol"; head: string }
  | { kind: "grandCol" };

type DisplayRow =
  | { kind: "data"; rk: string[]; rkIndex: number }
  | { kind: "subtotalRow"; head: string }
  | { kind: "grandRow" };

const NOT_ADDITIVE_TEXT = "—";

function renderEmpty(mode: EmptyCellMode): string {
  if (mode === "zero") return "0";
  if (mode === "dash") return "—";
  return "";
}

function renderValueCell(
  raw: unknown,
  format: MeasureFormatToken | null,
  emptyMode: EmptyCellMode,
): { text: string; isMissing: boolean } {
  if (raw === null || raw === undefined) {
    return { text: renderEmpty(emptyMode), isMissing: true };
  }
  return { text: formatMeasureValue(raw, format), isMissing: false };
}

function renderTotalCell(
  total: TotalValue,
  format: MeasureFormatToken | null,
  emptyMode: EmptyCellMode,
  notAdditiveLabel: string,
): { text: string; tooltip?: string } {
  if (total === NOT_ADDITIVE) {
    return { text: NOT_ADDITIVE_TEXT, tooltip: notAdditiveLabel };
  }
  if (total === null) {
    return { text: renderEmpty(emptyMode) };
  }
  return { text: formatMeasureValue(total, format) };
}

export default function PivotGrid({
  model,
  measure,
  extraMeasures = [],
  allTotals,
  showSubtotals,
  showGrandTotals,
  emptyCellMode,
  conditionalFormat,
  sort,
  onSortChange,
  onSortInvalid,
  onCellClick,
  drillableRowDims,
  drillHierarchyNames,
  onRowDrill,
  onRowOrderChange,
}: Props) {
  const t = useT();
  const clickable = Boolean(onCellClick);

  const allMeasures = [measure, ...extraMeasures];
  const measureCount = allMeasures.length;
  const multiMeasure = measureCount > 1;

  const { rowCols, colCols, rowLabels, colKeys, byKey } = model;
  const hasCols = colCols.length > 0;

  const resolvedSort = useMemo(
    () => resolvePivotSort(sort, allMeasures, colKeys),
    [sort, measure, extraMeasures, colKeys],
  );

  useEffect(() => {
    if (sort && !resolvedSort) onSortInvalid?.();
  }, [sort, resolvedSort, onSortInvalid]);

  // Get the value for a specific measure from a cell.
  function cellValue(cell: CellCoord | undefined, m: Measure): unknown {
    if (!cell) return undefined;
    return cell.measureValues?.[m.name] ?? (m.name === measure.name ? cell.measureValue : undefined);
  }

  const rowIndexByKey = useMemo(() => {
    const map = new Map<string, number>();
    model.rowKeys.forEach((rk, i) => map.set(tupleKey(rk), i));
    return map;
  }, [model.rowKeys]);

  // F-019-11: map a rendered row-key tuple to the raw row values of any cell
  // on that row, so a row-label drill can pin the underlying value (null /
  // number / string) instead of its rendered label.
  const rawRowValuesByKey = useMemo(() => {
    const map = new Map<string, unknown[]>();
    for (const cell of model.byKey.values()) {
      const k = tupleKey(cell.rowKey);
      if (!map.has(k)) map.set(k, cell.rowValues);
    }
    return map;
  }, [model.byKey]);

  const rawColValuesByKey = useMemo(() => {
    const map = new Map<string, unknown[]>();
    for (const cell of model.byKey.values()) {
      const key = tupleKey(cell.colKey);
      if (!map.has(key)) map.set(key, cell.colValues);
    }
    return map;
  }, [model.byKey]);

  function valuesForRow(row: DisplayRow): { keys: string[]; values: unknown[] } {
    if (row.kind === "grandRow") return { keys: [], values: [] };
    if (row.kind === "data") {
      return {
        keys: row.rk,
        values: rawRowValuesByKey.get(tupleKey(row.rk)) ?? row.rk,
      };
    }
    const representative = model.rowKeys.find((key) => (key[0] ?? "") === row.head);
    return {
      keys: [row.head],
      values: (representative
        ? rawRowValuesByKey.get(tupleKey(representative))
        : undefined)?.slice(0, 1) ?? [row.head],
    };
  }

  function valuesForColumn(column: DisplayCol): { keys: string[]; values: unknown[] } {
    if (column.kind === "grandCol") return { keys: [], values: [] };
    if (column.kind === "data") {
      return {
        keys: column.ck,
        values: rawColValuesByKey.get(tupleKey(column.ck)) ?? column.ck,
      };
    }
    const representative = model.colKeys.find((key) => (key[0] ?? "") === column.head);
    return {
      keys: [column.head],
      values: (representative
        ? rawColValuesByKey.get(tupleKey(representative))
        : undefined)?.slice(0, 1) ?? [column.head],
    };
  }

  function totalCoordinate(row: DisplayRow, column: DisplayCol, total: TotalValue): CellCoord {
    const rowCoordinate = valuesForRow(row);
    const columnCoordinate = valuesForColumn(column);
    const measureValue = total === NOT_ADDITIVE ? null : total;
    return {
      rowKey: rowCoordinate.keys,
      colKey: columnCoordinate.keys,
      rowValues: rowCoordinate.values,
      colValues: columnCoordinate.values,
      measureValue,
    };
  }

  /**
   * Bug-8505: a drillable total cell previously carried
   * `aria-label="Drill-through: <measure>"`. An aria-label REPLACES the cell's
   * text content for assistive technology, so screen-reader users lost the
   * value AND every total cell in the grid announced identically — there was
   * no way to tell a row subtotal from the grand total, or which row/column it
   * belonged to. The label below restates the rendered value, the measure, and
   * the row/column grain of the specific total, then the drill affordance.
   */
  function rowScopeLabel(row: DisplayRow): string {
    if (row.kind === "grandRow") return t("pivotGrid.total");
    if (row.kind === "subtotalRow") return t("pivotGrid.subtotalPrefix", { head: row.head });
    return row.rk.join(" / ");
  }

  function columnScopeLabel(column: DisplayCol): string {
    if (column.kind === "grandCol") return t("pivotGrid.total");
    if (column.kind === "subtotalCol") return t("pivotGrid.subtotalPrefix", { head: column.head });
    return column.ck.join(" / ");
  }

  function totalCellAriaLabel(
    row: DisplayRow,
    column: DisplayCol,
    valueText: string,
    measureLabel: string,
  ): string {
    const value = valueText.trim() === "" ? t("pivotGrid.emptyValue") : valueText;
    const params = {
      value,
      measure: measureLabel,
      row: rowScopeLabel(row),
      column: columnScopeLabel(column),
    };
    return hasCols
      ? t("pivotGrid.totalCellAria", params)
      : t("pivotGrid.totalCellAriaNoColumns", params);
  }

  function totalCellKind(row: DisplayRow, column: DisplayCol): string {
    if (row.kind === "grandRow" && column.kind === "grandCol") return "grand-grand";
    if (row.kind === "grandRow") return column.kind === "subtotalCol" ? "column-subtotal" : "column-grand";
    if (column.kind === "grandCol") return row.kind === "subtotalRow" ? "row-subtotal" : "row-grand";
    if (row.kind === "subtotalRow" && column.kind === "subtotalCol") return "cross-subtotal";
    if (row.kind === "subtotalRow") return "row-subtotal";
    return "column-subtotal";
  }

  // Sort rows by whichever measure column was selected. Grand-total sorting uses
  // the original row index so totals stay aligned after the visible rows move.
  const rowKeys = useMemo(() => {
    const src = model.rowKeys;
    if (!resolvedSort || src.length === 0) return src;
    const activeSort = resolvedSort;
    const sortM = allMeasures[activeSort.measureIndex] ?? measure;
    const sortCk = activeSort.ckIndex === "grand" ? null : model.colKeys[activeSort.ckIndex];
    if (activeSort.ckIndex !== "grand" && !sortCk) return src;

    function sortValue(rk: string[]): unknown {
      if (activeSort.ckIndex === "grand") {
        const originalIndex = rowIndexByKey.get(tupleKey(rk));
        if (originalIndex === undefined) return undefined;
        const total = getMeasureTotals(sortM)?.grandCol[originalIndex];
        return total === NOT_ADDITIVE ? undefined : total;
      }
      const cell = byKey.get(cellLookupKey(rk, sortCk ?? []));
      return cellValue(cell, sortM);
    }

    const groups = new Map<string, string[][]>();
    const groupOrder: string[] = [];
    for (const rk of src) {
      const head = rk[0] ?? "";
      const bucket = groups.get(head);
      if (bucket) {
        bucket.push(rk);
      } else {
        groups.set(head, [rk]);
        groupOrder.push(head);
      }
    }
    const sign = activeSort.dir === "asc" ? 1 : -1;
    const sorted: string[][] = [];
    for (const head of groupOrder) {
      const bucket = groups.get(head)!;
      bucket.sort((a, b) => sign * compareForSort(sortValue(a), sortValue(b)));
      sorted.push(...bucket);
    }
    return sorted;
  }, [model.rowKeys, model.colKeys, byKey, resolvedSort, rowIndexByKey, allTotals]);

  // F-019-08: surface the currently-rendered row order so the export reflects
  // the user's sort. Reported on every sort/data change.
  useEffect(() => {
    onRowOrderChange?.(rowKeys);
  }, [rowKeys, onRowOrderChange]);

  function cycleSort(ckIndex: number | "grand", measureIndex: number) {
    const nextTarget: PivotSort["target"] = ckIndex === "grand"
      ? { kind: "grand" }
      : { kind: "column", columnKey: [...(model.colKeys[ckIndex] ?? [])] };
    const isSameTarget = resolvedSort?.measureIndex === measureIndex &&
      resolvedSort.ckIndex === ckIndex;
    if (!isSameTarget) {
      onSortChange({
        measure: pivotSortMeasureIdentity(allMeasures, measureIndex),
        target: nextTarget,
        direction: "asc",
      });
    } else if (resolvedSort.dir === "asc") {
      onSortChange({
        measure: pivotSortMeasureIdentity(allMeasures, measureIndex),
        target: nextTarget,
        direction: "desc",
      });
    } else {
      onSortChange(null);
    }
  }

  // Conditional formatting uses the first measure's values for the color scale.
  const [valMin, valMax] = useMemo(() => {
    if (conditionalFormat.kind === "none") return [0, 1];
    let lo = Infinity;
    let hi = -Infinity;
    for (const cell of byKey.values()) {
      const v = cellValue(cell, measure);
      if (typeof v === "number") {
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    }
    if (!isFinite(lo)) return [0, 1];
    if (lo === hi) return [lo, lo + 1];
    return [lo, hi];
  }, [byKey, conditionalFormat.kind]);

  function getCellBg(raw: unknown): string | undefined {
    if (conditionalFormat.kind === "none" || typeof raw !== "number") return undefined;
    const tVal = Math.max(0, Math.min(1, (raw - valMin) / (valMax - valMin)));
    if (conditionalFormat.kind === "color-scale") {
      return interpolateColor(conditionalFormat.low, conditionalFormat.high, tVal);
    }
    if (conditionalFormat.kind === "data-bars") {
      const pct = Math.round(tVal * 100);
      return `linear-gradient(to right, ${conditionalFormat.color}40 ${pct}%, transparent ${pct}%)`;
    }
    if (conditionalFormat.kind === "threshold") {
      return raw >= conditionalFormat.threshold ? conditionalFormat.above : conditionalFormat.below;
    }
    return undefined;
  }

  const hasTotals = Boolean(allTotals?.size);
  const rowSubtotalsActive = hasTotals && showSubtotals && rowCols.length >= 2;
  const colSubtotalsActive = hasTotals && showSubtotals && colCols.length >= 2;
  const grandActive = hasTotals && showGrandTotals;

  // Lookup totals for a specific measure.
  function getMeasureTotals(m: Measure): TotalsModel | null {
    return allTotals?.get(m.name) ?? null;
  }

  // Build display column list (one entry per col-key group + subtotals/grand).
  const displayCols: DisplayCol[] = [];
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
  } else {
    displayCols.push({ kind: "data", ck: [], ckIndex: 0 });
  }
  if (grandActive && (hasCols || rowCols.length > 0)) displayCols.push({ kind: "grandCol" });

  // Build display row list.
  const displayRows: DisplayRow[] = [];
  const rowHeads: string[] = [];
  const seenRowHeads = new Set<string>();
  for (const rk of rowKeys) {
    const h = rk[0] ?? "";
    if (!seenRowHeads.has(h)) { seenRowHeads.add(h); rowHeads.push(h); }
  }
  for (const head of rowHeads) {
    rowKeys.forEach((rk, rkIndex) => {
      if ((rk[0] ?? "") === head) {
        displayRows.push({ kind: "data", rk, rkIndex: rowIndexByKey.get(tupleKey(rk)) ?? rkIndex });
      }
    });
    if (rowSubtotalsActive) displayRows.push({ kind: "subtotalRow", head });
  }
  if (grandActive && (hasCols || rowCols.length > 0)) displayRows.push({ kind: "grandRow" });

  // ---- Render headers ----
  const colDimHeaderRows = Math.max(1, colCols.length);
  const rowDimHeaderSpan = hasCols && multiMeasure ? colDimHeaderRows + 1 : colDimHeaderRows;

  const headerRows: React.ReactNode[] = [];
  if (hasCols) {
    for (let lvl = 0; lvl < colCols.length; lvl++) {
      headerRows.push(
        <TableRow key={`h-${lvl}`}>
          {lvl === 0
            ? rowCols.map((rc, i) => (
                <TableCell key={`rh-${i}`} rowSpan={rowDimHeaderSpan} sx={{ fontWeight: 600 }}>
                  {rowLabels[i] ?? rc}
                </TableCell>
              ))
            : null}
          {displayCols.map((dc, j) => {
            if (dc.kind === "data") {
              // When multi-measure, span N leaf cells per col-dim value.
              // Clicking is handled in the measure sub-row instead.
              const colSpanVal = multiMeasure ? measureCount : 1;
              const isLeaf = lvl === colCols.length - 1;
              const sorted = !multiMeasure && isLeaf && resolvedSort?.ckIndex === dc.ckIndex;
              const ariaSort: "ascending" | "descending" | "none" | undefined =
                isLeaf && !multiMeasure
                  ? sorted ? (resolvedSort?.dir === "asc" ? "ascending" : "descending") : "none"
                  : undefined;
              return (
                <TableCell
                  key={`${lvl}-${j}`}
                  scope="col"
                  align="right"
                  role="columnheader"
                  colSpan={colSpanVal}
                  aria-sort={ariaSort}
                  tabIndex={isLeaf && !multiMeasure ? 0 : -1}
                  onClick={isLeaf && !multiMeasure ? () => cycleSort(dc.ckIndex, 0) : undefined}
                  onKeyDown={isLeaf && !multiMeasure
                    ? (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); cycleSort(dc.ckIndex, 0); } }
                    : undefined}
                  title={isLeaf && !multiMeasure ? t("pivotGrid.sortColumn") : undefined}
                  sx={{
                    fontWeight: 600,
                    cursor: isLeaf && !multiMeasure ? "pointer" : "default",
                    userSelect: "none",
                    "&:hover": isLeaf && !multiMeasure ? { bgcolor: "action.hover" } : undefined,
                  }}
                >
                  <Box component="span" sx={{ display: "inline-flex", alignItems: "center", gap: 0.5 }}>
                    {dc.ck[lvl] ?? ""}
                    {isLeaf && !multiMeasure && <SortGlyph active={Boolean(sorted)} dir={resolvedSort?.dir} />}
                  </Box>
                </TableCell>
              );
            }
            if (lvl > 0) return null;
            const isGrandCol = dc.kind === "grandCol";
            const label = dc.kind === "subtotalCol"
              ? t("pivotGrid.subtotalPrefix", { head: dc.head })
              : t("pivotGrid.total");
            // Single-measure grand column is sortable; multi-measure grand
            // sorting is delegated to the per-measure sub-row below.
            const grandSortable = isGrandCol && !multiMeasure;
            const grandSorted = grandSortable && resolvedSort?.ckIndex === "grand" && resolvedSort?.measureIndex === 0;
            return (
              <TableCell
                key={`${lvl}-${j}`}
                align="right"
                rowSpan={rowDimHeaderSpan}
                colSpan={multiMeasure ? measureCount : 1}
                role={grandSortable ? "columnheader" : undefined}
                aria-sort={grandSortable ? (grandSorted ? (resolvedSort?.dir === "asc" ? "ascending" : "descending") : "none") : undefined}
                tabIndex={grandSortable ? 0 : -1}
                onClick={grandSortable ? () => cycleSort("grand", 0) : undefined}
                onKeyDown={grandSortable
                  ? (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); cycleSort("grand", 0); } }
                  : undefined}
                title={grandSortable ? t("pivotGrid.sortColumn") : undefined}
                sx={{
                  fontWeight: 700,
                  bgcolor: isGrandCol ? ui.grandTotalBg : ui.subtotalBg,
                  cursor: grandSortable ? "pointer" : "default",
                  userSelect: "none",
                }}
              >
                <Box component="span" sx={{ display: "inline-flex", alignItems: "center", gap: 0.5, justifyContent: "flex-end" }}>
                  {label}
                  {grandSortable && <SortGlyph active={Boolean(grandSorted)} dir={resolvedSort?.dir} />}
                </Box>
              </TableCell>
            );
          })}
        </TableRow>,
      );
    }

    // Measure sub-row: one sortable header cell per measure per col-group.
    if (multiMeasure) {
      headerRows.push(
        <TableRow key="h-measures">
          {displayCols.flatMap((dc, j) => {
            if (dc.kind === "data") {
              return allMeasures.map((m, mi) => {
                const sorted = resolvedSort?.ckIndex === dc.ckIndex && resolvedSort?.measureIndex === mi;
                return (
                  <TableCell
                    key={`ms-${j}-${mi}`}
                    align="right"
                    scope="col"
                    role="columnheader"
                    aria-sort={sorted ? (resolvedSort?.dir === "asc" ? "ascending" : "descending") : "none"}
                    tabIndex={0}
                    onClick={() => cycleSort(dc.ckIndex, mi)}
                    onKeyDown={(ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); cycleSort(dc.ckIndex, mi); } }}
                    title={t("pivotGrid.sortColumn")}
                    sx={{
                      fontWeight: 600,
                      fontSize: "0.7rem",
                      cursor: "pointer",
                      userSelect: "none",
                      color: "text.secondary",
                      "&:hover": { bgcolor: "action.hover" },
                    }}
                  >
                    <Box component="span" sx={{ display: "inline-flex", alignItems: "center", gap: 0.5, justifyContent: "flex-end" }}>
                      {m.display_name || m.name}
                      <SortGlyph active={Boolean(sorted)} dir={resolvedSort?.dir} />
                    </Box>
                  </TableCell>
                );
              });
            }
            // Grand column: per-measure sortable sub-headers. Subtotal columns
            // stay non-sortable placeholders.
            if (dc.kind === "grandCol") {
              return allMeasures.map((m, mi) => {
                const sorted = resolvedSort?.ckIndex === "grand" && resolvedSort?.measureIndex === mi;
                return (
                  <TableCell
                    key={`ms-${j}-grand-${mi}`}
                    align="right"
                    scope="col"
                    role="columnheader"
                    aria-sort={sorted ? (resolvedSort?.dir === "asc" ? "ascending" : "descending") : "none"}
                    tabIndex={0}
                    onClick={() => cycleSort("grand", mi)}
                    onKeyDown={(ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); cycleSort("grand", mi); } }}
                    title={t("pivotGrid.sortColumn")}
                    sx={{
                      fontWeight: 700,
                      fontSize: "0.7rem",
                      cursor: "pointer",
                      userSelect: "none",
                      bgcolor: ui.grandTotalBg,
                      "&:hover": { bgcolor: ui.grandTotalBg },
                    }}
                  >
                    <Box component="span" sx={{ display: "inline-flex", alignItems: "center", gap: 0.5, justifyContent: "flex-end" }}>
                      {m.display_name || m.name}
                      <SortGlyph active={Boolean(sorted)} dir={resolvedSort?.dir} />
                    </Box>
                  </TableCell>
                );
              });
            }
            return (
              <TableCell
                key={`ms-${j}-total`}
                align="right"
                colSpan={measureCount}
                sx={{ fontWeight: 700, bgcolor: ui.subtotalBg }}
              />
            );
          })}
        </TableRow>,
      );
    }
  } else {
    // No col dims: one header cell per measure.
    headerRows.push(
      <TableRow key="h-0">
        {rowCols.map((rc, i) => (
          <TableCell key={`rh-${i}`} sx={{ fontWeight: 600 }}>{rowLabels[i] ?? rc}</TableCell>
        ))}
        {allMeasures.map((m, mi) => {
          const sorted = resolvedSort?.measureIndex === mi && resolvedSort?.ckIndex === 0;
          return (
            <TableCell
              key={`mh-${mi}`}
              align="right"
              scope="col"
              role="columnheader"
              aria-sort={sorted ? (resolvedSort?.dir === "asc" ? "ascending" : "descending") : "none"}
              tabIndex={0}
              onClick={() => cycleSort(0, mi)}
              onKeyDown={(ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); cycleSort(0, mi); } }}
              title={t("pivotGrid.sortColumn")}
              sx={{ fontWeight: 600, cursor: "pointer", userSelect: "none", "&:hover": { bgcolor: "action.hover" } }}
            >
              <Box component="span" sx={{ display: "inline-flex", alignItems: "center", gap: 0.5, justifyContent: "flex-end" }}>
                {m.display_name || m.name}
                <SortGlyph active={Boolean(sorted)} dir={resolvedSort?.dir} />
              </Box>
            </TableCell>
          );
        })}
        {grandActive && rowCols.length > 0 && allMeasures.map((m, mi) => {
          const sorted = resolvedSort?.ckIndex === "grand" && resolvedSort?.measureIndex === mi;
          return (
            <TableCell
              key={`gh-${mi}`}
              align="right"
              scope="col"
              role="columnheader"
              aria-sort={sorted ? (resolvedSort?.dir === "asc" ? "ascending" : "descending") : "none"}
              tabIndex={0}
              onClick={() => cycleSort("grand", mi)}
              onKeyDown={(ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); cycleSort("grand", mi); } }}
              title={t("pivotGrid.sortColumn")}
              sx={{ fontWeight: 700, cursor: "pointer", userSelect: "none", bgcolor: ui.grandTotalBg, "&:hover": { bgcolor: ui.grandTotalBg } }}
            >
              <Box component="span" sx={{ display: "inline-flex", alignItems: "center", gap: 0.5, justifyContent: "flex-end" }}>
                {multiMeasure ? (m.display_name || m.name) : t("pivotGrid.total")}
                <SortGlyph active={Boolean(sorted)} dir={resolvedSort?.dir} />
              </Box>
            </TableCell>
          );
        })}
      </TableRow>,
    );
  }

  // ---- Render body ----
  const bodyRows: React.ReactNode[] = [];
  displayRows.forEach((dr, drIdx) => {
    const isTotalRow = dr.kind !== "data";
    const labelCells: React.ReactNode[] = [];
    if (rowCols.length > 0) {
      if (dr.kind === "data") {
        dr.rk.forEach((rv, i) => {
          const dimName = rowCols[i];
          const isDrillable = dimName && drillableRowDims?.has(dimName);
          const hierarchyName = isDrillable ? drillHierarchyNames?.get(dimName) : undefined;
          const cell = (
            <TableCell
              key={`rv-${drIdx}-${i}`}
              tabIndex={isDrillable ? 0 : -1}
              sx={{
                whiteSpace: "nowrap",
                ...(isDrillable
                  ? {
                      cursor: "pointer",
                      fontWeight: 600,
                      "&:hover": { bgcolor: "action.hover", color: "primary.main" },
                      "&:focus-visible": { outline: "2px solid", outlineColor: "primary.main", outlineOffset: -2 },
                    }
                  : {}),
              }}
              onClick={isDrillable && onRowDrill ? () => onRowDrill(dimName, rv, rawRowValuesByKey.get(tupleKey(dr.rk))?.[i]) : undefined}
              onKeyDown={isDrillable && onRowDrill ? (ev) => {
                if (ev.key === "Enter" || ev.key === " ") {
                  ev.preventDefault();
                  onRowDrill(dimName, rv, rawRowValuesByKey.get(tupleKey(dr.rk))?.[i]);
                }
              } : undefined}
            >
              {rv}
              {isDrillable && (
                <Box component="span" sx={{ ml: 0.5, fontSize: 10, color: "primary.main", verticalAlign: "middle" }}>▸</Box>
              )}
            </TableCell>
          );
          labelCells.push(
            hierarchyName ? (
              <Tooltip key={`rv-${drIdx}-${i}`} title={t("pivot.drillAvailableHint", { hierarchy: hierarchyName })} placement="top" arrow>
                {cell}
              </Tooltip>
            ) : (
              cell
            ),
          );
        });
      } else if (dr.kind === "subtotalRow") {
        labelCells.push(
          <TableCell key={`rv-${drIdx}-sub`} colSpan={rowCols.length} sx={{ fontWeight: 700, bgcolor: ui.subtotalBg }}>
            {t("pivotGrid.subtotalPrefix", { head: dr.head })}
          </TableCell>,
        );
      } else {
        labelCells.push(
          <TableCell key={`rv-${drIdx}-grand`} colSpan={rowCols.length} sx={{ fontWeight: 700, bgcolor: ui.grandTotalBg }}>
            {t("pivotGrid.total")}
          </TableCell>,
        );
      }
    }

    // Each displayCol renders one cell per measure.
    const valueCells: React.ReactNode[] = displayCols.flatMap((dc, dcIdx) => {
      const isTotalCol = dc.kind !== "data";
      const keyBase = `c-${drIdx}-${dcIdx}`;

      if (dr.kind === "data" && dc.kind === "data") {
        const cell = byKey.get(cellLookupKey(dr.rk, dc.ck));
        return allMeasures.map((m, mi) => {
          const fmt = (m.format ?? null) as MeasureFormatToken | null;
          const rawVal = cellValue(cell, m);
          const { text, isMissing } = renderValueCell(rawVal, fmt, emptyCellMode);
          // Clickable for any non-scratchpad, non-record-count measure.
          const isScratchpad = (m as { _scratchpad?: boolean })._scratchpad === true;
          const isRecordCount = (m as { _recordCount?: boolean })._recordCount === true;
          const thisClickable = !isScratchpad && !isRecordCount && Boolean(cell) && clickable;
          // Conditional formatting applies only to the first measure.
          const bg = mi === 0 ? getCellBg(rawVal) : undefined;
          const isGradient = bg?.startsWith("linear-gradient");
          return (
            <TableCell
              key={`${keyBase}-${mi}`}
              align="right"
              onClick={thisClickable && cell ? () => onCellClick && onCellClick(cell, m) : undefined}
              title={thisClickable ? t("drill.drawerTitle", { name: m.display_name || m.name }) : undefined}
              sx={{
                cursor: thisClickable ? "pointer" : "default",
                color: isMissing && emptyCellMode === "blank" ? "text.disabled" : "text.primary",
                whiteSpace: "nowrap",
                fontVariantNumeric: "tabular-nums",
                ...(bg && !isGradient && { bgcolor: bg }),
                ...(bg && isGradient && { background: bg }),
                "&:hover": thisClickable ? { bgcolor: "action.hover", textDecoration: "underline" } : undefined,
              }}
            >
              {text}
            </TableCell>
          );
        });
      }

      // Total / subtotal cells — computed per measure. Grand cells use a
      // stronger brand tint than subtotal cells so the two read distinctly.
      const isGrandCell = dr.kind === "grandRow" || dc.kind === "grandCol";
      const cellSx = isTotalRow || isTotalCol
        ? { fontWeight: 700, bgcolor: isGrandCell ? ui.grandTotalBg : ui.subtotalBg }
        : undefined;
      return allMeasures.map((m, mi) => {
        const fmt = (m.format ?? null) as MeasureFormatToken | null;
        const mTotals = getMeasureTotals(m);
        let total: TotalValue = null;
        if (mTotals) {
          if (dr.kind === "data" && dc.kind === "subtotalCol") {
            total = mTotals.colSubtotals.get(dc.head)?.[dr.rkIndex] ?? null;
          } else if (dr.kind === "data" && dc.kind === "grandCol") {
            total = mTotals.grandCol[dr.rkIndex] ?? null;
          } else if (dr.kind === "subtotalRow" && dc.kind === "data") {
            total = mTotals.rowSubtotals.get(dr.head)?.[dc.ckIndex] ?? null;
          } else if (dr.kind === "subtotalRow" && dc.kind === "subtotalCol") {
            total = mTotals.crossSubtotals.get(`${dr.head}||${dc.head}`) ?? null;
          } else if (dr.kind === "subtotalRow" && dc.kind === "grandCol") {
            total = mTotals.rowSubtotalGrand.get(dr.head) ?? null;
          } else if (dr.kind === "grandRow" && dc.kind === "data") {
            total = mTotals.grandRow[dc.ckIndex] ?? null;
          } else if (dr.kind === "grandRow" && dc.kind === "subtotalCol") {
            total = mTotals.colSubtotalGrand.get(dc.head) ?? null;
          } else if (dr.kind === "grandRow" && dc.kind === "grandCol") {
            total = mTotals.grandGrand;
          }
        }
        const rendered = renderTotalCell(total, fmt, emptyCellMode, t("pivotGrid.notAdditive"));
        const isScratchpad = (m as { _scratchpad?: boolean })._scratchpad === true;
        const isRecordCount = (m as { _recordCount?: boolean })._recordCount === true;
        const totalClickable = clickable && !isScratchpad && !isRecordCount;
        const coord = totalCoordinate(dr, dc, total);
        const node = (
          <TableCell
            key={`${keyBase}-${mi}`}
            data-total-kind={totalCellKind(dr, dc)}
            align="right"
            tabIndex={totalClickable ? 0 : undefined}
            aria-label={
              totalClickable
                ? totalCellAriaLabel(dr, dc, rendered.text, m.display_name || m.name)
                : undefined
            }
            title={totalClickable ? t("drill.drawerTitle", { name: m.display_name || m.name }) : undefined}
            onClick={totalClickable ? () => onCellClick?.(coord, m) : undefined}
            onKeyDown={totalClickable ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onCellClick?.(coord, m);
              }
            } : undefined}
            sx={{
              ...cellSx,
              cursor: totalClickable ? "pointer" : "default",
              "&:hover": totalClickable ? { textDecoration: "underline" } : undefined,
              "&:focus-visible": totalClickable
                ? { outline: "2px solid", outlineColor: "primary.main", outlineOffset: -2 }
                : undefined,
            }}
          >
            {rendered.text}
          </TableCell>
        );
        if (rendered.tooltip) {
          return <Tooltip key={`${keyBase}-${mi}`} title={rendered.tooltip}>{node}</Tooltip>;
        }
        return node;
      });
    });

    bodyRows.push(
      <TableRow key={`r-${drIdx}`}>
        {labelCells}
        {valueCells}
      </TableRow>,
    );
  });

  if (displayRows.length === 0) {
    const totalCols = rowCols.length + displayCols.length * measureCount;
    bodyRows.push(
      <TableRow key="empty">
        <TableCell colSpan={Math.max(1, totalCols)}>{t("pivotGrid.noRows")}</TableCell>
      </TableRow>,
    );
  }

  return (
    <Box sx={{ flexGrow: 1, minHeight: 0, display: "flex", flexDirection: "column", gap: 0.75 }}>
      <TableContainer
        component={Paper}
        variant="outlined"
        sx={{
          flexGrow: 1,
          minHeight: 260,
          overflow: "auto",
          borderRadius: 1,
          "& .MuiTableCell-root": { borderColor: "divider" },
          "& .MuiTableHead-root .MuiTableCell-root": { bgcolor: "background.paper" },
        }}
      >
        <Table
          size="small"
          stickyHeader
          role="grid"
          aria-label={t("pivotGrid.ariaLabel", { name: measure.display_name || measure.name })}
        >
          <TableHead>{headerRows}</TableHead>
          <TableBody>{bodyRows}</TableBody>
        </Table>
      </TableContainer>
    </Box>
  );
}
