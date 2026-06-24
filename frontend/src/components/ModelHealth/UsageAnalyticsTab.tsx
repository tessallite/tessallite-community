import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useT } from "../../i18n";
import {
  Box,
  Chip,
  CircularProgress,
  FormControl,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import { analyticsApi } from "../../api/client";
import HelpIconButton from "../HelpIconButton";

interface Props {
  projectId: string;
  modelId: string;
}

// F-030-11: keys are under the usageAnalytics.* namespace in en.json; the old
// analytics.* keys did not exist and rendered as raw dotted strings.
const RANGE_OPTIONS = [
  { value: 7, label: "usageAnalytics.last7Days" },
  { value: 30, label: "usageAnalytics.last30Days" },
  { value: 90, label: "usageAnalytics.last90Days" },
] as const;

const ROUTE_COLORS: Record<string, string> = {
  aggregate: "#4caf50",
  pocket: "#2196f3",
  source: "#ff9800",
};

function formatMs(ms: number): string {
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)} min`;
  if (ms >= 1_000) return `${(ms / 1_000).toFixed(1)} s`;
  return `${ms} ms`;
}

export default function UsageAnalyticsTab({ projectId, modelId }: Props) {
  const t = useT();
  const [days, setDays] = useState(30);

  const summary = useQuery({
    queryKey: ["analytics", "summary", projectId, modelId, days],
    queryFn: () => analyticsApi.summary(projectId, modelId, days),
  });

  const volume = useQuery({
    queryKey: ["analytics", "volume", projectId, modelId, days],
    queryFn: () => analyticsApi.queryVolume(projectId, modelId, days),
  });

  const topMeasures = useQuery({
    queryKey: ["analytics", "topMeasures", projectId, modelId, days],
    queryFn: () => analyticsApi.topMeasures(projectId, modelId, days),
  });

  const topAggregates = useQuery({
    queryKey: ["analytics", "topAggregates", projectId, modelId, days],
    queryFn: () => analyticsApi.topAggregates(projectId, modelId, days),
  });

  const routing = useQuery({
    queryKey: ["analytics", "routing", projectId, modelId, days],
    queryFn: () => analyticsApi.routingBreakdown(projectId, modelId, days),
  });

  const savings = useQuery({
    queryKey: ["analytics", "savings", projectId, modelId, days],
    queryFn: () => analyticsApi.estimatedSavings(projectId, modelId, days),
  });

  const topUsers = useQuery({
    queryKey: ["analytics", "topUsers", projectId, modelId, days],
    queryFn: () => analyticsApi.topUsers(projectId, modelId, days),
  });

  const loading =
    summary.isLoading || volume.isLoading || topMeasures.isLoading || topAggregates.isLoading;

  return (
    <Box sx={{ p: 2, maxWidth: 1400, mx: "auto" }}>
      <Box display="flex" alignItems="center" mb={2} gap={2}>
        <Typography variant="h6" fontWeight={600} flex={1}>
          {t("usageAnalytics.title")}
        </Typography>
        <HelpIconButton href="/help/modelling/usage-analytics.html" />
        <FormControl size="small" sx={{ minWidth: 160 }}>
          <InputLabel>{t("usageAnalytics.dateRange")}</InputLabel>
          <Select
            value={days}
            label={t("usageAnalytics.dateRange")}
            onChange={(e) => setDays(Number(e.target.value))}
          >
            {RANGE_OPTIONS.map((o) => (
              <MenuItem key={o.value} value={o.value}>
                {t(o.label)}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      </Box>

      {loading ? (
        <CircularProgress size={20} />
      ) : (
        <>
          {/* Summary cards */}
          <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
            <Stack direction="row" spacing={4} flexWrap="wrap">
              <StatCard label={t("usageAnalytics.totalQueries")} value={summary.data?.total_queries ?? 0} />
              <StatCard
                label={t("usageAnalytics.accelerationRate")}
                value={`${summary.data?.acceleration_rate ?? 0}%`}
              />
              <StatCard
                label={t("usageAnalytics.aggregateHitRate")}
                value={`${summary.data?.aggregate_hit_rate ?? 0}%`}
              />
              <StatCard label={t("usageAnalytics.topMissedMeasure")} value={summary.data?.top_measure ?? "—"} />
              <StatCard
                label={t("usageAnalytics.avgResponse")}
                value={
                  summary.data?.avg_response_ms != null
                    ? `${summary.data.avg_response_ms} ms`
                    : "—"
                }
              />
              {savings.data && savings.data.time_saved_ms > 0 && (
                <StatCard
                  label={t("usageAnalytics.estTimeSaved")}
                  value={formatMs(savings.data.time_saved_ms)}
                />
              )}
            </Stack>
          </Paper>

          {/* Routing breakdown + Estimated savings */}
          <Box sx={{ display: "flex", gap: 2, mb: 2, flexWrap: "wrap" }}>
            <Box sx={{ flex: 1, minWidth: 300 }}>
              <Typography variant="subtitle2" fontWeight={600} color="text.secondary" sx={{ mb: 1 }}>
                {t("usageAnalytics.routingBreakdown")}
              </Typography>
              <Paper variant="outlined" sx={{ p: 2 }}>
                {routing.isLoading ? (
                  <CircularProgress size={16} />
                ) : (routing.data ?? []).length === 0 ? (
                  <Typography variant="body2" color="text.secondary">
                    {t("usageAnalytics.noRoutingData")}
                  </Typography>
                ) : (
                  <>
                    <Box sx={{ display: "flex", height: 24, borderRadius: 1, overflow: "hidden", mb: 1 }}>
                      {(routing.data ?? []).map((r) => (
                        <Box
                          key={r.route_type}
                          sx={{
                            width: `${r.pct}%`,
                            bgcolor: ROUTE_COLORS[r.route_type] ?? "grey.500",
                            minWidth: r.pct > 0 ? 2 : 0,
                          }}
                          title={`${r.route_type}: ${r.count} (${r.pct}%)`}
                        />
                      ))}
                    </Box>
                    <Stack direction="row" spacing={2} flexWrap="wrap">
                      {(routing.data ?? []).map((r) => (
                        <Box key={r.route_type} display="flex" alignItems="center" gap={0.5}>
                          <Box
                            sx={{
                              width: 12,
                              height: 12,
                              borderRadius: 0.5,
                              bgcolor: ROUTE_COLORS[r.route_type] ?? "grey.500",
                            }}
                          />
                          <Typography variant="caption">
                            {r.route_type}: {r.count} ({r.pct}%)
                          </Typography>
                        </Box>
                      ))}
                    </Stack>
                  </>
                )}
              </Paper>
            </Box>

            <Box sx={{ flex: 1, minWidth: 300 }}>
              <Typography variant="subtitle2" fontWeight={600} color="text.secondary" sx={{ mb: 1 }}>
                {t("usageAnalytics.accelerationSavings")}
              </Typography>
              <Paper variant="outlined" sx={{ p: 2 }}>
                {savings.isLoading ? (
                  <CircularProgress size={16} />
                ) : !savings.data ? (
                  <Typography variant="body2" color="text.secondary">
                    {t("usageAnalytics.noSavingsData")}
                  </Typography>
                ) : (
                  <Stack direction="row" spacing={3} flexWrap="wrap">
                    <StatCard
                      label={t("usageAnalytics.acceleratedQueries")}
                      value={`${savings.data.accelerated_queries} / ${savings.data.total_queries}`}
                    />
                    <StatCard
                      label={t("usageAnalytics.avgSourceResponse")}
                      value={savings.data.avg_source_ms != null ? `${savings.data.avg_source_ms} ms` : "—"}
                    />
                    <StatCard
                      label={t("usageAnalytics.avgAcceleratedResponse")}
                      value={savings.data.avg_accelerated_ms != null ? `${savings.data.avg_accelerated_ms} ms` : "—"}
                    />
                  </Stack>
                )}
              </Paper>
            </Box>
          </Box>

          {/* Query volume */}
          <Typography variant="subtitle2" fontWeight={600} color="text.secondary" sx={{ mt: 2, mb: 1 }}>
            {t("usageAnalytics.queryVolumeDaily")}
          </Typography>
          {(volume.data ?? []).length === 0 ? (
            <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
              <Typography variant="body2" color="text.secondary">
                {t("usageAnalytics.noQueryData")}
              </Typography>
            </Paper>
          ) : (
            <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
              <Box sx={{ display: "flex", alignItems: "flex-end", gap: 0.5, height: 120 }}>
                {(volume.data ?? []).map((b) => {
                  const max = Math.max(...(volume.data ?? []).map((v) => v.count), 1);
                  const pct = (b.count / max) * 100;
                  return (
                    <Box
                      key={b.bucket}
                      sx={{
                        flex: 1,
                        minWidth: 4,
                        maxWidth: 24,
                        bgcolor: "primary.main",
                        borderRadius: "2px 2px 0 0",
                        height: `${pct}%`,
                        opacity: 0.8,
                      }}
                      title={`${b.bucket}: ${b.count}`}
                    />
                  );
                })}
              </Box>
              <Box sx={{ display: "flex", justifyContent: "space-between", mt: 0.5 }}>
                <Typography variant="caption" color="text.secondary">
                  {volume.data?.[0]?.bucket ?? ""}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {volume.data?.[volume.data.length - 1]?.bucket ?? ""}
                </Typography>
              </Box>
            </Paper>
          )}

          {/* Top measures + Top users side by side */}
          <Box sx={{ display: "flex", gap: 2, mb: 2, flexWrap: "wrap" }}>
            <Box sx={{ flex: 1, minWidth: 300 }}>
              <Typography variant="subtitle2" fontWeight={600} color="text.secondary" sx={{ mb: 1 }}>
                {t("usageAnalytics.topMissedMeasures")}
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
                {t("usageAnalytics.topMissedMeasuresHint")}
              </Typography>
              <TableContainer component={Paper} variant="outlined">
                <Table size="small">
                  <TableHead>
                    <TableRow sx={{ bgcolor: "grey.50" }}>
                      <TableCell>{t("usageAnalytics.rankHeader")}</TableCell>
                      <TableCell>{t("usageAnalytics.measureHeader")}</TableCell>
                      <TableCell align="right">{t("usageAnalytics.queryCountHeader")}</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {(topMeasures.data ?? []).length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={3}>
                          <Typography variant="body2" color="text.secondary">
                            {t("usageAnalytics.noMeasureData")}
                          </Typography>
                        </TableCell>
                      </TableRow>
                    ) : (
                      (topMeasures.data ?? []).map((m, i) => (
                        <TableRow key={m.measure_name}>
                          <TableCell>{i + 1}</TableCell>
                          <TableCell sx={{ fontFamily: "monospace", fontSize: 12 }}>
                            {m.measure_name}
                          </TableCell>
                          <TableCell align="right">{m.query_count}</TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
              </TableContainer>
            </Box>

            <Box sx={{ flex: 1, minWidth: 300 }}>
              <Typography variant="subtitle2" fontWeight={600} color="text.secondary" sx={{ mb: 1 }}>
                {t("usageAnalytics.topUsers")}
              </Typography>
              <TableContainer component={Paper} variant="outlined">
                <Table size="small">
                  <TableHead>
                    <TableRow sx={{ bgcolor: "grey.50" }}>
                      <TableCell>{t("usageAnalytics.rankHeader")}</TableCell>
                      <TableCell>{t("usageAnalytics.userHeader")}</TableCell>
                      <TableCell align="right">{t("usageAnalytics.queryCountHeader")}</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {topUsers.isLoading ? (
                      <TableRow>
                        <TableCell colSpan={3} align="center">
                          <CircularProgress size={16} />
                        </TableCell>
                      </TableRow>
                    ) : (topUsers.data ?? []).length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={3}>
                          <Typography variant="body2" color="text.secondary">
                            {t("usageAnalytics.noUserData")}
                          </Typography>
                        </TableCell>
                      </TableRow>
                    ) : (
                      (topUsers.data ?? []).map((u, i) => (
                        <TableRow key={u.user_identity}>
                          <TableCell>{i + 1}</TableCell>
                          <TableCell sx={{ fontSize: 12 }}>
                            {u.user_identity}
                          </TableCell>
                          <TableCell align="right">{u.query_count}</TableCell>
                        </TableRow>
                      ))
                    )}
                  </TableBody>
                </Table>
              </TableContainer>
            </Box>
          </Box>

          {/* Top aggregates */}
          <Typography variant="subtitle2" fontWeight={600} color="text.secondary" sx={{ mt: 2, mb: 1 }}>
            {t("usageAnalytics.topAggregates")}
          </Typography>
          <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
            <Table size="small">
              <TableHead>
                <TableRow sx={{ bgcolor: "grey.50" }}>
                  <TableCell>{t("usageAnalytics.rankHeader")}</TableCell>
                  <TableCell>{t("usageAnalytics.tableHeader")}</TableCell>
                  <TableCell>{t("usageAnalytics.grainHeader")}</TableCell>
                  <TableCell align="right">{t("usageAnalytics.hitCountHeader")}</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {(topAggregates.data ?? []).length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={4}>
                      <Typography variant="body2" color="text.secondary">
                        {t("usageAnalytics.noAggregateData")}
                      </Typography>
                    </TableCell>
                  </TableRow>
                ) : (
                  (topAggregates.data ?? []).map((a, i) => (
                    <TableRow key={a.aggregate_id}>
                      <TableCell>{i + 1}</TableCell>
                      <TableCell sx={{ fontFamily: "monospace", fontSize: 12 }}>
                        {a.physical_table_name}
                      </TableCell>
                      <TableCell>
                        <Typography variant="caption">
                          {(a.grain ?? []).join(", ") || "—"}
                        </Typography>
                      </TableCell>
                      <TableCell align="right">{a.query_count}</TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </TableContainer>
        </>
      )}
    </Box>
  );
}

function StatCard({ label, value }: { label: string; value: string | number }) {
  return (
    <Box>
      <Typography variant="caption" color="text.secondary" display="block">
        {label}
      </Typography>
      <Typography variant="h6" fontWeight={600} sx={{ lineHeight: 1 }}>
        {value}
      </Typography>
    </Box>
  );
}
