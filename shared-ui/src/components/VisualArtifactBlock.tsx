import { useEffect, useMemo, useRef } from "react";
import { Box, Stack, Typography } from "@mui/material";
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
import { LegacyGridContainLabel } from "echarts/features";
import { SVGRenderer } from "echarts/renderers";
import type { EChartsCoreOption } from "echarts/core";
import { ErrorBoundary } from "./ErrorBoundary";
import { resolveEchartsThemeName } from "../utils/echartsTheme";
import { useCompactChartHeight } from "../utils/chartLayout";
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
  LegacyGridContainLabel,
  SVGRenderer,
]);

// Bug-5959: chart titles and export-button labels are localized via the
// shared chat i18n table, so the pure chart-option builders below need the
// translate function threaded through as a parameter (they run outside
// React and cannot call useChatContext() themselves).
type TFn = (key: string, params?: Record<string, string | number>) => string;

export interface VisualArtifact {
  kind: "tessallite.visual.v1";
  renderer: "echarts";
  chart_type: string | null;
  columns: string[];
  rows: Record<string, unknown>[];
  palette?: string;
  size?: "sm" | "md" | "lg";
  include_table?: boolean;
  legacy_html?: string;
}

export function parseVisualArtifact(value: string | null | undefined): VisualArtifact | null {
  if (!value || !value.trim().startsWith("{")) return null;
  try {
    const parsed = JSON.parse(value) as Partial<VisualArtifact>;
    if (
      parsed.kind === "tessallite.visual.v1" &&
      parsed.renderer === "echarts" &&
      Array.isArray(parsed.columns) &&
      Array.isArray(parsed.rows)
    ) {
      return parsed as VisualArtifact;
    }
  } catch {
    return null;
  }
  return null;
}

