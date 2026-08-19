/**
 * Scheduler Panel — rule-based and AI optimiser schedule configuration.
 */
import { useCallback, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Box,
  Button,
  Divider,
  FormControlLabel,
  IconButton,
  Switch,
  Slider,
  Tooltip,
  Typography,
  Stack,
  MenuItem,
  Select,
  InputLabel,
  FormControl,
  Alert,
  CircularProgress,
} from "@mui/material";
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome";
import CachedIcon from "@mui/icons-material/Cached";
import CalculateIcon from "@mui/icons-material/Calculate";
import CameraAltIcon from "@mui/icons-material/CameraAlt";
import StorageIcon from "@mui/icons-material/Storage";
import AccountTreeIcon from "@mui/icons-material/AccountTree";
import DeleteIcon from "@mui/icons-material/Delete";
import DeleteSweepIcon from "@mui/icons-material/DeleteSweep";
import ArrowForwardIcon from "@mui/icons-material/ArrowForward";
import { useAISchedulerConfig, useAggregates } from "../../api/hooks";
import { useBuilderStore } from "../../store/builderStore";
import { aiSchedulerApi, aiOptimizerApi, optimizerApiClient, schedulerApiClient } from "../../api/client";
import type { ModelAISchedulerConfigUpdate } from "../../api/types";
import { SLAConfigPanel } from "../Settings/SLAConfigPanel";
import { ui } from "../../theme/tokens";
import { useT } from "../../i18n";

// Human-readable schedule options → cron expressions
// These are translated dynamically in cronToLabel based on locale
const SCHEDULE_CRON_OPTIONS = [
  { key: "scheduler.schedule.every6Hours",  cron: "0 */6 * * *"  },
  { key: "scheduler.schedule.every12Hours", cron: "0 */12 * * *" },
  { key: "scheduler.schedule.onceADay",     cron: "0 5 * * *"    },
  { key: "scheduler.schedule.onceAWeek",    cron: "0 5 * * 1"    },
];

function cronToLabel(cron: string, t: ReturnType<typeof useT>): string {
  const option = SCHEDULE_CRON_OPTIONS.find((o) => o.cron === cron);
  if (option) {
    return t(option.key);
  }
  // Unrecognized (custom) cron: return a "Custom" label instead of coercing
  // to "once a day" (Bug-5228).
  return t("scheduler.schedule.custom");
}

interface Props {
  projectId: string;
  modelId: string;
  tenantId: string;
}

