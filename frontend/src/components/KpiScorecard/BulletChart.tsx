import { useMemo } from "react";
import ReactECharts from "echarts-for-react";
import type { KpiThresholdBand } from "../../api/types";
import { ui, palette } from "../../theme/tokens";
import { ECHARTS_THEME_NAME } from "../../theme/echartsTheme";
import { useT } from "../../i18n";
import { deriveScale } from "./chartUtils";

interface Props {
  value: number | null;
  target?: number | null;
  label?: string;
  bands?: KpiThresholdBand[];
  width?: number;
  height?: number;
}

/** Distinct boundary readings (min, interior edges, max) for the band set. */
function bandBoundaries(
  bands: KpiThresholdBand[],
  scaleMin: number,
  scaleMax: number,
): number[] {
  const vals = new Set<number>();
  for (const b of bands) {
    vals.add(b.min ?? scaleMin);
    vals.add(b.max ?? scaleMax);
  }
  return [...vals].filter((v) => v >= scaleMin && v <= scaleMax).sort((a, b) => a - b);
}

function fmtBoundary(v: number): string {
  if (Number.isInteger(v)) return String(v);
  const abs = Math.abs(v);
  return abs < 10 ? v.toFixed(2).replace(/0$/, "") : v.toFixed(1);
}

export default function BulletChart({
  value,
  target,
  bands,
  width = 240,
  height = 58,
}: Props) {
  const t = useT();
  // F-017-18 (Bug-852 regression): i18n-computed default band labels.
  const defaultBands = useMemo<KpiThresholdBand[]>(
    () => [
      { label: t("kpiScorecard.poor"), color: ui.red, min: 0, max: 40 },
      { label: t("kpiScorecard.warning"), color: "#ed6c02", min: 40, max: 70 },
      { label: t("kpiScorecard.good"), color: ui.green, min: 70, max: 100 },
    ],
    [t],
  );
  const effectiveBands = bands && bands.length > 0 ? bands : defaultBands;
  const { scaleMin, scaleMax } = useMemo(
    () => deriveScale(effectiveBands),
    [effectiveBands],
  );
  const clamped =
    value !== null ? Math.max(scaleMin, Math.min(scaleMax, value)) : scaleMin;
  const clampedTarget =
    target !== null && target !== undefined
      ? Math.max(scaleMin, Math.min(scaleMax, target))
      : null;
  const boundaries = useMemo(
    () => bandBoundaries(effectiveBands, scaleMin, scaleMax),
    [effectiveBands, scaleMin, scaleMax],
  );

  const option = useMemo(
    () => ({
      grid: { left: 8, right: 10, top: 8, bottom: 20 },
      xAxis: {
        type: "value" as const,
        min: scaleMin,
        max: scaleMax,
        // Band-boundary readings printed beneath the track (ticks + labels
        // pinned exactly to the band edges via customValues).
        axisLine: { show: false },
        axisTick: {
          show: true,
          length: 4,
          customValues: boundaries,
          lineStyle: { color: palette.slateBorder },
        },
        axisLabel: {
          show: true,
          fontSize: 9,
          color: palette.textSecondary,
          margin: 5,
          customValues: boundaries,
          formatter: (v: number) => fmtBoundary(v),
        },
        splitLine: { show: false },
      },
      yAxis: {
        type: "category" as const,
        data: [""],
        show: false,
      },
      series: [
        // Bug-7820: transparent offset bar so the visible band stack starts at
        // scaleMin instead of 0. For negative scaleMin (z_score presets) the
        // bar extends left from 0; for positive scaleMin it shifts bands right.
        // When scaleMin === 0 the bar is invisible and has no effect.
        ...(scaleMin !== 0
          ? [
              {
                type: "bar" as const,
                stack: "bg",
                barWidth: "64%",
                data: [scaleMin],
                itemStyle: { color: "transparent" },
                silent: true,
                animation: false,
              },
            ]
          : []),
        // Saturated band track — the lanes the bullet passes through.
        ...effectiveBands.map((b) => ({
          type: "bar" as const,
          stack: "bg",
          barWidth: "64%",
          data: [(b.max ?? scaleMax) - (b.min ?? scaleMin)],
          itemStyle: {
            color: b.color,
            opacity: 0.5,
            borderColor: palette.white,
            borderWidth: 1,
          },
          silent: true,
          animation: false,
        })),
        // Boundary separators on the track for crisp band edges.
        {
          type: "bar" as const,
          barWidth: "64%",
          barGap: "-100%",
          data: [0],
          markLine: {
            silent: true,
            symbol: "none",
            lineStyle: { color: palette.white, width: 1.5, type: "solid" as const },
            label: { show: false },
            data: boundaries
              .filter((b) => b > scaleMin && b < scaleMax)
              .map((b) => ({ xAxis: b })),
          },
          itemStyle: { color: "transparent" },
          silent: true,
          animation: false,
          z: 5,
        },
        // Bug-7820: transparent offset for the bullet stack, same as the band
        // track, so the bullet baselines at scaleMin (not 0).
        ...(scaleMin !== 0
          ? [
              {
                type: "bar" as const,
                stack: "bullet",
                barWidth: "34%",
                barGap: "-100%",
                data: [scaleMin],
                itemStyle: { color: "transparent" },
                silent: true,
                animation: false,
              },
            ]
          : []),
        // The bullet itself — a dark bar clearly riding over the bands.
        {
          type: "bar" as const,
          stack: "bullet",
          barWidth: "34%",
          barGap: "-100%",
          data: [clamped - scaleMin],
          itemStyle: {
            color: palette.charcoal,
            borderRadius: [3, 3, 3, 3],
            shadowBlur: 3,
            shadowColor: "rgba(15,23,42,0.25)",
            shadowOffsetY: 1,
          },
          animationDuration: 800,
          animationEasing: "cubicOut" as const,
          z: 10,
        },
        ...(clampedTarget !== null
          ? [
              {
                type: "scatter" as const,
                data: [[clampedTarget, 0]],
                symbol: "rect",
                symbolSize: [3, height * 0.55],
                itemStyle: {
                  color: ui.red,
                  borderColor: palette.white,
                  borderWidth: 1,
                },
                z: 20,
                silent: true,
              },
            ]
          : []),
      ],
      tooltip: { show: false },
    }),
    [effectiveBands, boundaries, clamped, clampedTarget, height, scaleMin, scaleMax],
  );

  return (
    <ReactECharts
      option={option}
      theme={ECHARTS_THEME_NAME}
      style={{ width, height }}
      opts={{ renderer: "svg" }}
      notMerge
    />
  );
}
