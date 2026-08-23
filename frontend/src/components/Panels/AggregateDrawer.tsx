import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Drawer,
  FormControl,
  FormControlLabel,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Switch,
  Tab,
  Tabs,
  Tooltip,
  Typography,
} from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import DeleteIcon from "@mui/icons-material/Delete";
import RefreshIcon from "@mui/icons-material/Refresh";

import { aggregatesApi, schedulerApiClient } from "../../api/client";
import { useDimensions, useMeasures, useTargets } from "../../api/hooks";
import type {
  AggregateCreate,
  AggregateDefinition,
  Dimension,
  Measure,
} from "../../api/types";
import AggregateEstimate from "../AggregateEstimate";
import { useConfirm } from "../Confirm";
import { useT } from "../../i18n";

type Mode = "create" | "edit";

type TabKey = "definition" | "advanced";

interface MeasureRow {
  name: string;
  functions: string[];
}

interface Props {
  open: boolean;
  mode: Mode;
  projectId: string;
  modelId: string;
  aggregate?: AggregateDefinition | null;
  onClose: () => void;
  onDeleted?: () => void;
}

const DEFAULT_CRON = "0 2 * * *";

export default function AggregateDrawer({
  open,
  mode,
  projectId,
  modelId,
  aggregate,
  onClose,
  onDeleted,
}: Props) {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const t = useT();
  const targets = useTargets(projectId, modelId);
  const dimensions = useDimensions(projectId, modelId);
  const measures = useMeasures(projectId, modelId);

  const [tab, setTab] = useState<TabKey>("definition");

  // Definition state
  const [targetId, setTargetId] = useState<string>("");
  const [grain, setGrain] = useState<string[]>([]);
  const [measureRows, setMeasureRows] = useState<MeasureRow[]>([]);
  const [includeQuantiles, setIncludeQuantiles] = useState<boolean>(false);
  const [includeStats, setIncludeStats] = useState<boolean>(false);
  const [status, setStatus] = useState<string>("active");

  // Initial snapshots for dirty detection
  const [initial, setInitial] = useState({
    targetId: "",
    grain: [] as string[],
    measures: [] as MeasureRow[],
    includeQuantiles: false,
    includeStats: false,
    status: "active",
  });

  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    if (mode === "edit" && aggregate) {
      const nextGrain = aggregate.grain ?? [];
      const nextMeasures: MeasureRow[] = (aggregate.measure_names ?? []).map(
        (name) => {
          const m = measures.data?.find((mm) => mm.name === name);
          return {
            name,
            functions: m ? [m.default_agg.toUpperCase()] : ["SUM"],
          };
        },
      );
      // Aggregates don't expose target_id on the definition; leave blank in
      // the read-only target row for edit mode.
      const nextTarget = "";
      setTargetId(nextTarget);
      setGrain(nextGrain);
      setMeasureRows(nextMeasures);
      setIncludeQuantiles(aggregate.include_quantiles);
      setIncludeStats(aggregate.include_stats ?? false);
      setStatus(aggregate.status);
      setInitial((prev) => ({
        ...prev,
        targetId: nextTarget,
        grain: nextGrain,
        measures: nextMeasures,
        includeQuantiles: aggregate.include_quantiles,
        includeStats: aggregate.include_stats ?? false,
        status: aggregate.status,
      }));
    } else {
      const nextTarget = targets.data?.[0]?.id ?? "";
      setTargetId(nextTarget);
      setGrain([]);
      setMeasureRows([]);
      setIncludeQuantiles(false);
      setIncludeStats(false);
      setStatus("active");
      setInitial({
        targetId: nextTarget,
        grain: [],
        measures: [],
        includeQuantiles: false,
        includeStats: false,
        status: "active",
      });
    }
    setTab("definition");
    setSubmitError(null);
  }, [open, mode, aggregate, targets.data, measures.data]);

  const isDirty = useMemo(() => {
    return (
      targetId !== initial.targetId ||
      JSON.stringify([...grain].sort()) !==
        JSON.stringify([...initial.grain].sort()) ||
      JSON.stringify(measureRows) !== JSON.stringify(initial.measures) ||
      includeQuantiles !== initial.includeQuantiles ||
      includeStats !== initial.includeStats ||
      status !== initial.status
    );
  }, [
    targetId,
    initial,
    grain,
    measureRows,
    includeQuantiles,
    includeStats,
    status,
  ]);

  const [redundantItems, setRedundantItems] = useState<
    Array<{ grain: string; partner_column: string; reason: string }> | null
  >(null);

  const saveMutation = useMutation({
    mutationFn: async () => {
      setSubmitError(null);
      setRedundantItems(null);
      if (mode === "create") {
        const measureNames = measureRows.map((r) => r.name).filter(Boolean);
        const data: AggregateCreate = {
          target_id: targetId,
          grain,
          measure_names: measureNames,
          include_quantiles: includeQuantiles,
          include_stats: includeStats,
          creation_reason: "manual",
        };
        try {
          const created = await aggregatesApi.create(projectId, modelId, data);
          await aggregatesApi.setPolicy(projectId, modelId, created.id, {
            refresh_mode: "scheduled",
            cron_expression: DEFAULT_CRON,
            is_enabled: true,
          });
          return created;
        } catch (err: unknown) {
          const detail = (
            err as { response?: { data?: { detail?: { error?: string; redundant?: unknown[] } } } }
          )?.response?.data?.detail;
          if (detail && typeof detail === "object" && detail.error === "redundant_grain") {
            setRedundantItems(
              (detail.redundant ?? []) as Array<{ grain: string; partner_column: string; reason: string }>,
            );
            throw err;
          }
          throw err;
        }
      }
      if (!aggregate) throw new Error("No aggregate to edit");
      // Status / include_quantiles / include_stats changes go through the
      // lightweight PATCH.
      if (
        status !== initial.status ||
        includeQuantiles !== initial.includeQuantiles ||
        includeStats !== initial.includeStats
      ) {
        await aggregatesApi.update(projectId, modelId, aggregate.id, {
          status,
          include_quantiles: includeQuantiles,
          include_stats: includeStats,
        });
      }
      return aggregate;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["aggregates", projectId, modelId] });
      onClose();
    },
    onError: (err: unknown) => setSubmitError(extractError(err, t)),
  });

  const confirmRedundantMutation = useMutation({
    mutationFn: async () => {
      setSubmitError(null);
      const measureNames = measureRows.map((r) => r.name).filter(Boolean);
      const data: AggregateCreate = {
        target_id: targetId,
        grain,
        measure_names: measureNames,
        include_quantiles: includeQuantiles,
        include_stats: includeStats,
        creation_reason: "manual",
        confirm_redundant_grain: true,
      };
      const created = await aggregatesApi.create(projectId, modelId, data);
      await aggregatesApi.setPolicy(projectId, modelId, created.id, {
        refresh_mode: "scheduled",
        cron_expression: DEFAULT_CRON,
        is_enabled: true,
      });
      return created;
    },
    onSuccess: () => {
      setRedundantItems(null);
      qc.invalidateQueries({ queryKey: ["aggregates", projectId, modelId] });
      onClose();
    },
    onError: (err: unknown) => setSubmitError(extractError(err, t)),
  });

  const refreshNowMutation = useMutation({
    mutationFn: () =>
      schedulerApiClient.triggerRefresh({
        aggregate_id: aggregate!.id,
        model_id: modelId,
        mode: "full",
      }),
    onSuccess: () => {
      qc.invalidateQueries({
        queryKey: ["aggregate-runs", projectId, modelId, aggregate?.id],
      });
      qc.invalidateQueries({ queryKey: ["aggregates", projectId, modelId] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () =>
      aggregatesApi.delete(projectId, modelId, aggregate!.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["aggregates", projectId, modelId] });
      onDeleted?.();
      onClose();
    },
  });

  const canSave = useMemo(() => {
    if (mode === "create") {
      return Boolean(
        targetId &&
          grain.length > 0 &&
          measureRows.some((r) => r.name),
      );
    }
    return Boolean(aggregate) && isDirty;
  }, [mode, targetId, grain, measureRows, aggregate, isDirty]);

  async function handleClose() {
    if (!isDirty) {
      onClose();
      return;
    }
    const ok = await confirm({
      title: t("aggDrawer.discardTitle"),
      message: t("aggDrawer.discardMessage"),
      confirmLabel: t("aggDrawer.discardConfirm"),
      destructive: true,
    });
    if (ok) onClose();
  }

  async function handleDelete() {
    if (!aggregate) return;
    const ok = await confirm({
      title: t("aggDrawer.deleteTitle"),
      message: t("aggDrawer.deleteMessage", { tableName: aggregate.physical_table_name }),
      confirmLabel: t("aggDrawer.deleteConfirm"),
      destructive: true,
    });
    if (ok) deleteMutation.mutate();
  }

  const selectedMeasureObjects: Measure[] =
    measures.data?.filter((m) =>
      measureRows.some((r) => r.name === m.name),
    ) ?? [];

  const title =
    mode === "create"
      ? t("aggregate.drawerTitleCreate")
      : t("aggregate.drawerTitleEdit", { name: aggregate?.physical_table_name ?? "" });

  return (
    <Drawer
      anchor="right"
      open={open}
      onClose={handleClose}
      sx={{ zIndex: (t) => t.zIndex.drawer + 3 }}
      PaperProps={{ sx: { width: 720 } }}
    >
      <Box
        display="flex"
        alignItems="center"
        px={2}
        py={1}
        borderBottom={1}
        borderColor="divider"
      >
        <Typography variant="h6" flexGrow={1}>
          {title}
        </Typography>
        {mode === "edit" && aggregate && (
          <Tooltip title={t("aggregate.refreshNow")}>
            <span>
              <IconButton
                onClick={() => refreshNowMutation.mutate()}
                disabled={refreshNowMutation.isPending}
              >
                {refreshNowMutation.isPending ? (
                  <CircularProgress size={18} />
                ) : (
                  <RefreshIcon />
                )}
              </IconButton>
            </span>
          </Tooltip>
        )}
        <IconButton onClick={handleClose}>
          <CloseIcon />
        </IconButton>
      </Box>

      <Tabs
        value={tab}
        onChange={(_, v) => setTab(v as TabKey)}
        sx={{ px: 2 }}
      >
        <Tab value="definition" label={t("aggregate.definition")} />
        <Tab value="advanced" label={t("aggregate.advanced")} disabled={mode === "create"} />
      </Tabs>

      <Box flexGrow={1} overflow="auto" px={2} py={2}>
        {tab === "definition" && (
          <Stack spacing={1.5}>
            {mode === "create" && (
              <FormControl size="small" fullWidth>
                <InputLabel>{t("aggregate.targetLabel")}</InputLabel>
                <Select
                  value={targetId}
                  label={t("aggregate.targetLabel")}
                  onChange={(e) => setTargetId(String(e.target.value))}
                >
                  {(targets.data ?? []).map((t) => (
                    <MenuItem key={t.id} value={t.id}>
                      {t.display_name}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
            )}

            <Alert severity="info" variant="outlined" sx={{ py: 0.5 }}>
              {t("aggDrawer.aggregateInfo")}
            </Alert>

            {mode === "edit" &&
              aggregate?.creation_reason === "ai" &&
              aggregate.rationale && (
                <Box
                  sx={{
                    p: 1.5,
                    bgcolor: "primary.50",
                    borderRadius: 1,
                    border: "1px solid",
                    borderColor: "primary.200",
                  }}
                >
                  <Typography
                    variant="caption"
                    color="primary.main"
                    fontWeight={600}
                    display="block"
                    mb={0.5}
                  >
                    {t("aggDrawer.aiRationale")}
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    {aggregate.rationale}
                  </Typography>
                </Box>
              )}

            {mode === "create" ? (
              <>
                <FormControl size="small" fullWidth>
                   <InputLabel>{t("aggregate.grainLabel")}</InputLabel>
                   <Select
                     multiple
                     value={grain}
                     label={t("aggregate.grainLabel")}
                    onChange={(e) =>
                      setGrain(
                        typeof e.target.value === "string"
                          ? e.target.value.split(",")
                          : (e.target.value as string[]),
                      )
                    }
                    renderValue={(sel) => (sel as string[]).join(", ")}
                  >
                    {(dimensions.data ?? []).map((d) => renderDimensionItem(d, t))}
                  </Select>
                </FormControl>

                <Box>
                  <Typography variant="subtitle2" fontWeight={700} mb={0.5}>
                    {t("aggregate.measuresLabel")}
                  </Typography>
                  <FormControl size="small" fullWidth>
                    <InputLabel>{t("aggregate.measuresLabel")}</InputLabel>
                    <Select
                      multiple
                      value={measureRows.map((r) => r.name)}
                      label={t("aggregate.measuresLabel")}
                      onChange={(e) => {
                        const names =
                          typeof e.target.value === "string"
                            ? e.target.value.split(",")
                            : (e.target.value as string[]);
                        setMeasureRows(
                          names.map((name) => {
                            const existing = measureRows.find(
                              (r) => r.name === name,
                            );
                            if (existing) return existing;
                            const m = measures.data?.find(
                              (mm) => mm.name === name,
                            );
                            return {
                              name,
                              functions: m
                                ? [m.default_agg.toUpperCase()]
                                : ["SUM"],
                            };
                          }),
                        );
                      }}
                      renderValue={(sel) => (sel as string[]).join(", ")}
                    >
                      {(measures.data ?? []).map((m) => renderMeasureItem(m, t))}
                    </Select>
                  </FormControl>
                </Box>
              </>
            ) : (
              <>
                <Box>
                  <Typography
                    variant="subtitle2"
                    fontWeight={700}
                    display="block"
                    mb={0.25}
                  >
                    {t("aggDrawer.grainReadOnly")}
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    {grain.length === 0 ? "—" : grain.join(", ")}
                  </Typography>
                </Box>

                <Box>
                  <Typography
                    variant="subtitle2"
                    fontWeight={700}
                    display="block"
                    mb={0.25}
                  >
                    {t("aggregate.measuresLabel")}
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    {measureRows.length === 0
                      ? "—"
                      : measureRows
                          .map((r) => `${r.name} (${r.functions.join(", ")})`)
                          .join(", ")}
                  </Typography>
                </Box>

                <Alert severity="info" variant="outlined" sx={{ py: 0.5 }}>
                  {t("aggDrawer.grainMeasuresReadOnly")}
                </Alert>
              </>
            )}

            <Tooltip title={t("aggregate.includeQuantilesTooltip")} arrow>
              <FormControlLabel
                control={
                  <Switch
                    checked={includeQuantiles}
                    onChange={(_, v) => setIncludeQuantiles(v)}
                  />
                }
                label={t("aggregate.includeQuantiles")}
              />
            </Tooltip>

            <FormControlLabel
              control={
                <Switch
                  checked={includeStats}
                  onChange={(_, v) => setIncludeStats(v)}
                />
              }
              label={t("aggregate.includeStats")}
            />

            {mode === "edit" && (status === "active" || status === "disabled") && (
              <FormControl size="small" fullWidth>
                <InputLabel>{t("aggregate.statusLabel")}</InputLabel>
                <Select
                  value={status}
                  label={t("aggregate.statusLabel")}
                  onChange={(e) => setStatus(e.target.value)}
                >
                  <MenuItem value="active">{t("aggregate.active")}</MenuItem>
                  {status === "disabled" && (
                    <MenuItem value="disabled">{t("aggregate.disabledStatus")}</MenuItem>
                  )}
                  <MenuItem value="retired">{t("aggregate.retiredStatus")}</MenuItem>
                </Select>
              </FormControl>
            )}
            {mode === "edit" && status === "disabled" && (
              <Alert severity="warning" variant="outlined" sx={{ py: 0.5 }}>
                {t("aggregate.disabledAlert")}
              </Alert>
            )}
            {mode === "edit" && status === "retired" && (
              <Alert severity="info" variant="outlined" sx={{ py: 0.5 }}>
                {t("aggregate.retiredAlert")}
              </Alert>
            )}
            {mode === "edit" && (status === "invalid" || status === "pending") && (
              <Alert
                severity={status === "invalid" ? "warning" : "info"}
                variant="outlined"
                sx={{ py: 0.5 }}
              >
                {status === "invalid"
                  ? t("aggregate.invalidAlert", { reason: aggregate?.invalid_reason ?? t("aggregate.unknownReason") })
                  : t("aggregate.pendingBuildAlert")}
              </Alert>
            )}

            {mode === "create" &&
              (grain.length > 0 || selectedMeasureObjects.length > 0) && (
                <AggregateEstimate
                  selectedDimensions={grain}
                  selectedMeasures={selectedMeasureObjects}
                  includeQuantiles={includeQuantiles}
                  includeStats={includeStats}
                />
              )}
          </Stack>
        )}

        {tab === "advanced" && aggregate && (
          <Stack spacing={1}>
            <KV label={t("aggregate.statusLabel")} value={aggregate.status} />
            <KV label={t("aggregate.physicalTable")} value={aggregate.physical_table_name} />
            <KV
              label={t("aggregate.created")}
              value={new Date(aggregate.created_at).toLocaleString()}
            />
            <KV
              label={t("aggregate.retired")}
              value={
                aggregate.retired_at
                  ? new Date(aggregate.retired_at).toLocaleString()
                  : "—"
              }
            />
            <KV
              label={t("aggregate.estimatedHitRate")}
              value={
                aggregate.estimated_hit_rate != null
                  ? `${(aggregate.estimated_hit_rate * 100).toFixed(1)}%`
                  : "—"
              }
            />
            <KV
              label={t("aggregate.creationReason")}
              value={creationReasonLabel(aggregate.creation_reason, t)}
            />
            {aggregate.invalid_reason && (
              <KV label={t("aggregate.invalidReason")} value={aggregate.invalid_reason} />
            )}
            {aggregate.rationale && (
              <KV label={t("aggregate.rationale")} value={aggregate.rationale} />
            )}
          </Stack>
        )}
      </Box>

      {redundantItems && redundantItems.length > 0 && (
        <Alert
          severity="warning"
          sx={{ mx: 2, mb: 1 }}
          action={
            <Button
              size="small"
              onClick={() => confirmRedundantMutation.mutate()}
              disabled={confirmRedundantMutation.isPending}
            >
              {confirmRedundantMutation.isPending ? t("aggregate.creating") : t("aggregate.createAnyway")}
            </Button>
          }
        >
          <Typography variant="body2" fontWeight={600} mb={0.5}>
            {t("aggregate.redundantGrainDetected")}
          </Typography>
          {redundantItems.map((r) => (
            <Typography key={r.grain} variant="caption" display="block">
              {t("aggregate.redundantGrainOverlap", {
                grain: r.grain,
                partner: r.partner_column,
                reason: r.reason,
              })}
            </Typography>
          ))}
        </Alert>
      )}

      {submitError && !redundantItems && (
        <Alert severity="error" sx={{ mx: 2, mb: 1 }}>
          {submitError}
        </Alert>
      )}

      <Box
        display="flex"
        gap={1}
        p={2}
        borderTop={1}
        borderColor="divider"
        alignItems="center"
      >
        {mode === "edit" && aggregate && (
          <Button
            startIcon={<DeleteIcon />}
            onClick={handleDelete}
            disabled={deleteMutation.isPending}
          >
            {deleteMutation.isPending ? t("aggregate.deleting") : t("aggregate.delete")}
          </Button>
        )}
        <Box flexGrow={1} />
        <Button onClick={handleClose}>{t("aggregate.cancel")}</Button>
        <Button
          variant="contained"
          onClick={() => saveMutation.mutate()}
          disabled={!canSave || saveMutation.isPending}
        >
          {saveMutation.isPending ? <CircularProgress size={16} /> : t("aggregate.save")}
        </Button>
      </Box>
    </Drawer>
  );
}

function renderDimensionItem(d: Dimension, t: ReturnType<typeof useT>) {
  const redundant = d.redundant_partner;
  const item = (
    <MenuItem
      key={d.id}
      value={d.name}
      disabled={!!redundant}
      sx={
        redundant ? { color: "text.disabled", fontStyle: "italic" } : undefined
      }
    >
      {d.display_name || d.name}
      {d.is_time_dim && (
        <Typography component="span" variant="caption" color="text.secondary" sx={{ ml: 1 }}>
          {t("aggDrawer.timeIndicator")}
        </Typography>
      )}
      {redundant && (
        <Typography component="span" variant="caption" color="text.disabled" sx={{ ml: 1, fontStyle: "italic" }}>
          → {redundant.partner_column_name}
        </Typography>
      )}
    </MenuItem>
  );
  return redundant ? (
    <Tooltip key={d.id} title={redundant.reason} placement="right">
      <span>{item}</span>
    </Tooltip>
  ) : (
    item
  );
}

function renderMeasureItem(m: Measure, t: ReturnType<typeof useT>) {
  const redundant = m.redundant_partner;
  const item = (
    <MenuItem
      key={m.id}
      value={m.name}
      disabled={!!redundant}
      sx={
        redundant ? { color: "text.disabled", fontStyle: "italic" } : undefined
      }
    >
      {m.display_name || m.name} ({m.default_agg})
      {!m.is_additive && (
        <Typography component="span" variant="caption" color="warning.main" sx={{ ml: 1, fontWeight: 600 }}>
          {t("aggDrawer.nonAdditiveIndicator")}
        </Typography>
      )}
      {redundant && (
        <Typography component="span" variant="caption" color="text.disabled" sx={{ ml: 1, fontStyle: "italic" }}>
          → {redundant.partner_column_name}
        </Typography>
      )}
    </MenuItem>
  );
  return redundant ? (
    <Tooltip key={m.id} title={redundant.reason} placement="right">
      <span>{item}</span>
    </Tooltip>
  ) : (
    item
  );
}

function creationReasonLabel(reason: string, t: ReturnType<typeof useT>): string {
  if (reason === "ai") return t("lineage.generatorAi");
  if (reason === "auto") return t("lineage.generatorAuto");
  if (reason === "manual") return t("lineage.generatorManual");
  return t("aggDrawer.creationReason", { reason });
}

function KV({ label, value }: { label: string; value: string }) {
  return (
    <Box display="flex" gap={2}>
      <Typography variant="body2" color="text.secondary" sx={{ width: 180 }}>
        {label}
      </Typography>
      <Typography variant="body2">{value}</Typography>
    </Box>
  );
}

function extractError(err: unknown, t: ReturnType<typeof useT>): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })
    ?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const obj = detail as { message?: string };
    if (obj.message) return obj.message;
    return JSON.stringify(detail);
  }
  if (err instanceof Error) return err.message;
  return t("errors.requestFailed");
}

