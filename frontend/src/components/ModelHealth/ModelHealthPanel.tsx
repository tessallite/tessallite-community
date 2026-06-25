/**
 * Model Health — the dashboard that replaces the disabled "Matrix"
 * tab in the Model Builder. Five read-only sections:
 *
 *   A. Exposed model info (business + technical facts)
 *   B. Alerts stream (dedup-aware, paginated, dismissable)
 *   C. Aggregate refresh summary (paginated)
 *   D. Optimiser runs summary (AI + rule-based)
 *   E. Invalid objects (dims / measures / aggregates with reason)
 *
 * The summary strip at the top reads the per-severity alert counts
 * and the invalid-object totals so the modeler can see at a glance
 * whether anything needs attention without scrolling.
 *
 * Kept in one file to avoid sprawl — each section is a small
 * component defined below. Split into its own file only when a
 * section grows beyond ~100 lines.
 */
import { useMemo, useState } from "react";
import { useT } from "../../i18n";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  FormControl,
  FormControlLabel,
  IconButton,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TablePagination,
  TableRow,
  Tooltip,
  Typography,
} from "@mui/material";
import RefreshIcon from "@mui/icons-material/Refresh";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import ClearIcon from "@mui/icons-material/Clear";
import {
  alertsApi,
  modelsApi,
  aggregatesApi,
  dimensionsApi,
  measuresApi,
  schemaDriftApi,
  dataQualityApi,
} from "../../api/client";
import type { DataQualityRule, MeasureWarning, ModelAlert, SchemaChangeEvent } from "../../api/types";
import {
  useMetrics,
  useModelRefreshRuns,
  useAIOptimizerRuns,
  useOptimizerRuns,
  usePersonas,
} from "../../api/hooks";
import HelpIconButton from "../HelpIconButton";
import ColdStartLatencySection from "./ColdStartLatencySection";

interface Props {
  projectId: string;
  modelId: string;
}

const SEVERITY_COLOR: Record<
  string,
  "default" | "info" | "warning" | "error" | "success"
> = {
  info: "info",
  warning: "warning",
  error: "error",
  critical: "error",
};

const PERSONA_FILTER_ALL = "__all__";
const PERSONA_FILTER_GLOBAL = "__global__";
const ALERT_SEVERITY_ALL = "__all__";

