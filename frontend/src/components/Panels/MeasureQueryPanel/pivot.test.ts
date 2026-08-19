import { describe, it, expect } from "vitest";
import type { Dimension, ExecuteResponse, FieldCompatibilityResponse, Measure } from "../../../api/types";
import { cellLookupKey, computePivot, evaluatePivotCompatibility } from "./pivot";
import { NOT_ADDITIVE, computeTotals } from "./totals";

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

function compatibilityMatrix(
  measures: FieldCompatibilityResponse["measures"],
  multi_measure: FieldCompatibilityResponse["multi_measure"] = null,
): FieldCompatibilityResponse {
  return {
    model_id: "model",
    version_id: "version",
    generated_at: "2026-06-14T00:00:00Z",
    status: "incompatible",
    measures,
    multi_measure,
  };
}

function incompatibleIssue(
  measureId: string,
  dimensionId: string,
  message: string,
  compatibleNames: string[] = ["School"],
  code: "NO_JOIN_PATH" | "PERSONA_FIELD_UNAVAILABLE" | "AMBIGUOUS_JOIN_PATH" = "NO_JOIN_PATH",
  severity: "error" | "warning" = "error",
) {
  return {
    code,
    severity,
    message,
    measure_id: measureId,
    dimension_id: dimensionId,
    compatible_dimension_ids: compatibleNames.map((name) => name.toLowerCase().replace(/\s+/g, "_")),
    compatible_dimension_names: compatibleNames,
  };
}

const schoolDim = dim("school");
const teacherDim = dim("teacher");
const productDim = dim("product");
const secretDim = { ...dim("secret-id"), name: "secret_dimension", display_name: "Secret Dimension" };

describe("computePivot", () => {
  it("indexes rows by row-tuple × col-tuple", () => {
    const m = measure("Revenue");
    const rowDims = [dim("region")];
    const colDims = [dim("year")];
    const response = exec([
      { region: "EMEA", year: "2024", Revenue: 100 },
      { region: "EMEA", year: "2025", Revenue: 120 },
      { region: "APAC", year: "2024", Revenue: 80 },
    ]);

    const pivot = computePivot(response, m, rowDims, colDims);

    expect(pivot.rowKeys).toEqual([["APAC"], ["EMEA"]]);
    expect(pivot.colKeys).toEqual([["2024"], ["2025"]]);
    expect(pivot.byKey.get(cellLookupKey(["EMEA"], ["2025"]))?.measureValue).toBe(120);
    expect(pivot.byKey.get(cellLookupKey(["APAC"], ["2025"]))).toBeUndefined();
  });

  it("handles zero row-dims (single implicit row)", () => {
    const m = measure("Revenue");
    const response = exec([{ year: "2024", Revenue: 42 }, { year: "2025", Revenue: 58 }]);
    const pivot = computePivot(response, m, [], [dim("year")]);
    expect(pivot.rowKeys).toEqual([[]]);
    expect(pivot.colKeys).toEqual([["2024"], ["2025"]]);
    expect(pivot.byKey.get(cellLookupKey([], ["2024"]))?.measureValue).toBe(42);
  });

  // F-019-07: distinct multi-level tuples that concatenate ambiguously must
  // NOT collide. ["ab","c"] and ["a","bc"] both joined to "abc" under the old
  // ``join("")`` key, dropping a cell. With a collision-free key they stay
  // separate and both cells survive.
  it("F-019-07: ambiguous multi-level tuples do not collide", () => {
    const m = measure("Revenue");
    const rowDims = [dim("a"), dim("b")];
    const response = exec([
      { a: "ab", b: "c", Revenue: 10 },
      { a: "a", b: "bc", Revenue: 20 },
    ]);
    const pivot = computePivot(response, m, rowDims, []);
    expect(pivot.rowKeys).toHaveLength(2);
    expect(pivot.byKey.get(cellLookupKey(["ab", "c"], []))?.measureValue).toBe(10);
    expect(pivot.byKey.get(cellLookupKey(["a", "bc"], []))?.measureValue).toBe(20);
  });

  // F-019-13: numeric members order naturally (2 before 10), not lexically.
  it("F-019-13: numeric row members sort numerically, not lexically", () => {
    const m = measure("Revenue");
    const rowDims = [dim("month")];
    const response = exec([
      { month: 10, Revenue: 1 },
      { month: 2, Revenue: 2 },
      { month: 1, Revenue: 3 },
      { month: 11, Revenue: 4 },
    ]);
    const pivot = computePivot(response, m, rowDims, []);
    expect(pivot.rowKeys).toEqual([["1"], ["2"], ["10"], ["11"]]);
  });

  // F-019-16: null members render through the caller-supplied localized label,
  // not a hardcoded "(null)".
  it("F-019-16: null dimension members use the supplied null label", () => {
    const m = measure("Revenue");
    const rowDims = [dim("region")];
    const response = exec([
      { region: null, Revenue: 5 },
      { region: "EMEA", Revenue: 6 },
    ]);
    const pivot = computePivot(response, m, rowDims, [], [], "∅null∅");
    expect(pivot.rowKeys).toContainEqual(["∅null∅"]);
    expect(pivot.byKey.get(cellLookupKey(["∅null∅"], []))?.measureValue).toBe(5);
  });
});

