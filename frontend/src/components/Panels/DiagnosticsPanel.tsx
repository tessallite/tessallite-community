import { Fragment, useCallback, useMemo, useState } from "react";
import { safeLocalGet } from "../../utils/safeLocalStorage";
import { useParams } from "react-router-dom";
import { useT } from "../../i18n";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Collapse,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import RefreshIcon from "@mui/icons-material/Refresh";
import DownloadIcon from "@mui/icons-material/Download";
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome";
import CalculateIcon from "@mui/icons-material/Calculate";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import { useQuery } from "@tanstack/react-query";
import { useQueryLogs, useModelRefreshRuns, useOptimizerRuns, useAIOptimizerRuns, useAIOptimizerRun } from "../../api/hooks";
import { logsApi } from "../../api/client";
import type { QueryLog, AIOptimizerRun, OptimizerRunEntry } from "../../api/types";
import { ui, statusColor } from "../../theme/tokens";

type OptimisationRow =
  | { kind: "ai"; at: number; ai: AIOptimizerRun }
  | { kind: "rule"; at: number; rule: OptimizerRunEntry; idx: number };

/**
 * One row of the unified Optimisation activity log for an AI optimiser run.
 *
 * The list endpoint returns only a lightweight summary (F-011-15); when this
 * row is expanded it fetches the full run detail — recommendations, decision
 * log, and raw LLM response — on demand. The summary fields (status, counts,
 * timestamps) render from the list payload immediately.
 */
