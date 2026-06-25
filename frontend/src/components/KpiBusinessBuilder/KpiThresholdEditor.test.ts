import { describe, expect, it } from "vitest";
import { createDefaultPresentationMeta } from "./KpiThresholdEditor";

describe("createDefaultPresentationMeta", () => {
  it("scales default absolute-value bands to a static target", () => {
    const meta = createDefaultPresentationMeta(10);

    expect(meta.evaluation_type).toBe("absolute_value");
    expect(meta.bands).toEqual([
      { label: "Off Target", color: "#D32F2F", min: null, max: 8 },
      { label: "Near Target", color: "#F57C00", min: 8, max: 10 },
      { label: "On Track", color: "#388E3C", min: 10, max: null },
    ]);
  });

  it("lower_is_better: best band at low end, worst at high end", () => {
    const meta = createDefaultPresentationMeta(100, "lower_is_better");
    const bands = meta.bands!;
    expect(bands[0].label).toBe("On Track");
    expect(bands[0].color).toBe("#388E3C");
    expect(bands[bands.length - 1].label).toBe("Off Target");
    expect(bands[bands.length - 1].color).toBe("#D32F2F");
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

  it("higher_is_better: best band at high end (default)", () => {
    const meta = createDefaultPresentationMeta(100, "higher_is_better");
    const bands = meta.bands!;
    expect(bands[bands.length - 1].label).toBe("On Track");
    expect(bands[bands.length - 1].color).toBe("#388E3C");
  });

  it("closer_is_better defaults to percentage_of_target evaluation type", () => {
    const meta = createDefaultPresentationMeta(100, "closer_is_better");
    expect(meta.evaluation_type).toBe("percentage_of_target");
    const bands = meta.bands!;
    // Ratio-scale bands: first min is null (open-ended), last max is null
    expect(bands[0].min).toBeNull();
    expect(bands[bands.length - 1].max).toBeNull();
  });

  it("closer_is_better bands are not target-scaled (stay on ratio scale)", () => {
    const meta10 = createDefaultPresentationMeta(10, "closer_is_better");
    const meta200 = createDefaultPresentationMeta(200, "closer_is_better");
    expect(meta10.bands).toEqual(meta200.bands);
  });

  it("higher_is_better defaults to absolute_value evaluation type", () => {
    const meta = createDefaultPresentationMeta(100, "higher_is_better");
    expect(meta.evaluation_type).toBe("absolute_value");
  });

  it("direction change from higher_is_better to closer_is_better must regenerate as percentage_of_target", () => {
    const initialMeta = createDefaultPresentationMeta(100, "higher_is_better");
    expect(initialMeta.evaluation_type).toBe("absolute_value");

    const regenerated = createDefaultPresentationMeta(100, "closer_is_better");
    expect(regenerated.evaluation_type).toBe("percentage_of_target");
    // Ratio-scale bands with null at boundaries
    expect(regenerated.bands![0].min).toBeNull();
    expect(regenerated.bands![regenerated.bands!.length - 1].max).toBeNull();
  });

  it("direction change from closer_is_better to lower_is_better regenerates absolute bands", () => {
    const closerMeta = createDefaultPresentationMeta(50, "closer_is_better");
    expect(closerMeta.evaluation_type).toBe("percentage_of_target");

    const lowerMeta = createDefaultPresentationMeta(50, "lower_is_better");
    expect(lowerMeta.evaluation_type).toBe("absolute_value");
    expect(lowerMeta.bands![0].label).toBe("On Track");
  });

  it("target change from 10 to 100 rescales absolute bands (H-001 regression)", () => {
    const metaAt10 = createDefaultPresentationMeta(10, "higher_is_better");
    expect(metaAt10.bands![2].max).toBeNull();
    expect(metaAt10.bands![1].max).toBe(10);

    const metaAt100 = createDefaultPresentationMeta(100, "higher_is_better");
    expect(metaAt100.bands![1].max).toBe(100);
    expect(metaAt100.bands![0].max).toBe(80);
  });

  it("target change must not leave stale bands — regeneration produces correct evaluation for value near new target", () => {
    const staleMetaAt10 = createDefaultPresentationMeta(10, "higher_is_better");
    // All finite maxes < 90
    expect(staleMetaAt10.bands!.every((b) => b.max === null || b.max < 90)).toBe(true);

    const freshMetaAt100 = createDefaultPresentationMeta(100, "higher_is_better");
    const onTrack = freshMetaAt100.bands!.find((b) => b.label === "On Track")!;
    // On Track band starts at 100 (value at or above target is on track)
    expect(onTrack.min).toBe(100);
  });

  it("absolute_value basis switch with target=200 generates target-scaled bands, not 0-100", () => {
    const meta = createDefaultPresentationMeta(200, "higher_is_better");
    expect(meta.evaluation_type).toBe("absolute_value");
    // Bands: [null, 160], [160, 200], [200, null]
    expect(meta.bands![0].max).toBe(160);
    expect(meta.bands![1].max).toBe(200);
    expect(meta.bands![2].max).toBeNull();
  });

  it("no target falls back to 100 scale for absolute_value", () => {
    const meta = createDefaultPresentationMeta(null, "higher_is_better");
    // max=100 fallback: [null, 80], [80, 100], [100, null]
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
    // Ratio-scale: [null, 0.80], [0.80, 1.00], [1.00, null]
    expect(meta.bands![0].max).toBe(0.80);
    expect(meta.bands![1].max).toBe(1.00);
    expect(meta.bands![2].max).toBeNull();
  });

  it("closer_is_better without forceEvaluationType still defaults to percentage_of_target", () => {
    const meta = createDefaultPresentationMeta(200, "closer_is_better");
    expect(meta.evaluation_type).toBe("percentage_of_target");
    // Ratio-scale BANDS_CLOSER: [null, 0.80], [0.80, 0.90], [0.90, null]
    expect(meta.bands![1].max).toBe(0.90);
    expect(meta.bands![2].max).toBeNull();
  });

  it("percentage_of_target bands match backend ratio scale for cross-layer consistency (C-001)", () => {
    const meta = createDefaultPresentationMeta(100, "higher_is_better", "percentage_of_target");
    // Backend computes ratio as value/target (e.g. 90/100 = 0.90).
    // Stored bands must be on the same 0-1 scale so 0.90 matches "Near Target" [0.80, 1.00).
    expect(meta.bands![0].max).toBe(0.80);
    expect(meta.bands![1].min).toBe(0.80);
    expect(meta.bands![1].max).toBe(1.00);
    expect(meta.bands![2].min).toBe(1.00);
  });

  it("basis switch to percentage_of_target on higher_is_better produces ratio-scale bands (R3-H001)", () => {
    // Simulates what the Select onChange handler does:
    // createDefaultPresentationMeta(null, direction, newType)
    const meta = createDefaultPresentationMeta(null, "higher_is_better", "percentage_of_target");
    expect(meta.evaluation_type).toBe("percentage_of_target");
    // Must be ratio-scale (0-1), NOT target-scaled (0-100)
    expect(meta.bands![0].max).toBe(0.80);
    expect(meta.bands![1].max).toBe(1.00);
    expect(meta.bands![2].max).toBeNull();
  });

  it("basis switch to percentage_of_target on lower_is_better produces ratio-scale bands with On Track at high end (R3-H001+M001)", () => {
    const meta = createDefaultPresentationMeta(null, "lower_is_better", "percentage_of_target");
    expect(meta.evaluation_type).toBe("percentage_of_target");
    // Backend normalises lower_is_better ratio as target/value (high = good),
    // so On Track must be at the high end, matching BANDS_HIGHER.
    expect(meta.bands![0].label).toBe("Off Target");
    expect(meta.bands![0].max).toBe(0.80);
    expect(meta.bands![2].label).toBe("On Track");
    expect(meta.bands![2].min).toBe(1.00);
    expect(meta.bands![2].max).toBeNull();
  });

  it("lower_is_better + absolute_value still has On Track at low end (R3-M001 regression guard)", () => {
    const meta = createDefaultPresentationMeta(100, "lower_is_better");
    expect(meta.evaluation_type).toBe("absolute_value");
    // Absolute mode: low raw value = good
    expect(meta.bands![0].label).toBe("On Track");
    expect(meta.bands![2].label).toBe("Off Target");
  });

  it("basis switch round-trip: percentage -> absolute -> percentage produces consistent ratio bands", () => {
    const pct1 = createDefaultPresentationMeta(null, "higher_is_better", "percentage_of_target");
    const abs = createDefaultPresentationMeta(200, "higher_is_better", "absolute_value");
    const pct2 = createDefaultPresentationMeta(null, "higher_is_better", "percentage_of_target");
    // Both percentage metas should be identical ratio-scale bands
    expect(pct1.bands).toEqual(pct2.bands);
    // Absolute should be target-scaled, different from percentage
    expect(abs.bands![1].max).toBe(200);
    expect(pct1.bands![1].max).toBe(1.00);
  });

  it("closer_is_better + forceEvaluationType=absolute_value produces target-scaled absolute bands (defense in depth, R5-M001)", () => {
    // Even if absolute_value is forced for closer, the function should return
    // the absolute-scaled bands using BANDS_CLOSER (which is the correct band set).
    // The UI prevents this combination, but verify the generator doesn't crash.
    const meta = createDefaultPresentationMeta(100, "closer_is_better", "absolute_value");
    expect(meta.evaluation_type).toBe("absolute_value");
    // Bands are BANDS_CLOSER scaled by 100: [null..80, 80..90, 90..null]
    expect(meta.bands![0].max).toBe(80);
    expect(meta.bands![1].max).toBe(90);
  });

  it("BANDS_CLOSER matches backend centred preset contract (cross-layer drift guard, R7-L001)", () => {
    // Pin the ratio-scale BANDS_CLOSER to the same values the backend uses
    // in _CLOSER_RATIO_BANDS / the 'centred' preset. If either side changes,
    // this test (or the backend equivalent) fails before a cross-layer mismatch ships.
    const meta = createDefaultPresentationMeta(null, "closer_is_better", "percentage_of_target");
    const bands = meta.bands!;
    expect(bands).toHaveLength(3);
    expect(bands[0]).toEqual({ label: "Off Target",  color: "#D32F2F", min: null, max: 0.80 });
    expect(bands[1]).toEqual({ label: "Near Target", color: "#F57C00", min: 0.80, max: 0.90 });
    expect(bands[2]).toEqual({ label: "On Track",    color: "#388E3C", min: 0.90, max: null });
  });
});
