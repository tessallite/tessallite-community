import { describe, it, expect } from "vitest";
import { formatMeasureValue, MEASURE_FORMAT_TOKENS, MEASURE_FORMAT_LABELS } from "./measureFormat";

describe("formatMeasureValue", () => {
  describe("edge cases", () => {
    it("returns empty string for null", () => {
      expect(formatMeasureValue(null, "integer")).toBe("");
    });

    it("returns empty string for undefined", () => {
      expect(formatMeasureValue(undefined, "integer")).toBe("");
    });

    it("returns empty string for empty string", () => {
      expect(formatMeasureValue("", "integer")).toBe("");
    });

    it("returns string representation for non-finite numbers", () => {
      expect(formatMeasureValue(Infinity, "integer")).toBe("Infinity");
      expect(formatMeasureValue(-Infinity, "integer")).toBe("-Infinity");
      expect(formatMeasureValue(NaN, "integer")).toBe("NaN");
    });

    it("returns string value when token is null", () => {
      expect(formatMeasureValue(42, null)).toBe("42");
    });

    it("returns string value when token is undefined", () => {
      expect(formatMeasureValue(42, undefined)).toBe("42");
    });

    it("converts string values to numbers", () => {
      expect(formatMeasureValue("1234.5", "integer")).toBe("1,235");
    });

    it("returns raw string for non-numeric strings", () => {
      expect(formatMeasureValue("abc", "integer")).toBe("abc");
    });
  });

  describe("currency", () => {
    it("formats with USD currency symbol and 2dp", () => {
      expect(formatMeasureValue(1234.567, "currency")).toBe("$1,234.57");
    });

    it("formats negative values", () => {
      expect(formatMeasureValue(-500.1, "currency")).toBe("-$500.10");
    });

    it("formats zero", () => {
      expect(formatMeasureValue(0, "currency")).toBe("$0.00");
    });
  });

  // F-015-07: the `percent` token treats the backend value as a decimal
  // ratio and always multiplies by 100 (matching kpi_formatter.py). The old
  // magnitude heuristic flipped meaning at 1.0; these cases pin the ratio
  // convention including the >= 1 ratios that the heuristic destroyed.
  describe("percent", () => {
    it("scales a sub-1 ratio by 100", () => {
      expect(formatMeasureValue(0.125, "percent")).toBe("13%");
    });

    it("scales a 1.0+ ratio by 100 (boundary that the old heuristic flipped)", () => {
      expect(formatMeasureValue(1.01, "percent")).toBe("101%");
      expect(formatMeasureValue(1.5, "percent")).toBe("150%");
    });

    it("renders exactly 1.0 as 100%, not 1%", () => {
      expect(formatMeasureValue(1, "percent")).toBe("100%");
    });

    it("rounds to 0dp", () => {
      expect(formatMeasureValue(0.5678, "percent")).toBe("57%");
    });
  });

  describe("percent_2dp", () => {
    it("scales a sub-1 ratio by 100 with 2dp", () => {
      expect(formatMeasureValue(0.12345, "percent_2dp")).toBe("12.35%");
    });

    it("scales a 1.0+ ratio by 100 with 2dp", () => {
      expect(formatMeasureValue(1.2345, "percent_2dp")).toBe("123.45%");
    });
  });

  describe("integer", () => {
    it("rounds and formats with grouping", () => {
      expect(formatMeasureValue(1234567.89, "integer")).toBe("1,234,568");
    });

    it("formats zero", () => {
      expect(formatMeasureValue(0, "integer")).toBe("0");
    });
  });

  describe("decimal tokens", () => {
    it("formats decimal_0 with 0 decimal places", () => {
      expect(formatMeasureValue(1234.5, "decimal_0")).toBe("1,235");
    });

    it("formats decimal_1 with 1 decimal place", () => {
      expect(formatMeasureValue(1234.56, "decimal_1")).toBe("1,234.6");
    });

    it("formats decimal_2dp with 2 decimal places", () => {
      expect(formatMeasureValue(1234.567, "decimal_2dp")).toBe("1,234.57");
    });

    it("formats decimal_3 with 3 decimal places", () => {
      expect(formatMeasureValue(1.23456, "decimal_3")).toBe("1.235");
    });

    it("formats decimal_4 with 4 decimal places", () => {
      expect(formatMeasureValue(1.23456, "decimal_4")).toBe("1.2346");
    });

    it("formats decimal_5 with 5 decimal places", () => {
      expect(formatMeasureValue(1.234567, "decimal_5")).toBe("1.23457");
    });

    it("formats decimal_6 with 6 decimal places", () => {
      expect(formatMeasureValue(1.2345678, "decimal_6")).toBe("1.234568");
    });

    it("pads with trailing zeros", () => {
      expect(formatMeasureValue(1, "decimal_2dp")).toBe("1.00");
    });
  });

  describe("token registry", () => {
    it("every token has a label", () => {
      for (const token of MEASURE_FORMAT_TOKENS) {
        expect(MEASURE_FORMAT_LABELS[token]).toBeTruthy();
      }
    });
  });
});