export function VisualArtifactBlock({
  artifact,
  echartsTheme,
  heightOverride,
  compact = false,
}: {
  artifact: VisualArtifact;
  echartsTheme?: Record<string, unknown>;
  /**
   * Replaces the computed canvas height. Used by the maximised view, which
   * needs the chart to fill the dialog rather than sit at its inline size.
   * The existing ResizeObserver re-lays the chart out when this changes.
  */
  heightOverride?: number | string;
  compact?: boolean;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const { t } = useChatContext();

  // Bug-6555 — resolve the theme to a stable per-object global name (shared
  // WeakMap resolver) so distinct themes never collide and identical themes
  // never leak the ECharts registry. Same resolver as ChartBlock; see
  // utils/echartsTheme.ts. (Previously this registered under the shared global
  // name "tessallite", which collided across instances with different themes.)
  const themeName = resolveEchartsThemeName(echartsTheme);

  // Bug-9921 — the compact chart height is derived from the pane's visible
  // height (see utils/chartLayout.ts), not a literal, so it adapts as the
  // Excel task pane is resized. No-op when not compact.
  const compactHeight = useCompactChartHeight(compact);

  const option = useMemo(
    () => buildEchartsOption(artifact, t, compact),
    [artifact, t, compact],
  );

  useEffect(() => {
    if (!containerRef.current || !option) return;
    const chart = chartRef.current ?? echarts.init(
      containerRef.current,
      themeName,
      { renderer: "svg" },
    );
    chartRef.current = chart;
    chart.setOption(option, true);
    if (typeof ResizeObserver === "undefined") {
      const resize = () => chart.resize();
      window.addEventListener("resize", resize);
      return () => window.removeEventListener("resize", resize);
    }
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(containerRef.current);
    return () => observer.disconnect();
  }, [option]);

  useEffect(() => {
    return () => {
      chartRef.current?.dispose();
      chartRef.current = null;
    };
  }, []);

  if (artifact.chart_type === "kpi") {
    return <KpiCards artifact={artifact} />;
  }

  if (!option) return null;

  return (
    <ErrorBoundary>
      <Box
        sx={{
          mt: compact ? 0 : 1,
          border: compact ? 0 : 1,
          borderColor: "divider",
          borderRadius: compact ? 0 : 1,
          bgcolor: "background.paper",
          minWidth: 0,
        }}
      >
        {!compact && (
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
            <Typography variant="caption" color="text.secondary" fontWeight={600}>
              {chartTitle(artifact, t)}
            </Typography>
          </Box>
        )}
        <Box
          ref={containerRef}
          role="img"
          aria-label={t("chart.ariaLabel", { title: chartTitle(artifact, t) })}
          sx={{
            height: heightOverride ?? (compact ? compactHeight : chartHeight(artifact)),
            width: "100%",
            minWidth: 0,
          }}
        />
      </Box>
    </ErrorBoundary>
  );
}

function KpiCards({ artifact }: { artifact: VisualArtifact }) {
  const columns = artifact.columns;
  const row = artifact.rows[0] ?? {};
  const numeric = columns.filter((col) => toNumber(row[col]) !== null);
  const cards = numeric.length > 0 ? numeric : columns.slice(-1);
  return (
    <Stack direction="row" flexWrap="wrap" gap={1} sx={{ mt: 1 }}>
      {cards.map((col) => (
        <Box
          key={col}
          sx={{
            flex: "1 1 180px",
            minWidth: 160,
            border: 1,
            borderColor: "divider",
            borderRadius: 1,
            p: 1.5,
            bgcolor: "background.paper",
            backgroundImage: "linear-gradient(135deg, rgba(0,108,53,0.10), transparent 58%)",
          }}
        >
          <Typography variant="caption" color="text.secondary" fontWeight={700}>
            {prettify(col)}
          </Typography>
          <Typography sx={{ mt: 0.5, fontSize: 30, lineHeight: 1.05, fontWeight: 750 }}>
            {formatValue(row[col])}
          </Typography>
        </Box>
      ))}
    </Stack>
  );
}

function buildEchartsOption(artifact: VisualArtifact, t: TFn, compact: boolean): EChartsCoreOption | null {
  const chartType = artifact.chart_type;
  if (!chartType || chartType === "kpi" || artifact.rows.length === 0) return null;

  if (chartType === "pie") return buildPieOption(artifact, t);
  if (chartType === "multi_line") return buildLongSeriesOption(artifact, "line", false, t, compact);
  if (chartType === "stacked_bar" && artifact.columns.length >= 3) {
    return buildLongSeriesOption(artifact, "bar", true, t, compact);
  }

  const xColumn = artifact.columns[0];
  if (!xColumn) return null;
  const numericColumns = artifact.columns.slice(1).filter((col) =>
    artifact.rows.some((row) => toNumber(row[col]) !== null),
  );
  if (numericColumns.length === 0) return null;

  const labels = artifact.rows.map((row, idx) =>
    String(row[xColumn] ?? t("chart.rowLabel", { n: String(idx + 1) })),
  );
  const kind = chartType === "h_bar" ? "bar" : chartType === "line" || chartType === "multi_line_wide" ? "line" : "bar";
  const horizontal = chartType === "h_bar";
  const dualAxis = kind === "line" && needsDualAxis(artifact.rows, numericColumns);
  const yAxes = dualAxis
    ? numericColumns.map((col, index) => ({
        type: "value",
        name: prettify(col),
        position: index === 0 ? "left" : "right",
        alignTicks: true,
        axisLabel: { fontSize: 11 },
      }))
    : [{ type: "value", axisLabel: { fontSize: 11 } }];

  return {
    animationDuration: 650,
    tooltip: { trigger: "axis", confine: true },
    legend: { type: "scroll", top: 2 },
    toolbox: { right: 8, top: 0, feature: { saveAsImage: { title: t("chart.exportTitle") } } },
    // F-037-02: expose a screen-reader description of the chart's type/series/values.
    aria: { enabled: true, label: { description: t("chart.ariaDescription") } },
    grid: {
      top: 54,
      left: horizontal ? 12 : 20,
      right: dualAxis ? 56 : 24,
      // Bug-9921: compact drops the visible dataZoom slider (below), so it
      // does not need the extra bottom margin reserved for that slider.
      bottom: labels.length > 8 && !horizontal ? (compact ? 48 : 72) : 38,
      containLabel: true,
    },
    xAxis: horizontal
      ? { type: "value", axisLabel: { fontSize: 11 } }
      : {
          type: "category",
          data: labels,
          axisLabel: {
            fontSize: 11,
            rotate: labels.length > 8 ? 35 : 0,
            interval: labels.length > 30 ? "auto" : 0,
          },
          axisTick: { alignWithLabel: true },
        },
    yAxis: horizontal
      ? {
          type: "category",
          data: [...labels].reverse(),
          axisLabel: { fontSize: 11, width: 150, overflow: "truncate" },
        }
      : yAxes,
    // Bug-9921: the compact task pane is short on vertical room and the
    // owner flagged the slider as consuming a large share of a small chart,
    // so compact keeps the "inside" (wheel/pinch/drag) zoom but drops the
    // visible slider strip via ECharts' own dataZoom config rather than
    // shrinking it after the fact.
    dataZoom: labels.length > 12 && !horizontal
      ? compact
        ? [{ type: "inside" }]
        : [{ type: "slider", height: 18, bottom: 12 }, { type: "inside" }]
      : [],
    series: numericColumns.map((col, index) => ({
      type: kind,
      name: prettify(col),
      data: horizontal
        ? [...artifact.rows].reverse().map((row) => toNumber(row[col]))
        : artifact.rows.map((row) => toNumber(row[col])),
      yAxisIndex: dualAxis ? index : undefined,
      smooth: kind === "line",
      showSymbol: labels.length <= 24,
      barMaxWidth: horizontal ? 28 : 44,
      emphasis: { focus: "series" },
      areaStyle: kind === "line" && numericColumns.length === 1 ? { opacity: 0.08 } : undefined,
    })),
  };
}

function buildPieOption(artifact: VisualArtifact, t: TFn): EChartsCoreOption | null {
  const labelCol = artifact.columns[0];
  const valueCol = firstNumericColumn(artifact, artifact.columns.slice(1));
  if (!labelCol || !valueCol) return null;
  return {
    animationDuration: 650,
    tooltip: { trigger: "item", confine: true },
    legend: { type: "scroll", top: 2 },
    toolbox: { right: 8, top: 0, feature: { saveAsImage: { title: t("chart.exportTitle") } } },
    // F-037-02: expose a screen-reader description of the chart's type/series/values.
    aria: { enabled: true, label: { description: t("chart.ariaDescription") } },
    series: [{
      type: "pie",
      name: prettify(valueCol),
      radius: ["42%", "72%"],
      center: ["50%", "56%"],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 4, borderWidth: 2 },
      label: { formatter: "{b}", fontSize: 11 },
      data: artifact.rows.map((row) => ({
        name: String(row[labelCol] ?? ""),
        value: toNumber(row[valueCol]) ?? 0,
      })),
    }],
  };
}

