import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Checkbox,
  CircularProgress,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import { hierarchiesApi, optimizerApiClient } from "../../api/client";
import type {
  GrainSuggestion,
  PredictiveBuildAccepted,
  PredictiveBuildResult,
  PredictiveCandidate,
  PredictivePreview,
} from "../../api/types";
import { isTenantAdmin } from "../../auth/currentUser";
import { useT } from "../../i18n";

function candidateKey(c: PredictiveCandidate): string {
  return `${c.fact_table}|${c.grain.join(",")}|${c.measure_names.join(",")}`;
}

function formatNumber(n: number | null | undefined, tFn: (key: string) => string): string {
  if (n === null || n === undefined) return tFn("common.na");
  return n.toLocaleString();
}

function formatPercent(ratio: number | null | undefined, tFn: (key: string) => string): string {
  if (ratio === null || ratio === undefined) return tFn("common.na");
  return `${(ratio * 100).toFixed(1)}%`;
}

// row_reduction is a multiplier (source_rows / estimated_rows, >= 1),
// not a 0-1 ratio — render it as "N×", never through the percent formatter.
function formatMultiplier(
  multiplier: number | null | undefined,
  tFn: (key: string, vars?: Record<string, string>) => string,
): string {
  if (multiplier === null || multiplier === undefined) return tFn("common.na");
  return tFn("predictiveAgg.rowReductionValue", {
    value: multiplier.toLocaleString(undefined, { maximumFractionDigits: 1 }),
  });
}

interface Props {
  /** When the model requires approval, auto-builds are paused and the
   *  candidates below are the pending approval queue. */
  requiresApproval?: boolean;
}

