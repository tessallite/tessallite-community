import { useCallback, useEffect, useMemo, useState } from "react";
import { useT } from "../../i18n";
import { useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  CircularProgress,
  FormControl,
  FormControlLabel,
  InputLabel,
  MenuItem,
  Select,
  Slider,
  Stack,
  Switch,
  Typography,
} from "@mui/material";
import SearchIcon from "@mui/icons-material/Search";
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome";
import {
  aiSchedulerApi,
  aiOptimizerApi,
  optimizerApiClient,
} from "../../api/client";
import { useAISchedulerConfig, useLLMConfigs } from "../../api/hooks";
import type { ModelAISchedulerConfigUpdate } from "../../api/types";
import { ui } from "../../theme/tokens";


interface Props {
  projectId: string;
  modelId: string;
  tenantId: string;
}

export default function SmartBuilderSection({ projectId, modelId, tenantId }: Props) {
  const t = useT();
  const FREQUENCY_OPTIONS = useMemo(() => [
    { label: t("smartBuilder.frequency.every6h"), cron: "0 */6 * * *" },
    { label: t("smartBuilder.frequency.every12h"), cron: "0 */12 * * *" },
    { label: t("smartBuilder.frequency.onceADay"), cron: "0 5 * * *" },
    { label: t("smartBuilder.frequency.onceAWeek"), cron: "0 5 * * 1" },
  ], [t]);
  // Track the loaded cron so an unrecognised (custom) cron set via the API is
  // preserved and round-tripped unchanged instead of being coerced to the
  // "Once a day" preset on the next save (F-011-16c).
  const [loadedCron, setLoadedCron] = useState<string>("0 5 * * *");
  const CUSTOM_LABEL = t("smartBuilder.frequency.custom");
  const isPresetCron = useCallback((cron: string): boolean => {
    return FREQUENCY_OPTIONS.some((o) => o.cron === cron);
  }, [FREQUENCY_OPTIONS]);
  const cronToLabel = useCallback((cron: string): string => {
    return FREQUENCY_OPTIONS.find((o) => o.cron === cron)?.label ?? CUSTOM_LABEL;
  }, [FREQUENCY_OPTIONS, CUSTOM_LABEL]);
  const { data: config, isLoading } = useAISchedulerConfig(projectId, modelId);
  const { data: llmConfigs } = useLLMConfigs(projectId);
  const qc = useQueryClient();

  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [runningSweep, setRunningSweep] = useState(false);
  const [message, setMessage] = useState<{ type: "success" | "error"; text: string } | null>(null);

  const [aiEnabled, setAiEnabled] = useState(false);
  const [scheduleLabel, setScheduleLabel] = useState("");
  const [lookbackDays, setLookbackDays] = useState(7);
  const [maxCreates, setMaxCreates] = useState(3);
  const [dryRun, setDryRun] = useState(false);
  // Review gate (F-011-05): when ON, AI suggestions are registered disabled for
  // manual review rather than materialised and routed immediately. It is the
  // inverse of the backend's enable_ai_aggregation flag.
  const [requireReview, setRequireReview] = useState(false);
  const [minConfidence, setMinConfidence] = useState(0.5);
  const [llmConfigId, setLlmConfigId] = useState("");

  const [initialized, setInitialized] = useState(false);
  // Whether the saved config currently differs from the in-form values. Used to
  // gate "Run AI now" so the user does not run against stale server state and
  // get a raw backend 409 (F-011-16b).
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    if (config && llmConfigs !== undefined && !initialized) {
      setAiEnabled(config.ai_enabled);
      setLoadedCron(config.cron_expression);
      setScheduleLabel(cronToLabel(config.cron_expression));
      setLookbackDays(Math.round(config.lookback_hours / 24));
      setMaxCreates(config.max_creates_per_run);
      setDryRun(config.dry_run);
      setRequireReview(!config.enable_ai_aggregation);
      setMinConfidence(config.min_confidence);
      setLlmConfigId(config.llm_config_id ?? "");
      setInitialized(true);
      setDirty(false);
    }
  }, [config, llmConfigs, initialized]);

  useEffect(() => {
    setInitialized(false);
  }, [modelId]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setMessage(null);
    try {
      // When the schedule shows the Custom label (an API-set cron the presets
      // don't cover), keep the loaded cron rather than coercing to a preset
      // (F-011-16c).
      const matchedPreset = FREQUENCY_OPTIONS.find((o) => o.label === scheduleLabel);
      const cronToSave = matchedPreset ? matchedPreset.cron : loadedCron;
      const update: ModelAISchedulerConfigUpdate = {
        ai_enabled: aiEnabled,
        cron_expression: cronToSave,
        lookback_hours: lookbackDays * 24,
        max_creates_per_run: maxCreates,
        min_confidence: minConfidence,
        dry_run: dryRun,
        enable_ai_aggregation: !requireReview,
        llm_config_id: llmConfigId || undefined,
      };
      const saved = await aiSchedulerApi.update(projectId, modelId, update);
      qc.setQueryData(["aiSchedulerConfig", projectId, modelId], saved);
      setAiEnabled(saved.ai_enabled);
      setLoadedCron(saved.cron_expression);
      setScheduleLabel(cronToLabel(saved.cron_expression));
      setLookbackDays(Math.round(saved.lookback_hours / 24));
      setMaxCreates(saved.max_creates_per_run);
      setDryRun(saved.dry_run);
      setRequireReview(!saved.enable_ai_aggregation);
      setMinConfidence(saved.min_confidence);
      setLlmConfigId(saved.llm_config_id ?? "");
      setDirty(false);
      setMessage({ type: "success", text: t("smartBuilder.settingsSaved") });
    } catch {
      setMessage({ type: "error", text: t("smartBuilder.saveFailed") });
    } finally {
      setSaving(false);
    }
  }, [projectId, modelId, aiEnabled, scheduleLabel, loadedCron, lookbackDays, maxCreates, dryRun, requireReview, minConfidence, llmConfigId, cronToLabel, qc, t]);

  const handleRunAI = useCallback(async () => {
    // F-011-16b: running against unsaved changes (e.g. flipping AI on then
    // clicking Run before Save) returns a raw backend 409 like "AI optimiser is
    // not enabled for model <uuid>". Block it with a clear, translated prompt to
    // save first rather than surfacing the raw detail.
    if (dirty) {
      setMessage({ type: "error", text: t("smartBuilder.saveBeforeRun") });
      return;
    }
    setRunning(true);
    setMessage(null);
    try {
      await aiOptimizerApi.triggerRun({ model_id: modelId, dry_run: dryRun });
      setMessage({ type: "success", text: t("smartBuilder.aiCheckStarted") });
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
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
          text: typeof detail === "string" ? detail : t("smartBuilder.aiCheckFailed"),
        });
      }
    } finally {
      setRunning(false);
    }
  }, [modelId, dryRun, dirty, t]);

  const handleRunSweep = useCallback(async () => {
    setRunningSweep(true);
    setMessage(null);
    try {
      const result = await optimizerApiClient.runModelSweep(modelId);
      if (result.errors && result.errors.length > 0) {
        setMessage({
          type: "error",
          text: t("smartBuilder.sweepFoundWithErrors", { candidates: String(result.candidates_found), errors: String(result.errors.length), firstError: result.errors[0] }),
        });
      } else {
        setMessage({
          type: "success",
          text: t("smartBuilder.sweepFoundAndCreated", { candidates: String(result.candidates_found), created: String(result.aggregates_created) }),
        });
      }
    } catch {
      setMessage({ type: "error", text: t("smartBuilder.sweepFailed") });
    } finally {
      setRunningSweep(false);
    }
  }, [modelId]);

  if (isLoading) return <CircularProgress size={24} />;

  return (
    <Stack spacing={2} data-testid="smart-builder-section">
      {message && (
        <Alert severity={message.type} onClose={() => setMessage(null)}>
          {message.text}
        </Alert>
      )}

      <Typography variant="caption" color="text.secondary">
        {t("smartBuilder.description")}
      </Typography>

      {/* Pattern-based */}
      <Card variant="outlined">
        <CardContent sx={{ py: 1.25, "&:last-child": { pb: 1.25 } }}>
          <Box display="flex" alignItems="center" gap={1} mb={0.5}>
            <SearchIcon fontSize="small" sx={{ color: ui.green }} />
            <Typography variant="subtitle2" fontWeight={700}>
              {t("smartBuilder.patternBasedTitle")}
            </Typography>
          </Box>
          <Typography variant="caption" color="text.secondary" display="block" mb={1.5}>
            {t("smartBuilder.patternBasedDescription")}
          </Typography>
          <Button
            variant="contained"
            size="small"
            startIcon={runningSweep ? <CircularProgress size={16} color="inherit" /> : <SearchIcon />}
            onClick={handleRunSweep}
            disabled={runningSweep}
            data-testid="run-pattern-sweep"
          >
            {runningSweep ? t("smartBuilder.checking") : t("smartBuilder.findOpportunities")}
          </Button>
        </CardContent>
      </Card>

      {/* AI-powered */}
      <Card variant="outlined">
        <CardContent sx={{ py: 1.25, "&:last-child": { pb: 1.25 } }}>
          <Box display="flex" alignItems="center" gap={1} mb={0.5}>
            <AutoAwesomeIcon fontSize="small" sx={{ color: ui.green }} />
            <Typography variant="subtitle2" fontWeight={700}>
              {t("smartBuilder.aiPoweredTitle")}
            </Typography>
          </Box>
          <Typography variant="caption" color="text.secondary" display="block" mb={1.5}>
            {t("smartBuilder.aiPoweredDescription")}
          </Typography>

          <Stack spacing={1.5}>
            <FormControlLabel
              control={
                <Switch
                  checked={aiEnabled}
                  onChange={(e) => { setAiEnabled(e.target.checked); setDirty(true); }}
                  data-testid="ai-enabled-toggle"
                />
              }
              label={t("smartBuilder.aiEnabledLabel")}
            />

            <FormControl size="small" fullWidth disabled={!aiEnabled}>
              <InputLabel>{t("smartBuilder.checkFrequency")}</InputLabel>
              <Select
                value={scheduleLabel}
                label={t("smartBuilder.checkFrequency")}
                onChange={(e) => { setScheduleLabel(e.target.value); setDirty(true); }}
                data-testid="ai-frequency-select"
              >
                {FREQUENCY_OPTIONS.map((o) => (
                  <MenuItem key={o.cron} value={o.label}>
                    {o.label}
                  </MenuItem>
                ))}
                {/* F-011-16c: a custom (API-set) cron the presets don't cover is
                    shown read-only as "Custom (<cron>)" and round-trips unchanged. */}
                {!isPresetCron(loadedCron) && (
                  <MenuItem key="__custom" value={CUSTOM_LABEL}>
                    {t("smartBuilder.frequency.customWithCron", { cron: loadedCron })}
                  </MenuItem>
                )}
              </Select>
            </FormControl>

            <Box>
              <Typography variant="caption" color="text.secondary">
                {t("smartBuilder.lookbackLabel", { days: String(lookbackDays), unit: lookbackDays === 1 ? t("smartBuilder.lookbackDayUnit") : t("smartBuilder.lookbackDaysUnit") })}
              </Typography>
              <Slider
                value={lookbackDays}
                onChange={(_, v) => { setLookbackDays(v as number); setDirty(true); }}
                min={1}
                max={30}
                step={1}
                marks={[
                  { value: 1, label: t("smartBuilder.sliderLabelOneDay") },
                  { value: 7, label: "7" },
                  { value: 14, label: "14" },
                  { value: 30, label: t("smartBuilder.sliderLabelThirtyDays") },
                ]}
                disabled={!aiEnabled}
                valueLabelDisplay="auto"
                valueLabelFormat={(v) => t("smartBuilder.sliderValueDays", { v: String(v) })}
                data-testid="ai-lookback-slider"
              />
            </Box>

            <Box>
              <Typography variant="caption" color="text.secondary">
                {t("smartBuilder.maxCreatesLabel", { count: String(maxCreates), unit: maxCreates !== 1 ? t("smartBuilder.summariesUnit") : t("smartBuilder.summaryUnit") })}
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
                disabled={!aiEnabled}
                valueLabelDisplay="auto"
                data-testid="ai-max-creates-slider"
              />
            </Box>

            <FormControlLabel
              control={
                <Switch
                  checked={dryRun}
                  onChange={(e) => { setDryRun(e.target.checked); setDirty(true); }}
                  disabled={!aiEnabled}
                  data-testid="ai-dry-run-toggle"
                />
              }
              label={t("smartBuilder.dryRunLabel")}
            />

            <Box>
              <FormControlLabel
                control={
                  <Switch
                    checked={requireReview}
                    onChange={(e) => { setRequireReview(e.target.checked); setDirty(true); }}
                    disabled={!aiEnabled || dryRun}
                    data-testid="ai-require-review-toggle"
                  />
                }
                label={t("smartBuilder.requireReviewLabel")}
              />
              <Typography variant="caption" color="text.secondary" display="block">
                {t("smartBuilder.requireReviewHelp")}
              </Typography>
            </Box>

            {/* F-011-06: minimum-confidence quality gate. Recommendations the
                model scores below this are logged in the decision log but not
                acted upon. */}
            <Box>
              <Typography variant="caption" color="text.secondary">
                {t("smartBuilder.minConfidenceLabel", { value: minConfidence.toFixed(2) })}
              </Typography>
              <Slider
                value={minConfidence}
                onChange={(_, v) => { setMinConfidence(v as number); setDirty(true); }}
                min={0}
                max={1}
                step={0.05}
                marks={[
                  { value: 0, label: "0" },
                  { value: 0.5, label: "0.5" },
                  { value: 1, label: "1" },
                ]}
                disabled={!aiEnabled}
                valueLabelDisplay="auto"
                data-testid="ai-min-confidence-slider"
              />
              <Typography variant="caption" color="text.secondary" display="block">
                {t("smartBuilder.minConfidenceHelp")}
              </Typography>
            </Box>

            <FormControl size="small" fullWidth disabled={!aiEnabled}>
              <InputLabel>{t("smartBuilder.aiModelLabel")}</InputLabel>
              <Select
                value={llmConfigId}
                label={t("smartBuilder.aiModelLabel")}
                onChange={(e) => { setLlmConfigId(e.target.value); setDirty(true); }}
                data-testid="ai-llm-config-select"
              >
                <MenuItem value=""><em>{t("smartBuilder.useProjectDefault")}</em></MenuItem>
                {llmConfigs?.map((c: { id: string; display_name: string }) => (
                  <MenuItem key={c.id} value={c.id}>
                    {c.display_name}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>

            <Stack direction="row" spacing={1}>
              <Button
                variant="outlined"
                size="small"
                onClick={handleSave}
                disabled={saving}
                data-testid="ai-save-settings"
              >
                {saving ? t("smartBuilder.saving") : t("smartBuilder.saveSettings")}
              </Button>
              <Button
                variant="contained"
                size="small"
                startIcon={running ? <CircularProgress size={16} color="inherit" /> : <AutoAwesomeIcon />}
                onClick={handleRunAI}
                disabled={running || !aiEnabled}
                data-testid="run-ai-optimizer"
              >
                {running ? t("smartBuilder.running") : t("smartBuilder.runAiNow")}
              </Button>
            </Stack>
          </Stack>
        </CardContent>
      </Card>
    </Stack>
  );
}
