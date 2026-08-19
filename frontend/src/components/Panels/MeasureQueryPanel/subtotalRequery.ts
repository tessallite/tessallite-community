/**
 * F-019-02 (Bug-8046): server-side non-additive subtotals for the web pivot.
 *
 * The client-only `computeTotals` can only SUM per-cell values, so AVG / MIN /
 * MAX / COUNT DISTINCT / calculated measures rendered as an em-dash ("—") in the
 * web pivot while XMLA re-queried each grain with the real aggregation. That
 * cross-surface disagreement is the finding.
 *
 * This module mirrors the XMLA subtotal engine: for a non-composable measure it
 * issues one supplementary GROUP BY query PER SUBTOTAL GRAIN through the SAME
 * query-router execute route the pivot uses, then folds the server-computed
 * values into the {@link TotalsModel}. There are at most a fixed number of grain
 * shapes (independent of member cardinality), so the extra work is bounded.
 *
 * Additive measures (SUM / COUNT) keep the fast client-side path — this module
 * is invoked only when `isTotalableMeasure` is false but the measure can still be
 * totalled server-side (a real, non-calculated standard measure with a chosen
 * aggregate, or a calculated measure the router can evaluate at grain).
 */
import type { Dimension, ExecuteResponse, Model } from "../../../api/types";
import type { Measure } from "../../../api/types";
import { buildPivotSql } from "./sql";
import { cellLookupKey, renderDimValue } from "./pivot";
import type { PivotColumnMeasure } from "./measureColumns";
import type { PivotModel, Slicer } from "./types";
import { NOT_ADDITIVE, type TotalsModel, type TotalValue } from "./totals";

/** A single supplementary grain query and how its rows map into the totals. */
export type GrainSpec = {
  /** Stable id for the grain shape (used to route the response back). */
  id:
    | "rowSub" // rowDim0 x all colDims  -> rowSubtotals[head][colKey]
    | "colSub" // all rowDims x colDim0  -> colSubtotals[head][rowKey]
    | "cross" // rowDim0 x colDim0       -> crossSubtotals[rh||ch]
    | "rowSubGrand" // rowDim0 only       -> rowSubtotalGrand[head]
    | "colSubGrand" // colDim0 only       -> colSubtotalGrand[head]
    | "grandRow" // all colDims only      -> grandRow[colKey]
    | "grandCol" // all rowDims only      -> grandCol[rowKey]
    | "grandGrand"; // ()                 -> grandGrand
  rowDims: Dimension[];
  colDims: Dimension[];
};

/**
 * Whether a measure needs a server-side supplementary subtotal fetch: it is not
 * client-totalable (not additive SUM/COUNT) but CAN be totalled by the router at
 * grain. Record Count columns are excluded (they are COUNT(*), already additive
 * and handled client-side). A pure calculated measure is included because the
 * router evaluates it at each grain — the correct number, not a summed proxy.
 */
export function needsServerSubtotals(measure: Measure): boolean {
  const col = measure as PivotColumnMeasure;
  // Record Count is COUNT(*), additive — handled client-side.
  if (col._recordCount) return false;
  const agg = (col._agg || measure.default_agg || "").toUpperCase();
  // Additive standard measures (SUM/COUNT) are handled client-side.
  if (measure.measure_type === "standard" && measure.is_additive) {
    if (!agg || agg === "SUM" || agg === "COUNT") return false;
  }
  // A grain re-query is only correct when buildPivotSql can express the measure
  // at grain. It can for: a standard measure (agg over the base column) and a
  // scratchpad measure that carries an expression. A model-defined calculated
  // measure WITHOUT an inline expression cannot be re-projected here safely, so
  // it stays on the client path (renders the NOT_ADDITIVE marker — no wrong
  // number) rather than emitting an agg over a non-column base.
  if (measure.measure_type === "standard") return true;
  if (col._scratchpad && measure.expression) return true;
  return false;
}