function AIRunRow({
  run,
  expanded,
  onToggle,
  showRaw,
  onToggleRaw,
  t,
}: {
  run: AIOptimizerRun;
  expanded: boolean;
  onToggle: () => void;
  showRaw: boolean;
  onToggleRaw: () => void;
  t: (key: string, vars?: Record<string, string>) => string;
}) {
  const detail = useAIOptimizerRun(expanded ? run.id : null);
  // Prefer the fetched detail (has recommendations / decision log / raw
  // response); fall back to the summary while the detail is loading.
  const r = detail.data ?? run;
  return (
    <Fragment>
      <TableRow hover sx={{ cursor: "pointer" }} onClick={onToggle}>
        <TableCell sx={{ width: 28, px: 0.5 }}>
          <IconButton size="small">
            {expanded ? <ExpandLessIcon fontSize="small" /> : <ExpandMoreIcon fontSize="small" />}
          </IconButton>
        </TableCell>
        <TableCell sx={{ whiteSpace: "nowrap" }}>
          <Typography variant="caption">{new Date(run.started_at).toLocaleString()}</Typography>
        </TableCell>
        <TableCell>
          <Typography component="span" variant="caption" sx={{ display: "inline-flex", alignItems: "center", gap: 0.25, px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.greenBg, color: ui.green, fontWeight: 500, fontSize: 11 }}><AutoAwesomeIcon sx={{ fontSize: 12 }} /> {t("diagnostics.aiLabel")}</Typography>
          {run.is_dry_run && <Typography component="span" variant="caption" sx={{ ml: 0.5, px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.mutedBg, color: ui.muted, fontSize: 11 }}>{t("diagnostics.dryRunLabel")}</Typography>}
        </TableCell>
        <TableCell>
          {(() => { const sc = statusColor(run.status); return <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, fontWeight: 500, fontSize: 11, bgcolor: sc.bg, color: sc.fg }}>{run.status}</Typography>; })()}
        </TableCell>
        <TableCell sx={{ fontFamily: "monospace", fontSize: 11 }}>{run.llm_model ?? "--"}</TableCell>
        <TableCell>{run.recommendations_count}</TableCell>
        <TableCell>
          {(() => { const sc = statusColor(run.aggregates_created > 0 ? "success" : "default"); return <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, fontWeight: 500, fontSize: 11, bgcolor: sc.bg, color: sc.fg }}>{run.aggregates_created}</Typography>; })()}
        </TableCell>
      </TableRow>
      <TableRow>
        <TableCell colSpan={7} sx={{ py: 0, borderBottom: expanded ? undefined : "none" }}>
          <Collapse in={expanded} unmountOnExit>
            <Box sx={{ py: 1.5, px: 1 }}>
              <Box display="flex" gap={1} mb={1} flexWrap="wrap">
                <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.mutedBg, color: ui.muted, fontSize: 11 }}>{t("diagnostics.triggeredLabel")}: {r.triggered_by}</Typography>
                <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.mutedBg, color: ui.muted, fontSize: 11 }}>{t("diagnostics.providerLabel")}: {r.llm_provider ?? "—"}</Typography>
                <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.mutedBg, color: ui.muted, fontSize: 11 }}>{t("diagnostics.skippedLabel")}: {r.aggregates_skipped}</Typography>
                {r.completed_at && <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.mutedBg, color: ui.muted, fontSize: 11 }}>{t("diagnostics.completedLabel")}: {new Date(r.completed_at).toLocaleString()}</Typography>}
              </Box>
              {detail.isLoading && <CircularProgress size={16} />}
              {r.error_message && <Alert severity="error" sx={{ mb: 1 }}>{r.error_message}</Alert>}
              {r.analysis_notes && (
                <Box mb={1}>
                  <Typography variant="caption" fontWeight={600} display="block" mb={0.5}>{t("diagnostics.analysisNotesLabel")}</Typography>
                  <Typography variant="caption" display="block" color="text.secondary">{r.analysis_notes}</Typography>
                </Box>
              )}
              {r.recommendations && r.recommendations.length > 0 && (
                <>
                  <Typography variant="caption" fontWeight={600} display="block" mb={0.5}>{t("diagnostics.recommendationsLabel")}</Typography>
                  {r.recommendations.map((rec, idx) => (
                    <Box key={rec.id ?? idx} sx={{ p: 1, mb: 0.5, border: "1px solid", borderColor: "divider", borderRadius: 1, bgcolor: ui.mutedBg }}>
                      <Box display="flex" gap={0.5} flexWrap="wrap" mb={0.5}>
                        <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.mutedBg, color: ui.muted, fontSize: 11 }}>{rec.status}</Typography>
                      </Box>
                      <Typography variant="caption" display="block"><strong>{t("diagnostics.grainLabel")}:</strong> {rec.grain.join(", ")}</Typography>
                      <Typography variant="caption" display="block"><strong>{t("diagnostics.measuresLabel")}:</strong> {rec.measures.map((m) => (m.aggregation_function ? `${m.name} (${m.aggregation_function})` : m.name)).join(", ")}</Typography>
                      {rec.rationale && <Typography variant="caption" display="block" color="text.secondary" mt={0.5}>{rec.rationale}</Typography>}
                    </Box>
                  ))}
                </>
              )}
              {r.diagnostics_log && r.diagnostics_log.length > 0 && (
                <Box mt={1}>
                  <Typography variant="caption" fontWeight={600} display="block" mb={0.5}>{t("diagnostics.decisionLogLabel")}</Typography>
                  {r.diagnostics_log.map((entry, di) => (
                    <Box key={di} mb={0.25}>
                      <Box display="flex" gap={0.5} alignItems="baseline">
                        <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: entry.step === "materialise" ? ui.greenBg : ui.mutedBg, color: entry.step === "materialise" ? ui.green : ui.muted, fontSize: 10, fontWeight: 500, minWidth: 60, textAlign: "center" }}>
                          {entry.step}
                        </Typography>
                        <Typography variant="caption" sx={{ fontSize: 11 }}>{entry.message}</Typography>
                      </Box>
                      {entry.code_map && Object.keys(entry.code_map).length > 0 && (
                        <Box sx={{ ml: "68px", mt: 0.25, p: 0.5, borderRadius: 0.5, bgcolor: ui.mutedBg, fontFamily: "monospace", fontSize: 10, lineHeight: 1.6, maxHeight: 120, overflow: "auto" }}>
                          {Object.entries(entry.code_map).map(([code, name]) => (
                            <Box key={code}>{code} &rarr; {name}</Box>
                          ))}
                        </Box>
                      )}
                    </Box>
                  ))}
                </Box>
              )}
              {r.raw_llm_response && (
                <Box mt={1}>
                  <Button size="small" variant="text" onClick={(e) => { e.stopPropagation(); onToggleRaw(); }}>
                    {showRaw ? t("diagnostics.hideRawLlmResponse") : t("diagnostics.showRawLlmResponse")}
                  </Button>
                  <Collapse in={showRaw}>
                    <Paper variant="outlined" sx={{ p: 1, mt: 0.5, fontFamily: "monospace", fontSize: 11, whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: 200, overflow: "auto", bgcolor: ui.mutedBg }}>
                      {r.raw_llm_response}
                    </Paper>
                  </Collapse>
                </Box>
              )}
            </Box>
          </Collapse>
        </TableCell>
      </TableRow>
    </Fragment>
  );
}

/**
 * Stored parse -> bind -> route trace for one logged query (F-030-25).
 *
 * RouteLog stages were write-only and reachable only via the new
 * ``/logs/queries/{id}/trace`` endpoint (F-030-16); this renders them inline
 * in the query-detail dialog so a modeler can see why a query routed as it did
 * without raw DB access. Fetched lazily on expand so opening a log is cheap.
 */
