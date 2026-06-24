import type { KpiThresholdBand } from "../../api/types";

export function deriveScale(bands: KpiThresholdBand[]): {
  scaleMin: number;
  scaleMax: number;
} {
  const boundaries: number[] = [];
  for (const b of bands) {
    if (b.min !== null && b.min !== undefined) boundaries.push(b.min);
    if (b.max !== null && b.max !== undefined) boundaries.push(b.max);
  }
  if (boundaries.length === 0) return { scaleMin: 0, scaleMax: 100 };

  let scaleMin = Math.min(...boundaries);
  let scaleMax = Math.max(...boundaries);

  const sorted = [...bands].sort(
    (a, b) => (a.min ?? -Infinity) - (b.min ?? -Infinity),
  );
  const range = scaleMax - scaleMin || 1;

  const hasOpenMin = sorted[0]?.min === null || sorted[0]?.min === undefined;
  const hasOpenMax =
    sorted[sorted.length - 1]?.max === null ||
    sorted[sorted.length - 1]?.max === undefined;

  if (hasOpenMin) {
    scaleMin = scaleMin >= 0 ? 0 : scaleMin - range * 0.5;
  }
  if (hasOpenMax) {
    // F-017-06 / Bug-5341: an open-ended top band (e.g. "On Track" [1.0, ∞) on a
    // ratio scale) must occupy a visible lane, not a sliver. The headroom must be
    // based on the EFFECTIVE range after the open-min expansion — not the bare
    // boundary span. For ratio bands [_, 0.8][0.8, 1.0][1.0, ∞) the boundary span
    // is only 0.2, so range*0.25 = 0.05 collapsed the top band to ~4% of the bar,
    // making an on-target (green) KPI read mostly-red and crowding the axis
    // labels at the right. Using the post-open-min range (0 → 1.0) gives a
    // proportionate top lane that agrees with the green status badge.
    const effectiveRange = scaleMax - scaleMin || 1;
    scaleMax = scaleMax + effectiveRange * 0.25;
  }

  if (scaleMin >= scaleMax) scaleMax = scaleMin + 100;
  return { scaleMin, scaleMax };
}
