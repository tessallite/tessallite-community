import { describe, it, expect } from "vitest";
import type { DrillThroughResponse } from "../../../../api/types";
import { drillCurrentPageCsvFilename, drillRowsToCsv } from "./drillCsv";

function makeResult(
  overrides: Partial<DrillThroughResponse> = {},
): DrillThroughResponse {
  return {
    columns: ["region", "amount"],
    rows: [
      { region: "US", amount: 100 },
      { region: "EU", amount: 200 },
    ],
    page: { cursor: "c0", next_cursor: null, has_more: false },
    drill_mode: "leaf",
    drill_dimension: null,
    hierarchy_path: [],
    drillable_hierarchies: [],
    route_type: "source",
    execution_ms: 10,
    bytes_processed: 0,
    rows_returned: 2,
    ...overrides,
  };
}

describe("drillRowsToCsv", () => {
  it("exports all visible columns", () => {
    const csv = drillRowsToCsv(makeResult(), ["region", "amount"]);
    const lines = csv.split("\r\n");
    expect(lines[0]).toBe("region,amount");
    expect(lines[1]).toBe("US,100");
    expect(lines[2]).toBe("EU,200");
  });

  it("labels drill downloads as current-page exports", () => {
    expect(drillCurrentPageCsvFilename("Gross Margin %")).toBe(
      "Gross_Margin_-drill-current-page.csv",
    );
  });

  it("filters to only visible columns", () => {
    const csv = drillRowsToCsv(makeResult(), ["amount"]);
    const lines = csv.split("\r\n");
    expect(lines[0]).toBe("amount");
    expect(lines[1]).toBe("100");
  });

  it("handles null and undefined values", () => {
    const result = makeResult({
      rows: [{ region: null, amount: undefined }],
    });
    const csv = drillRowsToCsv(result, ["region", "amount"]);
    const lines = csv.split("\r\n");
    expect(lines[1]).toBe(",");
  });

  it("escapes values containing commas", () => {
    const result = makeResult({
      columns: ["name", "amount"],
      rows: [{ name: "Doe, John", amount: 42 }],
    });
    const csv = drillRowsToCsv(result, ["name", "amount"]);
    expect(csv).toContain('"Doe, John"');
  });

  it("escapes values containing double quotes", () => {
    const result = makeResult({
      columns: ["name", "amount"],
      rows: [{ name: 'She said "hello"', amount: 1 }],
    });
    const csv = drillRowsToCsv(result, ["name", "amount"]);
    expect(csv).toContain('"She said ""hello"""');
  });

  it("returns header only when rows are empty", () => {
    const result = makeResult({ rows: [] });
    const csv = drillRowsToCsv(result, ["region", "amount"]);
    expect(csv).toBe("region,amount\r\n");
  });

  it("preserves hierarchy drill results with drill_dimension", () => {
    const result = makeResult({
      columns: ["month", "amount"],
      rows: [
        { month: "2025-01", amount: 300 },
        { month: "2025-02", amount: 400 },
      ],
      drill_mode: "hierarchy",
      drill_dimension: {
        id: "dim-1",
        name: "month",
        display_name: "Month",
      },
      hierarchy_path: [
        { level_name: "Year", dimension_name: "year", value: 2025 },
      ],
    });
    const csv = drillRowsToCsv(result, ["month", "amount"]);
    const lines = csv.split("\r\n");
    expect(lines[0]).toBe("month,amount");
    expect(lines[1]).toBe("2025-01,300");
    expect(lines[2]).toBe("2025-02,400");
  });
});
