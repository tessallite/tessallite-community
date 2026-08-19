import { describe, it, expect } from "vitest";
import type { Dimension, ExecuteResponse, Measure, Model } from "../../../api/types";
import { computePivot } from "./pivot";
import type { PivotColumnMeasure } from "./measureColumns";
import {
  needsServerSubtotals,
  grainSpecsFor,
  buildGrainSql,
  assembleServerTotals,
  type GrainSpec,
} from "./subtotalRequery";

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

function colMeasure(
  overrides: Partial<Measure> & Partial<PivotColumnMeasure>,
): PivotColumnMeasure {
  return {
    id: "Revenue",
    name: "Revenue__avg__0",
    display_name: "Revenue (Avg)",
    source_column_id: null,
    source_column_name: "Revenue",
    source_table_id: null,
    user_defined_attribute_id: null,
    user_defined_attribute_name: null,
    measure_type: "standard",
    expression: null,
    default_agg: "avg",
    data_type: "numeric",
    format: null,
    is_additive: false,
    redundant_partner: null,
    _alias: "Revenue__avg__0",
    _agg: "AVG",
    _baseName: "Revenue",
    _measureId: "Revenue",
    ...overrides,
  } as PivotColumnMeasure;
}

function exec(rows: Record<string, unknown>[]): ExecuteResponse {
  return {
    rows,
    columns: Object.keys(rows[0] ?? {}),
    route_type: "source",
  } as unknown as ExecuteResponse;
}

const MODEL = { slug: "modely" } as unknown as Model;

describe("needsServerSubtotals", () => {
  it("additive SUM/COUNT stay client-side", () => {
    expect(needsServerSubtotals(colMeasure({ _agg: "SUM", is_additive: true }))).toBe(false);
    expect(needsServerSubtotals(colMeasure({ _agg: "COUNT", is_additive: true }))).toBe(false);
  });
  it("non-additive aggs need server totals", () => {
    for (const agg of ["AVG", "MIN", "MAX", "COUNT_DISTINCT"]) {
      expect(needsServerSubtotals(colMeasure({ _agg: agg, is_additive: false }))).toBe(true);
    }
  });
  it("record count stays client-side", () => {
    expect(needsServerSubtotals(colMeasure({ _recordCount: true, _agg: "COUNT" }))).toBe(false);
  });
  it("scratchpad calc with expression needs server totals", () => {
    expect(
      needsServerSubtotals(
        colMeasure({ measure_type: "calculated", _scratchpad: true, expression: "a/b" }),
      ),
    ).toBe(true);
  });
  it("model calc measure without an inline expression stays client-side (safe)", () => {
    expect(
      needsServerSubtotals(colMeasure({ measure_type: "calculated", expression: null })),
    ).toBe(false);
  });
});

describe("grainSpecsFor", () => {
  it("emits the full grain set for a 1x1 pivot", () => {
    const specs = grainSpecsFor([dim("region")], [dim("year")]);
    const ids = specs.map((s) => s.id).sort();
    expect(ids).toEqual(
      ["colSub", "colSubGrand", "cross", "grandCol", "grandGrand", "grandRow", "rowSub", "rowSubGrand"].sort(),
    );
  });
  it("row-only pivot omits column grains", () => {
    const ids = grainSpecsFor([dim("region")], []).map((s) => s.id);
    expect(ids).toContain("rowSubGrand");
    expect(ids).toContain("grandGrand");
    expect(ids).not.toContain("colSub");
    expect(ids).not.toContain("cross");
  });
});

describe("buildGrainSql", () => {
  it("aggregates the measure at the grain's dims with the real aggregation", () => {
    const spec: GrainSpec = { id: "grandGrand", rowDims: [], colDims: [] };
    const sql = buildGrainSql(MODEL, colMeasure({ _agg: "AVG" }), spec, [], []);
    expect(sql).toContain('AVG("Revenue")');
    expect(sql).toContain('FROM "modely"');
  });
  it("count_distinct renders COUNT(DISTINCT ...)", () => {
    const spec: GrainSpec = { id: "rowSubGrand", rowDims: [dim("region")], colDims: [] };
    const sql = buildGrainSql(MODEL, colMeasure({ _agg: "COUNT_DISTINCT" }), spec, [], []);
    expect(sql).toContain('COUNT(DISTINCT "Revenue")');
    expect(sql).toContain('GROUP BY "region"');
  });
});

describe("assembleServerTotals (AVG known answers)", () => {
  const m = colMeasure({ _agg: "AVG" });
  const rowDims = [dim("region")];
  const colDims = [dim("year")];
  // Detail cells (each an AVG at leaf grain): EMEA/2024=100, EMEA/2025=120,
  // APAC/2024=80, APAC/2025=90.
  const pivot = computePivot(
    exec([
      { region: "EMEA", year: "2024", Revenue__avg__0: 100 },
      { region: "EMEA", year: "2025", Revenue__avg__0: 120 },
      { region: "APAC", year: "2024", Revenue__avg__0: 80 },
      { region: "APAC", year: "2025", Revenue__avg__0: 90 },
    ]),
    m,
    rowDims,
    colDims,
  );

  it("uses the re-aggregated grand total, not the sum of averages", () => {
    // Server grand-total AVG over all rows = 97.5 (NOT 100+120+80+90 = 390).
    const results = new Map<GrainSpec["id"], ExecuteResponse>();
    results.set("grandGrand", exec([{ Revenue__avg__0: 97.5 }]));
    // Row-subtotal-grand: per-region AVG (EMEA=110, APAC=85), not the row sum.
    results.set(
      "rowSubGrand",
      exec([
        { region: "EMEA", Revenue__avg__0: 110 },
        { region: "APAC", Revenue__avg__0: 85 },
      ]),
    );
    // Grand row (per year AVG across regions): 2024=90, 2025=105.
    results.set(
      "grandRow",
      exec([
        { year: "2024", Revenue__avg__0: 90 },
        { year: "2025", Revenue__avg__0: 105 },
      ]),
    );

    const totals = assembleServerTotals(pivot, m, results);
    expect(totals.grandGrand).toBe(97.5);
    // rowKeys are alpha-sorted: ["APAC"], ["EMEA"].
    expect(totals.rowSubtotalGrand.get("APAC")).toBe(85);
    expect(totals.rowSubtotalGrand.get("EMEA")).toBe(110);
    // colKeys: ["2024"], ["2025"].
    expect(totals.grandRow).toEqual([90, 105]);
  });

  it("a grain with no server row leaves the slot null, never NOT_ADDITIVE", () => {
    const results = new Map<GrainSpec["id"], ExecuteResponse>();
    // Only grandGrand supplied; row/col subtotal slots have no data.
    results.set("grandGrand", exec([{ Revenue__avg__0: 97.5 }]));
    const totals = assembleServerTotals(pivot, m, results);
    expect(totals.grandGrand).toBe(97.5);
    expect(totals.rowSubtotalGrand.get("APAC")).toBeNull();
    expect(totals.grandRow.every((v) => v === null)).toBe(true);
  });
});