export default function SchedulerPanel({ projectId, modelId, tenantId }: Props) {
  const t = useT();
  // Bug-5301: gate mutating controls when the model is opened read-only.
  const readOnly = useBuilderStore((s) => s.readOnly);
  const { data: config, isLoading } = useAISchedulerConfig(projectId, modelId);
  const aggregates = useAggregates(projectId, modelId);
  const qc = useQueryClient();
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [runningSweep, setRunningSweep] = useState(false);
  const [refreshingAll, setRefreshingAll] = useState(false);
  const [runningKpiSnapshot, setRunningKpiSnapshot] = useState(false);
  const [runningKpiPurge, setRunningKpiPurge] = useState(false);
  const [runningPocketSweep, setRunningPocketSweep] = useState(false);
  const [runningPocketEviction, setRunningPocketEviction] = useState(false);
  const [message, setMessage] = useState<{
    type: "success" | "error" | "warning";
    text: string;
  } | null>(null);

  // Local form state
  const [aiEnabled, setAiEnabled] = useState(false);
  // Track the loaded cron so a custom (API-set) cron is preserved instead
  // of being silently overwritten on save (Bug-5228, mirrors SmartBuilder).
  const [loadedCron, setLoadedCron] = useState("0 5 * * *");
  const [scheduleLabel, setScheduleLabel] = useState("scheduler.schedule.onceADay");
  const [lookbackDays, setLookbackDays] = useState(7);
  const [maxCreates, setMaxCreates] = useState(3);
  const [dryRun, setDryRun] = useState(false);

  // Sync from server on initial load only — NOT after save.
  const [initialized, setInitialized] = useState(false);
  // Whether the AI Optimiser form currently differs from the saved config.
  // Gates "Run AI now" so a change made and not yet saved cannot be run
  // against stale server state (Bug-7117, mirrors SmartBuilderSection's
  // F-011-16b guard).
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    if (config && !initialized) {
      setAiEnabled(config.ai_enabled);
      setLoadedCron(config.cron_expression);
      setScheduleLabel(cronToLabel(config.cron_expression, t));
      setLookbackDays(Math.round(config.lookback_hours / 24));
      setMaxCreates(config.max_creates_per_run);
      setDryRun(config.dry_run);
      setInitialized(true);
      setDirty(false);
    }
  }, [config, initialized, t]);

  // Reset init flag when model changes so we re-sync for the new model
  useEffect(() => {
    setInitialized(false);
  }, [modelId]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setMessage(null);
    try {
      // When the schedule shows the Custom label (an API-set cron the presets
      // don't cover), keep the loaded cron rather than coercing to a preset
      // (Bug-5228, mirrors SmartBuilder's loadedCron protection).
      const selectedOption = SCHEDULE_CRON_OPTIONS.find((o) => t(o.key) === scheduleLabel);
      const cronToSave = selectedOption ? selectedOption.cron : loadedCron;
      // Only send the fields this panel actually controls. min_confidence and
      // enable_ai_aggregation are owned by SmartBuilderSection; omitting them
      // (the backend uses exclude_unset) preserves the stored values instead of
      // clobbering them with hardcoded constants (F-011-06).
      const update: ModelAISchedulerConfigUpdate = {
        ai_enabled: aiEnabled,
        cron_expression: cronToSave,
        lookback_hours: lookbackDays * 24,
        max_creates_per_run: maxCreates,
        dry_run: dryRun,
      };
      // Sync local state from the response — don't call refetch() which would re-run
      // useEffect and potentially clobber the form with stale server data.
      const saved = await aiSchedulerApi.update(projectId, modelId, update);
      qc.setQueryData(["aiSchedulerConfig", projectId, modelId], saved);
      setAiEnabled(saved.ai_enabled);
      setLoadedCron(saved.cron_expression);
      setScheduleLabel(cronToLabel(saved.cron_expression, t));
      setLookbackDays(Math.round(saved.lookback_hours / 24));
      setMaxCreates(saved.max_creates_per_run);
      setDryRun(saved.dry_run);
      setDirty(false);
      setMessage({ type: "success", text: t("scheduler.saveSuccess") });
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        t("scheduler.saveFailed");
      setMessage({ type: "error", text: detail });
    } finally {
      setSaving(false);
    }
  }, [projectId, modelId, aiEnabled, scheduleLabel, loadedCron, lookbackDays, maxCreates, dryRun, t]);

  const handleRunNow = useCallback(async () => {
    // Bug-7117: running against unsaved changes (e.g. flipping AI on then
    // clicking Run before Save) hits the backend with stale config and
    // surfaces a raw error such as "AI optimiser is not enabled for model
    // <uuid>" — the exact UX SmartBuilderSection's F-011-16b guard already
    // fixed for its own Run AI button. Block it here with a clear, translated
    // prompt to save first instead.
    if (dirty) {
      setMessage({ type: "error", text: t("scheduler.saveBeforeRun") });
      return;
    }
    setRunning(true);
    setMessage(null);
    try {
      await aiOptimizerApi.triggerRun({ model_id: modelId, dry_run: dryRun });
      setMessage({ type: "success", text: t("scheduler.aiOptimiserTriggered") });
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      // 409 with code "ai_run_in_progress": another run holds the per-model
      // lock — show the translated conflict message instead of raw detail.
      if (
        typeof detail === "object" && detail !== null
        && (detail as { code?: string }).code === "ai_run_in_progress"
      ) {
        setMessage({ type: "error", text: t("aiOptimizer.runInProgress") });
      } else {
        setMessage({
          type: "error",
          text: typeof detail === "string" ? detail : t("scheduler.aiOptimiserTriggerFailed"),
        });
      }
    } finally {
      setRunning(false);
    }
  }, [tenantId, modelId, dryRun, dirty, t]);

  const handleRefreshAll = useCallback(async () => {
    const active = (aggregates.data ?? []).filter((a) => a.status === "active");
    if (active.length === 0) {
      setMessage({ type: "error", text: t("scheduler.noActiveAggregates") });
      return;
    }
    setRefreshingAll(true);
    setMessage(null);
    let ok = 0;
    let failed = 0;
    try {
      for (const agg of active) {
        try {
          const res = await schedulerApiClient.triggerRefresh({
            // F-012-15: the refresh endpoint has no model_id field — it was
            // silently dropped by the backend Pydantic model. Send only the
            // fields the endpoint actually accepts.
            aggregate_id: agg.id,
            mode: "full",
          });
          if (res.status === "failed") failed++;
          else ok++;
        } catch {
          failed++;
        }
      }
      setMessage({
        type: failed === 0 ? "success" : "error",
        text: t("scheduler.refreshResult", { ok: String(ok), failed: String(failed) }),
      });
    } finally {
      setRefreshingAll(false);
    }
  }, [aggregates.data, tenantId, modelId, t]);

  const handleRunSweep = useCallback(async () => {
    setRunningSweep(true);
    setMessage(null);
    try {
      const result = await optimizerApiClient.runModelSweep(modelId);
      setMessage({
        type: "success",
        text: t("scheduler.ruleBasedResult", {
          candidates: String(result.candidates_found),
          created: String(result.aggregates_created)
        }),
      });
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        t("scheduler.ruleBasedFailed");
      setMessage({ type: "error", text: detail });
    } finally {
      setRunningSweep(false);
    }
  }, [modelId, t]);

  const handleRunKpiSnapshot = useCallback(async () => {
    setRunningKpiSnapshot(true);
    setMessage(null);
    try {
      const result = await schedulerApiClient.triggerKpiSnapshotSweep();
      if (result.status === "failed") {
        setMessage({
          type: "error",
          text: t("scheduler.kpiSnapshotSweepAllFailed", {
            count: String(result.models_failed),
          }),
        });
      } else if (result.status === "partial") {
        setMessage({
          type: "warning",
          text: t("scheduler.kpiSnapshotSweepPartial", {
            written: String(result.snapshots_written),
            failed: String(result.models_failed),
          }),
        });
      } else if (result.latest_suppressed > 0) {
        // Bug-7982 epoch-monotonicity guard (opus5 completion-round R4):
        // a suppressed write is the guard working as intended (a fresher
        // $KPIs value was already published, e.g. by the post-deploy
        // re-eval trigger), not a failure — status stays "completed". But
        // it must still be surfaced, not silently identical to a sweep
        // with nothing suppressed, so a CHRONIC condition is noticeable.
        setMessage({
          type: "warning",
          text: t("scheduler.kpiSnapshotSweepSuccessWithSuppressed", {
            count: String(result.snapshots_written),
            suppressed: String(result.latest_suppressed),
          }),
        });
      } else {
        setMessage({
          type: "success",
          text: t("scheduler.kpiSnapshotSweepSuccessCount", {
            count: String(result.snapshots_written),
          }),
        });
      }
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        t("scheduler.kpiSnapshotSweepFailed");
      setMessage({ type: "error", text: detail });
    } finally {
      setRunningKpiSnapshot(false);
    }
  }, [t]);

  const handleRunKpiPurge = useCallback(async () => {
    setRunningKpiPurge(true);
    setMessage(null);
    try {
      const result = await schedulerApiClient.triggerKpiSnapshotPurge();
      setMessage({
        type: "success",
        text: t("scheduler.kpiSnapshotPurgeSuccess", { count: String(result.purged) }),
      });
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        t("scheduler.kpiSnapshotPurgeFailed");
      setMessage({ type: "error", text: detail });
    } finally {
      setRunningKpiPurge(false);
    }
  }, [t]);

  const handleRunPocketSweep = useCallback(async () => {
    setRunningPocketSweep(true);
    setMessage(null);
    try {
      const result = await schedulerApiClient.triggerPocketSweep();
      setMessage({
        type: "success",
        text: t("scheduler.pocketSweepSuccess", { count: String(result.refreshed) }),
      });
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        t("scheduler.pocketSweepFailed");
      setMessage({ type: "error", text: detail });
    } finally {
      setRunningPocketSweep(false);
    }
  }, [t]);

  const handleRunPocketEviction = useCallback(async () => {
    setRunningPocketEviction(true);
    setMessage(null);
    try {
      const result = await schedulerApiClient.triggerPocketEviction();
      setMessage({
        type: "success",
        text: t("scheduler.pocketEvictionSuccess", { count: String(result.evicted) }),
      });
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        t("scheduler.pocketEvictionFailed");
      setMessage({ type: "error", text: detail });
    } finally {
      setRunningPocketEviction(false);
    }
  }, [t]);

  if (isLoading) return <CircularProgress size={24} />;

  const activeAggCount = (aggregates.data ?? []).filter((a) => a.status === "active").length;

  return (
    <Stack spacing={2.5}>
      {message && (
        <Alert severity={message.type} onClose={() => setMessage(null)}>
          {message.text}
        </Alert>
      )}

      {/* ── Rule-based Optimiser ── */}
      <SchedulerSection
        icon={<CalculateIcon fontSize="small" sx={{ color: ui.green }} />}
        title={t("scheduler.ruleBasedTitle")}
        description={t("scheduler.ruleBasedDescription")}
        secondary={
          <>{t("scheduler.ruleBasedThresholds")}</>
        }
        actionLabel={t("scheduler.runRuleBasedButton")}
        actionRunningLabel={t("scheduler.running")}
        actionIcon={<CalculateIcon />}
        running={runningSweep}
        readOnly={readOnly}
        onAction={handleRunSweep}
      />

      <Divider />

      {/* ── AI Optimiser ── */}
      <SchedulerSection
        icon={<AutoAwesomeIcon fontSize="small" sx={{ color: ui.green }} />}
        title={t("scheduler.aiOptimiserTitle")}
        description={t("scheduler.aiOptimiserDescription")}
        actionLabel={t("scheduler.runAiButton")}
        actionRunningLabel={t("scheduler.running")}
        actionIcon={<AutoAwesomeIcon />}
        running={running}
        actionDisabled={!aiEnabled}
        readOnly={readOnly}
        onAction={handleRunNow}
      >
        <Stack spacing={2}>
          <FormControlLabel
            control={<Switch checked={aiEnabled} onChange={(e) => { setAiEnabled(e.target.checked); setDirty(true); }} disabled={readOnly} />}
            label={t("scheduler.enableAiOptimiser")}
          />

          <FormControl size="small" fullWidth disabled={readOnly || !aiEnabled}>
            <InputLabel>{t("scheduler.runFrequency")}</InputLabel>
            <Select
              value={scheduleLabel}
              label={t("scheduler.runFrequency")}
              onChange={(e) => { setScheduleLabel(e.target.value); setDirty(true); }}
            >
              {SCHEDULE_CRON_OPTIONS.map((o) => (
                <MenuItem key={o.cron} value={t(o.key)}>{t(o.key)}</MenuItem>
              ))}
              {/* Bug-5228: show a Custom entry for API-set crons not in presets */}
              {!SCHEDULE_CRON_OPTIONS.some((o) => o.cron === loadedCron) && (
                <MenuItem key="__custom" value={t("scheduler.schedule.custom")}>
                  {t("scheduler.schedule.customWithCron", { cron: loadedCron })}
                </MenuItem>
              )}
            </Select>
          </FormControl>

          <Box>
            <Typography variant="caption" color="text.secondary">
              {t("scheduler.analyseLastDays", { days: String(lookbackDays), day_label: lookbackDays === 1 ? t("scheduler.day") : t("scheduler.days") })}
            </Typography>
            <Slider
              value={lookbackDays}
              onChange={(_, v) => { setLookbackDays(v as number); setDirty(true); }}
              min={1}
              max={30}
              step={1}
              marks={[
                { value: 1, label: t("scheduler.mark1d") },
                { value: 7, label: t("scheduler.mark7d") },
                { value: 14, label: t("scheduler.mark14d") },
                { value: 30, label: t("scheduler.mark30d") },
              ]}
              disabled={readOnly || !aiEnabled}
              valueLabelDisplay="auto"
              valueLabelFormat={(v) => `${v}${t("scheduler.daySuffix")}`}
            />
          </Box>

          <Box>
            <Typography variant="caption" color="text.secondary">
              {t("scheduler.createUpToAggregates", {
                max: String(maxCreates),
                agg_label: maxCreates !== 1 ? t("scheduler.aggregates") : t("scheduler.aggregate")
              })}
            </Typography>
            <Slider
              value={maxCreates}
              onChange={(_, v) => { setMaxCreates(v as number); setDirty(true); }}
              min={1}
              max={10}
              step={1}
              marks={[
                { value: 1, label: "1" },
                { value: 5, label: "5" },
                { value: 10, label: "10" },
              ]}
              disabled={readOnly || !aiEnabled}
              valueLabelDisplay="auto"
            />
          </Box>

          <FormControlLabel
            control={<Switch checked={dryRun} onChange={(e) => { setDryRun(e.target.checked); setDirty(true); }} />}
            label={t("scheduler.previewOnly")}
            disabled={readOnly || !aiEnabled}
          />

          <Button variant="outlined" size="small" onClick={handleSave} disabled={saving || readOnly}>
            {saving ? t("scheduler.saving") : t("scheduler.saveSettings")}
          </Button>
        </Stack>
      </SchedulerSection>

      <Divider />

      {/* ── Aggregate Refresh ── */}
      <SchedulerSection
        icon={<CachedIcon fontSize="small" sx={{ color: ui.green }} />}
        title={t("scheduler.aggregateRefreshTitle")}
        description={t("scheduler.aggregateRefreshDescription")}
        secondary={<>{t("scheduler.activeAggregates", { count: String(activeAggCount) })}</>}
        actionLabel={t("scheduler.runAggregateRefreshButton")}
        actionRunningLabel={t("scheduler.refreshing")}
        actionIcon={<CachedIcon />}
        running={refreshingAll}
        actionDisabled={activeAggCount === 0}
        readOnly={readOnly}
        onAction={handleRefreshAll}
      />

      <Divider />

      {/* ── KPI Snapshots ── */}
      <SchedulerSection
        icon={<CameraAltIcon fontSize="small" sx={{ color: ui.green }} />}
        title={t("scheduler.kpiSnapshotsTitle")}
        description={t("scheduler.kpiSnapshotsDescription")}
        secondary={<>{t("scheduler.kpiSnapshotScheduleInfo")}</>}
        actionLabel={t("scheduler.kpiSnapshotSweepButton")}
        actionRunningLabel={t("scheduler.kpiSnapshotSweepRunning")}
        actionIcon={<CameraAltIcon />}
        running={runningKpiSnapshot}
        readOnly={readOnly}
        onAction={handleRunKpiSnapshot}
      >
        <Button
          variant="outlined"
          color="secondary"
          size="small"
          startIcon={runningKpiPurge ? <CircularProgress size={16} color="inherit" /> : <DeleteSweepIcon />}
          onClick={handleRunKpiPurge}
          disabled={readOnly || runningKpiPurge}
          sx={{ alignSelf: "flex-start" }}
        >
          {runningKpiPurge ? t("scheduler.kpiSnapshotPurgeRunning") : t("scheduler.kpiSnapshotPurgeButton")}
        </Button>
      </SchedulerSection>

      <Divider />

      {/* ── Pocket Tables ── */}
      <SchedulerSection
        icon={<StorageIcon fontSize="small" sx={{ color: ui.green }} />}
        title={t("scheduler.pocketTablesTitle")}
        description={t("scheduler.pocketTablesDescription")}
        secondary={<>{t("scheduler.pocketTablesScheduleInfo")}</>}
        actionLabel={t("scheduler.pocketSweepButton")}
        actionRunningLabel={t("scheduler.pocketSweepRunning")}
        actionIcon={<StorageIcon />}
        running={runningPocketSweep}
        readOnly={readOnly}
        onAction={handleRunPocketSweep}
      >
        <Button
          variant="outlined"
          color="secondary"
          size="small"
          startIcon={runningPocketEviction ? <CircularProgress size={16} color="inherit" /> : <DeleteSweepIcon />}
          onClick={handleRunPocketEviction}
          disabled={readOnly || runningPocketEviction}
          sx={{ alignSelf: "flex-start" }}
        >
          {runningPocketEviction ? t("scheduler.pocketEvictionRunning") : t("scheduler.pocketEvictionButton")}
        </Button>
      </SchedulerSection>

      <Divider />

      {/* ── Dependency Chains ── */}
      <DependencyChainSection modelId={modelId} aggregates={aggregates.data ?? []} />

      <Divider />

      {/* ── SLA Configuration ── */}
      <SLAConfigPanel projectId={projectId} modelId={modelId} />
    </Stack>
  );
}

