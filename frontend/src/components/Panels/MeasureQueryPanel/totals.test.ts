import { describe, it, expect } from "vitest";
import type { Dimension, ExecuteResponse, Measure } from "../../../api/types";
import { computePivot } from "./pivot";
import { NOT_ADDITIVE, computeTotals } from "./totals";

// Bug-7278: computeTotals is on the core "run a query, see results" path. The
// previous implementation re-scanned the full row×col grid once per
// (rowHead, colHead) pair, giving O(rowHeads × colHeads × rowKeys × colKeys)
// and freezing the browser on moderately large pivots. These tests pin the
// numeric results (correctness preserved) AND assert the single-pass rewrite
// completes a large-shape fixture without the nested blow-up.

function dim(name: string): Dimension {
  return {
    id: name,
    name,
    display_name: name,
    source_column_id: null,
    source_column_name: name,
    source_table_id: null,
    user_defined_attribute_id: null,
    user_defined_attribute_name: null,
    hierarchy: null,
    is_time_dim: false,
    time_grain: null,
    redundant_partner: null,
  } as unknown as Dimension;
}

function measure(name: string, overrides: Partial<Measure> = {}): Measure {
  return {
    id: name,
    name,
    display_name: name,
    source_column_id: null,
    source_column_name: name,
    source_table_id: null,
    user_defined_attribute_id: null,
    user_defined_attribute_name: null,
    measure_type: "standard",
    expression: null,
    default_agg: "sum",
    data_type: "numeric",
    format: null,
    is_additive: true,
    redundant_partner: null,
    ...overrides,
  } as unknown as Measure;
}

function exec(rows: Record<string, unknown>[]): ExecuteResponse {
  return {
    rows,
    columns: Object.keys(rows[0] ?? {}),
    route_type: "source",
    aggregate_id: null,
    execution_ms: 0,
    bytes_processed: 0,
    rows_returned: rows.length,
    trace: { stages: [] },
  } as unknown as ExecuteResponse;
}