describe("evaluatePivotCompatibility", () => {
  const noPathMessage =
    "There is no aggregation path between Average Student Age and Teacher.";

  it("keeps neutral behavior when no measures are selected", () => {
    const result = evaluatePivotCompatibility({
      measureIds: [],
      rowDimIds: ["teacher"],
      colDimIds: [],
      slicers: [],
      dimensions: [teacherDim],
      matrix: compatibilityMatrix({}),
    });

    expect(result.status).toBe("neutral");
    expect(result.hasVerifiedIncompatibilities).toBe(false);
    expect(result.disabledDimensionReasons).toEqual({});
  });

  it("supports measure-first picking by disabling incompatible dimensions", () => {
    const result = evaluatePivotCompatibility({
      measureIds: ["average_age"],
      rowDimIds: [],
      colDimIds: [],
      slicers: [],
      dimensions: [schoolDim, teacherDim],
      matrix: compatibilityMatrix({
        average_age: {
          name: "Average Student Age",
          compatible_dimension_ids: ["school"],
          incompatible_dimensions: {
            teacher: incompatibleIssue("average_age", "teacher", noPathMessage),
          },
        },
      }),
    });

    expect(result.status).toBe("compatible");
    expect(result.disabledDimensionReasons.teacher).toContain("aggregation path");
    expect(result.hasVerifiedIncompatibilities).toBe(false);
  });

  it("supports dimension-first saved state by surfacing selected incompatibilities", () => {
    const result = evaluatePivotCompatibility({
      measureIds: ["average_age"],
      rowDimIds: ["teacher"],
      colDimIds: [],
      slicers: [],
      dimensions: [teacherDim],
      matrix: compatibilityMatrix({
        average_age: {
          name: "Average Student Age",
          compatible_dimension_ids: ["school"],
          incompatible_dimensions: {
            teacher: incompatibleIssue("average_age", "teacher", noPathMessage),
          },
        },
      }),
    });

    expect(result.status).toBe("incompatible");
    expect(result.hasVerifiedIncompatibilities).toBe(true);
    expect(result.selectedIssues).toHaveLength(1);
    expect(result.selectedIssues[0].dimensionName).toBe("teacher");
    expect(result.actions.remove_incompatible_dimensions).toBe(true);
  });

  it("warns without blocking when a selected dimension has an ambiguous aggregation path", () => {
    const result = evaluatePivotCompatibility({
      measureIds: ["average_age"],
      rowDimIds: ["teacher"],
      colDimIds: [],
      slicers: [],
      dimensions: [teacherDim],
      matrix: compatibilityMatrix({
        average_age: {
          name: "Average Student Age",
          compatible_dimension_ids: ["school"],
          incompatible_dimensions: {
            teacher: incompatibleIssue(
              "average_age",
              "teacher",
              "Average Student Age and Teacher have more than one possible aggregation path.",
              ["School"],
              "AMBIGUOUS_JOIN_PATH",
              "warning",
            ),
          },
        },
      }),
    });

    expect(result.selectedIssues).toHaveLength(1);
    expect(result.selectedIssues[0].severity).toBe("warning");
    expect(result.hasVerifiedIncompatibilities).toBe(false);
    expect(result.disabledDimensionReasons.teacher).toBeUndefined();
    expect(result.actions.remove_incompatible_dimensions).toBe(false);
  });

  it("applies the same compatibility check to slicers", () => {
    const result = evaluatePivotCompatibility({
      measureIds: ["average_age"],
      rowDimIds: [],
      colDimIds: [],
      slicers: [{ dimensionId: "teacher", op: "eq", values: ["Smith"] }],
      dimensions: [teacherDim],
      matrix: compatibilityMatrix({
        average_age: {
          name: "Average Student Age",
          compatible_dimension_ids: ["school"],
          incompatible_dimensions: {
            teacher: incompatibleIssue("average_age", "teacher", noPathMessage),
          },
        },
      }),
    });

    expect(result.selectedIssues[0].location).toBe("slicer");
    expect(result.hasVerifiedIncompatibilities).toBe(true);
  });

  it("uses intersection compatibility for multi-measure pivots", () => {
    const result = evaluatePivotCompatibility({
      measureIds: ["revenue", "average_age"],
      rowDimIds: ["product"],
      colDimIds: ["school"],
      slicers: [],
      dimensions: [productDim, schoolDim],
      matrix: compatibilityMatrix(
        {
          revenue: {
            name: "Revenue",
            compatible_dimension_ids: ["product", "school"],
            incompatible_dimensions: {},
          },
          average_age: {
            name: "Average Student Age",
            compatible_dimension_ids: ["school"],
            incompatible_dimensions: {
              product: incompatibleIssue(
                "average_age",
                "product",
                "There is no aggregation path between Average Student Age and Product.",
                ["School"],
              ),
            },
          },
        },
        {
          selected_measure_ids: ["revenue", "average_age"],
          common_dimension_ids: ["school"],
          common_dimension_names: ["School"],
          conflicts_by_measure: [],
          suggested_actions: ["keep_common_dimensions", "split_pivot", "remove_incompatible_dimensions"],
        },
      ),
    });

    expect(result.commonDimensionIds).toEqual(["school"]);
    expect(result.commonDimensionNames).toEqual(["School"]);
    expect(result.conflictsByMeasure).toEqual([
      {
        measureId: "average_age",
        measureName: "Average Student Age",
        incompatibleDimensionIds: ["product"],
        incompatibleDimensionNames: ["product"],
        compatibleDimensionNames: ["School"],
      },
    ]);
    expect(result.actions.keep_common_dimensions).toBe(true);
    expect(result.actions.split_pivot).toBe(true);
  });

  it("does not construct unauthorized suggestions from local dimension lists", () => {
    const result = evaluatePivotCompatibility({
      measureIds: ["average_age"],
      rowDimIds: ["secret-id"],
      colDimIds: [],
      slicers: [],
      dimensions: [schoolDim, secretDim],
      matrix: compatibilityMatrix({
        average_age: {
          name: "Average Student Age",
          compatible_dimension_ids: ["school"],
          incompatible_dimensions: {
            "secret-id": incompatibleIssue(
              "average_age",
              "secret-id",
              "Average Student Age can be used with the dimensions available to your current persona: School.",
              ["School"],
              "PERSONA_FIELD_UNAVAILABLE",
            ),
          },
        },
      }),
    });

    expect(JSON.stringify(result)).toContain("School");
    expect(JSON.stringify(result)).not.toContain("Secret Dimension");
    expect(JSON.stringify(result)).not.toContain("secret_dimension");
    expect(result.selectedIssues[0].dimensionName).toBeUndefined();
  });

  it("reports no common dimensions and action availability", () => {
    const result = evaluatePivotCompatibility({
      measureIds: ["revenue", "average_age"],
      rowDimIds: [],
      colDimIds: [],
      slicers: [],
      dimensions: [schoolDim, productDim],
      matrix: compatibilityMatrix(
        {
          revenue: {
            name: "Revenue",
            compatible_dimension_ids: ["product"],
            incompatible_dimensions: {},
          },
          average_age: {
            name: "Average Student Age",
            compatible_dimension_ids: ["school"],
            incompatible_dimensions: {},
          },
        },
        {
          selected_measure_ids: ["revenue", "average_age"],
          common_dimension_ids: [],
          common_dimension_names: [],
          conflicts_by_measure: [],
          suggested_actions: ["keep_common_dimensions", "split_pivot", "remove_incompatible_dimensions"],
        },
      ),
    });

    expect(result.noCommonDimensions).toBe(true);
    expect(result.hasVerifiedIncompatibilities).toBe(false);
    expect(result.actions.keep_common_dimensions).toBe(false);
    expect(result.actions.split_pivot).toBe(true);
    expect(result.actions.remove_incompatible_dimensions).toBe(false);
  });
});

