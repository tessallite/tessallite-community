import type { Measure } from "../../../api/types";
import { cellLookupKey } from "./pivot";
import type { CellCoord, PivotModel } from "./types";

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

  const rowSubtotals = new Map<string, TotalValue[]>();
  const colSubtotals = new Map<string, TotalValue[]>();

  if (totalable) {
    // Row subtotals keyed by the outermost (level-0) element of rowKey.
    const rowGroups = new Map<string, string[][]>();
    for (const rk of rowKeys) {
      const head = rk[0] ?? "";
      const bucket = rowGroups.get(head) ?? [];
      bucket.push(rk);
      rowGroups.set(head, bucket);
    }
    for (const [head, keys] of rowGroups) {
      const vals: TotalValue[] = colKeys.map((ck) => {
        const parts: unknown[] = [];
        for (const rk of keys) {
          const cell = byKey.get(cellLookupKey(rk, ck));
          if (cell) parts.push(cell.measureValues?.[measure.name] ?? cell.measureValue);
        }
        return sum(parts);
      });
      rowSubtotals.set(head, vals);
    }

    // Col subtotals keyed by the outermost element of colKey.
    const colGroups = new Map<string, string[][]>();
    for (const ck of colKeys) {
      const head = ck[0] ?? "";
      const bucket = colGroups.get(head) ?? [];
      bucket.push(ck);
      colGroups.set(head, bucket);
    }
    for (const [head, keys] of colGroups) {
      const vals: TotalValue[] = rowKeys.map((rk) => {
        const parts: unknown[] = [];
        for (const ck of keys) {
          const cell = byKey.get(cellLookupKey(rk, ck));
          if (cell) parts.push(cell.measureValues?.[measure.name] ?? cell.measureValue);
        }
        return sum(parts);
      });
      colSubtotals.set(head, vals);
    }
  } else {
    // Non-totalable: fill every subtotal/grand with the NOT_ADDITIVE marker
    // so the grid can render "—" uniformly.
    const rowHeads = new Set(rowKeys.map((rk) => rk[0] ?? ""));
    for (const h of rowHeads) rowSubtotals.set(h, colKeys.map(() => marker));
    const colHeads = new Set(colKeys.map((ck) => ck[0] ?? ""));
    for (const h of colHeads) colSubtotals.set(h, rowKeys.map(() => marker));
  }

  const grandRow: TotalValue[] = colKeys.map((ck) => {
    if (!totalable) return marker;
    const parts: unknown[] = [];
    for (const rk of rowKeys) {
      const cell = byKey.get(cellLookupKey(rk, ck));
      if (cell) parts.push(cell.measureValues?.[measure.name] ?? cell.measureValue);
    }
    return sum(parts);
  });
  const grandCol: TotalValue[] = rowKeys.map((rk) => {
    if (!totalable) return marker;
    const parts: unknown[] = [];
    for (const ck of colKeys) {
      const cell = byKey.get(cellLookupKey(rk, ck));
      if (cell) parts.push(cell.measureValues?.[measure.name] ?? cell.measureValue);
    }
    return sum(parts);
  });
  const grandGrand: TotalValue = !totalable
    ? marker
    : sum([...byKey.values()].map((c: CellCoord) => c.measureValues?.[measure.name] ?? c.measureValue));

  const crossSubtotals = new Map<string, TotalValue>();
  const rowSubtotalGrand = new Map<string, TotalValue>();
  const colSubtotalGrand = new Map<string, TotalValue>();

  if (totalable) {
    const rowHeads = new Set(rowKeys.map((rk) => rk[0] ?? ""));
    const colHeads = new Set(colKeys.map((ck) => ck[0] ?? ""));
    for (const rh of rowHeads) {
      for (const ch of colHeads) {
        const parts: unknown[] = [];
        for (const rk of rowKeys) {
          if ((rk[0] ?? "") !== rh) continue;
          for (const ck of colKeys) {
            if ((ck[0] ?? "") !== ch) continue;
            const cell = byKey.get(cellLookupKey(rk, ck));
            if (cell) parts.push(cell.measureValues?.[measure.name] ?? cell.measureValue);
          }
        }
        crossSubtotals.set(`${rh}||${ch}`, sum(parts));
      }
      const rowTotals = rowSubtotals.get(rh) ?? [];
      rowSubtotalGrand.set(
        rh,
        sum(rowTotals.map((v) => (typeof v === "number" ? v : null))),
      );
    }
    for (const ch of colHeads) {
      const colTotals = colSubtotals.get(ch) ?? [];
      colSubtotalGrand.set(
        ch,
        sum(colTotals.map((v) => (typeof v === "number" ? v : null))),
      );
    }
  } else {
    const rowHeads = new Set(rowKeys.map((rk) => rk[0] ?? ""));
    const colHeads = new Set(colKeys.map((ck) => ck[0] ?? ""));
    for (const rh of rowHeads) {
      rowSubtotalGrand.set(rh, marker);
      for (const ch of colHeads) {
        crossSubtotals.set(`${rh}||${ch}`, marker);
      }
    }
    for (const ch of colHeads) colSubtotalGrand.set(ch, marker);
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
