import { describe, it, expect } from "vitest";
import {
  classifyJoinEndpoints,
  isDimTableType,
  isOuterJoinType,
  sameTypeWarningText,
} from "./joinRules";

// F-026-05: the API stores short join-type names (inner|left|right|full); none
// contain "outer", so the old includes("outer") test was always false and outer
// joins never rendered dashed. isOuterJoinType must treat left/right/full as
// outer and inner (and unknown/empty) as not.
describe("isOuterJoinType (F-026-05)", () => {
  it("treats left / right / full as outer (dashed)", () => {
    expect(isOuterJoinType("left")).toBe(true);
    expect(isOuterJoinType("right")).toBe(true);
    expect(isOuterJoinType("full")).toBe(true);
  });

  it("treats inner as not outer (solid)", () => {
    expect(isOuterJoinType("inner")).toBe(false);
  });

  it("is case- and whitespace-insensitive", () => {
    expect(isOuterJoinType("  LEFT ")).toBe(true);
    expect(isOuterJoinType("Full")).toBe(true);
  });

  it("treats null / undefined / unknown as not outer", () => {
    expect(isOuterJoinType(null)).toBe(false);
    expect(isOuterJoinType(undefined)).toBe(false);
    expect(isOuterJoinType("")).toBe(false);
    expect(isOuterJoinType("cross")).toBe(false);
  });
});

// Bug-7635: isDimTableType must recognise all dim subtypes the API produces
// (dim_detail, dim_aggregate) and reject non-dim types.
describe("isDimTableType (Bug-7635)", () => {
  it("recognises dim_detail and dim_aggregate as dimension types", () => {
    expect(isDimTableType("dim_detail")).toBe(true);
    expect(isDimTableType("dim_aggregate")).toBe(true);
  });

  it("rejects fact, unclassified, calendar, and empty", () => {
    expect(isDimTableType("fact")).toBe(false);
    expect(isDimTableType("unclassified")).toBe(false);
    expect(isDimTableType("calendar")).toBe(false);
    expect(isDimTableType("")).toBe(false);
    expect(isDimTableType(null)).toBe(false);
    expect(isDimTableType(undefined)).toBe(false);
  });
});

// F-026-12 / Bug-7635: the same-type warning must use real API domain values
// (dim_detail, dim_aggregate), not the dead "dimension" literal. Translated
// labels must be keyed through the translation function.
describe("same-type join warning with real domain values (F-026-12, Bug-7635)", () => {
  const t = (key: string, vars?: Record<string, string>) => {
    const table: Record<string, string> = {
      "joins.factToFact": "FACT_TO_FACT_XX",
      "joins.dimensionToDimension": "DIM_TO_DIM_XX",
      "joins.unusualJoinWarning": "WARN_XX: {{label}}",
    };
    let out = table[key] ?? key;
    if (vars) for (const [k, v] of Object.entries(vars)) out = out.replace(`{{${k}}}`, v);
    return out;
  };

  it("dim_detail <-> dim_detail is same-type with translated label", () => {
    const check = classifyJoinEndpoints("dim_detail", "dim_detail", t);
    expect(check.isSameType).toBe(true);
    expect(check.sameTypeLabel).toBe("DIM_TO_DIM_XX");
  });

  it("dim_detail <-> dim_aggregate is same-type (cross-dim-subtype)", () => {
    const check = classifyJoinEndpoints("dim_detail", "dim_aggregate", t);
    expect(check.isSameType).toBe(true);
    expect(check.sameTypeLabel).toBe("DIM_TO_DIM_XX");
  });

  it("uses the translated fact-to-fact label", () => {
    const check = classifyJoinEndpoints("fact", "fact", t);
    expect(check.sameTypeLabel).toBe("FACT_TO_FACT_XX");
  });

  it("fact <-> dim_detail is not a same-type warning", () => {
    const check = classifyJoinEndpoints("fact", "dim_detail", t);
    expect(check.isSameType).toBe(false);
    expect(check.isFactDim).toBe(true);
    expect(check.sameTypeLabel).toBeNull();
  });

  it("warning text interpolates the label through the keyed template", () => {
    expect(sameTypeWarningText("DIM_TO_DIM_XX", t)).toBe("WARN_XX: DIM_TO_DIM_XX");
  });
});
