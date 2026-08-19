import type { Measure } from "../../../api/types";
import { cellLookupKey } from "./pivot";
import type { PivotModel } from "./types";

export const NOT_ADDITIVE = Symbol("not-additive");
export type TotalValue = number | null | typeof NOT_ADDITIVE;

export type TotalsModel = {
  /** Per outermost-row-level-0 group: one subtotal value per col key. */
  rowSubtotals: Map<string, TotalValue[]>;
  /** Per outermost-col-level-0 group: one subtotal value per row key. */
  colSubtotals: Map<string, TotalValue[]>;
  /** Grand-total row: one value per col key. */
  grandRow: TotalValue[];
  /** Grand-total col: one value per row key. */
  grandCol: TotalValue[];
  /** Grand-grand (total of totals). */
  grandGrand: TotalValue;
  /** Cross subtotal (row-subtotal × col-subtotal) keyed by `rowHead||colHead`. */
  crossSubtotals: Map<string, TotalValue>;
  /** Grand × row-subtotal: row-subtotal summed across all cols. Keyed by rowHead. */
  rowSubtotalGrand: Map<string, TotalValue>;
  /** Grand × col-subtotal: col-subtotal summed across all rows. Keyed by colHead. */
  colSubtotalGrand: Map<string, TotalValue>;
};

