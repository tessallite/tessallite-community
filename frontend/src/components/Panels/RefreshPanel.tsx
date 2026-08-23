import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  CircularProgress,
  Collapse,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import ScheduleIcon from "@mui/icons-material/Schedule";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import { aggregatesApi, schedulerApiClient } from "../../api/client";
import { useAggregates } from "../../api/hooks";
import { statusColor } from "../../theme/tokens";
import type { RefreshPolicyCreate, TriggerRefreshResponse } from "../../api/types";
import { buildAggregateRefreshPolicy } from "../Refresh/policyPayload";

// User-friendly schedule presets — translated to cron behind the scenes.
const SCHEDULE_PRESETS = [
  { value: "manual", labelKey: "refresh.scheduleManual" },
  { value: "every_6h", labelKey: "refresh.scheduleEvery6h", cron: "0 */6 * * *" },
  { value: "every_12h", labelKey: "refresh.scheduleEvery12h", cron: "0 */12 * * *" },
  { value: "daily_2am", labelKey: "refresh.scheduleDaily2am", cron: "0 2 * * *" },
  { value: "daily_5am", labelKey: "refresh.scheduleDaily5am", cron: "0 5 * * *" },
  { value: "weekly_mon", labelKey: "refresh.scheduleWeeklyMon", cron: "0 5 * * 1" },
] as const;

type SchedulePresetValue = (typeof SCHEDULE_PRESETS)[number]["value"];

const REFRESH_STRATEGIES = [
  { value: "full", labelKey: "refresh.strategyFull" },
  { value: "incremental", labelKey: "refresh.strategyIncremental" },
] as const;