describe("computeTotals — correctness (single-pass, Bug-7278)", () => {
  // Two-level row and col keys so cross subtotals, per-head subtotals, and
  // empty intersections are all exercised.
  const rev = measure("Revenue");
  const rowDims = [dim("region"), dim("country")];
  const colDims = [dim("year"), dim("quarter")];
  const pivot = computePivot(
    exec([
      { region: "EMEA", country: "DE", year: "2024", quarter: "Q1", Revenue: 100 },
      { region: "EMEA", country: "DE", year: "2024", quarter: "Q2", Revenue: 50 },
      { region: "EMEA", country: "FR", year: "2025", quarter: "Q1", Revenue: 40 },
      { region: "APAC", country: "JP", year: "2024", quarter: "Q1", Revenue: 30 },
      { region: "APAC", country: "JP", year: "2025", quarter: "Q2", Revenue: 20 },
    ]),
    rev,
    rowDims,
    colDims,
  );

  it("grand-grand equals the sum of every cell", () => {
    const totals = computeTotals(pivot, rev);
    expect(totals.grandGrand).toBe(240);
  });

  it("row-subtotal grand sums all cells under each row head", () => {
    const totals = computeTotals(pivot, rev);
    expect(totals.rowSubtotalGrand.get("EMEA")).toBe(190); // 100+50+40
    expect(totals.rowSubtotalGrand.get("APAC")).toBe(50); // 30+20
  });

  it("col-subtotal grand sums all cells under each col head", () => {
    const totals = computeTotals(pivot, rev);
    expect(totals.colSubtotalGrand.get("2024")).toBe(180); // 100+50+30
    expect(totals.colSubtotalGrand.get("2025")).toBe(60); // 40+20
  });

  it("cross subtotals bucket by (rowHead, colHead), null for empty intersections", () => {
    const totals = computeTotals(pivot, rev);
    expect(totals.crossSubtotals.get("EMEA||2024")).toBe(150); // 100+50
    expect(totals.crossSubtotals.get("EMEA||2025")).toBe(40);
    expect(totals.crossSubtotals.get("APAC||2024")).toBe(30);
    expect(totals.crossSubtotals.get("APAC||2025")).toBe(20);
    // Every rowHead×colHead pair is present even when no cell intersects.
    expect(totals.crossSubtotals.size).toBe(4);
  });

  it("row/col subtotal arrays and grand arrays align with axis order and sum", () => {
    const totals = computeTotals(pivot, rev);
    // Grand row summed across all its col cells == grand-grand.
    const grandRowSum = totals.grandRow.reduce<number>(
      (a, v) => a + (typeof v === "number" ? v : 0),
      0,
    );
    expect(grandRowSum).toBe(240);
    // Grand col summed across all its row cells == grand-grand.
    const grandColSum = totals.grandCol.reduce<number>(
      (a, v) => a + (typeof v === "number" ? v : 0),
      0,
    );
    expect(grandColSum).toBe(240);
  });

  it("returns null (not 0) for a head bucket whose cells are all non-numeric", () => {
    const p = computePivot(
      exec([
        { region: "EMEA", country: "DE", year: "2024", quarter: "Q1", Revenue: null },
      ]),
      rev,
      rowDims,
      colDims,
    );
    const totals = computeTotals(p, rev);
    expect(totals.grandGrand).toBeNull();
    expect(totals.rowSubtotalGrand.get("EMEA")).toBeNull();
  });

  it("non-additive measure marks every total NOT_ADDITIVE", () => {
    const nonAdditive = measure("distinct_customers", { is_additive: false });
    const totals = computeTotals(pivot, nonAdditive);
    expect(totals.grandGrand).toBe(NOT_ADDITIVE);
    expect(totals.grandRow.every((v) => v === NOT_ADDITIVE)).toBe(true);
    expect(totals.grandCol.every((v) => v === NOT_ADDITIVE)).toBe(true);
    expect([...totals.crossSubtotals.values()].every((v) => v === NOT_ADDITIVE)).toBe(true);
  });

  // R1 stage-2 adversarial finding: rowSubtotalGrand / colSubtotalGrand must be
  // the sum of the DISPLAYED per-axis subtotals, not a separate raw-cell sum.
  // Summing raw cells in a different order can round differently for values that
  // straddle the float precision gap, so the grand-of-subtotals would stop
  // reconciling with the subtotal values a user sees. This pins the
  // reconciliation with the exact mixed-magnitude fixture that exposed it.
  it("grand-of-subtotals reconciles with the displayed subtotals (float assoc)", () => {
    const rev = measure("Revenue");
    const rowDims = [dim("region"), dim("country")];
    const colDims = [dim("year")];
    // One row head "H", three cols, big/small magnitudes interleaved so that
    // per-column the ±1e16 cancel and the small values survive, but a single
    // raw accumulator over all cells would drop them.
    const p = computePivot(
      exec([
        { region: "H", country: "a", year: "2024", Revenue: 1e16 },
        { region: "H", country: "b", year: "2024", Revenue: -1e16 },
        { region: "H", country: "c", year: "2024", Revenue: 0.3 },
        { region: "H", country: "d", year: "2025", Revenue: 1e16 },
        { region: "H", country: "e", year: "2025", Revenue: -1e16 },
      ]),
      rev,
      rowDims,
      colDims,
    );
    const totals = computeTotals(p, rev);
    const subtotalArray = totals.rowSubtotals.get("H")!;
    // Grand-of-subtotals must equal the plain sum of the displayed subtotal
    // values (nulls skipped), regardless of accumulation order.
    const expected = subtotalArray.reduce<number>(
      (a, v) => a + (typeof v === "number" ? v : 0),
      0,
    );
    expect(totals.rowSubtotalGrand.get("H")).toBe(expected);
    // And that reconciled value is the meaningful 0.3, not 0.
    expect(totals.rowSubtotalGrand.get("H")).toBe(0.3);
  });
});

describe("computeTotals — scale guard (Bug-7278)", () => {
  // A pivot with a large number of distinct row heads AND col heads is exactly
  // the shape the old nested cross-subtotal loop blew up on: its cost grew as
  // rowHeads × colHeads × rowKeys × colKeys. With 300 row heads × 300 col
  // heads × ~1 cell each that is ~8.1e9 iterations for the old algorithm — a
  // hard browser freeze. The single-pass version is O(cells), so this must
  // complete near-instantly.
  it("completes a large distinct-head pivot without the nested blow-up", () => {
    const rev = measure("Revenue");
    const rowDims = [dim("r")];
    const colDims = [dim("c")];
    const N = 300; // 300×300 = 90,000 populated cells
    const rows: Record<string, unknown>[] = [];
    let expected = 0;
    for (let i = 0; i < N; i++) {
      for (let j = 0; j < N; j++) {
        rows.push({ r: `r${i}`, c: `c${j}`, Revenue: 1 });
        expected += 1;
      }
    }
    const pivot = computePivot(exec(rows), rev, rowDims, colDims);
    expect(pivot.rowKeys.length).toBe(N);
    expect(pivot.colKeys.length).toBe(N);

    const start = Date.now();
    const totals = computeTotals(pivot, rev);
    const elapsedMs = Date.now() - start;

    // Correctness at scale.
    expect(totals.grandGrand).toBe(expected);
    expect(totals.crossSubtotals.size).toBe(N * N);

    // Complexity guard: single-pass must finish comfortably under a budget the
    // O(rowHeads×colHeads×rowKeys×colKeys) version could never meet (that would
    // be ~8.1e9 map lookups here). 4 s leaves generous slack for slow CI while
    // still failing hard if the quartic scan is ever reintroduced.
    expect(elapsedMs).toBeLessThan(4000);
  });
});
