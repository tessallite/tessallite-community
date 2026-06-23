import { describe, expect, it } from "vitest";
import type { QueryRouterFieldCompatibilityFeedback } from "../../api/types";
import {
  extractQueryFieldCompatibilityFromError,
  fieldCompatibilityCompatibleDimensionNames,
  fieldCompatibilityMessages,
  fieldCompatibilityValidationContextKey,
  hasBlockingFieldCompatibility,
  hasNotAnalyzedFieldCompatibility,
  shouldRenderFieldCompatibility,
} from "./queryFieldCompatibility";

function feedback(
  overrides: Partial<QueryRouterFieldCompatibilityFeedback> = {},
): QueryRouterFieldCompatibilityFeedback {
  return {
    status: "incompatible",
    issues: [
      {
        code: "NO_JOIN_PATH",
        severity: "error",
        measure_name: "average_student_age",
        dimension_name: "teacher_name",
        message:
          "Measure average_student_age cannot be grouped by teacher_name because they do not share an aggregation path.",
        compatible_dimension_names: ["School"],
      },
    ],
    ...overrides,
  };
}

describe("query field compatibility helpers", () => {
  it("extracts structured compatibility feedback from HTTP error detail", () => {
    const structured = feedback();
    const err = {
      response: {
        data: {
          detail: {
            error_type: "field_compatibility",
            message: structured.issues[0].message,
            field_compatibility: structured,
          },
        },
      },
    };

    expect(extractQueryFieldCompatibilityFromError(err)).toEqual(structured);
  });

  it("ignores malformed compatibility payloads so raw error handling can fall back", () => {
    const err = {
      response: {
        data: {
          detail: {
            error_type: "field_compatibility",
            field_compatibility: {
              status: "incompatible",
              issues: [{ code: "NO_JOIN_PATH" }],
            },
          },
        },
      },
    };

    expect(extractQueryFieldCompatibilityFromError(err)).toBeNull();
  });

  it("identifies not-analyzed responses by status or semantic code", () => {
    expect(
      hasNotAnalyzedFieldCompatibility(
        feedback({
          status: "not_analyzed",
          issues: [
            {
              code: "SEMANTIC_COMPATIBILITY_NOT_ANALYZED",
              severity: "warning",
              message:
                "This query uses advanced SQL that cannot be verified for field compatibility before execution.",
            },
          ],
        }),
      ),
    ).toBe(true);

    expect(
      hasNotAnalyzedFieldCompatibility(
        feedback({
          status: "compatible",
          issues: [
            {
              code: "SEMANTIC_COMPATIBILITY_NOT_ANALYZED",
              severity: "warning",
              message: "not analyzed",
            },
          ],
        }),
      ),
    ).toBe(true);
  });

  it("keeps exact backend measure-dimension messages", () => {
    const exactMessage =
      "Measure Sales cannot be grouped by Teacher because they do not share an aggregation path.";

    expect(
      fieldCompatibilityMessages(
        feedback({
          issues: [
            {
              code: "NO_JOIN_PATH",
              severity: "error",
              measure_name: "Sales",
              dimension_name: "Teacher",
              message: exactMessage,
              compatible_dimension_names: [],
            },
          ],
        }),
      ),
    ).toEqual([exactMessage]);
  });

  it("dedupes backend-provided compatible dimension names without deriving new names", () => {
    expect(
      fieldCompatibilityCompatibleDimensionNames(
        feedback({
          issues: [
            {
              code: "NO_JOIN_PATH",
              severity: "error",
              message: "first",
              compatible_dimension_names: ["School", "Region"],
            },
            {
              code: "AGGREGATE_GRAIN_MISMATCH",
              severity: "error",
              message: "second",
              compatible_dimension_names: ["Region", "Calendar"],
            },
          ],
        }),
      ),
    ).toEqual(["School", "Region", "Calendar"]);
  });

  it("does not render empty compatible feedback", () => {
    expect(
      shouldRenderFieldCompatibility(
        feedback({
          status: "compatible",
          issues: [],
        }),
      ),
    ).toBe(false);
  });

  it("blocks execution only for verified incompatible feedback", () => {
    expect(hasBlockingFieldCompatibility(feedback())).toBe(true);
    expect(
      hasBlockingFieldCompatibility(
        feedback({
          status: "not_analyzed",
          issues: [
            {
              code: "SEMANTIC_COMPATIBILITY_NOT_ANALYZED",
              severity: "warning",
              message: "not analyzed",
            },
          ],
        }),
      ),
    ).toBe(false);
    expect(
      hasBlockingFieldCompatibility(
        feedback({
          status: "compatible",
          issues: [],
        }),
      ),
    ).toBe(false);
  });

  it("keys validation blocking by the exact query context", () => {
    const original = fieldCompatibilityValidationContextKey({
      sql: "SELECT revenue, teacher_name FROM model",
      dialect: "postgresql",
      personaId: "persona-a",
      forceRoute: "aggregate",
    });

    expect(
      fieldCompatibilityValidationContextKey({
        sql: "SELECT revenue, teacher_name FROM model",
        dialect: "postgresql",
        personaId: "persona-a",
        forceRoute: "aggregate",
      }),
    ).toBe(original);
    expect(
      fieldCompatibilityValidationContextKey({
        sql: "SELECT revenue FROM model",
        dialect: "postgresql",
        personaId: "persona-a",
        forceRoute: "aggregate",
      }),
    ).not.toBe(original);
    expect(
      fieldCompatibilityValidationContextKey({
        sql: "SELECT revenue, teacher_name FROM model",
        dialect: "bigquery",
        personaId: "persona-a",
        forceRoute: "aggregate",
      }),
    ).not.toBe(original);
    expect(
      fieldCompatibilityValidationContextKey({
        sql: "SELECT revenue, teacher_name FROM model",
        dialect: "postgresql",
        personaId: "persona-b",
        forceRoute: "aggregate",
      }),
    ).not.toBe(original);
    expect(
      fieldCompatibilityValidationContextKey({
        sql: "SELECT revenue, teacher_name FROM model",
        dialect: "postgresql",
        personaId: "persona-a",
        forceRoute: "source",
      }),
    ).not.toBe(original);
  });
});
