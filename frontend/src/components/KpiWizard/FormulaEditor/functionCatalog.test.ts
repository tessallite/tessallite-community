import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, it, expect } from "vitest";
import {
  DSL_FUNCTIONS,
  DSL_FUNCTION_NAMES,
  CATEGORY_ORDER,
  CATEGORY_LABELS,
  type DslFunctionCategory,
} from "./functionCatalog";

// Bug-7243 R1 finding 3: parse the ACTUAL backend registry so drift on EITHER
// side goes red. A hardcoded snapshot only catches frontend regressions; reading
// the _reg("name", ...) calls from shared/semantic/kpi_expression.py makes a
// backend add/rename/removal fail this frontend test too (a real two-sided
// producer/consumer guard).
function readBackendRegisteredFunctions(): string[] {
  const here = dirname(fileURLToPath(import.meta.url));
  const registryPath = resolve(
    here,
    "../../../../../shared/semantic/kpi_expression.py",
  );
  const src = readFileSync(registryPath, "utf8");
  const names: string[] = [];
  const re = /_reg\(\s*"([a-z_]+)"/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src)) !== null) names.push(m[1]);
  return names;
}

// Bug-7243: the authoritative source of DSL functions is the backend
// FUNCTION_REGISTRY in shared/semantic/kpi_expression.py (the _reg(...) calls).
// The advanced picker only shows what this catalog lists, so any drift hides
// registered functions from users. This list is the full 31-function registry;
// keep it in lockstep with the backend _reg() calls.
const BACKEND_REGISTERED_FUNCTIONS = [
  // reference
  "measure", "kpi", "literal", "dimension",
  // safe_division
  "safe_div", "safe_ratio", "div",
  // conditional
  "coalesce", "if_then_else", "sla_condition",
  // arithmetic
  "abs", "round", "min_of", "max_of",
  // aggregation
  "sum", "avg", "min", "max", "count", "count_distinct",
  // analytics
  "share_of_total", "rank_over",
  // time intelligence
  "prior_period", "period_to_date", "moving_avg", "trailing_sum",
  "lag", "lead", "cagr", "pct_change", "fiscal_period_to_date",
];

describe("functionCatalog", () => {
  it("defines all 31 DSL functions", () => {
    expect(DSL_FUNCTIONS).toHaveLength(31);
  });

  it("exposes every backend-registered function (Bug-7243 producer/consumer parity)", () => {
    const catalogNames = new Set(DSL_FUNCTION_NAMES);
    const missing = BACKEND_REGISTERED_FUNCTIONS.filter((n) => !catalogNames.has(n));
    expect(missing).toEqual([]);
    // And no phantom functions the backend does not register.
    const backendSet = new Set(BACKEND_REGISTERED_FUNCTIONS);
    const extra = DSL_FUNCTION_NAMES.filter((n) => !backendSet.has(n));
    expect(extra).toEqual([]);
  });

  it("stays in exact lockstep with the LIVE backend _reg() registry (Bug-7243, two-sided)", () => {
    const backendNames = readBackendRegisteredFunctions();
    // Sanity: the parse found the registry (guards against a moved/renamed file
    // silently turning this into a no-op).
    expect(backendNames.length).toBeGreaterThanOrEqual(31);
    const backendSet = new Set(backendNames);
    const catalogSet = new Set(DSL_FUNCTION_NAMES);
    const missingFromCatalog = [...backendSet].filter((n) => !catalogSet.has(n));
    const extraInCatalog = [...catalogSet].filter((n) => !backendSet.has(n));
    expect(missingFromCatalog).toEqual([]);
    expect(extraInCatalog).toEqual([]);
    // The hardcoded snapshot must also match the live registry.
    expect([...backendSet].sort()).toEqual([...new Set(BACKEND_REGISTERED_FUNCTIONS)].sort());
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

  it("covers all 9 categories", () => {
    const usedCategories = new Set(DSL_FUNCTIONS.map((f) => f.category));
    for (const cat of CATEGORY_ORDER) {
      expect(usedCategories.has(cat)).toBe(true);
    }
  });

  it("CATEGORY_ORDER has exactly 9 entries", () => {
    expect(CATEGORY_ORDER).toHaveLength(9);
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
    // Bug-7243 additions:
    expect(names).toContain("dimension");
    expect(names).toContain("sla_condition");
    expect(names).toContain("sum");
    expect(names).toContain("count_distinct");
    expect(names).toContain("share_of_total");
    expect(names).toContain("rank_over");
  });
});