export default function PredictiveAggregatesPanel({ requiresApproval = false }: Props) {
  const t = useT();
  const { projectId, modelId } = useParams<{ projectId: string; modelId: string }>();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [lastResult, setLastResult] = useState<PredictiveBuildResult | null>(null);
  const [buildRunning, setBuildRunning] = useState(false);
  // The preview is read-only analysis open to every tenant user, but the
  // build endpoint requires tenant_admin — hide what the user cannot do
  // instead of surfacing a 403 alert (review F-1).
  const canBuild = isTenantAdmin();
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Clean up polling timer on unmount.
  useEffect(() => {
    return () => {
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    };
  }, []);

  const pollBuildStatus = useCallback(
    (buildId: string) => {
      if (!modelId) return;
      const poll = async () => {
        try {
          const result = await optimizerApiClient.getPredictiveBuildStatus(
            modelId,
            buildId,
          );
          if (result.status === "running") {
            pollTimerRef.current = setTimeout(poll, 2000);
            return;
          }
          // Build completed or failed.
          setBuildRunning(false);
          setLastResult(result);
          qc.invalidateQueries({ queryKey: ["predictive-preview", modelId] });
          qc.invalidateQueries({ queryKey: ["aggregates"] });
        } catch {
          // Poll failure — stop polling and show last known state.
          setBuildRunning(false);
        }
      };
      pollTimerRef.current = setTimeout(poll, 1500);
    },
    [modelId, qc],
  );

  const previewQuery = useQuery<PredictivePreview>({
    queryKey: ["predictive-preview", modelId],
    queryFn: () => optimizerApiClient.getPredictivePreview(modelId!),
    enabled: Boolean(modelId),
  });

  const candidates = useMemo(
    () => previewQuery.data?.candidates ?? [],
    [previewQuery.data],
  );

  const grainSuggestions = useQuery<GrainSuggestion[]>({
    queryKey: ["grain-suggestions", projectId, modelId],
    queryFn: () => hierarchiesApi.grainSuggestions(projectId!, modelId!),
    enabled: !!projectId && !!modelId,
  });

  const buildMutation = useMutation({
    mutationFn: (vars: {
      selections?: Array<{ grain: string[]; measure_names: string[] }>;
    }) => optimizerApiClient.runPredictiveBuild(modelId!, vars.selections),
    onSuccess: (data: PredictiveBuildAccepted) => {
      setSelected(new Set());
      setBuildRunning(true);
      setLastResult(null);
      pollBuildStatus(data.build_id);
    },
  });

  function toggleAll(checked: boolean) {
    if (!checked) {
      setSelected(new Set());
      return;
    }
    setSelected(new Set(candidates.map(candidateKey)));
  }

  function toggleOne(key: string, checked: boolean) {
    const next = new Set(selected);
    if (checked) next.add(key);
    else next.delete(key);
    setSelected(next);
  }

  function handleBuildSelected() {
    if (!modelId) return;
    const picks = candidates.filter((c) => selected.has(candidateKey(c)));
    if (picks.length === 0) return;
    buildMutation.mutate({
      selections: picks.map((c) => ({
        grain: c.grain,
        measure_names: c.measure_names,
      })),
    });
  }

  function handleBuildAll() {
    if (!modelId) return;
    buildMutation.mutate({});
  }

  if (!modelId) {
    return (
      <Box sx={{ p: 2 }}>
        <Alert severity="error">{t("predictiveAgg.noModelError")}</Alert>
      </Box>
    );
  }

  if (previewQuery.isLoading) {
    return (
      <Box sx={{ p: 4, display: "flex", justifyContent: "center" }}>
        <CircularProgress size={22} />
      </Box>
    );
  }

  if (previewQuery.isError) {
    return (
      <Box sx={{ p: 2 }}>
        <Alert severity="error">
          {t("predictiveAgg.loadError", {
            error: String((previewQuery.error as Error)?.message ?? "unknown error"),
          })}
        </Alert>
      </Box>
    );
  }

  const allSelected = candidates.length > 0 && selected.size === candidates.length;
  const someSelected = selected.size > 0 && !allSelected;
  // F-010-18 — undefined (older API) is treated as "has stats" so the empty
  // state still reads as informational; only an explicit false warns.
  const hasSourceStats = previewQuery.data?.had_stats !== false;
  const buildHadStats = lastResult?.had_stats !== false;

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <Typography variant="body2" color="text.secondary">
        {t("predictiveAgg.description")}
      </Typography>

      {canBuild && requiresApproval && candidates.length > 0 && (
        <Alert severity="warning" data-testid="predictive-approval-pending">
          {t("predictiveAgg.approvalPending", {
            count: String(candidates.length),
          })}
        </Alert>
      )}

      {!canBuild && candidates.length > 0 && (
        <Alert severity="info" data-testid="predictive-read-only-notice">
          {t("predictiveAgg.adminOnlyBuild")}
        </Alert>
      )}

      {(grainSuggestions.data ?? []).length > 0 && (
        <Card variant="outlined">
          <CardContent sx={{ py: 1, "&:last-child": { pb: 1 } }}>
            <Typography variant="subtitle2" fontWeight={700} mb={0.5}>
              {t("predictiveAgg.hierarchySuggestionsTitle")}
            </Typography>
            <Typography variant="caption" color="text.secondary" display="block" mb={1}>
              {t("predictiveAgg.hierarchySuggestionsDesc")}
            </Typography>
            {(grainSuggestions.data ?? []).map((s, i) => (
              <Box key={i} display="flex" alignItems="center" gap={1} mb={0.5}>
                <Typography variant="body2">{s.label}</Typography>
                <Typography variant="caption" color="text.secondary">
                  {t("predictiveAgg.grainLabel", { grain: s.grain.join(", ") })}
                </Typography>
              </Box>
            ))}
          </CardContent>
        </Card>
      )}

      {canBuild && (
        <Box display="flex" gap={1} justifyContent="flex-end" alignItems="center">
          {buildRunning && (
            <>
              <CircularProgress size={16} />
              <Typography variant="body2" color="text.secondary">
                {t("predictiveAgg.building")}
              </Typography>
            </>
          )}
          <Button
            size="small"
            variant="outlined"
            disabled={selected.size === 0 || buildMutation.isPending || buildRunning}
            onClick={handleBuildSelected}
          >
            {buildMutation.isPending && selected.size > 0
              ? t("predictiveAgg.building")
              : t("predictiveAgg.buildSelected", { count: String(selected.size) })}
          </Button>
          <Button
            size="small"
            variant="contained"
            disabled={candidates.length === 0 || buildMutation.isPending || buildRunning}
            onClick={handleBuildAll}
          >
            {buildMutation.isPending && selected.size === 0
              ? t("predictiveAgg.building")
              : t("predictiveAgg.buildAll")}
          </Button>
        </Box>
      )}

      {buildMutation.isError && (
        <Alert severity="error">
          {String((buildMutation.error as Error)?.message ?? t("predictiveAgg.buildFailed"))}
        </Alert>
      )}

      {lastResult && (
        <Alert
          severity={!buildHadStats || lastResult.errors.length ? "warning" : "success"}
          onClose={() => setLastResult(null)}
        >
          {!buildHadStats
            ? t("predictiveAgg.noStats")
            : lastResult.errors.length
            ? t("predictiveAgg.resultErrors", {
                summary: t("predictiveAgg.resultSummary", {
                  requested: String(lastResult.requested),
                  created: String(lastResult.created_aggregate_ids.length),
                  skipped: String(lastResult.skipped_count),
                }),
                errors: lastResult.errors.join("; "),
              })
            : t("predictiveAgg.resultSummary", {
                requested: String(lastResult.requested),
                created: String(lastResult.created_aggregate_ids.length),
                skipped: String(lastResult.skipped_count),
              })}
        </Alert>
      )}

      {/* Bug-7091 consumer: governance/capacity outcomes (e.g. byte-ceiling
          trimming) are NOT errors — the build still reports success. Surface
          them as an informational notice so an operator who sees fewer (or
          zero) aggregates created than requested understands WHY, rather than
          reading a bare "success with 0 created". */}
      {lastResult && (lastResult.governance_notes?.length ?? 0) > 0 && (
        <Alert severity="info" data-testid="predictive-governance-notes">
          {t("predictiveAgg.governanceNotesTitle")}
          <Box component="ul" sx={{ m: 0, pl: 2.5 }}>
            {lastResult.governance_notes!.map((note, i) => (
              <li key={i}>{note}</li>
            ))}
          </Box>
        </Alert>
      )}

      {candidates.length === 0 ? (
        <Alert
          severity={hasSourceStats ? "info" : "warning"}
          data-testid={hasSourceStats ? undefined : "predictive-no-stats"}
        >
          {hasSourceStats ? t("predictiveAgg.noCandidates") : t("predictiveAgg.noStats")}
        </Alert>
      ) : (
        <Card variant="outlined">
          <CardContent sx={{ p: 0, "&:last-child": { pb: 0 } }}>
            <TableContainer>
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    {canBuild && (
                      <TableCell padding="checkbox">
                        <Checkbox
                          size="small"
                          checked={allSelected}
                          indeterminate={someSelected}
                          onChange={(e) => toggleAll(e.target.checked)}
                        />
                      </TableCell>
                    )}
                    <TableCell sx={{ fontWeight: 600 }}>{t("predictiveAgg.factHeader")}</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>{t("predictiveAgg.grainHeader")}</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>{t("predictiveAgg.measuresHeader")}</TableCell>
                    <TableCell sx={{ fontWeight: 600 }} align="right">
                      {t("predictiveAgg.scoreHeader")}
                    </TableCell>
                    <TableCell sx={{ fontWeight: 600 }} align="right">
                      {t("predictiveAgg.hitRateHeader")}
                    </TableCell>
                    <TableCell sx={{ fontWeight: 600 }} align="right">
                      {t("predictiveAgg.rowReductionHeader")}
                    </TableCell>
                    <TableCell sx={{ fontWeight: 600 }} align="right">
                      {t("predictiveAgg.estRowsHeader")}
                    </TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>{t("predictiveAgg.whyHeader")}</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {candidates.map((c) => {
                    const key = candidateKey(c);
                    const checked = selected.has(key);
                    return (
                      <TableRow key={key} hover>
                        {canBuild && (
                          <TableCell padding="checkbox">
                            <Checkbox
                              size="small"
                              checked={checked}
                              onChange={(e) => toggleOne(key, e.target.checked)}
                            />
                          </TableCell>
                        )}
                        <TableCell sx={{ fontFamily: "monospace" }}>
                          {c.fact_table}
                        </TableCell>
                        <TableCell>
                          <Typography variant="caption">
                            {c.grain.join(", ")}
                          </Typography>
                        </TableCell>
                        <TableCell>
                          <Typography variant="caption">
                            {c.measure_names.join(", ")}
                          </Typography>
                        </TableCell>
                        <TableCell align="right">
                          {t("predictiveAgg.scoreValue", {
                            value: (c.score_pct ?? 0).toFixed(0),
                          })}
                        </TableCell>
                        <TableCell align="right">
                          {/* F-010-02: this is a cardinality-based reuse
                              HEURISTIC (a function of measure count), not a
                              measured or predicted hit rate — the header and
                              the "~" prefix signal that it is an estimate. */}
                          {`~${formatPercent(c.heuristic_reuse_score, t)}`}
                        </TableCell>
                        <TableCell align="right">
                          {formatMultiplier(c.row_reduction, t)}
                        </TableCell>
                        <TableCell align="right">
                          {formatNumber(c.estimated_rows, t)}
                        </TableCell>
                        <TableCell sx={{ maxWidth: 320 }}>
                          <Typography variant="caption" color="text.secondary">
                            {c.rationale}
                          </Typography>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </TableContainer>
          </CardContent>
        </Card>
      )}
    </Box>
  );
}
