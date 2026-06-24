import { describe, expect, it } from "vitest";
import type {
  Dimension,
  FieldCompatibilityIssue,
  FieldCompatibilityResponse,
} from "../../api/types";
import { summarizeMeasureCompatibility } from "./measureCompatibility";

function dimension(
  id: string,
  displayName: string,
  overrides: Partial<Dimension> = {},
): Dimension {
  return {
    id,
    name: id,
    display_name: displayName,
    source_column_id: null,
    source_column_name: id,
    source_table_id: null,
    source_table_alias: null,
    source_table_display_name: null,
    user_defined_attribute_id: null,
    user_defined_attribute_name: null,
    is_time_dim: false,
    time_grain: null,
    redundant_partner: null,
    hierarchy: null,
    ...overrides,
  };
}

function issue(
  measureId: string,
  dimensionId: string,
  code: FieldCompatibilityIssue["code"] = "NO_JOIN_PATH",
): FieldCompatibilityIssue {
  return {
    code,
    message: `${dimensionId} is incompatible`,
    measure_id: measureId,
    dimension_id: dimensionId,
    compatible_dimension_ids: [],
    compatible_dimension_names: [],
  };
}

function matrix(
  measureId: string,
  compatibleDimensionIds: string[],
  incompatibleDimensions: FieldCompatibilityResponse["measures"][string]["incompatible_dimensions"] = {},
): FieldCompatibilityResponse {
  return {
    model_id: "model",
    version_id: "version",
    generated_at: "2026-06-14T00:00:00Z",
    status: compatibleDimensionIds.length > 0 ? "compatible" : "incompatible",
    measures: {
      [measureId]: {
        name: measureId,
        compatible_dimension_ids: compatibleDimensionIds,
        incompatible_dimensions: incompatibleDimensions,
      },
    },
    multi_measure: null,
  };
}

const dimensions = [
  dimension("school", "School"),
  dimension("product", "Product"),
  dimension("secret", "Secret Dimension"),
  dimension("hidden", "Hidden Dimension", { is_hidden: true }),
];

describe("summarizeMeasureCompatibility", () => {
  it("returns compatible dimensions for an unrestricted measure", () => {
    const result = summarizeMeasureCompatibility({
      measureId: "revenue",
      matrix: matrix("revenue", ["school", "product"]),
      dimensions,
      loading: false,
      unavailable: false,
    });

    expect(result).toEqual({
      state: "ready",
      compatibleDimensionNames: ["School", "Product"],
      limitation: "none",
    });
  });

  it("shows only dimensions permitted by the scoped backend result", () => {
    const result = summarizeMeasureCompatibility({
      measureId: "revenue",
      matrix: matrix("revenue", ["school", "hidden"], {
        secret: issue("revenue", "secret", "PERSONA_FIELD_UNAVAILABLE"),
      }),
      dimensions,
      loading: false,
      unavailable: false,
    });

    expect(result.compatibleDimensionNames).toEqual(["School"]);
    expect(result.compatibleDimensionNames).not.toContain("Secret Dimension");
    expect(result.compatibleDimensionNames).not.toContain("Hidden Dimension");
  });

  it("uses the access-policy aggregate-only limitation when persona scope removes every dimension", () => {
    const result = summarizeMeasureCompatibility({
      measureId: "salary",
      matrix: matrix("salary", [], {
        school: issue("salary", "school", "PERSONA_FIELD_UNAVAILABLE"),
      }),
      dimensions,
      loading: false,
      unavailable: false,
    });

    expect(result).toEqual({
      state: "ready",
      compatibleDimensionNames: [],
      limitation: "accessPolicyAggregateOnly",
    });
  });

  it("returns a no-compatible summary when no dimensions are compatible for structural reasons", () => {
    const result = summarizeMeasureCompatibility({
      measureId: "average_age",
      matrix: matrix("average_age", [], {
        product: issue("average_age", "product", "NO_JOIN_PATH"),
      }),
      dimensions,
      loading: false,
      unavailable: false,
    });

    expect(result).toEqual({
      state: "ready",
      compatibleDimensionNames: [],
      limitation: "none",
    });
  });

  it("surfaces loading and unavailable states without producing dimension names", () => {
    expect(
      summarizeMeasureCompatibility({
        measureId: "revenue",
        matrix: undefined,
        dimensions,
        loading: true,
        unavailable: false,
      }),
    ).toEqual({
      state: "loading",
      compatibleDimensionNames: [],
      limitation: "none",
    });

    expect(
      summarizeMeasureCompatibility({
        measureId: "revenue",
        matrix: undefined,
        dimensions,
        loading: false,
        unavailable: true,
      }),
    ).toEqual({
      state: "unavailable",
      compatibleDimensionNames: [],
      limitation: "none",
    });
  });

  it("recomputes from the current compatibility matrix after a persona switch", () => {
    const unrestricted = summarizeMeasureCompatibility({
      measureId: "revenue",
      matrix: matrix("revenue", ["school", "product"]),
      dimensions,
      loading: false,
      unavailable: false,
    });
    const personaScoped = summarizeMeasureCompatibility({
      measureId: "revenue",
      matrix: matrix("revenue", ["school"], {
        product: issue("revenue", "product", "PERSONA_FIELD_UNAVAILABLE"),
      }),
      dimensions,
      loading: false,
      unavailable: false,
    });

    expect(unrestricted.compatibleDimensionNames).toEqual(["School", "Product"]);
    expect(personaScoped.compatibleDimensionNames).toEqual(["School"]);
  });
});
