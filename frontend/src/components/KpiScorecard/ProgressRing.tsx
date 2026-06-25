import { useMemo } from "react";
import ReactECharts from "echarts-for-react";
import type { KpiThresholdBand } from "../../api/types";
import { ui, palette } from "../../theme/tokens";
import { ECHARTS_THEME_NAME } from "../../theme/echartsTheme";
import { useT } from "../../i18n";
import { deriveScale } from "./chartUtils";
import { bandsToAxisLine, statusColor } from "./GaugeChart";

interface Props {
  value: number | null;
  bands?: KpiThresholdBand[];
  size?: number;
  showDetail?: boolean;
}

/**
 * Radial progress ring (new presentation type). The arc fills from the scale
 * floor to the value's position on the band scale and is coloured by the band
 * the value lands in, so the ring colour agrees with the status badge. The
 * centre shows the value's percent of the full scale.
 */
export default function ProgressRing({
  value,
  bands,
  size = 130,
  showDetail = true,
}: Props) {
  const t = useT();
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
  const range = scaleMax - scaleMin || 1;
  const axisLine = useMemo(
    () => bandsToAxisLine(effectiveBands, scaleMin, scaleMax),
    [effectiveBands, scaleMin, scaleMax],
  );

  const clamped =
    value !== null ? Math.max(scaleMin, Math.min(scaleMax, value)) : scaleMin;
  const normalised = (clamped - scaleMin) / range;
  const arcColor = value !== null ? statusColor(normalised, axisLine) : "#9e9e9e";
  const pct = Math.round(normalised * 100);

  const ringWidth = Math.max(8, Math.round(size * 0.09));
  const detailFontSize = Math.max(18, Math.round(size * 0.17));

  const option = useMemo(
    () => ({
      series: [
        {
          type: "gauge",
          startAngle: 90,
          endAngle: -270,
          radius: "90%",
          center: ["50%", "50%"],
          min: scaleMin,
          max: scaleMax,
          pointer: { show: false },
          progress: {
            show: true,
            overlap: false,
            roundCap: true,
            clip: false,
            width: ringWidth,
            itemStyle: { color: arcColor },
          },
          axisLine: {
            lineStyle: {
              width: ringWidth,
              color: [[1, "rgba(148,163,184,0.22)"]],
            },
          },
          splitLine: { show: false },
          axisTick: { show: false },
          axisLabel: { show: false },
          anchor: { show: false },
          detail: {
            show: showDetail,
            valueAnimation: true,
            offsetCenter: [0, 0],
            formatter: () => `${pct}%`,
            color: arcColor,
            fontSize: detailFontSize,
            fontWeight: 800,
            fontFamily: "'Inter', system-ui, sans-serif",
          },
          data: [{ value: value !== null ? clamped : scaleMin }],
          animationDuration: 1100,
          animationEasing: "cubicOut",
        },
      ],
    }),
    [
      arcColor,
      clamped,
      detailFontSize,
      pct,
      ringWidth,
      scaleMin,
      scaleMax,
      showDetail,
      value,
    ],
  );

  return (
    <ReactECharts
      option={option}
      theme={ECHARTS_THEME_NAME}
      style={{ width: size, height: size }}
      opts={{ renderer: "svg" }}
      notMerge
    />
  );
}
