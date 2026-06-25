import {
  Box,
  Chip,
  CircularProgress,
  Paper,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import WarningIcon from "@mui/icons-material/Warning";
import ErrorIcon from "@mui/icons-material/Error";
import HelpOutlineIcon from "@mui/icons-material/HelpOutline";
import TrendingUpIcon from "@mui/icons-material/TrendingUp";
import TrendingDownIcon from "@mui/icons-material/TrendingDown";
import TrendingFlatIcon from "@mui/icons-material/TrendingFlat";
import type { Kpi, KpiEvaluateResponse } from "../../api/types";
import { useT } from "../../i18n";
import { palette, ui } from "../../theme/tokens";
import KpiVisual from "./KpiVisual";
import Sparkline from "./Sparkline";
import { fitFontSize } from "./fitText";
import {
  resolveKpiDisplayStatus,
  localizeKpiLabel,
  type KpiDisplayStatus,
} from "./statusUtils";

function localizedBusinessSummary(
  tokens: Record<string, unknown> | undefined,
  t: (key: string, vars?: Record<string, string>) => string,
): string | null {
  if (!tokens) return null;
  const ft = tokens.formula_type as string | undefined;
  if (!ft) return null;
  const parts: string[] = [];
  const mName = (tokens.measure_name as string) ?? "";

  if (ft === "single_measure") {
    const agg = (tokens.aggregation as string) ?? "sum";
    const aggLabel = t(`kpiBusiness.agg${agg.charAt(0).toUpperCase()}${agg.slice(1)}`) || agg;
    parts.push(`${aggLabel} ${t("kpiBusiness.summaryOf")} ${mName}`);
  } else if (ft === "ratio") {
    const n = (tokens.numerator_name as string) ?? "";
    const d = (tokens.denominator_name as string) ?? "";
    parts.push(`${n} / ${d}`);
  } else if (ft === "count_records") {
    parts.push(t("kpiBusiness.summaryCountRecords"));
  } else if (ft === "count_distinct") {
    const dName = (tokens.dimension_name as string) ?? "";
    parts.push(`${t("kpiBusiness.summaryDistinctCount")} ${dName}`);
  } else if (ft === "share_rank") {
    const st = (tokens.share_type as string) ?? "share_of_total";
    parts.push(`${mName} (${t(`kpiBusiness.${st === "rank" ? "rank" : st === "top_n_contribution" ? "topN" : "sharePercent"}`)})`);
  } else if (ft === "exception_sla") {
    const sla = (tokens.sla_type as string) ?? "compliance_pct";
    parts.push(`${t("kpiBusiness.summarySlA")} (${t(`kpiBusiness.sla${sla === "compliance_pct" ? "CompliancePct" : sla === "exception_count" ? "BreachCount" : "Backlog"}`)})`);
  } else {
    parts.push(ft.replace(/_/g, " "));
  }

  const tcType = tokens.time_calc_type as string | undefined;
  if (tcType) {
    const tcGrain = (tokens.time_calc_grain as string) ?? "month";
    const tcPeriods = tokens.time_calc_periods as number | undefined;
    const tcLabels: Record<string, string> = {
      prior_period: t("kpiBusiness.tcPriorPeriod"),
      period_to_date: ((tokens.time_calc_period as string) ?? "YTD").toUpperCase(),
      trailing_sum: `${t("kpiBusiness.tcTrailing")} ${tcPeriods ?? ""} ${tcGrain}s`,
      moving_average: `${tcPeriods ?? ""}-${tcGrain} ${t("kpiBusiness.tcMovingAvg")}`,
      percentage_change: t("kpiBusiness.tcPctChange"),
      yoy_value: "YoY",
      yoy_growth_pct: "YoY %",
    };
    parts.push(tcLabels[tcType] ?? tcType.replace(/_/g, " "));
  }

  const twPreset = tokens.time_window_preset as string | undefined;
  if (twPreset) {
    parts.push(`${t("kpiBusiness.summaryFor")} ${twPreset.replace(/_/g, " ")}`);
  }

  const filterDims = tokens.filter_dimensions as string[] | undefined;
  if (filterDims && filterDims.length > 0) {
    parts.push(`${t("kpiBusiness.summaryWhere")} ${filterDims.join("; ")}`);
  }

  return parts.join(" | ");
}

interface Props {
  kpi: Kpi;
  evalData: KpiEvaluateResponse | null;
  loading: boolean;
}

const STATUS_ICONS: Record<KpiDisplayStatus, typeof CheckCircleIcon> = {
  1: CheckCircleIcon,
  0: WarningIcon,
  [-1]: ErrorIcon,
};

const STATUS_COLORS: Record<KpiDisplayStatus, string> = {
  1: ui.green,
  0: ui.goldDark,
  [-1]: ui.red,
};

const STATUS_BG: Record<KpiDisplayStatus, string> = {
  1: ui.greenBg,
  0: ui.goldBg,
  [-1]: ui.redBg,
};

const TABULAR_NUMS = { fontVariantNumeric: "tabular-nums" } as const;
const LABEL_STYLE = {
  color: palette.textSecondary,
  fontSize: 10,
  fontWeight: 700,
  textTransform: "uppercase",
  letterSpacing: 0,
  lineHeight: 1.2,
} as const;

// Every card body is the same height so cards in a row align. The visual
// region (gauge / bullet / rag) sits in a fixed-height slot regardless of
// presentation type.
const VISUAL_REGION_HEIGHT = 150;

function TrendArrow({
  trend,
  label,
}: {
  trend: number | null;
  label: string | null;
}) {
  const t = useT();
  if (trend === null || trend === undefined) return null;
  const config =
    trend > 0
      ? {
          Icon: TrendingUpIcon,
          color: ui.green,
          text: label || t("kpiScorecard.improving"),
        }
      : trend < 0
        ? {
            Icon: TrendingDownIcon,
            color: ui.red,
            text: label || t("kpiScorecard.declining"),
          }
        : {
            Icon: TrendingFlatIcon,
            color: ui.muted,
            text: label || t("kpiScorecard.flat"),
          };
  return (
    <Tooltip title={config.text}>
      <Box sx={{ display: "flex", alignItems: "center", gap: 0.25 }}>
        <config.Icon sx={{ fontSize: 16, color: config.color }} />
        {label && (
          <Typography
            variant="caption"
            sx={{ fontWeight: 600, fontSize: 11, color: config.color }}
          >
            {label}
          </Typography>
        )}
      </Box>
    </Tooltip>
  );
}

// Compact stat tile used three-up under the visual — identical dimensions so
// the indicator row is uniform across every card.
function StatTile({
  label,
  value,
  color,
  borderColor,
}: {
  label: string;
  value: string;
  color?: string;
  borderColor?: string;
}) {
  return (
    <Box
      sx={{
        p: 1.1,
        borderRadius: 1,
        bgcolor: "rgba(248,250,252,0.92)",
        border: `1px solid ${borderColor ?? `${palette.slateBorder}B3`}`,
        boxShadow: "inset 0 1px 0 rgba(255,255,255,0.8)",
        textAlign: "center",
        minHeight: 60,
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
      }}
    >
      <Typography variant="caption" sx={LABEL_STYLE}>
        {label}
      </Typography>
      <Typography
        variant="h6"
        fontWeight={700}
        sx={{
          color: color ?? palette.charcoal,
          mt: 0.4,
          // Fit long figures (e.g. a multi-million currency gap) on one line
          // rather than wrapping/overflowing the narrow tile.
          fontSize: `${fitFontSize(value, 1.05, 9, 0.72)}rem`,
          lineHeight: 1.2,
          whiteSpace: "nowrap",
          ...TABULAR_NUMS,
        }}
      >
        {value}
      </Typography>
    </Box>
  );
}

export default function KpiCard({ kpi, evalData, loading }: Props) {
  const t = useT();
  const deprecated = kpi.certification_status === "deprecated";
  const certified = kpi.certification_status === "certified";
  const businessSummary = localizedBusinessSummary(
    kpi.business_definition?._compiled?.summary_tokens as Record<string, unknown> | undefined,
    t,
  ) ?? kpi.business_definition?._compiled?.summary ?? null;

  const primaryValue =
    evalData?.formatted_value ??
    (evalData?.value !== null && evalData?.value !== undefined
      ? String(evalData.value)
      : null);
  const targetValue =
    evalData?.formatted_target ??
    evalData?.formatted_goal ??
    (evalData?.target !== null && evalData?.target !== undefined
      ? String(evalData.target)
      : evalData?.goal !== null && evalData?.goal !== undefined
        ? String(evalData.goal)
        : null);

  const statusNum = resolveKpiDisplayStatus(
    evalData?.status ?? null,
    evalData?.status_label ?? null,
  );
  // F-017-17 (Bug-853): prefer the backend-matched band colour so a custom band
  // colour flows through; fall back to the local RAG constant by status.
  const statusColor =
    evalData?.status_color ??
    (statusNum !== null ? STATUS_COLORS[statusNum] : ui.muted);

  // Bug-4255: a composite KPI is "degraded" when it scored from its working
  // inputs but at least one child KPI failed to evaluate. Surface a badge with
  // a tooltip naming the broken children so the failure is visible rather than
  // silently absorbed.
  const isDegraded =
    evalData?.composite_status === "degraded" &&
    Boolean(evalData?.errored_children?.length);
  const degradedTooltip = isDegraded
    ? t("kpiScorecard.degradedTooltip", {
        children: (evalData?.errored_children ?? [])
          .map((ec) =>
            t("kpiScorecard.erroredChild", {
              name: ec.kpi_name,
              reason: ec.error_reason,
            }),
          )
          .join("; "),
      })
    : "";

  const presentationType = kpi.presentation_type ?? "";
  // Every presentation type now renders a visual (Bug-5343: traffic_light is a
  // real lamp, no longer a no-op that collapsed to the bare number).
  const hasVisual = Boolean(presentationType);
  // Status-driven accent (Bug-5344): the prominent left rail must reflect the
  // KPI's status, not a hardcoded green that makes every card read "on track".
  const accentColor = statusNum !== null ? statusColor : palette.primaryGreen;

  const numericTarget = evalData?.target ?? evalData?.goal ?? null;
  const rawGoalDelta =
    evalData?.value !== null &&
    evalData?.value !== undefined &&
    numericTarget !== null &&
    numericTarget !== 0
      ? ((evalData.value - numericTarget) / Math.abs(numericTarget)) * 100
      : null;
  const goalDeltaPct =
    rawGoalDelta !== null
      ? kpi.direction === "lower_is_better"
        ? -rawGoalDelta
        : kpi.direction === "closer_is_better"
          ? -Math.abs(rawGoalDelta)
          : rawGoalDelta
      : null;
  const goalDeltaLabel =
    goalDeltaPct !== null
      ? t("kpiScorecard.fromGoal", {
          pct: `${goalDeltaPct > 0 ? "+" : ""}${goalDeltaPct.toFixed(1)}%`,
        })
      : null;

  const statusDisplayLabel =
    statusNum === 1
      ? t("kpiScorecard.statusGood")
      : statusNum === -1
        ? t("kpiScorecard.statusPoor")
        : statusNum === 0
          ? t("kpiScorecard.statusWarning")
          : t("kpiScorecard.statusUnknown");
  const StatusIcon =
    statusNum !== null && statusNum !== undefined
      ? (STATUS_ICONS[statusNum] ?? WarningIcon)
      : HelpOutlineIcon;

  const hasSparkline =
    evalData?.trend_series && evalData.trend_series.length > 1;

  return (
    <Paper
      variant="outlined"
      sx={{
        position: "relative",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        opacity: deprecated ? 0.55 : 1,
        borderColor: statusNum !== null ? `${statusColor}55` : palette.slateBorder,
        borderRadius: 1,
        overflow: "hidden",
        bgcolor: palette.white,
        boxShadow: "0 1px 2px rgba(15,23,42,0.04)",
        transition:
          "box-shadow 0.25s ease, border-color 0.25s ease, transform 0.2s ease",
        "&:hover": {
          borderColor: accentColor,
          boxShadow:
            "0 14px 34px rgba(15,23,42,0.12), 0 2px 8px rgba(15,23,42,0.06)",
          transform: "translateY(-1px)",
        },
      }}
    >
      <Box
        sx={{
          position: "absolute",
          left: 0,
          top: 0,
          bottom: 0,
          width: 5,
          bgcolor: accentColor,
          opacity: deprecated ? 0.45 : 1,
        }}
      />

      <Box
        sx={{
          p: 2,
          pl: 2.75,
          flex: 1,
          minHeight: 0,
          display: "flex",
          flexDirection: "column",
        }}
      >
        {/* Header */}
        <Stack
          direction="row"
          alignItems="flex-start"
          justifyContent="space-between"
          spacing={1.25}
          sx={{ mb: 1.25, minHeight: 34 }}
        >
          <Box sx={{ minWidth: 0, flex: 1 }}>
            <Typography
              variant="subtitle2"
              fontWeight={800}
              sx={{
                overflow: "hidden",
                display: "-webkit-box",
                WebkitLineClamp: 2,
                WebkitBoxOrient: "vertical",
                color: palette.charcoal,
                textDecoration: deprecated ? "line-through" : "none",
                fontSize: 14,
                lineHeight: 1.25,
              }}
              title={kpi.display_name || kpi.name}
            >
              {kpi.display_name || kpi.name}
            </Typography>
            {kpi.display_folder && (
              <Typography
                variant="caption"
                sx={{
                  display: "block",
                  color: palette.textSecondary,
                  fontSize: 10,
                  lineHeight: 1.2,
                  mt: 0.25,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {kpi.display_folder}
              </Typography>
            )}
            {businessSummary && (
              <Typography
                variant="caption"
                sx={{
                  display: "block",
                  color: palette.textSecondary,
                  fontSize: 10.5,
                  lineHeight: 1.25,
                  mt: 0.5,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
                title={businessSummary}
              >
                {businessSummary}
              </Typography>
            )}
          </Box>

          <Stack direction="row" spacing={0.5} flexShrink={0}>
            {isDegraded && (
              <Tooltip title={degradedTooltip}>
                <Chip
                  icon={<WarningIcon sx={{ fontSize: 13 }} />}
                  label={t("kpiScorecard.degradedBadge")}
                  size="small"
                  sx={{
                    fontSize: 9,
                    height: 22,
                    bgcolor: ui.goldBg,
                    color: ui.goldDark,
                    fontWeight: 700,
                    border: `1px solid ${ui.goldDark}30`,
                    borderRadius: 1,
                    "& .MuiChip-icon": { color: ui.goldDark },
                  }}
                />
              </Tooltip>
            )}
            {certified && (
              <Chip
                label={t("kpiScorecard.certified")}
                size="small"
                sx={{
                  fontSize: 9,
                  height: 22,
                  bgcolor: ui.greenBg,
                  color: ui.green,
                  fontWeight: 700,
                  letterSpacing: 0,
                  border: `1px solid ${ui.green}30`,
                  borderRadius: 1,
                }}
              />
            )}
            {deprecated && (
              <Chip
                label={t("kpiScorecard.deprecated")}
                size="small"
                sx={{
                  fontSize: 9,
                  height: 22,
                  bgcolor: ui.goldBg,
                  color: ui.goldDark,
                  fontWeight: 700,
                  border: `1px solid ${ui.goldDark}30`,
                  borderRadius: 1,
                }}
              />
            )}
          </Stack>
        </Stack>

        {kpi.description && (
          <Typography
            variant="caption"
            sx={{
              overflow: "hidden",
              display: "-webkit-box",
              WebkitLineClamp: 2,
              WebkitBoxOrient: "vertical",
              color: palette.textSecondary,
              fontSize: 11,
              lineHeight: 1.35,
              minHeight: 30,
              mb: 1.25,
            }}
          >
            {kpi.description}
          </Typography>
        )}

        {loading ? (
          <Box
            sx={{
              flex: 1,
              display: "flex",
              justifyContent: "center",
              alignItems: "center",
              minHeight: 170,
            }}
          >
            <CircularProgress size={28} sx={{ color: ui.green }} />
          </Box>
        ) : evalData ? (
          <Stack spacing={1.35} sx={{ flex: 1 }}>
            {/* Visual region — fixed height for every presentation type */}
            <Box
              sx={{
                display: "flex",
                justifyContent: "center",
                alignItems: "center",
                height: VISUAL_REGION_HEIGHT,
                overflow: "hidden",
                borderRadius: 1,
                bgcolor: "rgba(248,250,252,0.72)",
                border: `1px solid ${palette.slateBorder}80`,
              }}
            >
              {hasVisual ? (
                <KpiVisual
                  presentationType={kpi.presentation_type}
                  presentationMeta={kpi.presentation_meta}
                  evalData={evalData}
                  direction={kpi.direction}
                  size={210}
                  showDetail={false}
                />
              ) : (
                <Typography
                  variant="h3"
                  fontWeight={800}
                  sx={{
                    color: palette.charcoal,
                    fontSize: `${fitFontSize(primaryValue, 2.3, 7, 1.5)}rem`,
                    lineHeight: 1.1,
                    whiteSpace: "nowrap",
                    ...TABULAR_NUMS,
                  }}
                >
                  {primaryValue ?? "—"}
                </Typography>
              )}
            </Box>

            {/* Primary readout — the hero value appears here only when a chart
                occupies the visual region (otherwise it is the visual region). */}
            {hasVisual && (
              <Box sx={{ textAlign: "center" }}>
                <Typography variant="caption" sx={{ ...LABEL_STYLE }}>
                  {kpi.unit_label || t("kpiScorecard.currentScore")}
                </Typography>
                <Typography
                  variant="h4"
                  fontWeight={800}
                  sx={{
                    color: palette.charcoal,
                    // Calibrated to stay proportional to the compact visual and
                    // visually consistent across the grid: a moderate base that
                    // only eases down for long values, with a high floor so cards
                    // never jump between huge and tiny (Bug-5345).
                    fontSize: `${fitFontSize(primaryValue, 1.85, 8, 1.45)}rem`,
                    lineHeight: 1.1,
                    mt: 0.25,
                    whiteSpace: "nowrap",
                    ...TABULAR_NUMS,
                  }}
                >
                  {primaryValue ?? "—"}
                </Typography>
                {goalDeltaLabel && (
                  <Typography
                    variant="caption"
                    sx={{
                      display: "block",
                      color: palette.textSecondary,
                      fontSize: 11.5,
                      mt: 0.25,
                      ...TABULAR_NUMS,
                    }}
                  >
                    {goalDeltaLabel}
                  </Typography>
                )}
              </Box>
            )}

            {/* Uniform indicator tiles — Goal and Gap render when available */}
            {(targetValue || evalData.formatted_variance) && (
              <Box
                sx={{
                  display: "grid",
                  gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
                  gap: 1,
                }}
              >
                <StatTile
                  label={t("kpiScorecard.goalLabel")}
                  value={targetValue ?? "—"}
                />
                <StatTile
                  label={t("kpiScorecard.gapToGoal")}
                  value={evalData.formatted_variance ?? "—"}
                  color={statusColor}
                  borderColor={`${statusColor}33`}
                />
              </Box>
            )}

            {evalData.trend_pct !== null && evalData.trend_pct !== undefined && (
              <Box sx={{ display: "flex", justifyContent: "center" }}>
                <Chip
                  size="small"
                  label={`${evalData.trend_pct > 0 ? "+" : ""}${(evalData.trend_pct * 100).toFixed(1)}%`}
                  sx={{
                    fontSize: 10.5,
                    fontWeight: 700,
                    height: 22,
                    borderRadius: 1,
                    bgcolor: (evalData.trend ?? 0) >= 0 ? ui.greenBg : ui.redBg,
                    color: (evalData.trend ?? 0) >= 0 ? ui.green : ui.red,
                    ...TABULAR_NUMS,
                  }}
                />
              </Box>
            )}

            {/* Status footer + trend */}
            <Box sx={{ flex: 1, display: "flex", flexDirection: "column", justifyContent: "flex-end" }}>
              <Box
                sx={{
                  px: 1.5,
                  py: 0.85,
                  borderRadius: 999,
                  bgcolor: STATUS_BG[statusNum ?? 0] ?? ui.mutedBg,
                  border: `1px solid ${statusColor}40`,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  gap: 0.75,
                }}
              >
                <StatusIcon sx={{ fontSize: 17, color: statusColor }} />
                <Typography
                  variant="body2"
                  sx={{ color: palette.charcoal, fontSize: 12.5, overflowWrap: "anywhere" }}
                >
                  <Box component="span" sx={{ fontWeight: 700 }}>
                    {t("kpiScorecard.statusPrefix")}:
                  </Box>{" "}
                  {/* F-017-18: translate the backend's default-preset label;
                      custom band labels pass through verbatim. */}
                  {localizeKpiLabel(evalData.status_label, t) ?? statusDisplayLabel}
                </Typography>
                {evalData.trend !== null && evalData.trend !== undefined && (
                  <TrendArrow
                    trend={evalData.trend}
                    label={localizeKpiLabel(evalData.trend_label, t)}
                  />
                )}
              </Box>

              {hasSparkline && (
                <Box
                  sx={{
                    mt: 1,
                    height: 40,
                    display: "flex",
                    justifyContent: "flex-end",
                    alignItems: "center",
                    opacity: 0.88,
                  }}
                >
                  <Sparkline
                    data={evalData.trend_series!}
                    trend={evalData.trend ?? null}
                    width={140}
                    height={40}
                  />
                </Box>
              )}
            </Box>
          </Stack>
        ) : (
          <Box
            sx={{
              flex: 1,
              minHeight: 170,
              textAlign: "center",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              borderRadius: 1,
              bgcolor: palette.mint,
              border: `1px dashed ${palette.slateBorder}`,
            }}
          >
            <Typography
              variant="caption"
              sx={{ color: palette.textSecondary, fontSize: 11 }}
            >
              {t("kpiScorecard.noDataAvailable")}
            </Typography>
          </Box>
        )}
      </Box>
    </Paper>
  );
}
