import { describe, it, expect } from "vitest";
import type { Dimension, ExecuteResponse, Measure } from "../../../../api/types";
import { computePivot } from "../pivot";
import { computeTotals } from "../totals";
import { pivotToCsv } from "./csv";

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
  };
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
  };
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

describe("pivotToCsv (F-019-08)", () => {
  const rev = measure("Revenue");
  const cost = measure("Cost");

  it("emits every measure column, not just the first", () => {
    const pivot = computePivot(
      exec([
        { region: "EMEA", Revenue: 100, Cost: 40 },
        { region: "APAC", Revenue: 80, Cost: 30 },
      ]),
      rev,
      [dim("region")],
      [],
      [cost],
    );
    const csv = pivotToCsv(pivot, rev, { extraMeasures: [cost] });
    const lines = csv.trim().split("\r\n");
    // Header carries both measure names.
    expect(lines[0]).toContain("Revenue");
    expect(lines[0]).toContain("Cost");
    // Each data row carries both values.
    const emea = lines.find((l) => l.startsWith("EMEA"))!;
    expect(emea).toContain("100");
    expect(emea).toContain("40");
  });

  it("honours the supplied row order (sort)", () => {
    const pivot = computePivot(
      exec([
        { region: "APAC", Revenue: 80 },
        { region: "EMEA", Revenue: 100 },
      ]),
      rev,
      [dim("region")],
      [],
    );
    // Default order is APAC, EMEA; force EMEA first via rowKeyOrder.
    const csv = pivotToCsv(pivot, rev, {
      rowKeyOrder: [["EMEA"], ["APAC"]],
    });
    const lines = csv.trim().split("\r\n");
    expect(lines[1].startsWith("EMEA")).toBe(true);
    expect(lines[2].startsWith("APAC")).toBe(true);
  });

  it("includes the grand-total row when grand totals are on", () => {
    const pivot = computePivot(
      exec([
        { region: "EMEA", Revenue: 100 },
        { region: "APAC", Revenue: 80 },
      ]),
      rev,
      [dim("region")],
      [],
    );
    const totals = computeTotals(pivot, rev);
    const csv = pivotToCsv(pivot, rev, {
      allTotals: new Map([[rev.name, totals]]),
      showGrandTotals: true,
    });
    const grand = csv.trim().split("\r\n").find((l) => l.startsWith("Grand Total"))!;
    expect(grand).toBeDefined();
    expect(grand).toContain("180");
  });
});