/** Distinct grain shapes needed for a full subtotal/grand-total set. */
export function grainSpecsFor(
  rowDims: Dimension[],
  colDims: Dimension[],
): GrainSpec[] {
  const specs: GrainSpec[] = [];
  const rowHead = rowDims.length ? [rowDims[0]] : [];
  const colHead = colDims.length ? [colDims[0]] : [];

  if (rowDims.length && colDims.length) {
    specs.push({ id: "rowSub", rowDims: rowHead, colDims });
    specs.push({ id: "colSub", rowDims, colDims: colHead });
    specs.push({ id: "cross", rowDims: rowHead, colDims: colHead });
  }
  if (rowDims.length) {
    specs.push({ id: "rowSubGrand", rowDims: rowHead, colDims: [] });
    specs.push({ id: "grandCol", rowDims, colDims: [] });
  }
  if (colDims.length) {
    specs.push({ id: "colSubGrand", rowDims: [], colDims: colHead });
    specs.push({ id: "grandRow", rowDims: [], colDims });
  }
  specs.push({ id: "grandGrand", rowDims: [], colDims: [] });
  return specs;
}

/**
 * Build the SQL for a grain spec. Reuses {@link buildPivotSql} with the SINGLE
 * measure aggregated at the grain's dims, so the router evaluates the real
 * aggregation (AVG/MIN/MAX/COUNT DISTINCT or a calculated measure) at grain — the
 * same as XMLA's supplementary subtotal query.
 */
export function buildGrainSql(
  model: Model,
  measure: PivotColumnMeasure,
  spec: GrainSpec,
  slicers: Slicer[],
  slicerDims: Dimension[],
): string {
  return buildPivotSql(
    model,
    [measure],
    spec.rowDims,
    spec.colDims,
    slicers,
    slicerDims,
  );
}