function buildLongSeriesOption(
  artifact: VisualArtifact,
  kind: "line" | "bar",
  stacked: boolean,
  t: TFn,
  compact: boolean,
): EChartsCoreOption | null {
  const [xCol, seriesCol] = artifact.columns;
  const valueCol = firstNumericColumn(artifact, artifact.columns.slice(2));
  if (!xCol || !seriesCol || !valueCol) return null;

  const labels = Array.from(new Set(artifact.rows.map((row) => String(row[xCol] ?? ""))));
  const seriesNames = Array.from(new Set(artifact.rows.map((row) => String(row[seriesCol] ?? ""))));
  const byLabel = new Map<string, Map<string, number | null>>();
  artifact.rows.forEach((row) => {
    const label = String(row[xCol] ?? "");
    const series = String(row[seriesCol] ?? "");
    if (!byLabel.has(label)) byLabel.set(label, new Map());
    byLabel.get(label)!.set(series, toNumber(row[valueCol]));
  });

  return {
    animationDuration: 650,
    tooltip: { trigger: "axis", confine: true },
    legend: { type: "scroll", top: 2 },
    toolbox: { right: 8, top: 0, feature: { saveAsImage: { title: t("chart.exportTitle") } } },
    // F-037-02: expose a screen-reader description of the chart's type/series/values.
    aria: { enabled: true, label: { description: t("chart.ariaDescription") } },
    grid: {
      top: 54,
      left: 20,
      right: 24,
      // Bug-9921: compact drops the visible dataZoom slider (below), so it
      // does not need the extra bottom margin reserved for that slider.
      bottom: labels.length > 8 ? (compact ? 48 : 72) : 38,
      containLabel: true,
    },
    xAxis: {
      type: "category",
      data: labels,
      axisLabel: { fontSize: 11, rotate: labels.length > 8 ? 35 : 0 },
      axisTick: { alignWithLabel: true },
    },
    yAxis: { type: "value", axisLabel: { fontSize: 11 } },
    // Bug-9921: same compact dataZoom treatment as buildEchartsOption above
    // — keep "inside" zoom, drop the visible slider strip.
    dataZoom: labels.length > 12
      ? compact
        ? [{ type: "inside" }]
        : [{ type: "slider", height: 18, bottom: 12 }, { type: "inside" }]
      : [],
    series: seriesNames.map((name) => ({
      type: kind,
      name,
      stack: stacked ? "total" : undefined,
      data: labels.map((label) => byLabel.get(label)?.get(name) ?? null),
      smooth: kind === "line",
      showSymbol: kind === "line" && labels.length <= 24,
      barMaxWidth: 44,
      emphasis: { focus: "series" },
    })),
  };
}

function firstNumericColumn(artifact: VisualArtifact, columns: string[]) {
  return columns.find((col) => artifact.rows.some((row) => toNumber(row[col]) !== null));
}

function needsDualAxis(rows: Record<string, unknown>[], columns: string[]) {
  const maxima = columns.map((col) => {
    const values = rows.map((row) => Math.abs(toNumber(row[col]) ?? 0)).filter((v) => v > 0);
    return Math.max(...values, 0);
  }).filter((v) => v > 0);
  if (maxima.length < 2) return false;
  return Math.max(...maxima) / Math.min(...maxima) >= 100;
}

function chartTitle(artifact: VisualArtifact, t: TFn) {
  const numeric = artifact.columns.slice(1).filter((col) =>
    artifact.rows.some((row) => toNumber(row[col]) !== null),
  );
  if (artifact.chart_type === "pie") return t("chart.distribution");
  if (artifact.chart_type?.includes("line")) return t("chart.trend");
  return numeric.map(prettify).join(", ") || t("chart.result");
}

function chartHeight(artifact: VisualArtifact) {
  const base = artifact.size === "sm" ? 280 : artifact.size === "lg" ? 440 : 360;
  if (artifact.chart_type === "h_bar") {
    return Math.min(620, Math.max(240, artifact.rows.length * 32 + 72));
  }
  return { xs: Math.max(300, base - 40), sm: base };
}

function toNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value !== "string") return null;
  const cleaned = value.replace(/[$,%\s,]/g, "");
  if (!cleaned) return null;
  const parsed = Number(cleaned);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatValue(value: unknown) {
  const num = toNumber(value);
  if (num === null) return value == null ? "" : String(value);
  return num.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function prettify(value: string) {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}
