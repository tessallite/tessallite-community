import { useEffect, useMemo, useRef } from "react";
import { Box, Typography } from "@mui/material";
import * as echarts from "echarts/core";
import { BarChart, LineChart, PieChart } from "echarts/charts";
import {
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  ToolboxComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsCoreOption } from "echarts/core";
import { ErrorBoundary } from "./ErrorBoundary";
import { buildAutoChartSpec } from "../utils/chartSpec";
import { resolveEchartsThemeName } from "../utils/echartsTheme";
import { useChatContext } from "../providers/ChatProvider";

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  ToolboxComponent,
  TooltipComponent,
  CanvasRenderer,
]);

const THEME_PRIMARY = "#4ea397";

interface ChartBlockProps {
  rows: Record<string, unknown>[];
  echartsTheme?: Record<string, unknown>;
}

export function ChartBlock({ rows, echartsTheme }: ChartBlockProps) {
  const { t } = useChatContext();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const spec = useMemo(() => buildAutoChartSpec(rows), [rows]);

  // Bug-6555 — resolve the theme to a stable per-object global name (shared
  // WeakMap resolver) so distinct themes never collide and identical themes
  // never leak the registry. See utils/echartsTheme.ts.
  const themeName = resolveEchartsThemeName(echartsTheme);

  const option = useMemo<EChartsCoreOption | null>(() => {
    if (!spec || spec.kind === "metric") return null;

    const base = {
      animationDuration: 650,
      // F-037-02: enable ECharts' built-in ARIA so the canvas chart exposes a
      // screen-reader description of its type, series, and values instead of
      // being an opaque image. The label option gives the description a
      // localized lead-in; ECharts appends the data summary.
      aria: {
        enabled: true,
        label: { description: t("chart.ariaDescription") },
      },
      tooltip: {
        trigger: spec.kind === "pie" ? "item" : "axis",
        confine: true,
      },
      legend: { top: 0, type: "scroll" },
      toolbox: { right: 0, feature: { saveAsImage: { title: t("chart.exportTitle") } } },
    } satisfies EChartsCoreOption;

    if (spec.kind === "pie") {
      const series = spec.series[0];
      return {
        ...base,
        series: [
          {
            type: "pie",
            name: series?.name,
            radius: ["42%", "72%"],
            center: ["50%", "56%"],
            avoidLabelOverlap: true,
            itemStyle: { borderRadius: 4, borderWidth: 2 },
            label: { formatter: "{b}" },
            data: spec.labels.map((label, index) => ({
              name: label,
              value: series?.values[index] ?? 0,
            })),
          },
        ],
      };
    }

    if (spec.kind === "hbar") {
      return {
        ...base,
        grid: {
          top: 32,
          left: 8,
          right: 24,
          bottom: 16,
          containLabel: true,
        },
        yAxis: {
          type: "category",
          data: [...spec.labels].reverse(),
          axisLabel: { fontSize: 11, width: 140, overflow: "truncate" },
          axisTick: { alignWithLabel: true },
        },
        xAxis: { type: "value" },
        series: spec.series.map((s) => ({
          type: "bar",
          name: s.name,
          data: [...s.values].reverse(),
          barMaxWidth: 28,
          emphasis: { focus: "series" },
        })),
      };
    }

    return {
      ...base,
      grid: {
        top: 52,
        left: 16,
        right: 24,
        bottom: spec.labels.length > 8 ? 72 : 40,
        containLabel: true,
      },
      xAxis: {
        type: "category",
        data: spec.labels,
        axisLabel: {
          rotate: spec.labels.length > 8 ? 35 : 0,
          interval: spec.labels.length > 30 ? "auto" : 0,
        },
        axisTick: { alignWithLabel: true },
      },
      yAxis: { type: "value" },
      dataZoom:
        spec.labels.length > 8
          ? [
              { type: "slider", height: 18, bottom: 12 },
              { type: "inside" },
            ]
          : [],
      series: spec.series.map((s) => ({
        type: spec.kind,
        name: s.name,
        data: s.values,
        smooth: spec.kind === "line",
        showSymbol: spec.labels.length <= 24,
        connectNulls: false,
        barMaxWidth: 44,
        areaStyle:
          spec.kind === "line" && spec.series.length === 1
            ? { opacity: 0.08 }
            : undefined,
        emphasis: { focus: "series" },
      })),
    };
  }, [spec, t]);

  useEffect(() => {
    if (!containerRef.current || !option) return;

    const chart =
      chartRef.current ??
      echarts.init(containerRef.current, themeName);
    chartRef.current = chart;
    chart.setOption(option, true);

    const resizeObserver = new ResizeObserver(() => chart.resize());
    resizeObserver.observe(containerRef.current);

    return () => resizeObserver.disconnect();
  }, [option]);

  useEffect(() => {
    return () => {
      chartRef.current?.dispose();
      chartRef.current = null;
    };
  }, []);

  if (!spec) return null;

  if (spec.kind === "metric") {
    const value = spec.series[0]?.values[0] ?? 0;
    return (
      <ErrorBoundary>
        <Box
          sx={{
            mt: 1.5,
            p: 2,
            border: 1,
            borderColor: "divider",
            borderRadius: 1,
            bgcolor: "background.paper",
            backgroundImage: `linear-gradient(135deg, ${THEME_PRIMARY}22, transparent 54%)`,
            minWidth: 0,
          }}
        >
          <Typography
            variant="caption"
            color="text.secondary"
            fontWeight={700}
            sx={{ textTransform: "uppercase" }}
          >
            {spec.labels[0]}
          </Typography>
          <Typography
            sx={{
              mt: 0.5,
              fontSize: { xs: 30, sm: 38 },
              lineHeight: 1.05,
              fontWeight: 750,
              color: "text.primary",
              overflowWrap: "anywhere",
            }}
          >
            {formatMetricValue(value)}
          </Typography>
        </Box>
      </ErrorBoundary>
    );
  }

  if (!option) return null;

  return (
    <ErrorBoundary>
      <Box
        sx={{
          mt: 1.5,
          border: 1,
          borderColor: "divider",
          borderRadius: 1,
          bgcolor: "background.paper",
        }}
      >
        <Box
          sx={{
            px: 1.5,
            pt: 1.25,
            pb: 0.25,
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
            gap: 1,
          }}
        >
          <Typography
            variant="caption"
            color="text.secondary"
            fontWeight={600}
          >
            {spec.kind === "pie"
              ? t("chart.distribution")
              : spec.kind === "line"
                ? t("chart.trend")
                : prettifyDimension(spec.dimension)}
          </Typography>
          {spec.truncated && (
            <Typography variant="caption" color="text.disabled">
              {t("chart.truncated", {
                count: String(spec.truncated.shown),
                total: String(spec.truncated.total),
              })}
            </Typography>
          )}
        </Box>
        <Box
          ref={containerRef}
          role="img"
          aria-label={t("chart.ariaLabel", {
            title:
              spec.kind === "pie"
                ? t("chart.distribution")
                : spec.kind === "line"
                  ? t("chart.trend")
                  : prettifyDimension(spec.dimension),
          })}
          sx={{
            height:
              spec.kind === "hbar"
                ? Math.min(
                    600,
                    Math.max(200, spec.labels.length * 32 + 48),
                  )
                : { xs: 300, sm: 360 },
            width: "100%",
            minWidth: 0,
          }}
        />
      </Box>
    </ErrorBoundary>
  );
}

function formatMetricValue(value: number): string {
  return new Intl.NumberFormat(undefined, {
    maximumFractionDigits: Number.isInteger(value) ? 0 : 2,
  }).format(value);
}

function prettifyDimension(dim: string): string {
  return dim
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
