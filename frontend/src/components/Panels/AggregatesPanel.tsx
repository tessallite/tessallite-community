import { lazy, Suspense, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  IconButton,
  Stack,
  Switch,
  Tab,
  Tabs,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import CloseIcon from "@mui/icons-material/Close";
import RefreshIcon from "@mui/icons-material/Refresh";
import {
  aggregatesApi,
  dataQualityApi,
  modelsApi,
  optimizerApiClient,
  schedulerApiClient,
} from "../../api/client";
import { useAggregates, useModel, usePersonas } from "../../api/hooks";
import type {
  AggregateDefinition,
  AggregateROI,
  ModelUpdate,
  RefreshPolicy,
} from "../../api/types";
import { useBuilderStore } from "../../store/builderStore";
import { useCanAuthorModel } from "../../auth/useCanAuthorModel";
import AggregateCard from "../Aggregates/AggregateCard";
import PredictiveControls from "../Aggregates/PredictiveControls";
import { FrequencyPicker, RebuildMethodPicker, cronToPreset, presetToCron } from "../Refresh";
import type { RebuildMethod } from "../Refresh";
import { buildAggregateRefreshPolicy } from "../Refresh/policyPayload";
import AggregateDrawer from "./AggregateDrawer";
import { useConfirm } from "../Confirm";

const SmartBuilderSection = lazy(() => import("../Aggregates/SmartBuilderSection"));
const LifecycleLogPanel = lazy(() => import("./LifecycleLogPanel"));
const PredictiveAggregatesPanel = lazy(() => import("./PredictiveAggregatesPanel"));

type AggTab = "list" | "refresh" | "smart-builder" | "predictive" | "settings";

const TAB_KEYS: Record<AggTab, string> = {
  list: "aggregates.tabs.list",
  refresh: "aggregates.tabs.refresh",
  "smart-builder": "aggregates.tabs.smartBuilder",
  predictive: "aggregates.tabs.predictive",
  settings: "aggregates.tabs.settings",
};

export default function AggregatesPanel() {
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const t = useT();
  const aggregateTab = useBuilderStore((s) => s.aggregateTab);
  const setAggregateTab = useBuilderStore((s) => s.setAggregateTab);
  // F-026-04: gate every mutation entry point on the shared author capability.
  const canEdit = useCanAuthorModel();

  const [tab, setTab] = useState<AggTab>(aggregateTab ?? "list");
  const [createOpen, setCreateOpen] = useState(false);
  const [editAgg, setEditAgg] = useState<AggregateDefinition | null>(null);
  const [lifecycleOpen, setLifecycleOpen] = useState(false);

  const aggregates = useAggregates(projectId!, modelId!);
  const model = useModel(projectId!, modelId!);
  const modelEnabled = (model.data?.status ?? "active") !== "disabled";
  const aggregationsEnabled = model.data?.aggregations_enabled ?? true;

  const { data: aggViolations = {} } = useQuery<Record<string, number>>({
    queryKey: ["agg-violations", projectId, modelId],
    queryFn: () => dataQualityApi.aggregateViolationSummary(projectId!, modelId!),
    staleTime: 60_000,
  });

  const { data: roiData } = useQuery<AggregateROI[]>({
    queryKey: ["agg-roi", modelId],
    queryFn: () => optimizerApiClient.getModelROI(modelId!),
    staleTime: 120_000,
  });
  const roiByAggId = Object.fromEntries((roiData ?? []).map((r) => [r.aggregate_id, r]));

  const personas = usePersonas(projectId!, modelId!);
  const personaNameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const p of personas.data ?? []) map.set(p.id, p.slug ?? p.name ?? p.id);
    return map;
  }, [personas.data]);

  const toggleAggregations = useMutation({
    mutationFn: (enabled: boolean) =>
      modelsApi.update(projectId!, modelId!, { aggregations_enabled: enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["models", projectId, modelId] }),
  });

  const updateModel = useMutation({
    mutationFn: (patch: ModelUpdate) => modelsApi.update(projectId!, modelId!, patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["models", projectId, modelId] }),
  });

  const [refreshingAll, setRefreshingAll] = useState(false);
  const [refreshAllMsg, setRefreshAllMsg] = useState<string | null>(null);

  async function handleRefreshAll() {
    const active = (aggregates.data ?? []).filter((a) => a.status === "active");
    if (active.length === 0) return;
    setRefreshingAll(true);
    setRefreshAllMsg(null);
    let ok = 0;
    let failed = 0;
    for (const agg of active) {
      try {
        const res = await schedulerApiClient.triggerRefresh({
          aggregate_id: agg.id,
          model_id: modelId!,
          mode: "full",
        });
        if (res.status === "failed") failed++;
        else ok++;
      } catch {
        failed++;
      }
    }
    setRefreshingAll(false);
    setRefreshAllMsg(
      failed === 0
        ? t("aggregates.refreshAllSuccess", { count: String(ok) })
        : t("aggregates.refreshAllPartial", { ok: String(ok), failed: String(failed) }),
    );
    qc.invalidateQueries({ queryKey: ["aggregates", projectId, modelId] });
  }

  async function handleDeleteAgg(agg: AggregateDefinition) {
    const ok = await confirm({
      title: t("aggregates.deleteTitle"),
      message: (
        <span>
          {t("aggregates.deleteMessage", { name: agg.physical_table_name })}
        </span>
      ),
      confirmLabel: t("common.delete"),
      destructive: true,
    });
    if (ok) {
      await aggregatesApi.delete(projectId!, modelId!, agg.id);
      qc.invalidateQueries({ queryKey: ["aggregates", projectId, modelId] });
    }
  }

  function handleTabChange(_: unknown, v: AggTab) {
    setTab(v);
    setAggregateTab(v);
  }

  const activeAggCount = (aggregates.data ?? []).filter((a) => a.status === "active").length;

  return (
    <Box data-testid="aggregates-panel">
      <Tabs
        value={tab}
        onChange={handleTabChange}
        variant="scrollable"
        scrollButtons={false}
        sx={{ borderBottom: 1, borderColor: "divider", mb: 1.5, minHeight: 36 }}
      >
        {(Object.keys(TAB_KEYS) as AggTab[]).map((k) => (
          <Tab
            key={k}
            value={k}
            label={t(TAB_KEYS[k])}
            sx={{ minHeight: 36, py: 0, textTransform: "none", fontSize: 13 }}
            data-testid={`agg-tab-${k}`}
          />
        ))}
      </Tabs>

      {/* ═══ Tab: My Aggregates ═══ */}
      {tab === "list" && (
        <Box data-testid="agg-tab-content-list">
          <Box
            sx={{
              mb: 1,
              px: 1,
              py: 0.75,
              border: 1,
              borderColor: "divider",
              borderRadius: 1,
              bgcolor: "background.default",
            }}
          >
            <FormControlLabel
              control={
                <Switch
                  checked={aggregationsEnabled}
                  onChange={(e) => toggleAggregations.mutate(e.target.checked)}
                  disabled={!canEdit || toggleAggregations.isPending || !modelEnabled}
                  data-testid="agg-enable-toggle"
                />
              }
              label={t("aggregates.enableLabel")}
            />
            <FormControlLabel
              control={
                <Switch
                  checked={model.data?.include_all_measures ?? true}
                  onChange={(e) =>
                    updateModel.mutate({ include_all_measures: e.target.checked })
                  }
                  disabled={!canEdit || updateModel.isPending || !modelEnabled || !aggregationsEnabled}
                  data-testid="include-all-measures-toggle"
                />
              }
              label={t("aggregates.includeAllMeasures")}
              sx={{ ml: 0 }}
            />
          </Box>

          {canEdit && (
          <Box display="flex" gap={1} mb={1.5}>
            <Box flexGrow={1} />
            <Button
              size="small"
              variant="contained"
              startIcon={<AddIcon />}
              onClick={() => setCreateOpen(true)}
              disabled={!aggregationsEnabled || !modelEnabled}
              data-testid="agg-create-btn"
            >
              {t("aggregates.new")}
            </Button>
          </Box>
          )}

          {!modelEnabled && (
            <Alert severity="info" sx={{ mb: 1 }}>
              {t("aggregates.modelDisabled")}
            </Alert>
          )}
          {!aggregationsEnabled && (
            <Alert severity="info" sx={{ mb: 1 }}>
              {t("aggregates.summariesDisabled")}
            </Alert>
          )}

          {aggregates.isLoading ? (
            <CircularProgress size={20} />
          ) : aggregates.data?.length === 0 ? (
            <Typography variant="body2" color="text.secondary">
              {t("aggregates.none")}
            </Typography>
          ) : (
            <Stack spacing={1}>
              {aggregates.data?.map((agg) => (
                <AggregateCard
                  key={agg.id}
                  agg={agg}
                  projectId={projectId!}
                  modelId={modelId!}
                  violationCount={aggViolations[agg.id] ?? 0}
                  roi={roiByAggId[agg.id]}
                  personaName={agg.persona_id ? personaNameById.get(agg.persona_id) : undefined}
                  onEdit={() => setEditAgg(agg)}
                  onDelete={() => handleDeleteAgg(agg)}
                  canEdit={canEdit}
                />
              ))}
            </Stack>
          )}

          {/* Quick actions */}
          {activeAggCount > 0 && (
            <Box
              sx={{
                mt: 2,
                p: 1.5,
                border: 1,
                borderColor: "divider",
                borderRadius: 1,
              }}
            >
              <Typography variant="caption" color="text.secondary" display="block" mb={1}>
                {t("aggregates.activeSummaries")} {activeAggCount}
              </Typography>
              <Stack direction="row" spacing={1}>
                {canEdit && (
                <Button
                  size="small"
                  variant="outlined"
                  startIcon={
                    refreshingAll ? (
                      <CircularProgress size={14} color="inherit" />
                    ) : (
                      <RefreshIcon sx={{ fontSize: 16 }} />
                    )
                  }
                  onClick={handleRefreshAll}
                  disabled={refreshingAll}
                  data-testid="rebuild-all-btn"
                >
                  {refreshingAll ? t("aggregates.rebuilding") : t("aggregates.rebuildAll")}
                </Button>
                )}
                <Button
                  size="small"
                  variant="text"
                  onClick={() => setLifecycleOpen(true)}
                  data-testid="view-lifecycle-btn"
                >
                  {t("aggregates.viewLifecycle")}
                </Button>
              </Stack>
              {refreshAllMsg && (
                <Alert
                  severity={refreshAllMsg.includes("failed") ? "warning" : "success"}
                  sx={{ mt: 1 }}
                  onClose={() => setRefreshAllMsg(null)}
                >
                  {refreshAllMsg}
                </Alert>
              )}
            </Box>
          )}
        </Box>
      )}

      {/* ═══ Tab: Refresh ═══ */}
      {tab === "refresh" && (
        <RefreshTab projectId={projectId!} modelId={modelId!} canEdit={canEdit} />
      )}

      {/* ═══ Tab: Smart Builder ═══ */}
      {tab === "smart-builder" && (
        <Suspense fallback={<CircularProgress size={24} />}>
          <Box data-testid="agg-tab-content-smart-builder">
            <SmartBuilderSection
              projectId={projectId!}
              modelId={modelId!}
              tenantId=""
            />
          </Box>
        </Suspense>
      )}

      {/* ═══ Tab: Predictive ═══ */}
      {tab === "predictive" && (
        <Box data-testid="agg-tab-content-predictive">
          <Typography variant="caption" color="text.secondary" display="block" mb={1.5}>
            {t("aggregates.predictiveDescription")}
          </Typography>
          <PredictiveControls
            model={model.data}
            disabled={!canEdit || updateModel.isPending || !modelEnabled}
            onSave={(patch) => updateModel.mutate(patch)}
          />
          <Box mt={2}>
            <Suspense fallback={<CircularProgress size={24} />}>
              <PredictiveAggregatesPanel
                requiresApproval={Boolean(model.data?.predictive_requires_approval)}
              />
            </Suspense>
          </Box>
        </Box>
      )}

      {/* ═══ Tab: Settings ═══ */}
      {tab === "settings" && (
        <Box data-testid="agg-tab-content-settings">
          <FormControlLabel
            control={
              <Switch
                checked={aggregationsEnabled}
                onChange={(e) => toggleAggregations.mutate(e.target.checked)}
                disabled={!canEdit || toggleAggregations.isPending || !modelEnabled}
                data-testid="agg-settings-enable-toggle"
              />
            }
            label={t("aggregates.enableLabel")}
          />
          <Typography variant="caption" color="text.secondary" display="block" mt={1}>
            {t("aggregates.settings.description")}
          </Typography>
        </Box>
      )}

      {/* Drawers & Dialogs */}
      <AggregateDrawer
        open={createOpen}
        mode="create"
        projectId={projectId!}
        modelId={modelId!}
        onClose={() => setCreateOpen(false)}
      />
      <AggregateDrawer
        open={!!editAgg}
        mode="edit"
        projectId={projectId!}
        modelId={modelId!}
        aggregate={editAgg}
        onClose={() => setEditAgg(null)}
        onDeleted={() => setEditAgg(null)}
      />

      <Dialog
        open={lifecycleOpen}
        onClose={() => setLifecycleOpen(false)}
        maxWidth="md"
        fullWidth
      >
        <DialogTitle>
          <Box display="flex" alignItems="center">
            <Typography variant="h6" flexGrow={1}>
              {t("aggregates.lifecycleLog")}
            </Typography>
            <IconButton onClick={() => setLifecycleOpen(false)} size="small">
              <CloseIcon />
            </IconButton>
          </Box>
        </DialogTitle>
        <DialogContent>
          <Suspense fallback={<CircularProgress size={24} />}>
            <LifecycleLogPanel />
          </Suspense>
        </DialogContent>
      </Dialog>
    </Box>
  );
}

