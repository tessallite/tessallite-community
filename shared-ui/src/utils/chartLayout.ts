import { useEffect, useState } from "react";

/**
 * Bug-9921: the compact task-pane chart presentation (Excel add-in "Ask
 * Tessallite" chat mode, and any other host that opts into `compact` on
 * ChatCanvas) originally hard-coded the chart canvas to a single fixed
 * pixel height. A fixed number can't fit both a short pane and a tall one
 * — the pane is resizable vertically and users make it tall specifically to
 * read a chart — and it can't fit both a laptop at 150% Windows display
 * scaling and a 4K monitor at 100%.
 *
 * The rule: the chart takes about 40% of the pane's visible height,
 * clamped between a floor (a short pane still gets a legible chart) and a
 * ceiling (a tall pane doesn't turn into a wall of chart). `window.
 * innerHeight` is reported in CSS pixels, not device pixels, so this ratio
 * lands on the same visual size at any devicePixelRatio / OS scaling — it
 * must never be multiplied or divided by devicePixelRatio here.
 */
export const COMPACT_CHART_HEIGHT = 220; // floor: shortest usable pane
export const COMPACT_CHART_HEIGHT_CEILING = 420; // ceiling: tallest usable pane
const COMPACT_CHART_HEIGHT_RATIO = 0.4; // ~40% of the pane's visible height

export function computeCompactChartHeight(viewportHeight: number): number {
  return Math.min(
    COMPACT_CHART_HEIGHT_CEILING,
    Math.max(COMPACT_CHART_HEIGHT, Math.round(viewportHeight * COMPACT_CHART_HEIGHT_RATIO)),
  );
}

/**
 * Live compact chart height, re-measured on window resize — the Excel task
 * pane fills the whole browser viewport vertically, so `window.innerHeight`
 * IS the pane's available height. ChartBlock/VisualArtifactBlock already
 * fall back to a window "resize" listener (see their own chart-resize
 * effect) when ResizeObserver is unavailable, so this reuses that same
 * mechanism rather than adding a second one.
 *
 * Pass `enabled=false` outside compact mode to skip the listener entirely.
 */
export function useCompactChartHeight(enabled: boolean): number {
  const [height, setHeight] = useState<number>(() =>
    computeCompactChartHeight(window.innerHeight),
  );

  useEffect(() => {
    if (!enabled) return;
    const measure = () => setHeight(computeCompactChartHeight(window.innerHeight));
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [enabled]);

  return height;
}
