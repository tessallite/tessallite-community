import { describe, expect, it } from "vitest";
import { deriveScale } from "./chartUtils";

describe("deriveScale", () => {
  it("gives the open-ended top target band headroom so it is visible", () => {
    // F-017-06: the old rule clamped scaleMax to 100, collapsing the
    // open-ended On Track [100, ∞) band to a zero-width sliver — a value
    // meeting target then read amber on the seam. The top band must occupy a
    // visible arc, so scaleMax must exceed 100.
    const scale = deriveScale([
      { label: "Off Target", color: "#D32F2F", min: null, max: 80 },
      { label: "Near Target", color: "#F57C00", min: 80, max: 100 },
      { label: "On Track", color: "#388E3C", min: 100, max: null },
    ]);

    expect(scale.scaleMin).toBe(0);
    expect(scale.scaleMax).toBeGreaterThan(100);
  });

  it("still expands open-ended non-percentage bands", () => {
    const scale = deriveScale([
      { label: "Low", color: "#D32F2F", min: null, max: 200 },
      { label: "High", color: "#388E3C", min: 200, max: null },
    ]);

    expect(scale.scaleMin).toBe(0);
    expect(scale.scaleMax).toBeGreaterThan(200);
  });

  it("Bug-7820: derives a negative scaleMin for z_score bands", () => {
    const scale = deriveScale([
      { label: "Far", color: "#D32F2F", min: null, max: -0.5 },
      { label: "Near", color: "#F57C00", min: -0.5, max: 0.5 },
      { label: "On Target", color: "#388E3C", min: 0.5, max: null },
    ]);
    expect(scale.scaleMin).toBeLessThan(0);
    expect(scale.scaleMax).toBeGreaterThan(0.5);
  });

  it("Bug-5341: ratio bands give the On Track lane a proportionate width, not a sliver", () => {
    // Ratio-scale bands [_, 0.8][0.8, 1.0][1.0, ∞). The boundary span is only
    // 0.2, but the open-min floor expands the effective range to 0 → 1.0. The
    // open-max headroom must be based on that EFFECTIVE range so the green
    // On Track lane is a visible fraction of the bar (~20%), not the ~4% sliver
    // the old range*0.25-on-boundary-span produced (which made an on-target KPI
    // read mostly-red and disagree with its green status badge).
    const scale = deriveScale([
      { label: "Off Target", color: "#D32F2F", min: null, max: 0.8 },
      { label: "Near Target", color: "#F57C00", min: 0.8, max: 1.0 },
      { label: "On Track", color: "#388E3C", min: 1.0, max: null },
    ]);
    expect(scale.scaleMin).toBe(0);
    const onTrackWidth = scale.scaleMax - 1.0;
    const onTrackFraction = onTrackWidth / (scale.scaleMax - scale.scaleMin);
    expect(onTrackFraction).toBeGreaterThan(0.15);
  });
});