function RouteTraceSection({ projectId, queryLogId }: { projectId: string; queryLogId: string }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const trace = useQuery({
    queryKey: ["queryTrace", projectId, queryLogId],
    queryFn: () => logsApi.queryTrace(projectId, queryLogId),
    enabled: open,
  });
  return (
    <Box mt={2}>
      <Button
        size="small"
        variant="text"
        startIcon={open ? <ExpandLessIcon fontSize="small" /> : <ExpandMoreIcon fontSize="small" />}
        onClick={() => setOpen((v) => !v)}
      >
        {open ? t("diagnostics.hideRouteTrace") : t("diagnostics.showRouteTrace")}
      </Button>
      <Collapse in={open}>
        {trace.isLoading ? (
          <Box sx={{ p: 1 }}><CircularProgress size={16} /></Box>
        ) : (trace.data ?? []).length === 0 ? (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", p: 1 }}>
            {t("diagnostics.noRouteTrace")}
          </Typography>
        ) : (
          <Box mt={0.5}>
            {(trace.data ?? []).map((stage, i) => (
              <Box key={i} mb={0.5}>
                <Typography
                  component="span"
                  variant="caption"
                  sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.mutedBg, color: ui.muted, fontSize: 10, fontWeight: 600, textTransform: "uppercase" }}
                >
                  {stage.route_stage}
                </Typography>
                <Paper variant="outlined" sx={{ p: 1, mt: 0.25, fontFamily: "monospace", fontSize: 11, whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: 200, overflow: "auto", bgcolor: ui.mutedBg }}>
                  {JSON.stringify(stage.detail, null, 2)}
                </Paper>
              </Box>
            ))}
          </Box>
        )}
      </Collapse>
    </Box>
  );
}

