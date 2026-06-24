import { describe, it, expect } from "vitest";
import {
  RATIO_VARIANT_DEFAULT_FORMAT,
  RATIO_VARIANT_KINDS,
  isParametricVariant,
  isRatioVariant,
} from "./timeVariants";

describe("isRatioVariant (F-015-24)", () => {
  it("flags percentage/ratio-producing variant kinds", () => {
    expect(isRatioVariant("yoy_growth_pct")).toBe(true);
    expect(isRatioVariant("cagr")).toBe(true);
    expect(isRatioVariant("pct_change")).toBe(true);
  });

  it("does not flag absolute-delta or period variants", () => {
    // yoy_growth is an absolute delta in the base unit, not a ratio.
    expect(isRatioVariant("yoy_growth")).toBe(false);
    expect(isRatioVariant("ytd")).toBe(false);
    expect(isRatioVariant("prior_year")).toBe(false);
    expect(isRatioVariant("lag")).toBe(false);
  });

  it("defaults ratio variants to the percent format token", () => {
    expect(RATIO_VARIANT_DEFAULT_FORMAT).toBe("percent");
  });

  it("ratio set and parametric set are disjoint", () => {
    for (const kind of RATIO_VARIANT_KINDS) {
      expect(isParametricVariant(kind)).toBe(false);
    }
  });
});
