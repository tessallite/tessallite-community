import { describe, expect, it } from "vitest";
import { createDefaultPresentationMeta } from "./KpiThresholdEditor";

describe("createDefaultPresentationMeta", () => {
  // F-017-20: the default evaluation type is percentage_of_target (spec basis),
  // matching a null presentation_meta on the backend. absolute_value is still
  // reachable via forceEvaluationType.
  it("defaults to percentage_of_target with ratio-scale bands", () => {
    const meta = createDefaultPresentationMeta(10);
    expect(meta.evaluation_type).toBe("percentage_of_target");
    expect(meta.bands).toEqual([
      { label: "Off Target", color: "#D32F2F", min: null, max: 0.80 },
      { label: "Near Target", color: "#F57C00", min: 0.80, max: 1.00 },
      { label: "On Track", color: "#388E3C", min: 1.00, max: null },
    ]);
  });

  it("scales default absolute-value bands to a static target when forced", () => {
    const meta = createDefaultPresentationMeta(10, "higher_is_better", "absolute_value");

    expect(meta.evaluation_type).toBe("absolute_value");
    expect(meta.bands).toEqual([
      { label: "Off Target", color: "#D32F2F", min: null, max: 8 },
      { label: "Near Target", color: "#F57C00", min: 8, max: 10 },
      { label: "On Track", color: "#388E3C", min: 10, max: null },
    ]);
  });

  it("lower_is_better + absolute_value: best band at low end, worst at high end", () => {
    const meta = createDefaultPresentationMeta(100, "lower_is_better", "absolute_value");
    const bands = meta.bands!;
    expect(bands[0].label).toBe("On Track");
    expect(bands[0].color).toBe("#388E3C");
    expect(bands[bands.length - 1].label).toBe("Off Target");
    expect(bands[bands.length - 1].color).toBe("#D32F2F");
  });

  it("lower_is_better default (percentage_of_target) has On Track at the high end", () => {
    // Backend normalises lower_is_better ratio as target/value (high = good), so
    // On Track must be at the high end — same order as the higher_is_better bands.
    const meta = createDefaultPresentationMeta(100, "lower_is_better");
    expect(meta.evaluation_type).toBe("percentage_of_target");
    const bands = meta.bands!;
    expect(bands[0].label).toBe("Off Target");
    expect(bands[bands.length - 1].label).toBe("On Track");
    expect(bands[bands.length - 1].min).toBe(1.00);
  });

  it("closer_is_better: returns percentage_of_target with ratio-scale bands", () => {
    const meta = createDefaultPresentationMeta(100, "closer_is_better");
    const bands = meta.bands!;
    expect(meta.evaluation_type).toBe("percentage_of_target");
    expect(bands.some((b) => b.label === "On Track")).toBe(true);
    const onTrack = bands.find((b) => b.label === "On Track")!;
    expect(onTrack.min).toBeGreaterThan(0);
    expect(onTrack.max).toBeNull();
    const offTargetBands = bands.filter((b) => b.label === "Off Target");
    expect(offTargetBands.length).toBe(1);
    expect(offTargetBands[0].min).toBeNull();
  });

  it("higher_is_better default: best band at high end", () => {
    const meta = createDefaultPresentationMeta(100, "higher_is_better");
    expect(meta.evaluation_type).toBe("percentage_of_target");
    const bands = meta.bands!;
    expect(bands[bands.length - 1].label).toBe("On Track");
    expect(bands[bands.length - 1].color).toBe("#388E3C");
  });

  it("closer_is_better bands are not target-scaled (stay on ratio scale)", () => {
    const meta10 = createDefaultPresentationMeta(10, "closer_is_better");
    const meta200 = createDefaultPresentationMeta(200, "closer_is_better");
    expect(meta10.bands).toEqual(meta200.bands);
  });

  it("direction change from higher_is_better to closer_is_better stays percentage_of_target", () => {
    const initialMeta = createDefaultPresentationMeta(100, "higher_is_better");
    expect(initialMeta.evaluation_type).toBe("percentage_of_target");

    const regenerated = createDefaultPresentationMeta(100, "closer_is_better");
    expect(regenerated.evaluation_type).toBe("percentage_of_target");
    expect(regenerated.bands![0].min).toBeNull();
    expect(regenerated.bands![regenerated.bands!.length - 1].max).toBeNull();
  });

  it("forced absolute_value target change from 10 to 100 rescales bands (H-001 regression)", () => {
    const metaAt10 = createDefaultPresentationMeta(10, "higher_is_better", "absolute_value");
    expect(metaAt10.bands![2].max).toBeNull();
    expect(metaAt10.bands![1].max).toBe(10);

    const metaAt100 = createDefaultPresentationMeta(100, "higher_is_better", "absolute_value");
    expect(metaAt100.bands![1].max).toBe(100);
    expect(metaAt100.bands![0].max).toBe(80);
  });

  it("forced absolute_value with target=200 generates target-scaled bands, not 0-100", () => {
    const meta = createDefaultPresentationMeta(200, "higher_is_better", "absolute_value");
    expect(meta.evaluation_type).toBe("absolute_value");
    expect(meta.bands![0].max).toBe(160);
    expect(meta.bands![1].max).toBe(200);
    expect(meta.bands![2].max).toBeNull();
  });

  it("forced absolute_value with no target falls back to 100 scale", () => {
    const meta = createDefaultPresentationMeta(null, "higher_is_better", "absolute_value");
    expect(meta.bands![1].max).toBe(100);
  });

  it("forceEvaluationType=absolute_value with closer_is_better produces target-scaled bands (BUG-R168-M001)", () => {
    const meta = createDefaultPresentationMeta(200, "closer_is_better", "absolute_value");
    expect(meta.evaluation_type).toBe("absolute_value");
    // BANDS_CLOSER scaled by 200: [null, 160], [160, 180], [180, null]
    expect(meta.bands![0].max).toBe(160);
    expect(meta.bands![1].max).toBe(180);
    expect(meta.bands![2].max).toBeNull();
  });

  it("forceEvaluationType=percentage_of_target with higher_is_better produces ratio-scale bands", () => {
    const meta = createDefaultPresentationMeta(200, "higher_is_better", "percentage_of_target");
    expect(meta.evaluation_type).toBe("percentage_of_target");
    expect(meta.bands![0].max).toBe(0.80);
    expect(meta.bands![1].max).toBe(1.00);
    expect(meta.bands![2].max).toBeNull();
  });

  it("percentage_of_target bands match backend ratio scale for cross-layer consistency (C-001)", () => {
    const meta = createDefaultPresentationMeta(100, "higher_is_better", "percentage_of_target");
    expect(meta.bands![0].max).toBe(0.80);
    expect(meta.bands![1].min).toBe(0.80);
    expect(meta.bands![1].max).toBe(1.00);
    expect(meta.bands![2].min).toBe(1.00);
  });

  it("lower_is_better + absolute_value still has On Track at low end (R3-M001 regression guard)", () => {
    const meta = createDefaultPresentationMeta(100, "lower_is_better", "absolute_value");
    expect(meta.evaluation_type).toBe("absolute_value");
    expect(meta.bands![0].label).toBe("On Track");
    expect(meta.bands![2].label).toBe("Off Target");
  });

  it("BANDS_CLOSER matches backend centred preset contract (cross-layer drift guard, R7-L001)", () => {
    const meta = createDefaultPresentationMeta(null, "closer_is_better", "percentage_of_target");
    const bands = meta.bands!;
    expect(bands).toHaveLength(3);
    expect(bands[0]).toEqual({ label: "Off Target",  color: "#D32F2F", min: null, max: 0.80 });
    expect(bands[1]).toEqual({ label: "Near Target", color: "#F57C00", min: 0.80, max: 0.90 });
    expect(bands[2]).toEqual({ label: "On Track",    color: "#388E3C", min: 0.90, max: null });
  });

  // F-017-01 / F-103-06: directional variance defaults must be signed favourable
  // and (for absolute_variance) scaled into the measure's units, matching the
  // backend variance_directional preset.
  it("lower_is_better percentage_variance defaults to favourable-ordered bands", () => {
    const meta = createDefaultPresentationMeta(100, "lower_is_better", "percentage_variance");
    expect(meta.evaluation_type).toBe("percentage_variance");
    expect(meta.bands).toEqual([
      { label: "Off Target", color: "#D32F2F", min: null, max: -0.20 },
      { label: "Near Target", color: "#F57C00", min: -0.20, max: 0.0 },
      { label: "On Track", color: "#388E3C", min: 0.0, max: null },
    ]);
  });

  it("absolute_variance defaults scale the directional bands by |target|", () => {
    const meta = createDefaultPresentationMeta(100_000, "lower_is_better", "absolute_variance");
    expect(meta.evaluation_type).toBe("absolute_variance");
    // BANDS_VARIANCE_DIRECTIONAL scaled by 100,000: a beating variance (>=0) is
    // green; missing by more than 20,000 is red.
    expect(meta.bands![0]).toEqual({ label: "Off Target", color: "#D32F2F", min: null, max: -20000 });
    expect(meta.bands![2]).toEqual({ label: "On Track", color: "#388E3C", min: 0, max: null });
  });

  it("closer_is_better variance keeps deviation-ordered bands (0 = best)", () => {
    const meta = createDefaultPresentationMeta(100, "closer_is_better", "percentage_variance");
    expect(meta.bands![0].label).toBe("On Track");
    expect(meta.bands![0].max).toBe(0.10);
    expect(meta.bands![meta.bands!.length - 1].label).toBe("Off Target");
  });

  // F-017-10: percentile_rank default bands must match the backend
  // percentile_rank preset (25 / 50), not the legacy 33 / 67.
  it("percentile_rank defaults align to backend 25/50 breakpoints", () => {
    const meta = createDefaultPresentationMeta(100, "higher_is_better", "percentile_rank");
    expect(meta.evaluation_type).toBe("percentile_rank");
    expect(meta.bands).toEqual([
      { label: "Off Target", color: "#D32F2F", min: null, max: 25 },
      { label: "Near Target", color: "#F57C00", min: 25, max: 50 },
      { label: "On Track", color: "#388E3C", min: 50, max: null },
    ]);
  });
});