describe("computeTotals", () => {
  const m = measure("Revenue");
  const rowDims = [dim("region")];
  const colDims = [dim("year")];
  const pivot = computePivot(
    exec([
      { region: "EMEA", year: "2024", Revenue: 100 },
      { region: "EMEA", year: "2025", Revenue: 120 },
      { region: "APAC", year: "2024", Revenue: 80 },
      { region: "APAC", year: "2025", Revenue: 90 },
    ]),
    m,
    rowDims,
    colDims,
  );

  it("grand total equals sum of every cell", () => {
    const totals = computeTotals(pivot, m);
    expect(totals.grandGrand).toBe(390);
  });

  it("per-row grand equals row sum", () => {
    const totals = computeTotals(pivot, m);
    // rowKeys order is ["APAC"], ["EMEA"] (alpha sorted)
    expect(totals.grandCol).toEqual([170, 220]);
  });

  it("non-additive measure marks every total NOT_ADDITIVE", () => {
    const nonAdditive = measure("distinct_customers", { is_additive: false });
    const totals = computeTotals(pivot, nonAdditive);
    expect(totals.grandGrand).toBe(NOT_ADDITIVE);
    expect(totals.grandRow.every((v) => v === NOT_ADDITIVE)).toBe(true);
    expect(totals.grandCol.every((v) => v === NOT_ADDITIVE)).toBe(true);
  });

  it("calculated measure marks every total NOT_ADDITIVE", () => {
    const calc = measure("margin_pct", { measure_type: "calculated", is_additive: false });
    const totals = computeTotals(pivot, calc);
    expect(totals.grandGrand).toBe(NOT_ADDITIVE);
  });

  // Multi-agg: the same measure shown under several functions. The client-side
  // totals algorithm can only sum, so only SUM/COUNT compose; AVG/MIN/MAX and
  // COUNT_DISTINCT must render as em-dash (NOT_ADDITIVE) instead of a wrong sum.
  function aggMeasure(agg: string): Measure {
    return { ...measure("Revenue"), _agg: agg } as Measure;
  }

  it("SUM agg column stays additive", () => {
    const totals = computeTotals(pivot, aggMeasure("SUM"));
    expect(totals.grandGrand).toBe(390);
  });

  it("COUNT agg column stays additive", () => {
    const totals = computeTotals(pivot, aggMeasure("COUNT"));
    expect(totals.grandGrand).toBe(390);
  });

  it.each(["AVG", "MIN", "MAX", "COUNT_DISTINCT"])(
    "%s agg column marks every total NOT_ADDITIVE",
    (agg) => {
      const totals = computeTotals(pivot, aggMeasure(agg));
      expect(totals.grandGrand).toBe(NOT_ADDITIVE);
      expect(totals.grandRow.every((v) => v === NOT_ADDITIVE)).toBe(true);
      expect(totals.grandCol.every((v) => v === NOT_ADDITIVE)).toBe(true);
    },
  );
});

describe("computePivot — business display labels (Bug-6285)", () => {
  it("exposes display names alongside technical names, in order", () => {
    const region: Dimension = { ...dim("region"), display_name: "Sales Region" };
    const year: Dimension = { ...dim("year"), display_name: "Fiscal Year" };
    const rows = [{ region: "EMEA", year: "2024", revenue: 10 }];
    const pivot = computePivot(exec(rows), measure("revenue"), [region], [year]);
    // Technical names still drive result lookup.
    expect(pivot.rowCols).toEqual(["region"]);
    expect(pivot.colCols).toEqual(["year"]);
    // Row display labels are what headers/exports render. (The column axis
    // renders member values, so there is no colLabels array.)
    expect(pivot.rowLabels).toEqual(["Sales Region"]);
  });

  it("falls back to the technical name when a dimension has no display name", () => {
    const region: Dimension = { ...dim("region"), display_name: "" };
    const rows = [{ region: "EMEA", revenue: 10 }];
    const pivot = computePivot(exec(rows), measure("revenue"), [region], []);
    expect(pivot.rowLabels).toEqual(["region"]);
  });
});
