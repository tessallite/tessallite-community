import { useMemo } from "react";
import ReactECharts from "echarts-for-react";
import type { KpiThresholdBand } from "../../api/types";
import { ui, palette } from "../../theme/tokens";
import { ECHARTS_THEME_NAME } from "../../theme/echartsTheme";
import { useT } from "../../i18n";
import { deriveScale } from "./chartUtils";

interface Props {
  value: number | null;
  label?: string;
  bands?: KpiThresholdBand[];
  width?: number;
  height?: number;
}

function fmtBoundary(v: number): string {
  if (Number.isInteger(v)) return String(v);
  return Math.abs(v) < 10 ? v.toFixed(2).replace(/0$/, "") : v.toFixed(1);
}

export default function RagBar({
  value,
  bands,
  width = 220,
  height = 40,
}: Props) {
  const t = useT();
  // F-017-18 (Bug-852 regression): i18n-computed default band labels.
  const defaultBands = useMemo<KpiThresholdBand[]>(
    () => [
      { label: t("kpiScorecard.poor"), color: ui.red, min: 0, max: 33 },
      { label: t("kpiScorecard.warning"), color: "#ed6c02", min: 33, max: 67 },
      { label: t("kpiScorecard.good"), color: ui.green, min: 67, max: 100 },
    ],
    [t],
  );
  const effectiveBands = bands && bands.length > 0 ? bands : defaultBands;
  const { scaleMin, scaleMax } = useMemo(
    () => deriveScale(effectiveBands),
    [effectiveBands],
  );
  const clamped =
    value !== null
      ? Math.max(scaleMin, Math.min(scaleMax, value))
      : null;
  const boundaries = useMemo(() => {
    const vals = new Set<number>();
    for (const b of effectiveBands) {
      vals.add(b.min ?? scaleMin);
      vals.add(b.max ?? scaleMax);
    }
    return [...vals]
      .filter((v) => v >= scaleMin && v <= scaleMax)
      .sort((a, b) => a - b);
  }, [effectiveBands, scaleMin, scaleMax]);

  const option = useMemo(
    () => ({
      grid: { left: 6, right: 6, top: 2, bottom: 18 },
      xAxis: {
        type: "value" as const,
        min: scaleMin,
        max: scaleMax,
        show: true,
        axisLine: { show: false },
        axisTick: {
          show: true,
          length: 3,
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
        ...effectiveBands.map((b, i) => ({
          type: "bar" as const,
          stack: "rag",
          barWidth: "90%",
          data: [(b.max ?? scaleMax) - (b.min ?? scaleMin)],
          itemStyle: {
            color: b.color,
            borderRadius:
              i === 0
                ? [5, 0, 0, 5]
                : i === effectiveBands.length - 1
                  ? [0, 5, 5, 0]
                  : 0,
          },
          silent: true,
          animation: false,
        })),
        ...(clamped !== null
          ? [
              {
                type: "scatter" as const,
                data: [[clamped, 0]],
                symbol: "diamond",
                symbolSize: 14,
                itemStyle: {
                  color: palette.charcoal,
                  borderColor: palette.white,
                  borderWidth: 2,
                  shadowBlur: 4,
                  shadowColor: "rgba(0,0,0,0.15)",
                },
                z: 20,
                silent: true,
                animationDuration: 600,
              },
            ]
          : []),
      ],
      tooltip: { show: false },
    }),
    [effectiveBands, boundaries, clamped, scaleMin, scaleMax],
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
