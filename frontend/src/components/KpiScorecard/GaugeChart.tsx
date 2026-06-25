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
  size?: number;
  showDetail?: boolean;
}

export function bandsToAxisLine(
  bands: KpiThresholdBand[],
  scaleMin: number,
  scaleMax: number,
): [number, string][] {
  const range = scaleMax - scaleMin;
  if (range <= 0) return [[1, ui.green]];
  const sorted = [...bands].sort(
    (a, b) => (a.min ?? scaleMin) - (b.min ?? scaleMin),
  );
  return sorted.map((b) => [
    Math.min(1, Math.max(0, ((b.max ?? scaleMax) - scaleMin) / range)),
    b.color,
  ]);
}

export function statusColor(
  normalisedValue: number,
  axisLine: [number, string][],
): string {
  // F-017-06: bands use the backend [min, max) convention — a value exactly on
  // a band's upper boundary belongs to the NEXT band, not the one ending there.
  // Each axisLine stop is a band's normalised upper edge, so use strict `<` for
  // every stop except the last (the open-ended top band), which catches the
  // clamped at-max needle. This makes a value meeting the target read green,
  // agreeing with the status badge.
  for (let i = 0; i < axisLine.length; i++) {
    const [threshold, color] = axisLine[i];
    const isLast = i === axisLine.length - 1;
    if (isLast || normalisedValue < threshold) return color;
  }
  return axisLine[axisLine.length - 1]?.[1] ?? ui.green;
}

