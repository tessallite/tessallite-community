/**
 * Phase 9 / F12 — cold-start latency dashboard.
 *
 * Reads the optimizer's /cold-start endpoint and renders the first-N
 * post-deploy query latencies as a small inline SVG bar chart, with
 * the pre-deploy median drawn as a horizontal reference. No chart
 * library — keeps the bundle slim.
 */
import { useT } from "../../i18n";
import { useQuery } from "@tanstack/react-query";
import {
  Box,
  CircularProgress,
  Paper,
  Stack,
  Typography,
} from "@mui/material";
import { optimizerApiClient } from "../../api/client";
import type { ColdStartResponse } from "../../api/types";

interface Props {
  modelId: string;
}

export default function ColdStartLatencySection({ modelId }: Props) {
  const t = useT();
  const query = useQuery<ColdStartResponse>({
    queryKey: ["cold-start", modelId],
    queryFn: () => optimizerApiClient.getColdStartLatency(modelId),
    enabled: Boolean(modelId),
  });

  return (
    <>
      <Typography
        variant="subtitle2"
        fontWeight={600}
        sx={{ mt: 2, mb: 1, color: "text.secondary" }}
      >
        {t("coldStart.title")}
      </Typography>
      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        {query.isLoading && (
          <Box sx={{ display: "flex", justifyContent: "center", py: 2 }}>
            <CircularProgress size={18} />
          </Box>
        )}
        {query.isError && (
          <Typography variant="body2" color="error">
            {t("coldStart.failedToLoad", {
              error: String((query.error as Error)?.message ?? "unknown error"),
            })}
          </Typography>
        )}
        {query.data && <Body data={query.data} />}
      </Paper>
    </>
  );
}

function Body({ data }: { data: ColdStartResponse }) {
  const t = useT();
  if (!data.last_deployed_at) {
    return (
      <Typography variant="body2" color="text.secondary">
        {t("coldStart.notDeployed")}
      </Typography>
    );
  }
  if (data.sample_count === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        {t("coldStart.noQueries", {
          time: new Date(data.last_deployed_at).toLocaleString(),
        })}
      </Typography>
    );
  }

  const max = Math.max(
    ...data.samples.map((s) => s.execution_ms),
    data.baseline_median_ms ?? 0,
    1,
  );
  const width = 600;
  const height = 160;
  const padTop = 12;
  const padBottom = 24;
  const padLeft = 36;
  const padRight = 12;
  const chartW = width - padLeft - padRight;
  const chartH = height - padTop - padBottom;
  const barWidth = chartW / data.samples.length;

  const baselineY =
    data.baseline_median_ms != null
      ? padTop + chartH - (data.baseline_median_ms / max) * chartH
      : null;

  return (
    <Stack spacing={1.5}>
      <Stack direction="row" spacing={3} flexWrap="wrap">
        <Stat label={t("coldStart.deployed")} value={new Date(data.last_deployed_at).toLocaleString()} />
        <Stat label={t("coldStart.samples")} value={String(data.sample_count)} />
        <Stat
          label={t("coldStart.medianPostDeploy")}
          value={data.median_ms != null ? `${Math.round(data.median_ms)} ms` : "—"}
        />
        <Stat
          label={t("coldStart.p95PostDeploy")}
          value={data.p95_ms != null ? `${Math.round(data.p95_ms)} ms` : "—"}
        />
        <Stat
          label={t("coldStart.baselineMedian", {
            days: String(data.baseline_window_days),
          })}
          value={
            data.baseline_median_ms != null
              ? `${Math.round(data.baseline_median_ms)} ms`
              : "—"
          }
        />
      </Stack>
      <Box sx={{ overflowX: "auto" }}>
        <svg width={width} height={height} role="img" aria-label={t("coldStart.chartAriaLabel")}>
          {/* y-axis */}
          <line
            x1={padLeft}
            y1={padTop}
            x2={padLeft}
            y2={padTop + chartH}
            stroke="#999"
            strokeWidth={1}
          />
          {/* x-axis */}
          <line
            x1={padLeft}
            y1={padTop + chartH}
            x2={padLeft + chartW}
            y2={padTop + chartH}
            stroke="#999"
            strokeWidth={1}
          />
          {/* y-axis ticks */}
          {[0, 0.5, 1].map((frac) => {
            const y = padTop + chartH - frac * chartH;
            return (
              <g key={frac}>
                <line x1={padLeft - 3} y1={y} x2={padLeft} y2={y} stroke="#999" />
                <text
                  x={padLeft - 5}
                  y={y + 3}
                  textAnchor="end"
                  fontSize={9}
                  fill="#666"
                >
                  {Math.round(frac * max)}
                </text>
              </g>
            );
          })}
          {/* bars */}
          {data.samples.map((s, i) => {
            const h = (s.execution_ms / max) * chartH;
            const x = padLeft + i * barWidth + 1;
            const y = padTop + chartH - h;
            return (
              <rect
                key={s.sequence}
                x={x}
                y={y}
                width={Math.max(1, barWidth - 2)}
                height={h}
                fill={s.aggregate_id ? "#4caf50" : "#1976d2"}
              >
                <title>
                  {`#${s.sequence} ${s.execution_ms} ms${
                    s.aggregate_id ? t("coldStart.aggregateHit") : ""
                  }`}
                </title>
              </rect>
            );
          })}
          {/* baseline line */}
          {baselineY != null && (
            <g>
              <line
                x1={padLeft}
                y1={baselineY}
                x2={padLeft + chartW}
                y2={baselineY}
                stroke="#d32f2f"
                strokeDasharray="4 3"
                strokeWidth={1.5}
              />
              <text
                x={padLeft + chartW - 4}
                y={baselineY - 4}
                textAnchor="end"
                fontSize={10}
                fill="#d32f2f"
              >
                {t("coldStart.preDeployMedian")}
              </text>
            </g>
          )}
        </svg>
      </Box>
      <Typography variant="caption" color="text.secondary">
        {t("coldStart.chartCaption", {
          count: String(data.sample_count),
          days: String(data.baseline_window_days),
        })}
      </Typography>
    </Stack>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <Box>
      <Typography variant="caption" color="text.secondary" display="block">
        {label}
      </Typography>
      <Typography variant="body2" fontWeight={500}>
        {value}
      </Typography>
    </Box>
  );
}