export default function DiagnosticsPanel() {
  const t = useT();
  const { tenantId, projectId, modelId } = useParams<{ tenantId: string; projectId: string; modelId: string }>();
  const resolvedTenantId = tenantId || safeLocalGet("tenant_id", "");
  const [tab, setTab] = useState(0);
  const [selectedLog, setSelectedLog] = useState<QueryLog | null>(null);
  const [expandedAIRun, setExpandedAIRun] = useState<string | null>(null);
  const [showRawLLM, setShowRawLLM] = useState<string | null>(null);
  const [logStatusFilter, setLogStatusFilter] = useState<string>("all");
  const [logRouteFilter, setLogRouteFilter] = useState<string>("all");
  const [logClientKindFilter, setLogClientKindFilter] = useState<"all" | "looker_studio" | "looker_cloud">("all");
  const [logUserFilter, setLogUserFilter] = useState<string>("");
  const [logDateFrom, setLogDateFrom] = useState<string>("");
  const [logDateTo, setLogDateTo] = useState<string>("");

  const queryLogFilters = useMemo(() => ({
    modelId: modelId!,
    page: 1,
    pageSize: 100,
    status: logStatusFilter === "all" ? undefined : logStatusFilter,
    routeType: logRouteFilter === "all" ? undefined : logRouteFilter,
    clientKind: logClientKindFilter === "all" ? undefined : logClientKindFilter,
    userIdentity: logUserFilter || undefined,
    dateFrom: logDateFrom || undefined,
    dateTo: logDateTo || undefined,
  }), [modelId, logStatusFilter, logRouteFilter, logClientKindFilter, logUserFilter, logDateFrom, logDateTo]);

  const queryLogs = useQueryLogs(projectId!, queryLogFilters);

  const handleExportCsv = useCallback(async () => {
    if (!projectId) return;
    const blob = await logsApi.exportCsv(projectId, queryLogFilters);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "query_log_export.csv";
    a.click();
    URL.revokeObjectURL(url);
  }, [projectId, queryLogFilters]);
  const refreshRuns = useModelRefreshRuns(projectId!, modelId!);
  const optimizerRuns = useOptimizerRuns();
  const aiRuns = useAIOptimizerRuns(resolvedTenantId, modelId);

  // F-011-14: merge AI runs (keyed on started_at) and rule-based runs (keyed on
  // ran_at) into one list sorted newest-first, so the activity log reads
  // chronologically instead of "all AI then all rule-based".
  const mergedOptimisationRows = useMemo<OptimisationRow[]>(() => {
    const rows: OptimisationRow[] = [];
    for (const ai of aiRuns.data ?? []) {
      rows.push({ kind: "ai", at: new Date(ai.started_at).getTime(), ai });
    }
    (optimizerRuns.data ?? []).forEach((rule, idx) => {
      rows.push({ kind: "rule", at: new Date(rule.ran_at).getTime(), rule, idx });
    });
    rows.sort((a, b) => b.at - a.at);
    return rows;
  }, [aiRuns.data, optimizerRuns.data]);

  return (
    <Box>
      <Tabs
        value={tab}
        onChange={(_, v) => setTab(v)}
        variant="scrollable"
        scrollButtons={false}
        sx={{ borderBottom: 1, borderColor: "divider", mb: 1, minHeight: 36 }}
      >
        <Tab label={t("diagnostics.tabQueryLog")} sx={{ minHeight: 36, py: 0, textTransform: "none", fontSize: 13 }} />
        <Tab label={t("diagnostics.tabOptimisation")} sx={{ minHeight: 36, py: 0, textTransform: "none", fontSize: 13 }} />
        <Tab label={t("diagnostics.tabAggregations")} sx={{ minHeight: 36, py: 0, textTransform: "none", fontSize: 13 }} />
      </Tabs>

      {/* ── Query Log ── */}
      {tab === 0 && (
        <Box>
          <Box display="flex" alignItems="center" mb={1} gap={1} flexWrap="wrap">
            <Typography variant="subtitle2" fontWeight={600} sx={{ mr: "auto" }}>
              {t("diagnostics.queryLogTitle")}
            </Typography>
            <TextField
              size="small"
              label={t("diagnostics.dateFromLabel")}
              type="datetime-local"
              value={logDateFrom}
              onChange={(e) => setLogDateFrom(e.target.value)}
              InputLabelProps={{ shrink: true, sx: { fontSize: 12 } }}
              sx={{ width: 180, "& input": { fontSize: 12, height: 14 } }}
            />
            <TextField
              size="small"
              label={t("diagnostics.dateToLabel")}
              type="datetime-local"
              value={logDateTo}
              onChange={(e) => setLogDateTo(e.target.value)}
              InputLabelProps={{ shrink: true, sx: { fontSize: 12 } }}
              sx={{ width: 180, "& input": { fontSize: 12, height: 14 } }}
            />
            <TextField
              size="small"
              label={t("diagnostics.userLabel")}
              value={logUserFilter}
              onChange={(e) => setLogUserFilter(e.target.value)}
              placeholder={t("diagnostics.userPlaceholder")}
              InputLabelProps={{ sx: { fontSize: 12 } }}
              sx={{ width: 140, "& input": { fontSize: 12, height: 14 } }}
            />
            <FormControl size="small" sx={{ minWidth: 90 }}>
              <InputLabel sx={{ fontSize: 12 }}>{t("diagnostics.statusLabel")}</InputLabel>
              <Select
                value={logStatusFilter}
                label={t("diagnostics.statusLabel")}
                onChange={(e) => setLogStatusFilter(e.target.value)}
                sx={{ fontSize: 12, height: 30 }}
              >
                <MenuItem value="all">{t("diagnostics.statusAll")}</MenuItem>
                <MenuItem value="success">{t("diagnostics.statusSuccess")}</MenuItem>
                <MenuItem value="error">{t("diagnostics.statusError")}</MenuItem>
              </Select>
            </FormControl>
            <FormControl size="small" sx={{ minWidth: 100 }}>
              <InputLabel sx={{ fontSize: 12 }}>{t("diagnostics.routeLabel")}</InputLabel>
              <Select
                value={logRouteFilter}
                label={t("diagnostics.routeLabel")}
                onChange={(e) => setLogRouteFilter(e.target.value)}
                sx={{ fontSize: 12, height: 30 }}
              >
                <MenuItem value="all">{t("diagnostics.routeAll")}</MenuItem>
                <MenuItem value="source">{t("diagnostics.routeSource")}</MenuItem>
                <MenuItem value="aggregate">{t("diagnostics.routeAggregate")}</MenuItem>
                <MenuItem value="pocket">{t("diagnostics.routePocket")}</MenuItem>
                <MenuItem value="introspect">{t("diagnostics.routeIntrospect")}</MenuItem>
              </Select>
            </FormControl>
            <FormControl size="small" sx={{ minWidth: 120 }}>
              <InputLabel id="query-log-client-kind-label" sx={{ fontSize: 12 }}>{t("diagnostics.clientLabel")}</InputLabel>
              <Select
                labelId="query-log-client-kind-label"
                id="query-log-client-kind"
                value={logClientKindFilter}
                label={t("diagnostics.clientLabel")}
                onChange={(e) => setLogClientKindFilter(e.target.value as "all" | "looker_studio" | "looker_cloud")}
                sx={{ fontSize: 12, height: 30 }}
              >
                <MenuItem value="all">{t("diagnostics.clientAll")}</MenuItem>
                <MenuItem value="looker_studio">{t("diagnostics.clientLookerStudio")}</MenuItem>
                <MenuItem value="looker_cloud">{t("diagnostics.clientLookerCloud")}</MenuItem>
              </Select>
            </FormControl>
            <Tooltip title={t("diagnostics.exportCsvTooltip")}>
              <IconButton size="small" onClick={handleExportCsv}>
                <DownloadIcon fontSize="small" />
              </IconButton>
            </Tooltip>
            <Tooltip title={t("common.refresh")}>
              <IconButton
                size="small"
                onClick={() => queryLogs.refetch()}
                disabled={queryLogs.isFetching}
              >
                {queryLogs.isFetching ? (
                  <CircularProgress size={16} />
                ) : (
                  <RefreshIcon fontSize="small" />
                )}
              </IconButton>
            </Tooltip>
          </Box>

          {queryLogs.isLoading ? (
            <CircularProgress size={20} />
          ) : (
            <TableContainer component={Paper} variant="outlined">
              <Table size="small">
                <TableHead>
                  <TableRow sx={{ bgcolor: ui.tableHeaderBg }}>
                    <TableCell>{t("diagnostics.timestampHeader")}</TableCell>
                    <TableCell>{t("diagnostics.userHeader")}</TableCell>
                    <TableCell>{t("diagnostics.queryHeader")}</TableCell>
                    <TableCell>{t("diagnostics.statusHeader")}</TableCell>
                    <TableCell>{t("diagnostics.hitMissHeader")}</TableCell>
                    <TableCell>{t("diagnostics.sourceHeader")}</TableCell>
                    <TableCell>{t("diagnostics.msHeader")}</TableCell>
                    <TableCell>{t("diagnostics.rowsHeader")}</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {queryLogs.data?.items.map((l) => (
                    <TableRow
                      key={l.id}
                      hover
                      sx={{ cursor: "pointer" }}
                      onClick={() => setSelectedLog(l)}
                    >
                      <TableCell sx={{ whiteSpace: "nowrap" }}>
                        <Typography variant="caption">
                          {new Date(l.created_at).toLocaleString()}
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ maxWidth: 120, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        <Tooltip title={l.user_identity || ""}>
                          <Typography variant="caption" sx={{ fontSize: 11 }}>{l.user_identity || "--"}</Typography>
                        </Tooltip>
                      </TableCell>
                      <TableCell sx={{ maxWidth: 320 }}>
                        <Typography
                          variant="caption"
                          sx={{
                            fontFamily: "monospace",
                            fontSize: 11,
                            display: "block",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {l.raw_query}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        {l.status === "error" ? (
                          <Tooltip title={l.error_type || t("diagnostics.errorLabel")}>
                            <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, fontWeight: 600, fontSize: 11, bgcolor: ui.redBg, color: ui.red }}>{t("diagnostics.errorLabel")}</Typography>
                          </Tooltip>
                        ) : (
                          <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, fontWeight: 600, fontSize: 11, bgcolor: ui.greenLight, color: ui.green }}>{t("diagnostics.okLabel")}</Typography>
                        )}
                      </TableCell>
                      <TableCell>
                        {(() => {
                          const isHit = l.route_type === "aggregate" || l.route_type === "pocket";
                          return (
                        <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, fontWeight: 600, fontSize: 11, bgcolor: isHit ? ui.greenLight : ui.goldLight, color: isHit ? ui.green : ui.goldDark }}>{isHit ? t("diagnostics.hitLabel") : t("diagnostics.missLabel")}</Typography>
                          );
                        })()}
                      </TableCell>
                      <TableCell>
                        <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, fontWeight: 500, fontSize: 11, ...(() => { const c = statusColor(l.route_type === "aggregate" || l.route_type === "pocket" ? "success" : "default"); return { bgcolor: c.bg, color: c.fg }; })() }}>{l.route_type}</Typography>
                      </TableCell>
                      <TableCell>{l.execution_ms ?? "--"}</TableCell>
                      <TableCell>{l.rows_returned ?? "--"}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          )}

          {/* Query Detail Dialog */}
          <Dialog
            open={!!selectedLog}
            onClose={() => setSelectedLog(null)}
            maxWidth="md"
            fullWidth
          >
            <DialogTitle>{t("diagnostics.queryDetailTitle")}</DialogTitle>
            {selectedLog && (
              <DialogContent>
                <Box display="flex" gap={1} mb={2} flexWrap="wrap">
                  {selectedLog.status === "error" ? (
                    <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, fontWeight: 600, fontSize: 11, bgcolor: ui.redBg, color: ui.red }}>{t("diagnostics.errorLabel")}</Typography>
                  ) : (
                    <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, fontWeight: 600, fontSize: 11, bgcolor: ui.greenLight, color: ui.green }}>{t("diagnostics.okLabel")}</Typography>
                  )}
                  {(() => {
                    const isHit = selectedLog.route_type === "aggregate" || selectedLog.route_type === "pocket";
                    return (
                  <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, fontWeight: 600, fontSize: 11, bgcolor: isHit ? ui.greenLight : ui.goldLight, color: isHit ? ui.green : ui.goldDark }}>{isHit ? t("diagnostics.hitLabel") : t("diagnostics.missLabel")}</Typography>
                    );
                  })()}
                  <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, fontWeight: 500, fontSize: 11, ...(() => { const c = statusColor(selectedLog.route_type === "aggregate" || selectedLog.route_type === "pocket" ? "success" : "default"); return { bgcolor: c.bg, color: c.fg }; })() }}>{selectedLog.route_type}</Typography>
                  <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.mutedBg, color: ui.muted, fontSize: 11 }}>{selectedLog.execution_ms ?? "--"} ms</Typography>
                  <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.mutedBg, color: ui.muted, fontSize: 11 }}>{selectedLog.rows_returned ?? "--"} {t("diagnostics.rowsLabel")}</Typography>
                  <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.mutedBg, color: ui.muted, fontSize: 11 }}>{selectedLog.protocol}</Typography>
                  {selectedLog.client_kind && (
                    <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.mutedBg, color: ui.muted, fontSize: 11 }}>
                      {selectedLog.client_kind === "looker_cloud" ? t("diagnostics.clientLookerCloud") : t("diagnostics.clientLookerStudio")}
                    </Typography>
                  )}
                </Box>

                <Typography variant="caption" color="text.secondary" display="block" mb={0.5}>{t("diagnostics.timestampLabel")}</Typography>
                <Typography variant="body2" mb={2}>{new Date(selectedLog.created_at).toLocaleString()}</Typography>

                {selectedLog.user_identity && (
                  <>
                    <Typography variant="caption" color="text.secondary" display="block" mb={0.5}>{t("diagnostics.userLabel")}</Typography>
                    <Typography variant="body2" mb={2}>{selectedLog.user_identity}</Typography>
                  </>
                )}

                <Typography variant="caption" color="text.secondary" display="block" mb={0.5}>{t("diagnostics.originalQueryLabel")}</Typography>
                <Paper variant="outlined" sx={{ p: 1.5, mb: 2, fontFamily: "monospace", fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-word", bgcolor: ui.mutedBg, maxHeight: 200, overflow: "auto" }}>
                  {selectedLog.raw_query}
                </Paper>

                {selectedLog.rewritten_query && (
                  <>
                    <Typography variant="caption" color="text.secondary" display="block" mb={0.5}>{t("diagnostics.rewrittenQueryLabel")}</Typography>
                    <Paper variant="outlined" sx={{ p: 1.5, mb: 2, fontFamily: "monospace", fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-word", bgcolor: ui.mutedBg, maxHeight: 200, overflow: "auto" }}>
                      {selectedLog.rewritten_query}
                    </Paper>
                  </>
                )}

                {selectedLog.security_rules_applied != null &&
                  (!Array.isArray(selectedLog.security_rules_applied) ||
                    selectedLog.security_rules_applied.length > 0) && (
                  <>
                    <Typography variant="caption" color="text.secondary" display="block" mb={0.5}>{t("diagnostics.securityRulesLabel")}</Typography>
                    <Paper variant="outlined" sx={{ p: 1.5, mb: 2, fontFamily: "monospace", fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-word", bgcolor: ui.mutedBg, maxHeight: 200, overflow: "auto" }}>
                      {JSON.stringify(selectedLog.security_rules_applied, null, 2)}
                    </Paper>
                  </>
                )}

                {selectedLog.status === "error" && (
                  <>
                    <Typography variant="caption" color="text.secondary" display="block" mb={0.5}>{t("diagnostics.errorTypeLabel")}</Typography>
                    <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, fontWeight: 600, fontSize: 11, bgcolor: ui.redBg, color: ui.red, mb: 1, display: "inline-block" }}>{selectedLog.error_type}</Typography>
                    {selectedLog.error_detail && (
                      <>
                        <Typography variant="caption" color="text.secondary" display="block" mb={0.5} mt={1}>{t("diagnostics.errorDetailLabel")}</Typography>
                        <Paper variant="outlined" sx={{ p: 1.5, mb: 2, fontFamily: "monospace", fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-word", bgcolor: ui.redBg, maxHeight: 200, overflow: "auto" }}>
                          {selectedLog.error_detail}
                        </Paper>
                      </>
                    )}
                  </>
                )}

                {selectedLog.aggregate_id && (
                  <>
                    <Typography variant="caption" color="text.secondary" display="block" mb={0.5}>{t("diagnostics.aggregateIdLabel")}</Typography>
                    <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: 12 }} mb={2}>{selectedLog.aggregate_id}</Typography>
                  </>
                )}
                {selectedLog.pocket_id && (
                  <>
                    <Typography variant="caption" color="text.secondary" display="block" mb={0.5}>{t("diagnostics.pocketIdLabel")}</Typography>
                    <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: 12 }} mb={2}>{selectedLog.pocket_id}</Typography>
                  </>
                )}

                <Typography variant="caption" color="text.secondary" display="block" mb={0.5}>{t("diagnostics.queryFingerprintLabel")}</Typography>
                <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: 11 }}>{selectedLog.query_fingerprint}</Typography>

                {projectId && (
                  <RouteTraceSection projectId={projectId} queryLogId={selectedLog.id} />
                )}
              </DialogContent>
            )}
            <DialogActions>
              <Button onClick={() => setSelectedLog(null)}>{t("common.close")}</Button>
            </DialogActions>
          </Dialog>
        </Box>
      )}

      {/* ── Optimisation (unified AI + Rule-based log) ── */}
      {tab === 1 && (
        <Box>
          <Box display="flex" alignItems="center" mb={1} gap={1}>
            <Typography variant="subtitle2" fontWeight={600} flexGrow={1}>
              {t("diagnostics.optimisationLogTitle")}
            </Typography>
            <Tooltip title={t("common.refresh")}>
              <IconButton size="small" onClick={() => { optimizerRuns.refetch(); aiRuns.refetch(); }} disabled={optimizerRuns.isFetching || aiRuns.isFetching}>
                {(optimizerRuns.isFetching || aiRuns.isFetching) ? <CircularProgress size={16} /> : <RefreshIcon fontSize="small" />}
              </IconButton>
            </Tooltip>
          </Box>

          {(optimizerRuns.isLoading && aiRuns.isLoading) ? (
            <CircularProgress size={20} />
          ) : (
            <TableContainer component={Paper} variant="outlined">
              <Table size="small">
                <TableHead>
                  <TableRow sx={{ bgcolor: ui.tableHeaderBg }}>
                    <TableCell />
                    <TableCell>{t("diagnostics.timestampHeader")}</TableCell>
                    <TableCell>{t("diagnostics.originHeader")}</TableCell>
                    <TableCell>{t("diagnostics.statusHeader")}</TableCell>
                    <TableCell>{t("diagnostics.llmHeader")}</TableCell>
                    <TableCell>{t("diagnostics.candidatesHeader")}</TableCell>
                    <TableCell>{t("diagnostics.createdHeader")}</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {/* F-011-14: AI and rule-based runs are merged into one
                      chronologically-sorted activity log (newest first), rather
                      than rendering all AI runs then all rule-based runs. */}
                  {mergedOptimisationRows.map((row) =>
                    row.kind === "ai" ? (
                      <AIRunRow
                        key={`ai-${row.ai.id}`}
                        run={row.ai}
                        expanded={expandedAIRun === row.ai.id}
                        onToggle={() => setExpandedAIRun(expandedAIRun === row.ai.id ? null : row.ai.id)}
                        showRaw={showRawLLM === row.ai.id}
                        onToggleRaw={() => setShowRawLLM(showRawLLM === row.ai.id ? null : row.ai.id)}
                        t={t}
                      />
                    ) : (
                      <TableRow key={`rule-${row.idx}`}>
                        <TableCell />
                        <TableCell sx={{ whiteSpace: "nowrap" }}>
                          <Typography variant="caption">{new Date(row.rule.ran_at).toLocaleString()}</Typography>
                        </TableCell>
                        <TableCell>
                          <Typography component="span" variant="caption" sx={{ display: "inline-flex", alignItems: "center", gap: 0.25, px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.purpleBg, color: ui.purple, fontWeight: 500, fontSize: 11 }}><CalculateIcon sx={{ fontSize: 12 }} /> {t("diagnostics.ruleBasedLabel")}</Typography>
                        </TableCell>
                        <TableCell>
                          {/* F-011-14: status reflects whether the sweep had
                              errors instead of being hardcoded "completed". */}
                          {(() => {
                            const hasErrors = row.rule.errors.length > 0;
                            const sc = statusColor(hasErrors ? "error" : "completed");
                            return <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, fontWeight: 500, fontSize: 11, bgcolor: sc.bg, color: sc.fg }}>{hasErrors ? t("diagnostics.statusError") : t("diagnostics.completedStatus")}</Typography>;
                          })()}
                        </TableCell>
                        <TableCell>--</TableCell>
                        <TableCell>{row.rule.candidates_found}</TableCell>
                        <TableCell>
                          {(() => { const sc = statusColor(row.rule.aggregates_created > 0 ? "success" : "default"); return <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, fontWeight: 500, fontSize: 11, bgcolor: sc.bg, color: sc.fg }}>{row.rule.aggregates_created}</Typography>; })()}
                          {row.rule.errors.length > 0 && (
                            <Tooltip title={row.rule.errors.join("\n")}>
                              <Typography component="span" variant="caption" sx={{ ml: 0.5, px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.redBg, color: ui.red, fontWeight: 500, fontSize: 11 }}>{row.rule.errors.length} {t("diagnostics.errorsSuffix")}</Typography>
                            </Tooltip>
                          )}
                        </TableCell>
                      </TableRow>
                    )
                  )}
                  {(aiRuns.data ?? []).length === 0 && (optimizerRuns.data ?? []).length === 0 && (
                    <TableRow>
                      <TableCell colSpan={7}>
                        <Typography variant="body2" color="text.secondary" textAlign="center">
                          {t("diagnostics.noOptimiserRuns")}
                        </Typography>
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </TableContainer>
          )}
        </Box>
      )}

      {/* ── Aggregations ── */}
      {tab === 2 && (
        <Box>
          <Box display="flex" alignItems="center" mb={1} gap={1}>
            <Typography variant="subtitle2" fontWeight={600} flexGrow={1}>
              {t("diagnostics.aggregationRefreshHistoryTitle")}
            </Typography>
            <Tooltip title={t("common.refresh")}>
              <IconButton size="small" onClick={() => refreshRuns.refetch()} disabled={refreshRuns.isFetching}>
                {refreshRuns.isFetching ? <CircularProgress size={16} /> : <RefreshIcon fontSize="small" />}
              </IconButton>
            </Tooltip>
          </Box>

          {refreshRuns.isLoading ? (
            <CircularProgress size={20} />
          ) : refreshRuns.data?.length === 0 ? (
            <Typography variant="body2" color="text.secondary">{t("diagnostics.noRefreshRuns")}</Typography>
          ) : (
            <TableContainer component={Paper} variant="outlined">
              <Table size="small">
                <TableHead>
                  <TableRow sx={{ bgcolor: ui.tableHeaderBg }}>
                    <TableCell>{t("diagnostics.startedHeader")}</TableCell>
                    <TableCell>{t("diagnostics.aggregateHeader")}</TableCell>
                    <TableCell>{t("diagnostics.statusHeader")}</TableCell>
                    <TableCell>{t("diagnostics.modeHeader")}</TableCell>
                    <TableCell align="right">{t("diagnostics.rowsHeader")}</TableCell>
                    <TableCell align="right">{t("diagnostics.durationHeader")}</TableCell>
                    <TableCell>{t("diagnostics.triggeredByHeader")}</TableCell>
                    <TableCell>{t("diagnostics.detailsHeader")}</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {refreshRuns.data?.map((r) => {
                    const isFailed = r.status === "failed";
                    const isRunning = r.status === "running";
                    const rowsText =
                      r.rows_written != null ? r.rows_written.toLocaleString() : "--";
                    const durationText =
                      r.duration_ms != null
                        ? r.duration_ms >= 1000
                          ? `${(r.duration_ms / 1000).toFixed(2)}s`
                          : `${r.duration_ms}ms`
                        : isRunning
                        ? "…"
                        : "--";
                    const detailText = isFailed
                      ? r.error_message ?? t("diagnostics.failedNoError")
                      : r.status === "completed"
                      ? r.rows_written === 1
                        ? t("diagnostics.rowsWrittenSingular", { rows: rowsText })
                        : t("diagnostics.rowsWrittenPlural", { rows: rowsText })
                      : "";
                    return (
                      <TableRow key={r.id}>
                        <TableCell sx={{ whiteSpace: "nowrap" }}>
                          <Typography variant="caption">
                            {new Date(r.started_at).toLocaleString()}
                          </Typography>
                        </TableCell>
                        <TableCell
                          sx={{
                            fontFamily: "monospace",
                            fontSize: 11,
                            maxWidth: 200,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          <Tooltip title={r.aggregate_table ?? r.aggregate_definition_id}>
                            <span>{r.aggregate_table ?? "—"}</span>
                          </Tooltip>
                        </TableCell>
                        <TableCell>
                          {(() => { const sc = statusColor(r.status); return <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, fontWeight: 500, fontSize: 11, bgcolor: sc.bg, color: sc.fg }}>{r.status}</Typography>; })()}
                        </TableCell>
                        <TableCell>{r.refresh_mode}</TableCell>
                        <TableCell align="right">{rowsText}</TableCell>
                        <TableCell align="right">{durationText}</TableCell>
                        <TableCell>{r.triggered_by}</TableCell>
                        <TableCell
                          sx={{
                            maxWidth: 360,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                            color: isFailed ? "error.main" : "text.secondary",
                            fontSize: 12,
                          }}
                        >
                          <Tooltip title={detailText} placement="top">
                            <span>{detailText}</span>
                          </Tooltip>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </TableContainer>
          )}
        </Box>
      )}
    </Box>
  );
}
