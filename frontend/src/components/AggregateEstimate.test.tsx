import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import AggregateEstimate from "./AggregateEstimate";
import type { Measure } from "../api/types";

// Render real English strings (not i18n keys) so the assertions read the
// user-visible note text. Supports {{var}} interpolation like the real t().
// i18n is now per-domain split files merged by ../i18n; pull the real merged
// English bundle (default export) via importOriginal so the mock keeps real text.
vi.mock("../i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../i18n")>();
  const en = actual.default as Record<string, string>;
  return {
    ...actual,
    useT: () => (key: string, vars?: Record<string, unknown>) => {
      let s = en[key] ?? key;
      if (vars) {
        for (const [k, v] of Object.entries(vars)) {
          s = s.replace(`{{${k}}}`, String(v));
        }
      }
      return s;
    },
  };
});

const measure = (name: string): Measure =>
  ({ id: name, name, is_additive: true } as unknown as Measure);

describe("AggregateEstimate quantile note (MEDIUM-1 round 4)", () => {
  it("does not promise universal exact PERCENTILE_CONT for quantiles", () => {
    render(
      <AggregateEstimate
        selectedDimensions={["country"]}
        selectedMeasures={[measure("revenue")]}
        includeQuantiles
      />,
    );
    const note = screen.getByText(/quantile columns included/i);
    // BigQuery materialises APPROX_QUANTILES and Spark uses PERCENTILE(), so the
    // note must not advertise exact PERCENTILE_CONT for every source.
    expect(note.textContent).not.toMatch(/PERCENTILE_CONT/);
    // Exactness is conditional on the source, not universal.
    expect(note.textContent).toMatch(/where the source supports it/i);
  });

  it("omits the quantile note when quantiles are disabled", () => {
    render(
      <AggregateEstimate
        selectedDimensions={["country"]}
        selectedMeasures={[measure("revenue")]}
        includeQuantiles={false}
      />,
    );
    expect(screen.queryByText(/quantile columns included/i)).toBeNull();
  });
});
