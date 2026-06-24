import { describe, it, expect } from "vitest";
import { fitFontSize } from "./fitText";

describe("fitFontSize (Bug-5345 proportional digits)", () => {
  it("keeps the base size for short values within the budget", () => {
    expect(fitFontSize("82", 2.6, 5, 1.3)).toBe(2.6);
    expect(fitFontSize("100%", 2.6, 5, 1.3)).toBe(2.6);
  });

  it("shrinks long values past the budget", () => {
    const big = fitFontSize("1,234,567", 2.6, 5, 1.3);
    expect(big).toBeLessThan(2.6);
    expect(big).toBeGreaterThanOrEqual(1.3);
  });

  it("never shrinks below the floor", () => {
    expect(fitFontSize("$12,345,678,901.55", 2.6, 5, 1.3)).toBe(1.3);
  });

  it("falls back to base for null/empty text", () => {
    expect(fitFontSize(null, 2.1)).toBe(2.1);
    expect(fitFontSize("", 2.1)).toBe(2.1);
  });

  it("shrinks monotonically as length grows", () => {
    const a = fitFontSize("123456", 2.6, 5, 1.0);
    const b = fitFontSize("1234567", 2.6, 5, 1.0);
    const c = fitFontSize("12345678", 2.6, 5, 1.0);
    expect(a).toBeGreaterThan(b);
    expect(b).toBeGreaterThan(c);
  });
});
