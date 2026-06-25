import { describe, it, expect } from "vitest";
import {
  classifyJoinEndpoints,
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

// F-026-12: the same-type warning was hardcoded English and built untranslatable
// "dimension-to-dimension" labels. classifyJoinEndpoints must produce translated
// labels when a `t` is supplied, and sameTypeWarningText must use the keyed
// template.
describe("same-type join warning is translatable (F-026-12)", () => {
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

  it("uses the translated dimension-to-dimension label", () => {
    const check = classifyJoinEndpoints("dimension", "dimension", t);
    expect(check.isSameType).toBe(true);
    expect(check.sameTypeLabel).toBe("DIM_TO_DIM_XX");
  });

  it("uses the translated fact-to-fact label", () => {
    const check = classifyJoinEndpoints("fact", "fact", t);
    expect(check.sameTypeLabel).toBe("FACT_TO_FACT_XX");
  });

  it("fact <-> dimension is not a same-type warning", () => {
    const check = classifyJoinEndpoints("fact", "dimension", t);
    expect(check.isSameType).toBe(false);
    expect(check.isFactDim).toBe(true);
    expect(check.sameTypeLabel).toBeNull();
  });

  it("warning text interpolates the label through the keyed template", () => {
    expect(sameTypeWarningText("DIM_TO_DIM_XX", t)).toBe("WARN_XX: DIM_TO_DIM_XX");
  });
});