function toTotalValue(v: unknown): TotalValue {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v !== "") {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

/**
 * Fold the grain query results into a TotalsModel. Starts from the empty totals
 * shape (every subtotal/grand slot present as null) and fills each slot from the
 * matching grain result. Slots without a matching server row stay null (the
 * grain had no facts), never NOT_ADDITIVE — the server-side path never yields the
 * em-dash marker.
 */
export function assembleServerTotals(
  pivot: PivotModel,
  measure: PivotColumnMeasure,
  results: Map<GrainSpec["id"], ExecuteResponse>,
  nullLabel = "(null)",
): TotalsModel {
  const { rowKeys, colKeys, rowCols, colCols } = pivot;
  const alias = measure._alias;

  const rowHeads: string[] = [];
  for (const rk of rowKeys) {
    const h = rk[0] ?? "";
    if (!rowHeads.includes(h)) rowHeads.push(h);
  }
  const colHeads: string[] = [];
  for (const ck of colKeys) {
    const h = ck[0] ?? "";
    if (!colHeads.includes(h)) colHeads.push(h);
  }

  const colIndex = new Map<string, number>();
  colKeys.forEach((ck, i) => colIndex.set(cellLookupKey([], ck), i));
  const rowIndex = new Map<string, number>();
  rowKeys.forEach((rk, i) => rowIndex.set(cellLookupKey(rk, []), i));

  // Seed empty structures.
  const rowSubtotals = new Map<string, TotalValue[]>();
  const colSubtotals = new Map<string, TotalValue[]>();
  const crossSubtotals = new Map<string, TotalValue>();
  const rowSubtotalGrand = new Map<string, TotalValue>();
  const colSubtotalGrand = new Map<string, TotalValue>();
  for (const h of rowHeads) {
    rowSubtotals.set(h, colKeys.map(() => null));
    rowSubtotalGrand.set(h, null);
  }
  for (const h of colHeads) {
    colSubtotals.set(h, rowKeys.map(() => null));
    colSubtotalGrand.set(h, null);
  }
  for (const rh of rowHeads) {
    for (const ch of colHeads) crossSubtotals.set(`${rh}||${ch}`, null);
  }
  const grandRow: TotalValue[] = colKeys.map(() => null);
  const grandCol: TotalValue[] = rowKeys.map(() => null);
  let grandGrand: TotalValue = null;

  const rowCol0 = rowCols[0];
  const colCol0 = colCols[0];

  const rowSub = results.get("rowSub");
  if (rowSub) {
    for (const r of rowSub.rows as Record<string, unknown>[]) {
      const head = renderDimValue(r[rowCol0], nullLabel);
      const ck = colCols.map((c) => renderDimValue(r[c], nullLabel));
      const ci = colIndex.get(cellLookupKey([], ck));
      const arr = rowSubtotals.get(head);
      if (arr && ci !== undefined) arr[ci] = toTotalValue(r[alias]);
    }
  }
  const colSub = results.get("colSub");
  if (colSub) {
    for (const r of colSub.rows as Record<string, unknown>[]) {
      const head = renderDimValue(r[colCol0], nullLabel);
      const rk = rowCols.map((c) => renderDimValue(r[c], nullLabel));
      const ri = rowIndex.get(cellLookupKey(rk, []));
      const arr = colSubtotals.get(head);
      if (arr && ri !== undefined) arr[ri] = toTotalValue(r[alias]);
    }
  }
  const cross = results.get("cross");
  if (cross) {
    for (const r of cross.rows as Record<string, unknown>[]) {
      const rh = renderDimValue(r[rowCol0], nullLabel);
      const ch = renderDimValue(r[colCol0], nullLabel);
      if (crossSubtotals.has(`${rh}||${ch}`)) {
        crossSubtotals.set(`${rh}||${ch}`, toTotalValue(r[alias]));
      }
    }
  }
  const rowSubGrand = results.get("rowSubGrand");
  if (rowSubGrand) {
    for (const r of rowSubGrand.rows as Record<string, unknown>[]) {
      const head = renderDimValue(r[rowCol0], nullLabel);
      if (rowSubtotalGrand.has(head)) {
        rowSubtotalGrand.set(head, toTotalValue(r[alias]));
      }
    }
  }
  const colSubGrand = results.get("colSubGrand");
  if (colSubGrand) {
    for (const r of colSubGrand.rows as Record<string, unknown>[]) {
      const head = renderDimValue(r[colCol0], nullLabel);
      if (colSubtotalGrand.has(head)) {
        colSubtotalGrand.set(head, toTotalValue(r[alias]));
      }
    }
  }
  const gr = results.get("grandRow");
  if (gr) {
    for (const r of gr.rows as Record<string, unknown>[]) {
      const ck = colCols.map((c) => renderDimValue(r[c], nullLabel));
      const ci = colIndex.get(cellLookupKey([], ck));
      if (ci !== undefined) grandRow[ci] = toTotalValue(r[alias]);
    }
  }
  const gc = results.get("grandCol");
  if (gc) {
    for (const r of gc.rows as Record<string, unknown>[]) {
      const rk = rowCols.map((c) => renderDimValue(r[c], nullLabel));
      const ri = rowIndex.get(cellLookupKey(rk, []));
      if (ri !== undefined) grandCol[ri] = toTotalValue(r[alias]);
    }
  }
  const gg = results.get("grandGrand");
  if (gg && gg.rows.length) {
    grandGrand = toTotalValue((gg.rows[0] as Record<string, unknown>)[alias]);
  }

  return {
    rowSubtotals,
    colSubtotals,
    grandRow,
    grandCol,
    grandGrand,
    crossSubtotals,
    rowSubtotalGrand,
    colSubtotalGrand,
  };
}

// Re-export so callers importing from this module have the marker if they need
// to distinguish a genuinely unavailable total (should not happen server-side).
export { NOT_ADDITIVE };
