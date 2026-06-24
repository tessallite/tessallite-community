import { describe, expect, it } from "vitest";
import {
  extractMeasureReferences,
  rewriteMeasureReferences,
} from "./expressionBuilder";

describe("extractMeasureReferences", () => {
  it("extracts a single measure reference", () => {
    expect(extractMeasureReferences('measure("Revenue")')).toEqual(["Revenue"]);
  });

  it("extracts multiple unique references", () => {
    const expr = 'safe_div(measure("Gross Profit"), measure("Revenue"))';
    expect(extractMeasureReferences(expr)).toEqual(["Gross Profit", "Revenue"]);
  });

  it("deduplicates repeated references", () => {
    const expr = 'measure("Revenue") + measure("Revenue")';
    expect(extractMeasureReferences(expr)).toEqual(["Revenue"]);
  });

  it("returns empty array for expressions without measure()", () => {
    expect(extractMeasureReferences("literal(42)")).toEqual([]);
    expect(extractMeasureReferences("")).toEqual([]);
  });

  it("handles nested function calls", () => {
    const expr = 'pct_change(measure("Sales"), "month")';
    expect(extractMeasureReferences(expr)).toEqual(["Sales"]);
  });

  it("handles measure names with spaces and hyphens", () => {
    const expr = 'measure("Revenue-Net Amount")';
    expect(extractMeasureReferences(expr)).toEqual(["Revenue-Net Amount"]);
  });

  it("handles whitespace around the measure name", () => {
    const expr = 'measure(  "Cost"  )';
    expect(extractMeasureReferences(expr)).toEqual(["Cost"]);
  });
});

describe("rewriteMeasureReferences", () => {
  it("rewrites a single reference", () => {
    const result = rewriteMeasureReferences(
      'measure("Revenue")',
      { Revenue: "Total_Sales" },
    );
    expect(result).toBe('measure("Total_Sales")');
  });

  it("rewrites multiple references", () => {
    const result = rewriteMeasureReferences(
      'safe_div(measure("Gross Profit"), measure("Revenue"))',
      { "Gross Profit": "GP_Amount", Revenue: "Net_Revenue" },
    );
    expect(result).toBe('safe_div(measure("GP_Amount"), measure("Net_Revenue"))');
  });

  it("leaves unmapped references unchanged", () => {
    const result = rewriteMeasureReferences(
      'safe_div(measure("A"), measure("B"))',
      { A: "Mapped_A" },
    );
    expect(result).toBe('safe_div(measure("Mapped_A"), measure("B"))');
  });

  it("does nothing with empty mapping", () => {
    const expr = 'measure("Revenue")';
    expect(rewriteMeasureReferences(expr, {})).toBe(expr);
  });

  it("handles names with special regex characters", () => {
    const result = rewriteMeasureReferences(
      'measure("Cost (USD)")',
      { "Cost (USD)": "Expenses" },
    );
    expect(result).toBe('measure("Expenses")');
  });
});