function toNumber(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v !== "") {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

function sum(values: unknown[]): number | null {
  let acc = 0;
  let any = false;
  for (const v of values) {
    const n = toNumber(v);
    if (n !== null) {
      acc += n;
      any = true;
    }
  }
  return any ? acc : null;
}

// A running-sum accumulator that mirrors ``sum`` exactly: it yields ``null``
// unless at least one finite numeric value was added, in which case it yields
// the total. Used by the single-pass total builder so we never re-scan cells.
type Accumulator = { acc: number; any: boolean };

function newAccumulator(): Accumulator {
  return { acc: 0, any: false };
}

function addValue(a: Accumulator, v: unknown): void {
  const n = toNumber(v);
  if (n !== null) {
    a.acc += n;
    a.any = true;
  }
}

function accValue(a: Accumulator | undefined): number | null {
  if (!a || !a.any) return null;
  return a.acc;
}

// Aggregate functions whose per-cell results can be validly re-aggregated by
// summing (the client-side totals algorithm only knows how to sum). SUM and
// COUNT compose under addition; AVG / MIN / MAX / COUNT_DISTINCT do not, so
// they render as em-dash rather than a misleading summed total.
const SUMMABLE_AGGS = new Set(["SUM", "COUNT"]);

export function isTotalableMeasure(measure: Measure): boolean {
  // Calc and non-additive measures never get totals.
  if (measure.measure_type !== "standard") return false;
  if (!measure.is_additive) return false;
  // When the measure carries a chosen aggregate function, only summable
  // functions can be totalled client-side.
  const agg = (measure as { _agg?: string })._agg;
  if (agg && !SUMMABLE_AGGS.has(agg.toUpperCase())) return false;
  return true;
}

export function computeTotals(
  pivot: PivotModel,
  measure: Measure,
): TotalsModel {
  const totalable = isTotalableMeasure(measure);
  const marker: TotalValue = totalable ? null : NOT_ADDITIVE;
  const { rowKeys, colKeys, byKey } = pivot;

  // Bug-7278: totals are built in a SINGLE pass over the populated cells rather
  // than by nested re-scans of the full row×col grid per subtotal. The previous
  // cross-subtotal step was O(rowHeads × colHeads × rowKeys × colKeys), which
  // froze the browser on moderately large pivots. Here every axis subtotal,
  // grand row/col, grand-grand and cross subtotal is accumulated as we walk the
  // cell map once, so the cost is O(cells + rowKeys + colKeys).
  //
  // Semantics:
  //  - subtotal/grand values are ``null`` unless at least one finite numeric
  //    value contributed (matches ``sum``), and the marker is NOT_ADDITIVE for
  //    non-totalable measures;
  //  - grandRow is indexed by colKey order, grandCol by rowKey order;
  //  - rowSubtotalGrand / colSubtotalGrand are summed FROM the materialized
  //    per-axis subtotal arrays (not re-summed from raw cells), exactly as the
  //    original did. This keeps the "total of subtotals" reconciling with the
  //    subtotal values the grid actually displays, and removes a floating-point
  //    associativity difference vs the previous implementation (summing rounded
  //    subtotals vs summing raw cells can round differently for values that
  //    straddle the float precision gap).
  //
  // Per-bucket sums (rowSubtotals, colSubtotals, grandRow, grandCol, crossSubtotals,
  // grandGrand) accumulate in cell-map iteration order. For finite decimal
  // magnitudes within a subtotal group the result equals the prior nested
  // implementation; the only difference is the summation order for a single
  // aggregate, which is inherent IEEE-754 non-associativity, not a value error.

  const rowSubtotals = new Map<string, TotalValue[]>();
  const colSubtotals = new Map<string, TotalValue[]>();
  const crossSubtotals = new Map<string, TotalValue>();
  const rowSubtotalGrand = new Map<string, TotalValue>();
  const colSubtotalGrand = new Map<string, TotalValue>();

  // Distinct outermost (level-0) heads, in first-seen order over the sorted keys.
  const rowHeads = new Set<string>();
  for (const rk of rowKeys) rowHeads.add(rk[0] ?? "");
  const colHeads = new Set<string>();
  for (const ck of colKeys) colHeads.add(ck[0] ?? "");

  if (!totalable) {
    // Non-totalable: fill every subtotal/grand with the NOT_ADDITIVE marker so
    // the grid can render "—" uniformly. No cell scan needed.
    for (const h of rowHeads) {
      rowSubtotals.set(h, colKeys.map(() => marker));
      rowSubtotalGrand.set(h, marker);
    }
    for (const h of colHeads) {
      colSubtotals.set(h, rowKeys.map(() => marker));
      colSubtotalGrand.set(h, marker);
    }
    for (const rh of rowHeads) {
      for (const ch of colHeads) crossSubtotals.set(`${rh}||${ch}`, marker);
    }
    const grandRow: TotalValue[] = colKeys.map(() => marker);
    const grandCol: TotalValue[] = rowKeys.map(() => marker);
    return {
      rowSubtotals,
      colSubtotals,
      grandRow,
      grandCol,
      grandGrand: marker,
      crossSubtotals,
      rowSubtotalGrand,
      colSubtotalGrand,
    };
  }

  // Positional indices so cell contributions land in the right array slot.
  const colIndex = new Map<string, number>();
  colKeys.forEach((ck, i) => colIndex.set(cellLookupKey([], ck), i));
  const rowIndex = new Map<string, number>();
  rowKeys.forEach((rk, i) => rowIndex.set(cellLookupKey(rk, []), i));

  // Accumulators. rowSubtotals[head] is one accumulator per colKey; colSubtotals
  // [head] is one accumulator per rowKey. Grand arrays mirror those axes.
  const rowSubAcc = new Map<string, Accumulator[]>();
  for (const h of rowHeads) rowSubAcc.set(h, colKeys.map(() => newAccumulator()));
  const colSubAcc = new Map<string, Accumulator[]>();
  for (const h of colHeads) colSubAcc.set(h, rowKeys.map(() => newAccumulator()));
  const grandRowAcc = colKeys.map(() => newAccumulator());
  const grandColAcc = rowKeys.map(() => newAccumulator());
  const grandGrandAcc = newAccumulator();
  const crossAcc = new Map<string, Accumulator>();

  // Single pass over populated cells.
  for (const cell of byKey.values()) {
    const rh = cell.rowKey[0] ?? "";
    const ch = cell.colKey[0] ?? "";
    const ri = rowIndex.get(cellLookupKey(cell.rowKey, []));
    const ci = colIndex.get(cellLookupKey([], cell.colKey));
    const value = cell.measureValues?.[measure.name] ?? cell.measureValue;

    if (ci !== undefined) {
      const arr = rowSubAcc.get(rh);
      if (arr) addValue(arr[ci], value);
      addValue(grandRowAcc[ci], value);
    }
    if (ri !== undefined) {
      const arr = colSubAcc.get(ch);
      if (arr) addValue(arr[ri], value);
      addValue(grandColAcc[ri], value);
    }
    addValue(grandGrandAcc, value);

    const crossKey = `${rh}||${ch}`;
    let cross = crossAcc.get(crossKey);
    if (!cross) {
      cross = newAccumulator();
      crossAcc.set(crossKey, cross);
    }
    addValue(cross, value);
  }

  // Materialize accumulators into the public TotalsModel shape.
  for (const [head, accs] of rowSubAcc) {
    rowSubtotals.set(head, accs.map(accValue));
  }
  for (const [head, accs] of colSubAcc) {
    colSubtotals.set(head, accs.map(accValue));
  }
  const grandRow: TotalValue[] = grandRowAcc.map(accValue);
  const grandCol: TotalValue[] = grandColAcc.map(accValue);
  const grandGrand: TotalValue = accValue(grandGrandAcc);

  // rowSubtotalGrand / colSubtotalGrand are the grand of the per-axis subtotals.
  // Sum the MATERIALIZED subtotal arrays (not a separate raw-cell accumulator)
  // so the value reconciles with the displayed subtotals and matches the prior
  // implementation exactly. ``sum`` filters non-numeric entries (nulls) as the
  // original ``v => typeof v === "number" ? v : null`` mapping did.
  for (const [head, vals] of rowSubtotals) {
    rowSubtotalGrand.set(head, sum(vals));
  }
  for (const [head, vals] of colSubtotals) {
    colSubtotalGrand.set(head, sum(vals));
  }

  // Cross subtotals must exist for every (rowHead, colHead) pair — including
  // empty intersections (value null) — to match the prior full-grid behavior.
  for (const rh of rowHeads) {
    for (const ch of colHeads) {
      const key = `${rh}||${ch}`;
      crossSubtotals.set(key, accValue(crossAcc.get(key)));
    }
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