function DependencyChainSection({
  modelId,
  aggregates,
}: {
  modelId: string;
  aggregates: Array<{ id: string; physical_table_name: string; status: string }>;
}) {
  const t = useT();
  const qc = useQueryClient();
  const [upstreamId, setUpstreamId] = useState("");
  const [downstreamId, setDownstreamId] = useState("");

  const depsQuery = useQuery({
    queryKey: ["refresh-deps", modelId],
    queryFn: () => schedulerApiClient.getDependencies(modelId),
    enabled: Boolean(modelId),
  });

  const createMut = useMutation({
    mutationFn: () => schedulerApiClient.createDependency(upstreamId, downstreamId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["refresh-deps", modelId] });
      setUpstreamId("");
      setDownstreamId("");
    },
  });

  const deleteMut = useMutation({
    mutationFn: (depId: string) => schedulerApiClient.deleteDependency(depId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["refresh-deps", modelId] });
    },
  });

  const active = aggregates.filter((a) => a.status === "active");
  const deps = depsQuery.data?.dependencies ?? [];
  const order = depsQuery.data?.execution_order ?? [];

  const aggNameMap = new Map(active.map((a) => [a.id, a.physical_table_name]));

  return (
    <Box>
      <Box display="flex" alignItems="center" gap={1} mb={1}>
        <AccountTreeIcon fontSize="small" sx={{ color: ui.green }} />
        <Typography variant="subtitle2" fontWeight={700}>
          {t("scheduler.dependencyChainsTitle")}
        </Typography>
      </Box>
      <Stack spacing={1.5}>
        <Box sx={{ p: 1.5, bgcolor: ui.mutedBg, borderRadius: 1, border: "1px solid", borderColor: "grey.200" }}>
          <Typography variant="caption" color="text.secondary" display="block">
            {t("scheduler.dependencyChainsDescription")}
          </Typography>
        </Box>

        {createMut.isError && (
          <Alert severity="error" variant="outlined">
            {(createMut.error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? t("scheduler.failedToCreateDependency")}
          </Alert>
        )}

        {active.length >= 2 && (
          <Stack direction="row" spacing={1} alignItems="center">
            <FormControl size="small" sx={{ flex: 1 }}>
              <InputLabel>{t("scheduler.upstreamLabel")}</InputLabel>
              <Select
                label={t("scheduler.upstreamLabel")}
                value={upstreamId}
                onChange={(e) => setUpstreamId(e.target.value)}
              >
                {active.map((a) => (
                  <MenuItem key={a.id} value={a.id} disabled={a.id === downstreamId}>
                    {a.physical_table_name}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            <ArrowForwardIcon fontSize="small" color="action" />
            <FormControl size="small" sx={{ flex: 1 }}>
              <InputLabel>{t("scheduler.downstreamLabel")}</InputLabel>
              <Select
                label={t("scheduler.downstreamLabel")}
                value={downstreamId}
                onChange={(e) => setDownstreamId(e.target.value)}
              >
                {active.map((a) => (
                  <MenuItem key={a.id} value={a.id} disabled={a.id === upstreamId}>
                    {a.physical_table_name}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            <Button
              variant="contained"
              size="small"
              disabled={!upstreamId || !downstreamId || createMut.isPending}
              onClick={() => createMut.mutate()}
            >
              {t("common.add")}
            </Button>
          </Stack>
        )}

        {deps.length === 0 && (
          <Typography variant="caption" color="text.secondary">
            {t("scheduler.noDependencies")}
          </Typography>
        )}

        {deps.map((d) => (
          <Stack key={d.id} direction="row" alignItems="center" spacing={1} sx={{ pl: 1 }}>
            <Typography variant="body2" sx={{ fontWeight: 500 }}>
              {d.upstream_name ?? aggNameMap.get(d.upstream_aggregate_id) ?? t("joins.unknownColumn")}
            </Typography>
            <ArrowForwardIcon fontSize="small" color="action" />
            <Typography variant="body2" sx={{ fontWeight: 500 }}>
              {d.downstream_name ?? aggNameMap.get(d.downstream_aggregate_id) ?? t("joins.unknownColumn")}
            </Typography>
            <Tooltip title={t("scheduler.removeDependency")}>
              <IconButton
                size="small"
                onClick={() => deleteMut.mutate(d.id)}
                disabled={deleteMut.isPending}
              >
                <DeleteIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          </Stack>
        ))}

        {order.length > 1 && (
          <Box sx={{ mt: 1 }}>
            <Typography variant="caption" color="text.secondary" fontWeight={600}>
              {t("scheduler.executionOrderLabel")}
            </Typography>
            <Typography variant="caption" color="text.secondary" display="block">
              {order.map((id) => aggNameMap.get(id) ?? id.slice(0, 8)).join(" → ")}
            </Typography>
          </Box>
        )}
      </Stack>
    </Box>
  );
}

interface SectionProps {
  icon: React.ReactNode;
  title: string;
  description: string;
  secondary?: React.ReactNode;
  actionLabel: string;
  actionRunningLabel: string;
  actionIcon: React.ReactNode;
  running: boolean;
  actionDisabled?: boolean;
  /** Bug-5301: disable all mutating controls when the model is read-only. */
  readOnly?: boolean;
  onAction: () => void;
  children?: React.ReactNode;
}

function SchedulerSection({
  icon,
  title,
  description,
  secondary,
  actionLabel,
  actionRunningLabel,
  actionIcon,
  running,
  actionDisabled,
  readOnly: sectionReadOnly,
  onAction,
  children,
}: SectionProps) {
  return (
    <Box>
      <Box display="flex" alignItems="center" gap={1} mb={1}>
        {icon}
        <Typography variant="subtitle2" fontWeight={700}>
          {title}
        </Typography>
      </Box>
      <Stack spacing={1.5}>
        <Box sx={{ p: 1.5, bgcolor: ui.mutedBg, borderRadius: 1, border: "1px solid", borderColor: "grey.200" }}>
          <Typography variant="caption" color="text.secondary" display="block" mb={secondary ? 0.5 : 0}>
            {description}
          </Typography>
          {secondary && (
            <Typography variant="caption" color="text.secondary" display="block">
              {secondary}
            </Typography>
          )}
        </Box>
        {children}
        <Button
          variant="contained"
          color="primary"
          size="small"
          startIcon={running ? <CircularProgress size={16} color="inherit" /> : actionIcon}
          onClick={onAction}
          disabled={running || actionDisabled || sectionReadOnly}
          sx={{ alignSelf: "flex-start" }}
        >
          {running ? actionRunningLabel : actionLabel}
        </Button>
      </Stack>
    </Box>
  );
}