export default function GaugeChart({
  value,
  label,
  bands,
  size = 150,
  showDetail = true,
}: Props) {
  const t = useT();
  // F-017-18 (Bug-852 regression): default band labels are i18n-computed, not
  // hardcoded English. Labels feed the legend/tooltip; colours feed the arc.
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
  const range = scaleMax - scaleMin;

  const axisLine = useMemo(
    () => bandsToAxisLine(effectiveBands, scaleMin, scaleMax),
    [effectiveBands, scaleMin, scaleMax],
  );

  // Exact band-boundary score readings (not even gradations) so each band's
  // edge is labelled around the arc.
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

  const displayValue =
    value !== null ? Math.max(scaleMin, Math.min(scaleMax, value)) : scaleMin;
  const normalised = range > 0 ? (displayValue - scaleMin) / range : 0;

  const valColor = useMemo(
    () => (value !== null ? statusColor(normalised, axisLine) : "#9e9e9e"),
    [value, normalised, axisLine],
  );

  const cardScale = size <= 340;
  const arcWidth = cardScale ? Math.max(10, Math.round(size * 0.075)) : 16;
  const pointerWidth = cardScale ? Math.max(4, Math.round(size * 0.035)) : 7;
  const detailFontSize = cardScale ? Math.max(20, Math.round(size * 0.16)) : 26;
  const detailLineHeight = cardScale ? Math.round(detailFontSize * 1.12) : 32;

  const formattedValue =
    value !== null
      ? Number.isInteger(displayValue)
        ? String(displayValue)
        : displayValue.toFixed(1)
      : "—";

  // Place each band-boundary reading at its exact angular position around the
  // arc. The gauge sweeps startAngle 210deg -> endAngle -30deg (240deg span).
  const boundaryGraphics = useMemo(() => {
    if (range <= 0) return [];
    const chartH = size * (cardScale ? 0.66 : 0.72);
    const cx = size * 0.5;
    const cy = chartH * (cardScale ? 0.6 : 0.58);
    const rRef = Math.min(size, chartH) / 2;
    const r = (cardScale ? 0.84 : 0.88) * rRef;
    const rOuter = r + arcWidth * 0.5 + (cardScale ? 9 : 12);
    return boundaries.map((v) => {
      const n = (v - scaleMin) / range;
      const ang = ((210 - n * 240) * Math.PI) / 180;
      return {
        type: "text" as const,
        left: cx + rOuter * Math.cos(ang),
        top: cy - rOuter * Math.sin(ang),
        z: 50,
        style: {
          text: Number.isInteger(v)
            ? String(v)
            : Math.abs(v) < 10
              ? v.toFixed(2).replace(/0$/, "")
              : v.toFixed(1),
          textAlign: "center" as const,
          textVerticalAlign: "middle" as const,
          fontSize: cardScale ? 8.5 : 10,
          fontWeight: 600,
          fill: palette.textSecondary,
        },
      };
    });
  }, [boundaries, scaleMin, range, size, cardScale, arcWidth]);

  const option = useMemo(
    () => ({
      graphic: boundaryGraphics,
      series: [
        {
          type: "gauge",
          startAngle: 210,
          endAngle: -30,
          radius: cardScale ? "84%" : "88%",
          center: ["50%", cardScale ? "60%" : "58%"],
          min: scaleMin,
          max: scaleMax,
          splitNumber: cardScale ? 4 : 5,
          animationEasing: "elasticOut",
          animationDuration: 1500,
          pointer: {
            icon: "path://M12.8,0.7l12,40.1H0.7L12.8,0.7z",
            length: cardScale ? "46%" : "52%",
            width: pointerWidth,
            offsetCenter: [0, "-10%"],
            itemStyle: {
              color: valColor,
              shadowBlur: cardScale ? 2 : 6,
              shadowColor: "rgba(0,0,0,0.12)",
              shadowOffsetY: cardScale ? 1 : 2,
            },
          },
          // Progress fill is intentionally disabled: this gauge renders RAG
          // threshold bands on the axis line, and a single-colour progress arc
          // (drawn min -> value at the same width) overpaints those bands —
          // making a high value in the worst band colour the entire dial. The
          // needle/pointer indicates the value position over the visible bands.
          progress: {
            show: false,
          },
          axisLine: {
            roundCap: true,
            lineStyle: {
              width: arcWidth,
              color: axisLine,
            },
          },
          // The even-gradation axis ticks/labels are suppressed; band-boundary
          // readings are drawn exactly via the `graphic` overlay below (ECharts
          // gauge axes do not support custom tick positions).
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: { show: false },
          anchor: {
            show: true,
            showAbove: true,
            size: cardScale ? Math.max(8, Math.round(size * 0.06)) : 12,
            itemStyle: {
              borderWidth: cardScale ? 2 : 2.5,
              borderColor: valColor,
              shadowBlur: cardScale ? 1 : 4,
              shadowColor: "rgba(0,0,0,0.12)",
            },
          },
          detail: {
            show: showDetail,
            valueAnimation: true,
            offsetCenter: [0, cardScale ? "34%" : "38%"],
            formatter: `{big|${formattedValue}}`,
            rich: {
              big: {
                fontSize: detailFontSize,
                fontWeight: 800,
                fontFamily: "'Inter', system-ui, sans-serif",
                color: valColor,
                lineHeight: detailLineHeight,
              },
            },
          },
          data: [
            {
              value: value !== null ? displayValue : scaleMin,
              name: label ?? "",
              title: {
                show: Boolean(label),
                offsetCenter: [0, cardScale ? "78%" : "82%"],
                fontSize: 10,
                fontWeight: 600,
                color: palette.textSecondary,
              },
            },
          ],
        },
      ],
    }),
    [
      arcWidth,
      cardScale,
      detailFontSize,
      detailLineHeight,
      displayValue,
      value,
      axisLine,
      valColor,
      label,
      pointerWidth,
      showDetail,
      size,
      scaleMin,
      scaleMax,
      formattedValue,
      boundaryGraphics,
    ],
  );

  return (
    <ReactECharts
      option={option}
      theme={ECHARTS_THEME_NAME}
      style={{ width: size, height: cardScale ? size * 0.66 : size * 0.72 }}
      opts={{ renderer: "svg" }}
      notMerge
    />
  );
}
