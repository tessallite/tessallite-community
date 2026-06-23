import { useMemo } from "react";
import ReactECharts from "echarts-for-react";
import type { KpiTrendPoint } from "../../api/types";
import { ui, palette } from "../../theme/tokens";
import { ECHARTS_THEME_NAME } from "../../theme/echartsTheme";

interface Props {
  data: KpiTrendPoint[];
  trend: number | null;
  width?: number;
  height?: number;
}

const TREND_COLORS: Record<number, string> = {
  1: ui.green,
  0: "#9e9e9e",
  [-1]: ui.red,
};

export default function Sparkline({
  data,
  trend,
  width = 90,
  height = 36,
}: Props) {
  const color = TREND_COLORS[trend ?? 0] ?? "#9e9e9e";

  const seriesData = useMemo(
    () => data.map((d) => d.value),
    [data],
  );

  const hasData = useMemo(
    () => seriesData.some((v) => v !== null && v !== undefined),
    [seriesData],
  );

  const option = useMemo(
    () => ({
      grid: { left: 2, right: 4, top: 6, bottom: 4 },
      xAxis: {
        type: "category" as const,
        show: false,
        data: data.map((_, i) => i),
      },
      yAxis: { type: "value" as const, show: false },
      series: [
        {
          type: "line" as const,
          data: seriesData,
          symbol: "none",
          smooth: 0.35,
          lineStyle: { width: 2.5, color },
          areaStyle: {
            color: {
              type: "linear" as const,
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: color + "40" },
                { offset: 0.5, color: color + "15" },
                { offset: 1, color: color + "05" },
              ],
            },
          },
          animationDuration: 800,
          animationEasing: "cubicOut" as const,
        },
      ],
      tooltip: { show: false },
    }),
    [data, seriesData, color],
  );

  if (data.length < 2 || !hasData) return null;

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