export default function RefreshPanel() {
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const t = useT();
  const qc = useQueryClient();

  const [policyAggId, setPolicyAggId] = useState<string | null>(null);
  const [schedule, setSchedule] = useState<SchedulePresetValue>("daily_2am");
  const [strategy, setStrategy] = useState<"full" | "incremental">("full");
  const [incrCol, setIncrCol] = useState("");
  const [lookback, setLookback] = useState(1);
  const [fullRebuildInterval, setFullRebuildInterval] = useState<number | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);

  const aggregates = useAggregates(projectId!, modelId!);

  // Bug-8785 (sibling surface): this dialog opened with component defaults and
  // never read the persisted policy, so saving any change here wrote
  // incremental_append_only back from an unhydrated constant and silently
  // cleared the append-only authority the aggregate already had. The reset in
  // the open handler stays -- it stops a previously-opened aggregate's values
  // leaking across -- and the persisted policy overwrites it once loaded.
  const persistedPolicy = useQuery({
    queryKey: ["aggregate-refresh-policy", projectId, modelId, policyAggId],
    enabled: Boolean(policyAggId),
    // A 404 means no policy is configured yet; the defaults already seeded by
    // the open handler are then correct, so do not retry or surface an error.
    retry: false,
    gcTime: 0,
    queryFn: () => aggregatesApi.getPolicy(projectId!, modelId!, policyAggId!),
  });

  useEffect(() => {
    const p = persistedPolicy.data;
    if (!policyAggId || !p) return;
    const incremental = p.refresh_mode === "incremental";
    // "manual" carries no cron, hence the `in` narrowing.
    const preset = SCHEDULE_PRESETS.find(
      (x) => "cron" in x && x.cron === p.cron_expression,
    );
    setSchedule((preset?.value ?? "manual") as SchedulePresetValue);
    setStrategy(incremental ? "incremental" : "full");
    setIncrCol(p.incremental_column ?? "");
    setLookback(p.incremental_lookback ?? 1);
    setFullRebuildInterval(p.full_rebuild_interval_days);
  }, [policyAggId, persistedPolicy.data]);

  const setPolicy = useMutation({
    mutationFn: () => {
      const preset = SCHEDULE_PRESETS.find((p) => p.value === schedule);
      const isManual = !preset || preset.value === "manual";
      const data: RefreshPolicyCreate = buildAggregateRefreshPolicy({
        method: strategy,
        cron: isManual ? null : (preset as { cron: string }).cron,
        incrementalColumn: incrCol,
        lookbackDays: lookback,
        fullRebuildIntervalDays: fullRebuildInterval,
      });
      return aggregatesApi.setPolicy(
        projectId!,
        modelId!,
        policyAggId!,
        data,
      );
    },
    onSuccess: () => {
      qc.invalidateQueries({
        queryKey: ["aggregates", projectId, modelId],
      });
      setPolicyAggId(null);
    },
  });

  const triggerRefresh = useMutation({
    mutationFn: ({
      aggId,
      refreshMode,
    }: {
      aggId: string;
      refreshMode: "full" | "incremental";
    }) =>
      schedulerApiClient.triggerRefresh({
        aggregate_id: aggId,
        model_id: modelId,
        mode: refreshMode,
      }),
    onSuccess: (data: TriggerRefreshResponse) => {
      if (data.status === "failed") {
        setFeedback(t("refresh.triggerFailed", { error: data.error_message ?? t("refresh.unknownError") }));
      } else {
        setFeedback(t("refresh.triggerSuccess"));
      }
    },
    onError: (err: unknown) => {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? t("refresh.triggerError");
      setFeedback(detail);
    },
  });

  const activeAggs =
    aggregates.data?.filter((a) => a.status === "active") ?? [];

  return (
    <Box>
      {feedback && (
        <Alert
          severity={
            feedback.includes("successfully") || feedback.includes("created") || feedback.includes("found")
              ? "success"
              : "error"
          }
          onClose={() => setFeedback(null)}
          sx={{ mb: 1 }}
        >
          {feedback}
        </Alert>
      )}

      <Box display="flex" alignItems="center" mb={1.5} gap={1}>
        <Typography variant="subtitle2" fontWeight={600} flexGrow={1}>
          {t("refresh.aggregates")}
        </Typography>
      </Box>

      {aggregates.isLoading ? (
        <CircularProgress size={20} />
      ) : activeAggs.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t("refresh.noActiveAggregates")}
        </Typography>
      ) : (
        <Stack spacing={1}>
          {activeAggs.map((agg) => (
            <AggRefreshCard
              key={agg.id}
              agg={agg}
              projectId={projectId!}
              modelId={modelId!}
              onSetPolicy={() => {
                setPolicyAggId(agg.id);
                setSchedule("daily_2am");
                setStrategy("full");
                setIncrCol("");
                setLookback(1);
                setFullRebuildInterval(null);
              }}
              onTrigger={(m) =>
                triggerRefresh.mutate({ aggId: agg.id, refreshMode: m })
              }
              triggerPending={triggerRefresh.isPending}
            />
          ))}
        </Stack>
      )}

      <Dialog
        open={!!policyAggId}
        onClose={() => setPolicyAggId(null)}
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle>{t("refresh.dialogTitle")}</DialogTitle>
        <DialogContent>
          <Typography variant="caption" color="text.secondary" display="block" mb={1}>
            {t("refresh.dialogDescription")}
          </Typography>
          <FormControl fullWidth margin="normal">
            <InputLabel>{t("refresh.frequencyLabel")}</InputLabel>
            <Select
              value={schedule}
              label={t("refresh.frequencyLabel")}
              onChange={(e) => setSchedule(e.target.value as SchedulePresetValue)}
            >
              {SCHEDULE_PRESETS.map((p) => (
                <MenuItem key={p.value} value={p.value}>
                  {t(p.labelKey)}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth margin="normal">
            <InputLabel>{t("refresh.strategyLabel")}</InputLabel>
            <Select
              value={strategy}
              label={t("refresh.strategyLabel")}
              onChange={(e) => setStrategy(e.target.value as "full" | "incremental")}
            >
              {REFRESH_STRATEGIES.map((s) => (
                <MenuItem key={s.value} value={s.value}>
                  {t(s.labelKey)}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          {strategy === "incremental" && (
            <>
              <Alert severity="info" sx={{ mt: 1 }}>
                {t("rebuildMethod.appendOnlyNotice")}
              </Alert>
              <TextField
                label={t("refresh.columnLabel")}
                fullWidth
                margin="normal"
                value={incrCol}
                onChange={(e) => setIncrCol(e.target.value)}
                helperText={t("refresh.columnHelp")}
              />
              <TextField
                label={t("refresh.lookbackLabel")}
                type="number"
                fullWidth
                margin="normal"
                value={lookback}
                onChange={(e) => setLookback(Number(e.target.value))}
                helperText={t("refresh.lookbackHelp")}
              />
              <TextField
                label={t("rebuildMethod.fullRebuildIntervalLabel")}
                type="number"
                fullWidth
                margin="normal"
                value={fullRebuildInterval ?? ""}
                onChange={(e) => {
                  const raw = e.target.value;
                  setFullRebuildInterval(raw === "" ? null : Number(raw));
                }}
                inputProps={{ min: 1 }}
                helperText={t("rebuildMethod.fullRebuildIntervalHelp")}
              />
            </>
          )}
          {setPolicy.isError && (
            <Alert severity="error" sx={{ mt: 1 }}>
              {t("refresh.saveFailed")}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setPolicyAggId(null)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => setPolicy.mutate()}
            disabled={setPolicy.isPending}
          >
            {setPolicy.isPending ? <CircularProgress size={18} /> : t("common.save")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

function AggRefreshCard({
  agg,
  projectId,
  modelId,
  onSetPolicy,
  onTrigger,
  triggerPending,
}: {
  agg: { id: string; physical_table_name: string; grain: string[] };
  projectId: string;
  modelId: string;
  onSetPolicy: () => void;
  onTrigger: (mode: "full" | "incremental") => void;
  triggerPending: boolean;
}) {
  const t = useT();
  const [showRuns, setShowRuns] = useState(false);
  const runs = useQuery({
    queryKey: ["runs", projectId, modelId, agg.id],
    queryFn: () => aggregatesApi.getRuns(projectId, modelId, agg.id),
  });

  const recentRuns = runs.data?.slice(0, 3) ?? [];

  return (
    <Card variant="outlined">
      <CardContent sx={{ py: 1, "&:last-child": { pb: 1 } }}>
        <Box display="flex" alignItems="center" mb={0.5}>
          <Box flexGrow={1}>
            <Typography variant="body2" fontWeight={600}>
              {agg.physical_table_name}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {t("refresh.grain")}: {agg.grain.join(", ")}
            </Typography>
          </Box>
          <Button
            size="small"
            startIcon={<ScheduleIcon />}
            onClick={onSetPolicy}
            sx={{ mr: 0.5 }}
          >
            {t("refresh.scheduleButton")}
          </Button>
          <Button
            size="small"
            startIcon={<PlayArrowIcon />}
            onClick={() => onTrigger("full")}
            disabled={triggerPending}
            sx={{ mr: 0.5 }}
          >
            {t("refresh.fullButton")}
          </Button>
          <Button
            size="small"
            onClick={() => onTrigger("incremental")}
            disabled={triggerPending}
          >
            {t("refresh.incrButton")}
          </Button>
        </Box>
        {recentRuns.length > 0 && (
          <>
            <Box
              display="flex"
              alignItems="center"
              sx={{ cursor: "pointer" }}
              onClick={() => setShowRuns(!showRuns)}
            >
              <Typography variant="caption" color="text.secondary">
                {t("refresh.lastRuns", { count: String(recentRuns.length) })}
              </Typography>
              {showRuns ? (
                <ExpandLessIcon sx={{ fontSize: 16, color: "text.secondary" }} />
              ) : (
                <ExpandMoreIcon sx={{ fontSize: 16, color: "text.secondary" }} />
              )}
            </Box>
            <Collapse in={showRuns}>
              <Table size="small" sx={{ mt: 0.5 }}>
                <TableBody>
                  {recentRuns.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell sx={{ py: 0.25, pl: 0, border: 0 }}>
                        <Typography variant="caption" color="text.secondary">
                          {new Date(r.started_at).toLocaleString()}
                        </Typography>
                      </TableCell>
                      <TableCell sx={{ py: 0.25, border: 0 }}>
                        <Tooltip
                          title={r.status === "failed" && r.error_message ? r.error_message : ""}
                          placement="top"
                        >
                          <Typography
                            variant="caption"
                            fontWeight={600}
                            sx={{ color: statusColor(r.status).fg }}
                          >
                            {r.status}
                          </Typography>
                        </Tooltip>
                      </TableCell>
                      <TableCell sx={{ py: 0.25, border: 0 }} align="right">
                        <Typography variant="caption" color="text.secondary">
                          {t("refresh.rowsWritten", { count: String(r.rows_written ?? "?") })}
                        </Typography>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Collapse>
          </>
        )}
      </CardContent>
    </Card>
  );
}
