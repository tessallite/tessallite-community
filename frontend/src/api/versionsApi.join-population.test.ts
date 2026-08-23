import { describe, expect, it } from "vitest";
import { parseJoinPopulationBlockedError } from "./versionsApi";

const blockedError = {
  response: {
    data: {
      detail: {
        code: "JOIN_POPULATION_BLOCKED",
        message: "deployment refused",
        threshold: 0.15,
        joins: [
          {
            join_id: "join-1",
            join_label: "Fact.customer_id ↔ Customer.id",
            left_table_name: "Fact",
            right_table_name: "Customer",
            left_column_name: "customer_id",
            right_column_name: "id",
            population_participation: "undeclared",
            status: "BLOCKED",
            row_effect_ratio: 0.2,
            reason: "measured row effect exceeds threshold",
          },
          {
            join_id: "join-2",
            join_label: "Fact.region_id ↔ Region.id",
            left_table_name: null,
            right_table_name: null,
            left_column_name: null,
            right_column_name: null,
            population_participation: "enrichment_only",
            status: "BLOCKED",
            row_effect_ratio: 0.18,
            reason: "filtering enrichment effect exceeds threshold",
          },
        ],
      },
    },
  },
};

describe("parseJoinPopulationBlockedError", () => {
  it("preserves the complete typed refusal, including both actionable offenders", () => {
    expect(parseJoinPopulationBlockedError(blockedError)).toEqual(
      blockedError.response.data.detail,
    );
  });

  it("fails closed for a generic or partial error", () => {
    expect(parseJoinPopulationBlockedError({ response: { data: { detail: "nope" } } })).toBeNull();
    expect(
      parseJoinPopulationBlockedError({
        response: {
          data: {
            detail: {
              code: "JOIN_POPULATION_BLOCKED",
              message: "deployment refused",
              threshold: 0.15,
              joins: [{ join_id: "join-1" }],
            },
          },
        },
      }),
    ).toBeNull();
  });
});

export { blockedError };
