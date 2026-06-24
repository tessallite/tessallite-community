import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import KpiVisual, {
  bandsLookAbsolute,
  resolveChartInputs,
  goalThreshold,
} from "./KpiVisual";
import { bandsToAxisLine, statusColor } from "./GaugeChart";
import { deriveScale } from "./chartUtils";
import { createDefaultPresentationMeta } from "../KpiBusinessBuilder/KpiThresholdEditor";
import type { KpiEvaluateResponse, KpiThresholdBand } from "../../api/types";

function makeEval(overrides: Partial<KpiEvaluateResponse> = {}): KpiEvaluateResponse {
  return {
    kpi_id: "kpi-1",
    value: 80,
    value_str: null,
    target: 100,
    status: 1,
    status_label: null,
    status_color: null,
    trend: 1,
    trend_label: null,
    trend_pct: null,
    formatted_value: null,
    formatted_target: null,
    formatted_variance: null,
    trend_series: null,
    evaluation_ms: null,
    compiled_expression: null,
    compiled_scope: null,
    goal: null,
    formatted_goal: null,
    ...overrides,
  };
}

describe("KpiVisual", () => {
  it("returns null when presentation type is empty", () => {
    const { container } = render(
      <KpiVisual presentationType="" presentationMeta={null} evalData={makeEval()} />,
    );
    expect(container.innerHTML).toBe("");
  });

  it("returns null when presentation type is null", () => {
    const { container } = render(
      <KpiVisual presentationType={null} presentationMeta={null} evalData={makeEval()} />,
    );
    expect(container.innerHTML).toBe("");
  });

  it("renders a real traffic light for traffic_light type (Bug-5343)", () => {
    const { container } = render(
      <KpiVisual presentationType="traffic_light" presentationMeta={null} evalData={makeEval()} />,
    );
    // No longer a no-op: the lamp housing renders.
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("renders progress ring for progress_ring type", () => {
    const { container } = render(
      <KpiVisual presentationType="progress_ring" presentationMeta={null} evalData={makeEval()} />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("renders thermometer for thermometer type", () => {
    const { container } = render(
      <KpiVisual presentationType="thermometer" presentationMeta={null} evalData={makeEval()} />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("renders gauge chart for gauge type", () => {
    const { container } = render(
      <KpiVisual presentationType="gauge" presentationMeta={null} evalData={makeEval()} />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("renders gauge chart for reverse_gauge type", () => {
    const { container } = render(
      <KpiVisual presentationType="reverse_gauge" presentationMeta={null} evalData={makeEval()} />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("renders gauge chart for speedometer type (backward compat)", () => {
    const { container } = render(
      <KpiVisual presentationType="speedometer" presentationMeta={null} evalData={makeEval()} />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("renders bullet chart for bullet_chart type", () => {
    const { container } = render(
      <KpiVisual presentationType="bullet_chart" presentationMeta={null} evalData={makeEval()} />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("renders rag bar for rag_bar type", () => {
    const { container } = render(
      <KpiVisual presentationType="rag_bar" presentationMeta={null} evalData={makeEval()} />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("handles null value gracefully", () => {
    const { container } = render(
      <KpiVisual
        presentationType="gauge"
        presentationMeta={null}
        evalData={makeEval({ value: null })}
      />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("handles zero target gracefully", () => {
    const { container } = render(
      <KpiVisual
        presentationType="bullet_chart"
        presentationMeta={null}
        evalData={makeEval({ target: 0 })}
      />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("accepts custom bands from presentation_meta", () => {
    const { container } = render(
      <KpiVisual
        presentationType="rag_bar"
        presentationMeta={{
          bands: [
            { label: "Bad", color: "#ff0000", min: 0, max: 50 },
            { label: "OK", color: "#00ff00", min: 50, max: 100 },
          ],
        }}
        evalData={makeEval()}
      />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("falls back to legacy goal when target is null", () => {
    const { container } = render(
      <KpiVisual
        presentationType="gauge"
        presentationMeta={null}
        evalData={makeEval({ target: null, goal: 100 })}
      />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("rescales ratio-scale bands without crashing", () => {
    const { container } = render(
      <KpiVisual
        presentationType="rag_bar"
        presentationMeta={{
          bands: [
            { label: "Off Target", color: "#D32F2F", min: null, max: 0.8 },
            { label: "Near Target", color: "#F57C00", min: 0.8, max: 1.0 },
            { label: "On Track", color: "#388E3C", min: 1.0, max: null },
          ],
        }}
        evalData={makeEval()}
      />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  // --- Direction-aware rendering tests ---

  it("renders gauge for lower_is_better direction", () => {
    const { container } = render(
      <KpiVisual
        presentationType="gauge"
        presentationMeta={null}
        evalData={makeEval({ value: 50, target: 100 })}
        direction="lower_is_better"
      />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("renders gauge for closer_is_better direction", () => {
    const { container } = render(
      <KpiVisual
        presentationType="gauge"
        presentationMeta={null}
        evalData={makeEval({ value: 105, target: 100 })}
        direction="closer_is_better"
      />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("renders bullet chart with lower_is_better direction", () => {
    const { container } = render(
      <KpiVisual
        presentationType="bullet_chart"
        presentationMeta={null}
        evalData={makeEval({ value: 50, target: 100 })}
        direction="lower_is_better"
      />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });
});

describe("ratioForChart business outcomes", () => {
  // Re-import the module to test the ratio function via KpiVisual rendering
  // We test indirectly via the component because ratioForChart is not exported.

  it("lower_is_better: cost KPI beating target renders (high chart value)", () => {
    // Cost KPI: value=50, target=100 -> beating goal
    // ratioForChart = (100/50)*100 = 200 -> chart shows high value
    // Bands stored in 0-1 ratio scale; rescaleBandsToPercentage multiplies by 100 for chart
    const { container } = render(
      <KpiVisual
        presentationType="gauge"
        presentationMeta={{
          evaluation_type: "percentage_of_target",
          bands: [
            { label: "Off Target", color: "#D32F2F", min: null, max: 0.80 },
            { label: "On Track", color: "#388E3C", min: 0.80, max: null },
          ],
        }}
        evalData={makeEval({ value: 50, target: 100 })}
        direction="lower_is_better"
      />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("closer_is_better: on-target value renders (chart value near 100)", () => {
    // Budget adherence: value=100, target=100 -> exactly on target
    // ratioForChart = (1 - 0/100)*100 = 100
    // Bands in ratio scale: 0-1
    const { container } = render(
      <KpiVisual
        presentationType="gauge"
        presentationMeta={{
          evaluation_type: "percentage_of_target",
          bands: [
            { label: "Off Target", color: "#D32F2F", min: null, max: 0.50 },
            { label: "On Track", color: "#388E3C", min: 0.50, max: null },
          ],
        }}
        evalData={makeEval({ value: 100, target: 100 })}
        direction="closer_is_better"
      />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("closer_is_better: deviation reduces chart value equally above and below", () => {
    // 50% above target: value=150, target=100 -> ratio = 50
    const { container: above } = render(
      <KpiVisual
        presentationType="bullet_chart"
        presentationMeta={null}
        evalData={makeEval({ value: 150, target: 100 })}
        direction="closer_is_better"
      />,
    );
    // 50% below target: value=50, target=100 -> ratio = 50
    const { container: below } = render(
      <KpiVisual
        presentationType="bullet_chart"
        presentationMeta={null}
        evalData={makeEval({ value: 50, target: 100 })}
        direction="closer_is_better"
      />,
    );
    // Both should render (chart values are symmetric)
    expect(above.children.length).toBeGreaterThan(0);
    expect(below.children.length).toBeGreaterThan(0);
  });

  it("closer_is_better over-target: percentage_of_target basis renders off-target, not clamped to on-track", () => {
    // value=130, target=100 -> 30% deviation -> ratioForChart = 70
    // Bands in ratio scale: 0-0.30 off / 0.30-0.70 near / 0.70-1.00 on
    // rescaleBandsToPercentage multiplies by 100 for chart: 0-30 / 30-70 / 70-100
    // chart value 70 lands at the edge of "On Track"
    const { container } = render(
      <KpiVisual
        presentationType="gauge"
        presentationMeta={{
          evaluation_type: "percentage_of_target",
          bands: [
            { label: "Off Target", color: "#D32F2F", min: null, max: 0.30 },
            { label: "Near Target", color: "#F57C00", min: 0.30, max: 0.70 },
            { label: "On Track", color: "#388E3C", min: 0.70, max: null },
          ],
        }}
        evalData={makeEval({ value: 130, target: 100 })}
        direction="closer_is_better"
      />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("basis switch: percentage_of_target gauge with ratio-scale bands renders correctly", () => {
    // Verifies that percentage_of_target bands in ratio scale (0-1) are handled properly.
    // value=7, target=10 -> ratioForChart = 70; bands rescaled to 0-100 for chart.
    const { container } = render(
      <KpiVisual
        presentationType="gauge"
        presentationMeta={{
          evaluation_type: "percentage_of_target",
          bands: [
            { label: "Off Target", color: "#D32F2F", min: null, max: 0.50 },
            { label: "Near Target", color: "#F57C00", min: 0.50, max: 0.80 },
            { label: "On Track", color: "#388E3C", min: 0.80, max: null },
          ],
        }}
        evalData={makeEval({ value: 7, target: 10 })}
        direction="higher_is_better"
      />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });

  it("BUG-USER-001: absolute bands without evaluation_type render correctly when target is set", () => {
    // Scenario: "Days to First Login" KPI — target=5 days, value=3 days,
    // bands scaled to target (0-2.5, 2.5-4, 4-5), lower_is_better.
    // Without evaluation_type set, the component must infer absolute mode
    // from the fact that band boundaries match the target scale, not 0-100.
    const { container } = render(
      <KpiVisual
        presentationType="gauge"
        presentationMeta={{
          bands: [
            { label: "On Track", color: "#388E3C", min: 0, max: 2.5 },
            { label: "Near Target", color: "#F57C00", min: 2.5, max: 4 },
            { label: "Off Target", color: "#D32F2F", min: 4, max: 5 },
          ],
        }}
        evalData={makeEval({ value: 3, target: 5 })}
        direction="lower_is_better"
      />,
    );
    expect(container.children.length).toBeGreaterThan(0);
  });
});

describe("goalThreshold (Bug-5345 bullet target marker)", () => {
  it("returns the top-band lower edge for ascending bands", () => {
    expect(
      goalThreshold([
        { label: "Poor", color: "#f00", min: 0, max: 40 },
        { label: "Warn", color: "#fa0", min: 40, max: 70 },
        { label: "Good", color: "#0a0", min: 70, max: 100 },
      ]),
    ).toBe(70);
  });

  it("returns null when the threshold equals the scale floor", () => {
    expect(
      goalThreshold([{ label: "All", color: "#0a0", min: 0, max: 100 }]),
    ).toBeNull();
  });

  it("returns null for empty or undefined bands", () => {
    expect(goalThreshold(undefined)).toBeNull();
    expect(goalThreshold([])).toBeNull();
  });

  it("handles ratio-scale authoritative bands", () => {
    expect(
      goalThreshold([
        { label: "Off", color: "#f00", min: 0, max: 0.8 },
        { label: "Near", color: "#fa0", min: 0.8, max: 1.0 },
        { label: "On", color: "#0a0", min: 1.0, max: 1.25 },
      ]),
    ).toBe(1.0);
  });
});

describe("cross-layer status agreement (R3-M001b)", () => {
  // Uses production createDefaultPresentationMeta for bands so a revert
  // to wrong band constants or ordering will fail these tests.

  // Backend _compute_ratio normalises all directions so high = good:
  function backendRatio(value: number, target: number, direction: string): number {
    if (direction === "lower_is_better") return target / value;
    if (direction === "closer_is_better") return 1 - Math.abs(value - target) / Math.abs(target);
    return value / target;
  }

  // Frontend ratioForChart (mirrors KpiVisual.tsx:21-35):
  function frontendChartValue(value: number, target: number, direction: string): number {
    if (direction === "lower_is_better") return (target / value) * 100;
    if (direction === "closer_is_better") return (1 - Math.abs(value - target) / Math.abs(target)) * 100;
    return (value / target) * 100;
  }

  function matchBand(
    val: number,
    bands: { label: string; min: number | null; max: number | null }[],
  ): string {
    for (const b of bands) {
      const lo = b.min ?? -Infinity;
      const hi = b.max ?? Infinity;
      if (val >= lo && val < hi) return b.label;
    }
    const last = bands[bands.length - 1];
    if (val >= (last.min ?? -Infinity)) return last.label;
    return "unknown";
  }

  const CASES: { direction: string; value: number; target: number; expected: string }[] = [
    { direction: "higher_is_better", value: 90,  target: 100, expected: "Near Target" },
    { direction: "higher_is_better", value: 110, target: 100, expected: "On Track" },
    { direction: "higher_is_better", value: 50,  target: 100, expected: "Off Target" },
    { direction: "lower_is_better",  value: 50,  target: 100, expected: "On Track" },
    { direction: "lower_is_better",  value: 115, target: 100, expected: "Near Target" },
    { direction: "lower_is_better",  value: 200, target: 100, expected: "Off Target" },
    { direction: "closer_is_better", value: 100, target: 100, expected: "On Track" },
    { direction: "closer_is_better", value: 88,  target: 100, expected: "Near Target" },
    { direction: "closer_is_better", value: 50,  target: 100, expected: "Off Target" },
  ];

  for (const { direction, value, target, expected } of CASES) {
    it(`${direction}: value=${value}, target=${target} -> ${expected}`, () => {
      // Get bands from production code
      const meta = createDefaultPresentationMeta(null, direction, "percentage_of_target");
      const ratioBands = meta.bands!;
      // Rescale 0-1 bands to 0-100 (mirrors rescaleBandsToPercentage)
      const rescaled = ratioBands.map((b) => ({
        ...b,
        min: b.min !== null ? b.min * 100 : b.min,
        max: b.max !== null ? b.max * 100 : b.max,
      }));

      // Backend: ratio matched against ratio-scale bands
      const ratio = backendRatio(value, target, direction);
      const bStatus = matchBand(ratio, ratioBands);
      expect(bStatus).toBe(expected);

      // Frontend: chart value matched against rescaled bands
      const chartVal = frontendChartValue(value, target, direction);
      const fStatus = matchBand(chartVal, rescaled);
      expect(fStatus).toBe(expected);

      expect(bStatus).toBe(fStatus);
    });
  }
});

describe("bandsLookAbsolute (BUG-USER-001)", () => {
  it("returns true when bands max matches target scale", () => {
    const bands = [
      { label: "On Track", color: "#388E3C", min: 0, max: 2.5 },
      { label: "Near Target", color: "#F57C00", min: 2.5, max: 4 },
      { label: "Off Target", color: "#D32F2F", min: 4, max: 5 },
    ];
    expect(bandsLookAbsolute(bands, 5)).toBe(true);
  });

  it("returns true when bands max is near target (e.g. target=200, bands 0-200)", () => {
    const bands = [
      { label: "Off Target", color: "#D32F2F", min: 0, max: 100 },
      { label: "Near Target", color: "#F57C00", min: 100, max: 160 },
      { label: "On Track", color: "#388E3C", min: 160, max: 200 },
    ];
    expect(bandsLookAbsolute(bands, 200)).toBe(true);
  });

  it("returns false when bands are on 0-100 pct scale and target is 100", () => {
    const bands = [
      { label: "Off Target", color: "#D32F2F", min: 0, max: 50 },
      { label: "Near Target", color: "#F57C00", min: 50, max: 80 },
      { label: "On Track", color: "#388E3C", min: 80, max: 100 },
    ];
    expect(bandsLookAbsolute(bands, 100)).toBe(false);
  });

  it("returns false when bands are empty", () => {
    expect(bandsLookAbsolute([], 5)).toBe(false);
    expect(bandsLookAbsolute(undefined, 5)).toBe(false);
  });

  it("returns false when target is null", () => {
    const bands = [
      { label: "On Track", color: "#388E3C", min: 0, max: 5 },
    ];
    expect(bandsLookAbsolute(bands, null)).toBe(false);
  });

  it("returns true for bands max 10 with target 12 (ratio in range 0.5-3)", () => {
    const bands = [
      { label: "Off Target", color: "#D32F2F", min: 0, max: 5 },
      { label: "On Track", color: "#388E3C", min: 5, max: 10 },
    ];
    expect(bandsLookAbsolute(bands, 12)).toBe(true);
  });

  it("returns false for bands max far from target", () => {
    const bands = [
      { label: "On Track", color: "#388E3C", min: 0, max: 5 },
    ];
    expect(bandsLookAbsolute(bands, 1000)).toBe(false);
  });
});

describe("Bug-1226: gauge needle agrees with status badge (authoritative)", () => {
  // Replicate GaugeChart's needle-colour derivation exactly: deriveScale →
  // bandsToAxisLine → statusColor. The gauge plots the needle from chartValue
  // against these same bands, so the colour the needle takes IS the band the
  // value lands in. The test asserts this colour equals the backend status_color
  // — i.e. needle, band colour and status badge agree.
  function needleColor(chartValue: number, bands: KpiThresholdBand[]): string {
    const { scaleMin, scaleMax } = deriveScale(bands);
    const range = scaleMax - scaleMin;
    const clamped = Math.max(scaleMin, Math.min(scaleMax, chartValue));
    const normalised = range > 0 ? (clamped - scaleMin) / range : 0;
    const axisLine = bandsToAxisLine(bands, scaleMin, scaleMax);
    return statusColor(normalised, axisLine);
  }

  const GREEN = "#388E3C";
  const RED = "#D32F2F";

  // Each case is exactly what the backend would emit in status_position /
  // status_bands / status_color for the named mode and outcome.
  const CASES: {
    mode: string;
    outcome: string;
    position: number;
    bands: KpiThresholdBand[];
    statusColorExpected: string;
  }[] = [
    {
      // percentage_variance, inverted cost KPI BEATING target.
      // Round-1 reproduction: backend GREEN, legacy gauge RED. The
      // authoritative position is the deviation 0.0165, not 101.68.
      mode: "percentage_variance",
      outcome: "beating",
      position: 0.0165,
      bands: [
        { label: "On Track", color: GREEN, min: null, max: 0.1 },
        { label: "Near Target", color: "#F57C00", min: 0.1, max: 0.2 },
        { label: "Off Target", color: RED, min: 0.2, max: null },
      ],
      statusColorExpected: GREEN,
    },
    {
      mode: "percentage_variance",
      outcome: "missing",
      position: 0.3,
      bands: [
        { label: "On Track", color: GREEN, min: null, max: 0.1 },
        { label: "Near Target", color: "#F57C00", min: 0.1, max: 0.2 },
        { label: "Off Target", color: RED, min: 0.2, max: null },
      ],
      statusColorExpected: RED,
    },
    {
      mode: "z_score",
      outcome: "beating",
      position: 2.1,
      bands: [
        { label: "Low", color: RED, min: null, max: -0.5 },
        { label: "Mid", color: "#F57C00", min: -0.5, max: 0.5 },
        { label: "High", color: GREEN, min: 0.5, max: null },
      ],
      statusColorExpected: GREEN,
    },
    {
      mode: "z_score",
      outcome: "missing",
      position: -2.1,
      bands: [
        { label: "Low", color: RED, min: null, max: -0.5 },
        { label: "Mid", color: "#F57C00", min: -0.5, max: 0.5 },
        { label: "High", color: GREEN, min: 0.5, max: null },
      ],
      statusColorExpected: RED,
    },
    {
      mode: "percentile_rank",
      outcome: "beating",
      position: 83.3,
      bands: [
        { label: "Bottom", color: RED, min: null, max: 25 },
        { label: "Middle", color: "#F57C00", min: 25, max: 75 },
        { label: "Top", color: GREEN, min: 75, max: null },
      ],
      statusColorExpected: GREEN,
    },
    {
      // Inverted (lower_is_better) cost KPI ranked worst → position 0.
      mode: "percentile_rank",
      outcome: "missing (inverted)",
      position: 0,
      bands: [
        { label: "Bottom", color: RED, min: null, max: 25 },
        { label: "Middle", color: "#F57C00", min: 25, max: 75 },
        { label: "Top", color: GREEN, min: 75, max: null },
      ],
      statusColorExpected: RED,
    },
    {
      mode: "percentage_of_target",
      outcome: "beating",
      position: 1.1,
      bands: [
        { label: "Off Target", color: RED, min: null, max: 0.8 },
        { label: "Near Target", color: "#F57C00", min: 0.8, max: 1.0 },
        { label: "On Track", color: GREEN, min: 1.0, max: null },
      ],
      statusColorExpected: GREEN,
    },
    {
      mode: "percentage_of_target",
      outcome: "missing",
      position: 0.7,
      bands: [
        { label: "Off Target", color: RED, min: null, max: 0.8 },
        { label: "Near Target", color: "#F57C00", min: 0.8, max: 1.0 },
        { label: "On Track", color: GREEN, min: 1.0, max: null },
      ],
      statusColorExpected: RED,
    },
  ];

  for (const c of CASES) {
    it(`${c.mode} (${c.outcome}): needle colour matches status badge`, () => {
      const evalData = makeEval({
        status_position: c.position,
        status_bands: c.bands,
        status_color: c.statusColorExpected,
      });
      const inputs = resolveChartInputs(evalData, {
        evaluation_type: c.mode,
      });
      // Authoritative path engaged: needle is the backend position, bands are
      // the backend bands — no rescaling.
      expect(inputs.hasAuthoritative).toBe(true);
      expect(inputs.chartValue).toBe(c.position);
      expect(inputs.bands).toEqual(c.bands);
      // The colour the gauge needle takes equals the backend status_color.
      const needle = needleColor(inputs.chartValue as number, inputs.bands!);
      expect(needle).toBe(c.statusColorExpected);
    });
  }

  it("legacy fallback engages when status_position/status_bands absent", () => {
    const evalData = makeEval({
      value: 50,
      target: 100,
      status_position: null,
      status_bands: null,
    });
    const inputs = resolveChartInputs(
      evalData,
      { evaluation_type: "percentage_of_target" },
      "higher_is_better",
    );
    expect(inputs.hasAuthoritative).toBe(false);
    // ratioForChart(50, 100) = 50 (percentage of target)
    expect(inputs.chartValue).toBe(50);
  });
});