/* ─── Refresh Tab (per-aggregate schedule management) ─── */

export function RefreshTab({ projectId, modelId, canEdit }: { projectId: string; modelId: string; canEdit: boolean }) {
  const t = useT();
  const aggregates = useAggregates(projectId, modelId);
  const qc = useQueryClient();
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const activeAggs = aggregates.data?.filter((a) => a.status === "active") ?? [];

  // Bug-8785: the editor used to seed every row from a local daily/full/blank
  // default and then POST every active aggregate on save, so an aggregate with a
  // persisted incremental/append-only policy the user never opened was silently
  // overwritten with defaults. Persisted policy is now hydrated, edits are
  // tracked separately, and only edited rows are saved.
  const aggIdsKey = activeAggs.map((a) => a.id).join(",");
  const policies = useQuery({
    queryKey: ["aggregate-refresh-policies", projectId, modelId, aggIdsKey],
    enabled: activeAggs.length > 0,
    queryFn: async () => {
      const entries = await Promise.all(
        activeAggs.map(async (a) => {
          try {
            return [a.id, await aggregatesApi.getPolicy(projectId, modelId, a.id)] as const;
          } catch {
            // 404 = no policy configured yet. Distinct from "policy is default":
            // an unconfigured row must stay unconfigured unless the user edits it.
            return [a.id, null] as const;
          }
        }),
      );
      return Object.fromEntries(entries) as Record<string, RefreshPolicy | null>;
    },
  });

  type Schedule = {
    preset: string;
    method: RebuildMethod;
    incrCol: string;
    lookback: number;
    fullInterval: number | null;
  };
  const UNCONFIGURED: Schedule = {
    preset: "daily_2am",
    method: "full",
    incrCol: "",
    lookback: 1,
    fullInterval: null,
  };

  function fromPolicy(p: RefreshPolicy): Schedule {
    const incremental = p.refresh_mode === "incremental";
    return {
      preset: cronToPreset(p.cron_expression),
      method: incremental ? "incremental" : "full",
      incrCol: p.incremental_column ?? "",
      lookback: p.incremental_lookback ?? 1,
      fullInterval: p.full_rebuild_interval_days,
    };
  }

  // Only the fields the user actually touched. Never seeded from defaults, so a
  // row the user never edited can never be written back.
  const [edits, setEdits] = useState<Record<string, Partial<Schedule>>>({});

  function persistedSchedule(aggId: string): Schedule {
    const p = policies.data?.[aggId];
    return p ? fromPolicy(p) : UNCONFIGURED;
  }

  function getSchedule(aggId: string): Schedule {
    return { ...persistedSchedule(aggId), ...edits[aggId] };
  }

  function updateSchedule(aggId: string, patch: Partial<Schedule>) {
    setEdits((prev) => ({ ...prev, [aggId]: { ...prev[aggId], ...patch } }));
  }

  const dirtyIds = Object.keys(edits);

  async function handleSaveAll() {
    setSaving(true);
    setFeedback(null);
    let ok = 0;
    let failed = 0;
    for (const aggId of dirtyIds) {
      const s = getSchedule(aggId);
      const cron = presetToCron(s.preset);
      try {
        await aggregatesApi.setPolicy(
          projectId,
          modelId,
          aggId,
          buildAggregateRefreshPolicy({
            method: s.method,
            cron,
            incrementalColumn: s.incrCol,
            lookbackDays: s.lookback,
            fullRebuildIntervalDays: s.fullInterval,
          }),
        );
        ok++;
      } catch {
        failed++;
      }
    }
    setSaving(false);
    setFeedback(
      failed === 0
        ? t("aggregates.refreshSaveSuccess", { count: String(ok) })
        : t("aggregates.refreshSavePartial", { ok: String(ok), failed: String(failed) }),
    );
    if (failed === 0) setEdits({});
    qc.invalidateQueries({ queryKey: ["aggregates", projectId, modelId] });
    qc.invalidateQueries({ queryKey: ["aggregate-refresh-policies", projectId, modelId] });
  }

  return (
    <Box data-testid="agg-tab-content-refresh">
      <Typography variant="caption" color="text.secondary" display="block" mb={1.5}>
        {t("aggregates.refreshTabDescription")}
      </Typography>

      {aggregates.isLoading ? (
        <CircularProgress size={20} />
      ) : activeAggs.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t("aggregates.noActiveSummaries")}
        </Typography>
      ) : (
        <Stack spacing={2}>
          {activeAggs.map((agg) => {
            const s = getSchedule(agg.id);
            return (
              <Box
                key={agg.id}
                sx={{
                  p: 1.5,
                  border: 1,
                  borderColor: "divider",
                  borderRadius: 1,
                }}
                data-testid={`refresh-schedule-${agg.id}`}
              >
                <Typography variant="body2" fontWeight={600} mb={1}>
                  {agg.physical_table_name}
                </Typography>
                <Stack spacing={1.5}>
                  <FrequencyPicker
                    value={s.preset}
                    onChange={(v) => updateSchedule(agg.id, { preset: v })}
                  />
                  <RebuildMethodPicker
                    method={s.method}
                    onMethodChange={(m) => updateSchedule(agg.id, { method: m })}
                    incrementalColumn={s.incrCol}
                    onIncrementalColumnChange={(col) => updateSchedule(agg.id, { incrCol: col })}
                    lookbackDays={s.lookback}
                    onLookbackChange={(d) => updateSchedule(agg.id, { lookback: d })}
                    fullRebuildIntervalDays={s.fullInterval}
                    onFullRebuildIntervalChange={(d) => updateSchedule(agg.id, { fullInterval: d })}
                  />
                </Stack>
              </Box>
            );
          })}

          <Button
            variant="contained"
            size="small"
            onClick={handleSaveAll}
            disabled={!canEdit || saving || dirtyIds.length === 0}
            sx={{ alignSelf: "flex-start" }}
            data-testid="refresh-save-all"
          >
            {saving ? t("common.saving") : t("aggregates.saveChanges")}
          </Button>

          {feedback && (
            <Alert
              severity={feedback.includes("failed") ? "warning" : "success"}
              onClose={() => setFeedback(null)}
            >
              {feedback}
            </Alert>
          )}
        </Stack>
      )}
    </Box>
  );
}