export default function ModelHealthPanel({ projectId, modelId }: Props) {
  const t = useT();
  const qc = useQueryClient();
  const [personaFilter, setPersonaFilter] = useState<string>(PERSONA_FILTER_ALL);

  const model = useQuery({
    queryKey: ["model", projectId, modelId],
    queryFn: () => modelsApi.get(projectId, modelId),
  });

  const personas = usePersonas(projectId, modelId);
  const personaNameById = useMemo(() => {
    const m = new Map<string, string>();
    for (const p of personas.data ?? []) m.set(p.id, p.name);
    return m;
  }, [personas.data]);

  const alertCount = useQuery({
    queryKey: ["alerts", projectId, modelId, "count"],
    queryFn: () => alertsApi.count(projectId, modelId),
    refetchInterval: 30000,
  });

  const revalidate = useMutation({
    mutationFn: () => alertsApi.revalidate(projectId, modelId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["alerts", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["measures", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["aggregates", projectId, modelId] });
    },
  });

  const revalidating = revalidate.isPending;
  const revalidateResult = revalidate.data;

  return (
    <Box sx={{ p: 2, maxWidth: 1400, mx: "auto" }}>
      {/* Header */}
      <Box display="flex" alignItems="center" mb={2} gap={2}>
        <Box flexGrow={1}>
          <Typography variant="h6" fontWeight={600}>
            {t("modelHealth.title")}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {model.data?.display_name ?? ""}
            {model.data?.slug ? ` — ${model.data.slug}` : ""}
          </Typography>
        </Box>
        <HelpIconButton href="/help/concepts/model-health.html" />
        {(personas.data ?? []).length > 0 && (
          <FormControl size="small" sx={{ minWidth: 200 }}>
            <InputLabel id="model-health-persona-filter">{t("modelHealth.personaScope")}</InputLabel>
            <Select
              labelId="model-health-persona-filter"
              value={personaFilter}
              label={t("modelHealth.personaScope")}
              onChange={(e) => setPersonaFilter(e.target.value)}
            >
              <MenuItem value={PERSONA_FILTER_ALL}>{t("modelHealth.allPersonas")}</MenuItem>
              <MenuItem value={PERSONA_FILTER_GLOBAL}>{t("modelHealth.globalOnly")}</MenuItem>
              {(personas.data ?? []).map((p) => (
                <MenuItem key={p.id} value={p.id}>
                  {p.name}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
        )}
        <Tooltip title={t("modelHealth.revalidateTooltip")}>
          <span>
            <Button
              size="small"
              variant="outlined"
              startIcon={revalidating ? <CircularProgress size={14} /> : <RefreshIcon />}
              onClick={() => revalidate.mutate()}
              disabled={revalidating}
            >
              {t("modelHealth.recheck")}
            </Button>
          </span>
        </Tooltip>
      </Box>

      {/* Summary strip */}
      <Paper variant="outlined" sx={{ p: 1.5, mb: 2 }}>
        <Stack direction="row" spacing={3} flexWrap="wrap">
          <SummaryChip
            label={t("modelHealth.openAlerts")}
            value={alertCount.data?.total ?? 0}
            tone={(alertCount.data?.total ?? 0) > 0 ? "warning" : "default"}
          />
          <SummaryChip
            label={t("modelHealth.errors")}
            value={alertCount.data?.by_severity?.error ?? 0}
            tone={(alertCount.data?.by_severity?.error ?? 0) > 0 ? "error" : "default"}
          />
          <SummaryChip
            label={t("modelHealth.warnings")}
            value={alertCount.data?.by_severity?.warning ?? 0}
            tone="default"
          />
          <SummaryChip
            label={t("modelHealth.critical")}
            value={alertCount.data?.by_severity?.critical ?? 0}
            tone={(alertCount.data?.by_severity?.critical ?? 0) > 0 ? "error" : "default"}
          />
        </Stack>
        {revalidateResult && (
          <>
            <Alert severity="info" sx={{ mt: 1 }}>
              {t("modelHealth.revalidationComplete")}&nbsp;
              {t("modelHealth.invalid")} {revalidateResult.invalid_dimension_count} {t("modelHealth.dim")} /{" "}
              {revalidateResult.invalid_measure_count} {t("modelHealth.measure")} /{" "}
              {revalidateResult.invalid_aggregate_count} {t("modelHealth.aggregate")}.&nbsp;
              {t("modelHealth.newlyValid")} {revalidateResult.newly_valid_dimension_count} {t("modelHealth.dim")} /{" "}
              {revalidateResult.newly_valid_measure_count} {t("modelHealth.measure")} /{" "}
              {revalidateResult.newly_valid_aggregate_count} {t("modelHealth.aggregate")}.
            </Alert>
            {(revalidateResult.measure_warnings?.length ?? 0) > 0 && (
              <Alert severity="warning" sx={{ mt: 1 }}>
                <Typography variant="subtitle2" gutterBottom>
                  {revalidateResult.measure_warnings!.length > 1
                    ? t("modelHealth.misclassifiedMeasuresPlural", { count: String(revalidateResult.measure_warnings!.length) })
                    : t("modelHealth.misclassifiedMeasures", { count: String(revalidateResult.measure_warnings!.length) })}
                </Typography>
                {revalidateResult.measure_warnings!.map((w: MeasureWarning) => (
                  <Typography
                    key={w.column_id}
                    variant="caption"
                    display="block"
                    sx={{ ml: 1, mb: 0.5 }}
                  >
                    <strong>{w.column_name}</strong> ({w.severity}): {w.reason}
                  </Typography>
                ))}
              </Alert>
            )}
          </>
        )}
      </Paper>

      <ModelInfoSection model={model.data} />
      <ColdStartLatencySection modelId={modelId} />
      <ModelAlertsSection projectId={projectId} modelId={modelId} />
      <QueryRoutingMetricsSection projectId={projectId} modelId={modelId} />
      <AggregateHealthSection projectId={projectId} modelId={modelId} />
      <AggregateRefreshSummarySection
        projectId={projectId}
        modelId={modelId}
        personaFilter={personaFilter}
        personaNameById={personaNameById}
      />
      <OptimiserRunsSection projectId={projectId} modelId={modelId} />
      <InvalidObjectsSection
        projectId={projectId}
        modelId={modelId}
        personaFilter={personaFilter}
        personaNameById={personaNameById}
      />
      <SchemaDriftSection modelId={modelId} />
      <DataQualitySection projectId={projectId} modelId={modelId} />
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------

function SummaryChip({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "default" | "warning" | "error";
}) {
  const color =
    tone === "error"
      ? "error.main"
      : tone === "warning"
      ? "warning.main"
      : "text.primary";
  return (
    <Box>
      <Typography variant="caption" color="text.secondary" display="block">
        {label}
      </Typography>
      <Typography variant="h6" sx={{ color, fontWeight: 600, lineHeight: 1 }}>
        {value}
      </Typography>
    </Box>
  );
}

function SectionHeader({ title }: { title: string }) {
  return (
    <Typography
      variant="subtitle2"
      fontWeight={600}
      sx={{ mt: 2, mb: 1, color: "text.secondary" }}
    >
      {title}
    </Typography>
  );
}

function formatByteCount(bytes: number): string {
  if (bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

function formatDurationMs(ms: number): string {
  if (ms <= 0) return "0 ms";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${minutes.toFixed(1)} min`;
  return `${(minutes / 60).toFixed(1)} h`;
}

export function QueryRoutingMetricsSection({
  projectId,
  modelId,
}: {
  projectId: string;
  modelId: string;
}) {
  const t = useT();
  const [windowHours, setWindowHours] = useState(24);
  const metrics = useMetrics(projectId, modelId, windowHours);
  const data = metrics.data;

  const maxHourTotal = useMemo(() => {
    const totals = (data?.hourly_volume ?? []).map((h) => h.total);
    return totals.length ? Math.max(...totals, 1) : 1;
  }, [data?.hourly_volume]);

  return (
    <>
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mt: 2 }}>
        <SectionHeader title={t("modelHealth.sectionQueryRouting")} />
        <FormControl size="small" sx={{ minWidth: 160 }}>
          <InputLabel id="model-health-metrics-window">{t("modelHealth.metricsWindow")}</InputLabel>
          <Select
            labelId="model-health-metrics-window"
            value={String(windowHours)}
            label={t("modelHealth.metricsWindow")}
            onChange={(e) => setWindowHours(Number(e.target.value))}
          >
            <MenuItem value="24">{t("modelHealth.metricsWindow24h")}</MenuItem>
            <MenuItem value="168">{t("modelHealth.metricsWindow7d")}</MenuItem>
            <MenuItem value="720">{t("modelHealth.metricsWindow30d")}</MenuItem>
          </Select>
        </FormControl>
      </Stack>

      {metrics.isError ? (
        <Alert severity="error" sx={{ mb: 2 }}>
          {t("modelHealth.metricsLoadFailed")}
        </Alert>
      ) : metrics.isLoading ? (
        <Box sx={{ display: "flex", justifyContent: "center", py: 3 }}>
          <CircularProgress size={20} />
        </Box>
      ) : !data || data.total_queries === 0 ? (
        <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
          <Typography variant="body2" color="text.secondary" textAlign="center">
            {t("modelHealth.metricsNoTraffic")}
          </Typography>
        </Paper>
      ) : (
        <>
          <Paper variant="outlined" sx={{ p: 1.5, mb: 2 }}>
            <Stack direction="row" spacing={3} flexWrap="wrap" rowGap={1.5}>
              <Box>
                <Typography variant="caption" color="text.secondary" display="block">
                  {t("modelHealth.metricsAccelerationRate")}
                </Typography>
                <Typography variant="h6" sx={{ fontWeight: 600, lineHeight: 1, color: "success.main" }}>
                  {(data.hit_rate * 100).toFixed(1)}%
                </Typography>
              </Box>
              <SummaryChip
                label={t("modelHealth.metricsTotalQueries")}
                value={data.total_queries}
                tone="default"
              />
              <SummaryChip
                label={t("modelHealth.metricsAggregateHits")}
                value={data.aggregate_hits}
                tone="default"
              />
              <SummaryChip
                label={t("modelHealth.metricsPocketHits")}
                value={data.pocket_hits ?? 0}
                tone="default"
              />
              <SummaryChip
                label={t("modelHealth.metricsSourceHits")}
                value={data.source_hits}
                tone="default"
              />
              <Box>
                <Typography variant="caption" color="text.secondary" display="block">
                  {t("modelHealth.metricsBytesAvoided")}
                </Typography>
                <Typography variant="h6" sx={{ fontWeight: 600, lineHeight: 1 }}>
                  {formatByteCount(data.bytes_avoided)}
                </Typography>
              </Box>
            </Stack>
            <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>
              {t("modelHealth.metricsAccelerationHint")}
            </Typography>
          </Paper>

          {(data.pocket_hits ?? 0) > 0 && (
            <Paper variant="outlined" sx={{ p: 1.5, mb: 2 }}>
              <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
                {t("modelHealth.metricsPocketSavings")}
              </Typography>
              <Stack direction="row" spacing={3} flexWrap="wrap" rowGap={1.5}>
                <Box>
                  <Typography variant="caption" color="text.secondary" display="block">
                    {t("modelHealth.metricsPocketTimeSaved")}
                  </Typography>
                  <Typography variant="h6" sx={{ fontWeight: 600, lineHeight: 1 }}>
                    {formatDurationMs(data.pocket_time_saved_ms ?? 0)}
                  </Typography>
                </Box>
                <Box>
                  <Typography variant="caption" color="text.secondary" display="block">
                    {t("modelHealth.metricsPocketStorage")}
                  </Typography>
                  <Typography variant="h6" sx={{ fontWeight: 600, lineHeight: 1 }}>
                    {formatByteCount(data.pocket_storage_bytes ?? 0)}
                  </Typography>
                </Box>
                <SummaryChip
                  label={t("modelHealth.metricsPocketEvictions")}
                  value={data.pocket_evictions_24h ?? 0}
                  tone="default"
                />
              </Stack>
            </Paper>
          )}

          {data.hourly_volume.length > 0 && (
            <Paper variant="outlined" sx={{ p: 1.5, mb: 2 }}>
              <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
                {t("modelHealth.metricsHourlyVolume")}
              </Typography>
              <Box
                sx={{
                  display: "flex",
                  alignItems: "flex-end",
                  gap: 0.5,
                  height: 80,
                  overflowX: "auto",
                }}
              >
                {data.hourly_volume.map((h) => {
                  const accelerated = h.aggregate_hits + (h.pocket_hits ?? 0);
                  const acceleratedFrac = h.total > 0 ? accelerated / h.total : 0;
                  const heightFrac = h.total / maxHourTotal;
                  return (
                    <Tooltip
                      key={h.hour}
                      title={`${new Date(h.hour).toLocaleString()} — ${h.total} (${accelerated} accelerated)`}
                    >
                      <Box
                        sx={{
                          flex: "0 0 6px",
                          height: `${Math.max(heightFrac * 100, h.total > 0 ? 4 : 0)}%`,
                          minHeight: h.total > 0 ? 2 : 0,
                          bgcolor: "grey.300",
                          borderRadius: "2px 2px 0 0",
                          position: "relative",
                          overflow: "hidden",
                        }}
                      >
                        <Box
                          sx={{
                            position: "absolute",
                            bottom: 0,
                            left: 0,
                            right: 0,
                            height: `${acceleratedFrac * 100}%`,
                            bgcolor: "success.main",
                          }}
                        />
                      </Box>
                    </Tooltip>
                  );
                })}
              </Box>
            </Paper>
          )}
        </>
      )}
    </>
  );
}

function ModelInfoSection({ model }: { model: any }) {
  const t = useT();
  if (!model) return null;
  const business: [string, string | null | undefined][] = [
    [t("modelHealth.displayName"), model.display_name],
    [t("modelHealth.description"), model.description],
    [t("modelHealth.status"), model.status],
    [t("modelHealth.aggregationsEnabled"), model.aggregations_enabled ? t("modelHealth.yes") : t("modelHealth.no")],
    [t("modelHealth.includeAllMeasures"), (model.include_all_measures ?? true) ? t("modelHealth.yes") : t("modelHealth.no")],
  ];
  const technical: [string, string | null | undefined][] = [
    [t("modelHealth.modelId"), model.id],
    [t("modelHealth.slug"), model.slug],
    [t("modelHealth.target"), model.target_id],
    [t("modelHealth.refreshStrategy"), model.refresh_strategy],
    [t("modelHealth.maxAggregates"), String(model.max_aggregates ?? "")],
    [t("modelHealth.createdAt"), model.created_at],
  ];
  return (
    <>
      <SectionHeader title={t("modelHealth.sectionModelInfo")} />
      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Stack direction={{ xs: "column", md: "row" }} spacing={3}>
          <Box flex={1}>
            <Typography variant="caption" color="text.secondary" fontWeight={600}>
              {t("modelHealth.businessSection")}
            </Typography>
            {business.map(([k, v]) => (
              <Box key={k} display="flex" py={0.5}>
                <Typography variant="caption" sx={{ width: 160, color: "text.secondary" }}>
                  {k}
                </Typography>
                <Typography variant="caption" sx={{ flex: 1 }}>
                  {v ?? "—"}
                </Typography>
              </Box>
            ))}
          </Box>
          <Divider orientation="vertical" flexItem />
          <Box flex={1}>
            <Typography variant="caption" color="text.secondary" fontWeight={600}>
              {t("modelHealth.technical")}
            </Typography>
            {technical.map(([k, v]) => (
              <Box key={k} display="flex" py={0.5}>
                <Typography variant="caption" sx={{ width: 160, color: "text.secondary" }}>
                  {k}
                </Typography>
                <Typography
                  variant="caption"
                  sx={{ flex: 1, fontFamily: "monospace", fontSize: 11 }}
                >
                  {v ?? "—"}
                </Typography>
              </Box>
            ))}
          </Box>
        </Stack>
      </Paper>
    </>
  );
}

function ModelAlertsSection({
  projectId,
  modelId,
}: {
  projectId: string;
  modelId: string;
}) {
  const t = useT();
  const qc = useQueryClient();
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(10);
  // F-030-23: surface the alerts API's own filters and stop paging past the end.
  const [severityFilter, setSeverityFilter] = useState<string>(ALERT_SEVERITY_ALL);
  const [includeResolved, setIncludeResolved] = useState(false);

  const filterOpts = useMemo(
    () => ({
      severity: severityFilter === ALERT_SEVERITY_ALL ? undefined : severityFilter,
      include_resolved: includeResolved || undefined,
    }),
    [severityFilter, includeResolved],
  );

  const alerts = useQuery({
    queryKey: ["alerts", projectId, modelId, "list", page, rowsPerPage, filterOpts],
    queryFn: () =>
      alertsApi.list(projectId, modelId, {
        ...filterOpts,
        limit: rowsPerPage,
        offset: page * rowsPerPage,
      }),
    refetchInterval: 30000,
  });

  // Real total under the active filters drives pagination so the user cannot
  // page past the last row (F-030-23).
  const filteredCount = useQuery({
    queryKey: ["alerts", projectId, modelId, "filteredCount", filterOpts],
    queryFn: () => alertsApi.count(projectId, modelId, filterOpts),
    refetchInterval: 30000,
  });
  const totalCount = filteredCount.data?.filtered_total ?? -1;

  const dismiss = useMutation({
    mutationFn: (alertId: string) => alertsApi.dismiss(projectId, modelId, alertId),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["alerts", projectId, modelId] }),
  });

  return (
    <>
      <SectionHeader title={t("modelHealth.alertsSection")} />
      <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mb: 1 }}>
        <FormControl size="small" sx={{ minWidth: 160 }}>
          <InputLabel id="alerts-severity-filter">{t("modelHealth.filterSeverity")}</InputLabel>
          <Select
            labelId="alerts-severity-filter"
            value={severityFilter}
            label={t("modelHealth.filterSeverity")}
            onChange={(e) => { setSeverityFilter(e.target.value); setPage(0); }}
          >
            <MenuItem value={ALERT_SEVERITY_ALL}>{t("modelHealth.filterAllSeverities")}</MenuItem>
            <MenuItem value="critical">{t("modelHealth.severityCritical")}</MenuItem>
            <MenuItem value="error">{t("modelHealth.severityError")}</MenuItem>
            <MenuItem value="warning">{t("modelHealth.severityWarning")}</MenuItem>
            <MenuItem value="info">{t("modelHealth.severityInfo")}</MenuItem>
          </Select>
        </FormControl>
        <FormControlLabel
          control={
            <Switch
              size="small"
              checked={includeResolved}
              onChange={(e) => { setIncludeResolved(e.target.checked); setPage(0); }}
            />
          }
          label={t("modelHealth.includeResolved")}
        />
      </Stack>
      {alerts.isLoading ? (
        <CircularProgress size={18} />
      ) : (alerts.data ?? []).length === 0 ? (
        <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
          <Stack direction="row" alignItems="center" spacing={1}>
            <CheckCircleIcon fontSize="small" color="success" />
            <Typography variant="body2" color="text.secondary">
              {t("modelHealth.noAlerts")}
            </Typography>
          </Stack>
        </Paper>
      ) : (
        <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
          <Table size="small">
            <TableHead>
              <TableRow sx={{ bgcolor: "grey.50" }}>
                <TableCell>{t("modelHealth.colSeverity")}</TableCell>
                <TableCell>{t("modelHealth.colCategory")}</TableCell>
                <TableCell>{t("modelHealth.colTitle")}</TableCell>
                <TableCell>{t("modelHealth.colLastSeen")}</TableCell>
                <TableCell align="right">{t("modelHealth.colCount")}</TableCell>
                <TableCell align="center">{t("modelHealth.colDismiss")}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {(alerts.data ?? []).map((a: ModelAlert) => (
                <TableRow key={a.id}>
                  <TableCell>
                    <Chip
                      label={a.severity}
                      size="small"
                      color={SEVERITY_COLOR[a.severity] ?? "default"}
                    />
                  </TableCell>
                  <TableCell>
                    <Typography variant="caption" sx={{ fontFamily: "monospace" }}>
                      {a.category}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    <Tooltip title={a.detail ?? ""} placement="top">
                      <Typography variant="body2">{a.title}</Typography>
                    </Tooltip>
                  </TableCell>
                  <TableCell sx={{ whiteSpace: "nowrap" }}>
                    <Typography variant="caption">
                      {new Date(a.last_seen_at).toLocaleString()}
                    </Typography>
                  </TableCell>
                  <TableCell align="right">{a.occurrence_count}</TableCell>
                  <TableCell align="center">
                    <Tooltip title={t("modelHealth.dismissTooltip")}>
                      <IconButton
                        size="small"
                        onClick={() => dismiss.mutate(a.id)}
                        disabled={dismiss.isPending}
                      >
                        <ClearIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <TablePagination
            component="div"
            count={totalCount}
            page={page}
            onPageChange={(_, p) => setPage(p)}
            rowsPerPage={rowsPerPage}
            onRowsPerPageChange={(e) => {
              setRowsPerPage(parseInt(e.target.value, 10));
              setPage(0);
            }}
            rowsPerPageOptions={[10, 25, 50]}
          />
        </TableContainer>
      )}
    </>
  );
}

export function AggregateHealthSection({
  projectId,
  modelId,
}: {
  projectId: string;
  modelId: string;
}) {
  const t = useT();
  const aggs = useQuery({
    queryKey: ["aggregates", projectId, modelId],
    queryFn: () => aggregatesApi.list(projectId, modelId),
  });

  const counts = useMemo(() => {
    const data = aggs.data ?? [];
    // Health is freshness-aware (backend `health`, with a stale-aware status
    // fallback): an active-but-stale aggregate is unhealthy because it is not
    // routable until rebuilt.
    const isHealthy = (a: (typeof data)[number]) =>
      a.health
        ? a.health === "healthy"
        : a.status === "disabled" || (a.status === "active" && !a.is_stale);
    const healthy = data.filter(isHealthy).length;
    const pending = data.filter((a) => a.status === "pending").length;
    const invalid = data.filter((a) => a.status === "invalid").length;
    const retired = data.filter((a) => a.status === "retired").length;
    // Active rows that are unhealthy (stale / never refreshed) — not pending/
    // invalid/retired, but still not serving. Counted so the breakdown sums to
    // the unhealthy total.
    const outdated = data.filter((a) => a.status === "active" && !isHealthy(a)).length;
    return {
      total: data.length,
      healthy,
      pending,
      invalid,
      retired,
      outdated,
      unhealthy: data.length - healthy,
    };
  }, [aggs.data]);

  return (
    <>
      <SectionHeader title={t("modelHealth.sectionAggHealth")} />
      {aggs.isLoading ? (
        <CircularProgress size={20} />
      ) : counts.total === 0 ? (
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          {t("modelHealth.aggHealthNone")}
        </Typography>
      ) : (
        <Paper variant="outlined" sx={{ p: 1.5, mb: 2 }}>
          <Stack direction="row" spacing={4} flexWrap="wrap" useFlexGap>
            <SummaryChip label={t("modelHealth.aggHealthHealthy")} value={counts.healthy} tone="default" />
            <SummaryChip
              label={t("modelHealth.aggHealthPending")}
              value={counts.pending}
              tone={counts.pending > 0 ? "warning" : "default"}
            />
            <SummaryChip
              label={t("modelHealth.aggHealthOutdated")}
              value={counts.outdated}
              tone={counts.outdated > 0 ? "warning" : "default"}
            />
            <SummaryChip
              label={t("modelHealth.aggHealthInvalid")}
              value={counts.invalid}
              tone={counts.invalid > 0 ? "error" : "default"}
            />
            <SummaryChip label={t("modelHealth.aggHealthRetired")} value={counts.retired} tone="default" />
            <SummaryChip
              label={t("modelHealth.aggHealthUnhealthy")}
              value={counts.unhealthy}
              tone={counts.unhealthy > 0 ? "warning" : "default"}
            />
          </Stack>
          {counts.pending > 0 && (
            <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>
              {t("modelHealth.aggHealthPendingNote")}
            </Typography>
          )}
        </Paper>
      )}
    </>
  );
}

function AggregateRefreshSummarySection({
  projectId,
  modelId,
  personaFilter,
  personaNameById,
}: {
  projectId: string;
  modelId: string;
  personaFilter: string;
  personaNameById: Map<string, string>;
}) {
  const t = useT();
  const runs = useModelRefreshRuns(projectId, modelId);
  const aggs = useQuery({
    queryKey: ["aggregates", projectId, modelId],
    queryFn: () => aggregatesApi.list(projectId, modelId),
  });
  const aggPersonaById = useMemo(() => {
    const m = new Map<string, string | null>();
    for (const a of aggs.data ?? []) m.set(a.id, a.persona_id ?? null);
    return m;
  }, [aggs.data]);
  const rows = useMemo(() => {
    const all = runs.data ?? [];
    if (personaFilter === PERSONA_FILTER_ALL) return all.slice(0, 10);
    const filtered = all.filter((r) => {
      const pid = aggPersonaById.get(r.aggregate_definition_id) ?? null;
      if (personaFilter === PERSONA_FILTER_GLOBAL) return pid == null;
      return pid === personaFilter;
    });
    return filtered.slice(0, 10);
  }, [runs.data, aggPersonaById, personaFilter]);
  return (
    <>
      <SectionHeader title={t("modelHealth.sectionAggRefresh")} />
      <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
        <Table size="small">
          <TableHead>
            <TableRow sx={{ bgcolor: "grey.50" }}>
              <TableCell>{t("modelHealth.colStarted")}</TableCell>
              <TableCell>{t("modelHealth.colAggregate")}</TableCell>
              <TableCell>{t("modelHealth.colPersona")}</TableCell>
              <TableCell>{t("modelHealth.colStatus")}</TableCell>
              <TableCell align="right">{t("modelHealth.colRows")}</TableCell>
              <TableCell>{t("modelHealth.colError")}</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={6}>
                  <Typography variant="body2" color="text.secondary" textAlign="center">
                    {t("modelHealth.noRefreshRuns")}
                  </Typography>
                </TableCell>
              </TableRow>
            ) : (
              rows.map((r: any) => {
                const pid = aggPersonaById.get(r.aggregate_definition_id) ?? null;
                const personaLabel = pid
                  ? personaNameById.get(pid) ?? pid.slice(0, 8)
                  : t("modelHealth.globalPersona");
                return (
                <TableRow key={r.id}>
                  <TableCell sx={{ whiteSpace: "nowrap" }}>
                    <Typography variant="caption">
                      {new Date(r.started_at).toLocaleString()}
                    </Typography>
                  </TableCell>
                  <TableCell sx={{ fontFamily: "monospace", fontSize: 11 }}>
                    {r.aggregate_table ?? "—"}
                  </TableCell>
                  <TableCell>
                    <Chip
                      label={personaLabel}
                      size="small"
                      variant="outlined"
                      color={pid ? "primary" : "default"}
                    />
                  </TableCell>
                  <TableCell>
                    <Chip
                      label={r.status}
                      size="small"
                      color={
                        r.status === "completed"
                          ? "success"
                          : r.status === "failed"
                          ? "error"
                          : "default"
                      }
                    />
                  </TableCell>
                  <TableCell align="right">
                    {r.rows_written != null ? r.rows_written.toLocaleString() : "—"}
                  </TableCell>
                  <TableCell
                    sx={{
                      color: "error.main",
                      fontSize: 11,
                      maxWidth: 360,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    <Tooltip title={r.error_message ?? ""}>
                      <span>{r.error_message ?? ""}</span>
                    </Tooltip>
                  </TableCell>
                </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </TableContainer>
    </>
  );
}

function OptimiserRunsSection({
  projectId,
  modelId,
}: {
  projectId: string;
  modelId: string;
}) {
  const t = useT();
  const aiRuns = useAIOptimizerRuns("", modelId);
  const ruleRuns = useOptimizerRuns();
  const merged = useMemo(() => {
    const a = (aiRuns.data ?? []).map((r: any) => ({
      ...r,
      kind: "ai",
    }));
    const b = (ruleRuns.data ?? []).map((r: any) => ({
      ...r,
      kind: "rule",
    }));
    const all = [...a, ...b];
    all.sort(
      (x, y) =>
        new Date(y.started_at ?? y.created_at).getTime() -
        new Date(x.started_at ?? x.created_at).getTime(),
    );
    return all.slice(0, 10);
  }, [aiRuns.data, ruleRuns.data]);

  return (
    <>
      <SectionHeader title={t("modelHealth.optimiserTitle")} />
      <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
        <Table size="small">
          <TableHead>
            <TableRow sx={{ bgcolor: "grey.50" }}>
              <TableCell>{t("modelHealth.colStarted")}</TableCell>
              <TableCell>{t("modelHealth.colKind")}</TableCell>
              <TableCell>{t("modelHealth.colStatus")}</TableCell>
              <TableCell align="right">{t("modelHealth.colCreated")}</TableCell>
              <TableCell align="right">{t("modelHealth.colSkipped")}</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {merged.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5}>
                  <Typography variant="body2" color="text.secondary" textAlign="center">
                    {t("modelHealth.noOptimiserRuns")}
                  </Typography>
                </TableCell>
              </TableRow>
            ) : (
              merged.map((r: any) => (
                <TableRow key={`${r.kind}-${r.id}`}>
                  <TableCell sx={{ whiteSpace: "nowrap" }}>
                    <Typography variant="caption">
                      {new Date(r.started_at ?? r.created_at).toLocaleString()}
                    </Typography>
                  </TableCell>
                  <TableCell>
                    <Chip
                      label={r.kind === "ai" ? t("modelHealth.kindAI") : t("modelHealth.kindRuleBased")}
                      size="small"
                      variant="outlined"
                    />
                  </TableCell>
                  <TableCell>
                    <Chip
                      label={r.status ?? "—"}
                      size="small"
                      color={
                        r.status === "completed"
                          ? "success"
                          : r.status === "failed"
                          ? "error"
                          : "default"
                      }
                    />
                  </TableCell>
                  <TableCell align="right">
                    {r.aggregates_created ?? r.recommendations_count ?? 0}
                  </TableCell>
                  <TableCell align="right">{r.aggregates_skipped ?? 0}</TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </TableContainer>
    </>
  );
}

function InvalidObjectsSection({
  projectId,
  modelId,
  personaFilter,
  personaNameById,
}: {
  projectId: string;
  modelId: string;
  personaFilter: string;
  personaNameById: Map<string, string>;
}) {
  const t = useT();
  const dims = useQuery({
    queryKey: ["dimensions", projectId, modelId],
    queryFn: () => dimensionsApi.list(projectId, modelId),
  });
  const measures = useQuery({
    queryKey: ["measures", projectId, modelId],
    queryFn: () => measuresApi.list(projectId, modelId),
  });
  const aggs = useQuery({
    queryKey: ["aggregates", projectId, modelId],
    queryFn: () => aggregatesApi.list(projectId, modelId),
  });

  const rows = useMemo(() => {
    const out: {
      type: string;
      name: string;
      reason: string;
      persona: string;
    }[] = [];
    // Dimensions and measures do not carry persona scope — they are
    // always visible in "All" and "Global only" modes, and hidden when
    // the filter is a specific persona.
    const includeGlobalObjects =
      personaFilter === PERSONA_FILTER_ALL ||
      personaFilter === PERSONA_FILTER_GLOBAL;
    if (includeGlobalObjects) {
      for (const d of dims.data ?? []) {
        if (d.is_invalid) {
          out.push({
            type: "dimension",
            name: d.name,
            reason: d.invalid_reason ?? "",
            persona: "—",
          });
        }
      }
      for (const m of measures.data ?? []) {
        if (m.is_invalid) {
          out.push({
            type: "measure",
            name: m.name,
            reason: m.invalid_reason ?? "",
            persona: "—",
          });
        }
      }
    }
    for (const a of aggs.data ?? []) {
      if (a.status !== "invalid") continue;
      const pid = a.persona_id ?? null;
      if (personaFilter === PERSONA_FILTER_GLOBAL && pid != null) continue;
      if (
        personaFilter !== PERSONA_FILTER_ALL &&
        personaFilter !== PERSONA_FILTER_GLOBAL &&
        pid !== personaFilter
      ) {
        continue;
      }
      out.push({
        type: "aggregate",
        name: a.physical_table_name,
        reason: (a as any).invalid_reason ?? "",
        persona: pid
          ? personaNameById.get(pid) ?? pid.slice(0, 8)
          : "global",
      });
    }
    return out;
  }, [dims.data, measures.data, aggs.data, personaFilter, personaNameById]);

  return (
    <>
      <SectionHeader title={t("modelHealth.invalidObjectsTitle")} />
      <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
        <Table size="small">
          <TableHead>
            <TableRow sx={{ bgcolor: "grey.50" }}>
              <TableCell>{t("modelHealth.colType")}</TableCell>
              <TableCell>{t("modelHealth.colName")}</TableCell>
              <TableCell>{t("modelHealth.colPersona")}</TableCell>
              <TableCell>{t("modelHealth.colReason")}</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={4}>
                  <Stack
                    direction="row"
                    alignItems="center"
                    spacing={1}
                    justifyContent="center"
                  >
                    <CheckCircleIcon fontSize="small" color="success" />
                    <Typography variant="body2" color="text.secondary">
                      {t("modelHealth.allValid")}
                    </Typography>
                  </Stack>
                </TableCell>
              </TableRow>
            ) : (
              rows.map((r) => (
                <TableRow key={`${r.type}-${r.name}`}>
                  <TableCell>
                    <Chip label={r.type} size="small" variant="outlined" />
                  </TableCell>
                  <TableCell sx={{ fontFamily: "monospace", fontSize: 12 }}>
                    {r.name}
                  </TableCell>
                  <TableCell>
                    {r.persona === "—" ? (
                      <Typography variant="caption" color="text.secondary">
                        —
                      </Typography>
                    ) : (
                      <Chip
                        label={r.persona === "global" ? t("modelHealth.globalPersona") : r.persona}
                        size="small"
                        variant="outlined"
                        color={r.persona === "global" ? "default" : "primary"}
                      />
                    )}
                  </TableCell>
                  <TableCell sx={{ color: "error.main", fontSize: 12 }}>
                    {r.reason}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </TableContainer>
    </>
  );
}

function SchemaDriftSection({ modelId }: { modelId: string }) {
  const t = useT();
  const qc = useQueryClient();

  const events = useQuery({
    queryKey: ["schema-drift", modelId],
    queryFn: () => schemaDriftApi.list(modelId, false),
    refetchInterval: 60000,
  });

  const acknowledge = useMutation({
    mutationFn: (eventId: string) => schemaDriftApi.acknowledge(eventId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["schema-drift", modelId] }),
  });

  const rows = events.data?.items ?? [];

  return (
    <>
      <SectionHeader title={t("modelHealth.schemaDrift")} />
      {events.isLoading ? (
        <CircularProgress size={18} />
      ) : rows.length === 0 ? (
        <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
          <Stack direction="row" alignItems="center" spacing={1}>
            <CheckCircleIcon fontSize="small" color="success" />
            <Typography variant="body2" color="text.secondary">
              {t("modelHealth.noSchemaDrift")}
            </Typography>
          </Stack>
        </Paper>
      ) : (
        <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
          <Table size="small">
            <TableHead>
              <TableRow sx={{ bgcolor: "grey.50" }}>
                <TableCell>{t("modelHealth.colDetected")}</TableCell>
                <TableCell>{t("modelHealth.colTable")}</TableCell>
                <TableCell>{t("modelHealth.colColumn")}</TableCell>
                <TableCell>{t("modelHealth.colChange")}</TableCell>
                <TableCell>{t("modelHealth.colBreaking")}</TableCell>
                <TableCell align="center">{t("modelHealth.colAcknowledge")}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {rows.map((e: SchemaChangeEvent) => (
                <TableRow key={e.id}>
                  <TableCell sx={{ whiteSpace: "nowrap" }}>
                    <Typography variant="caption">
                      {new Date(e.detected_at).toLocaleString()}
                    </Typography>
                  </TableCell>
                  <TableCell sx={{ fontFamily: "monospace", fontSize: 11 }}>
                    {e.table_name ?? "—"}
                  </TableCell>
                  <TableCell sx={{ fontFamily: "monospace", fontSize: 11 }}>
                    {(e.detail.column_name as string) ?? "—"}
                  </TableCell>
                  <TableCell>
                    <Chip
                      label={e.change_type.replace(/_/g, " ")}
                      size="small"
                      color={
                        e.change_type === "column_removed"
                          ? "error"
                          : e.change_type === "type_changed"
                          ? "warning"
                          : "default"
                      }
                    />
                  </TableCell>
                  <TableCell>
                    {e.is_breaking ? (
                      <Chip label={t("modelHealth.breakingChip")} size="small" color="error" variant="outlined" />
                    ) : (
                      <Typography variant="caption" color="text.secondary">—</Typography>
                    )}
                  </TableCell>
                  <TableCell align="center">
                    <Tooltip title={t("modelHealth.acknowledgeTooltip")}>
                      <IconButton
                        size="small"
                        onClick={() => acknowledge.mutate(e.id)}
                        disabled={acknowledge.isPending}
                      >
                        <CheckCircleIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </>
  );
}


function DataQualitySection({
  projectId,
  modelId,
}: {
  projectId: string;
  modelId: string;
}) {
  const t = useT();
  const { data: rules } = useQuery<DataQualityRule[]>({
    queryKey: ["data-quality-rules", projectId, modelId],
    queryFn: () => dataQualityApi.list(projectId, modelId),
    refetchInterval: 120_000,
  });

  const failing = (rules ?? []).filter(
    (r) => r.is_enabled && r.last_violation_count != null && r.last_violation_count > 0,
  );

  return (
    <>
      <Typography variant="subtitle2" gutterBottom sx={{ mt: 2 }}>
        {t("modelHealth.dataQuality")}
      </Typography>
      {failing.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t("modelHealth.noDataQualityViolations")}
        </Typography>
      ) : (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>{t("modelHealth.colRule")}</TableCell>
              <TableCell>{t("modelHealth.colRuleType")}</TableCell>
              <TableCell>{t("modelHealth.colSeverity")}</TableCell>
              <TableCell>{t("modelHealth.colViolations")}</TableCell>
              <TableCell>{t("modelHealth.colLastChecked")}</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {failing.map((rule) => (
              <TableRow key={rule.id}>
                <TableCell>{rule.name}</TableCell>
                <TableCell>{rule.rule_type}</TableCell>
                <TableCell>
                  <Chip
                    label={rule.severity}
                    size="small"
                    color={rule.severity === "error" ? "error" : "warning"}
                  />
                </TableCell>
                <TableCell>{rule.last_violation_count}</TableCell>
                <TableCell sx={{ whiteSpace: "nowrap" }}>
                  {rule.last_checked_at
                    ? new Date(rule.last_checked_at).toLocaleString()
                    : "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </>
  );
}
