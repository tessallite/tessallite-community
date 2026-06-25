import { describe, it, expect } from "vitest";
import {
  DSL_FUNCTIONS,
  DSL_FUNCTION_NAMES,
  CATEGORY_ORDER,
  CATEGORY_LABELS,
  type DslFunctionCategory,
} from "./functionCatalog";

describe("functionCatalog", () => {
  it("defines all 21 DSL functions", () => {
    expect(DSL_FUNCTIONS).toHaveLength(21);
  });

  it("every function has required fields", () => {
    for (const fn of DSL_FUNCTIONS) {
      expect(fn.name).toBeTruthy();
      expect(fn.category).toBeTruthy();
      expect(fn.signature).toBeTruthy();
      expect(fn.descriptionKey).toBeTruthy();
      expect(fn.descriptionFallback).toBeTruthy();
      expect(fn.example).toBeTruthy();
      expect(fn.insertSnippet).toBeTruthy();
      expect(fn.parameters.length).toBeGreaterThan(0);
    }
  });

  it("has no duplicate function names", () => {
    const names = DSL_FUNCTIONS.map((f) => f.name);
    expect(new Set(names).size).toBe(names.length);
  });

  it("covers all 7 categories", () => {
    const usedCategories = new Set(DSL_FUNCTIONS.map((f) => f.category));
    for (const cat of CATEGORY_ORDER) {
      expect(usedCategories.has(cat)).toBe(true);
    }
  });

  it("CATEGORY_ORDER has exactly 7 entries", () => {
    expect(CATEGORY_ORDER).toHaveLength(7);
  });

  it("every category has a label entry", () => {
    for (const cat of CATEGORY_ORDER) {
      const label = CATEGORY_LABELS[cat];
      expect(label).toBeDefined();
      expect(label.key).toBeTruthy();
      expect(label.fallback).toBeTruthy();
    }
  });

  it("DSL_FUNCTION_NAMES matches DSL_FUNCTIONS", () => {
    expect(DSL_FUNCTION_NAMES).toEqual(DSL_FUNCTIONS.map((f) => f.name));
  });

  it("insert snippets contain placeholder markers", () => {
    for (const fn of DSL_FUNCTIONS) {
      expect(fn.insertSnippet).toContain("$");
    }
  });

  it("all categories used in functions are in CATEGORY_ORDER", () => {
    for (const fn of DSL_FUNCTIONS) {
      expect(CATEGORY_ORDER).toContain(fn.category);
    }
  });

  it("includes specific expected functions", () => {
    const names = DSL_FUNCTION_NAMES;
    expect(names).toContain("measure");
    expect(names).toContain("kpi");
    expect(names).toContain("safe_div");
    expect(names).toContain("prior_period");
    expect(names).toContain("pct_change");
    expect(names).toContain("moving_avg");
    expect(names).toContain("cagr");
  });
});
