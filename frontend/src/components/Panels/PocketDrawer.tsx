/**
 * PocketDrawer — create/edit drawer for pocket tables with tabs:
 * Query (SQL editor + validate + dry-run), Schedule (cron + enabled +
 * run history), Advanced (read-only metadata).
 */
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
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
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import RefreshIcon from "@mui/icons-material/Refresh";

import { pocketsApi } from "../../api/client";
import { useTargets } from "../../api/hooks";
import type {
  PocketCreate,
  PocketDefinition,
  PocketDryRunResponse,
  PocketUpdate,
  PocketValidateResponse,
  PocketViolationItem,
} from "../../api/types";
import { useConfirm } from "../Confirm";
import { FrequencyPicker, RefreshRunHistory, cronToPreset, presetToCron } from "../Refresh";
import SqlQueryEditor from "../Sql/SqlQueryEditor";

type Mode = "create" | "edit";

interface Props {
  open: boolean;
  mode: Mode;
  projectId: string;
  modelId: string;
  pocket?: PocketDefinition | null;
  // F-005-13: prefill the SQL editor when opening "create" from a suggestion.
  prefillSql?: string;
  onClose: () => void;
}

type TabKey = "query" | "schedule" | "advanced";

export default function PocketDrawer({
  open,
  mode,
  projectId,
  modelId,
  pocket,
  prefillSql,
  onClose,
}: Props) {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const t = useT();
  const targets = useTargets(projectId, modelId);

  const [tab, setTab] = useState<TabKey>("query");
  const [targetId, setTargetId] = useState<string>("");
  const [definingSql, setDefiningSql] = useState<string>("");

  const [cron, setCron] = useState<string>("0 2 * * *");
  const [enabled, setEnabled] = useState<boolean>(false);
  const [incrementalColumn, setIncrementalColumn] = useState<string>("");
  const [lookbackHours, setLookbackHours] = useState<number>(24);
  // F-005-21: refresh trigger — "schedule" (cron sweep) or "event"
  // (re-materialise when the source schema for the model drifts).
  const [refreshPolicy, setRefreshPolicy] = useState<"schedule" | "event">("schedule");
  const [initialRefreshPolicy, setInitialRefreshPolicy] = useState<"schedule" | "event">("schedule");

  const [initialTargetId, setInitialTargetId] = useState<string>("");
  const [initialSql, setInitialSql] = useState<string>("");
  const [initialCron, setInitialCron] = useState<string>("0 2 * * *");
  const [initialEnabled, setInitialEnabled] = useState<boolean>(false);
  const [initialIncrementalColumn, setInitialIncrementalColumn] = useState<string>("");
  const [initialLookbackHours, setInitialLookbackHours] = useState<number>(24);

  const [validateResult, setValidateResult] = useState<PocketValidateResponse | null>(null);
  const [dryRunResult, setDryRunResult] = useState<PocketDryRunResponse | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitViolations, setSubmitViolations] = useState<PocketViolationItem[] | null>(null);

  useEffect(() => {
    if (!open) return;
    const nextTarget = mode === "edit" && pocket ? pocket.target_id : (targets.data?.[0]?.id ?? "");
    // F-005-13: a "create from suggestion" open prefills the SQL editor.
    const nextSql = mode === "edit" && pocket ? pocket.defining_sql : (prefillSql ?? "");
    const nextIncCol = mode === "edit" && pocket ? (pocket.incremental_column ?? "") : "";
    const nextLookback = mode === "edit" && pocket ? (pocket.incremental_lookback_hours ?? 24) : 24;
    const nextPolicy: "schedule" | "event" =
      mode === "edit" && pocket && pocket.refresh_policy === "event" ? "event" : "schedule";
    setTargetId(nextTarget);
    setDefiningSql(nextSql);
    setIncrementalColumn(nextIncCol);
    setLookbackHours(nextLookback);
    setRefreshPolicy(nextPolicy);
    setInitialRefreshPolicy(nextPolicy);
    setInitialTargetId(nextTarget);
    setInitialSql(nextSql);
    setInitialIncrementalColumn(nextIncCol);
    setInitialLookbackHours(nextLookback);
    setTab("query");
    setValidateResult(null);
    setDryRunResult(null);
    setSubmitError(null);
    setSubmitViolations(null);
  }, [open, mode, pocket, targets.data, prefillSql]);

  const policyQuery = useQuery({
    queryKey: ["pocket-policy", projectId, modelId, pocket?.id],
    enabled: open && mode === "edit" && Boolean(pocket?.id),
    queryFn: () => pocketsApi.getPolicy(projectId, modelId, pocket!.id),
    retry: false,
  });

  useEffect(() => {
    if (policyQuery.data) {
      const nextCron = policyQuery.data.cron_expression ?? "0 2 * * *";
      const nextEnabled = policyQuery.data.is_enabled;
      setCron(nextCron);
      setEnabled(nextEnabled);
      setInitialCron(nextCron);
      setInitialEnabled(nextEnabled);
    }
  }, [policyQuery.data]);

  const runsQuery = useQuery({
    queryKey: ["pocket-runs", projectId, modelId, pocket?.id],
    enabled: open && mode === "edit" && Boolean(pocket?.id),
    queryFn: () => pocketsApi.listRuns(projectId, modelId, pocket!.id),
  });

  const validateMutation = useMutation({
    mutationFn: () =>
      pocketsApi.validate(projectId, modelId, {
        defining_sql: definingSql,
        target_id: targetId || null,
      }),
    onSuccess: (data) => setValidateResult(data),
    onError: (err: unknown) => setValidateResult({ ok: false, stage: "parse", error: extractError(err, t) }),
  });

  const dryRunMutation = useMutation({
    mutationFn: () =>
      pocketsApi.dryRun(projectId, modelId, {
        defining_sql: definingSql,
        target_id: targetId || null,
      }),
    onSuccess: (data) => setDryRunResult(data),
    onError: (err: unknown) => setDryRunResult({ ok: false, error: extractError(err, t) }),
  });

  const saveMutation = useMutation({
    mutationFn: async () => {
      setSubmitError(null);
      setSubmitViolations(null);
      // F-005-21: an "event" pocket refreshes on source drift, not on a cron,
      // so its schedule policy is disabled.
      const isEvent = refreshPolicy === "event";
      if (mode === "create") {
        const payload: PocketCreate = {
          target_id: targetId,
          defining_sql: definingSql,
          refresh_policy: refreshPolicy,
          incremental_column: incrementalColumn || null,
          incremental_lookback_hours: incrementalColumn ? lookbackHours : null,
        };
        const created = await pocketsApi.create(projectId, modelId, payload);
        await pocketsApi.setPolicy(projectId, modelId, created.id, {
          cron_expression: isEvent ? null : (cron || null),
          is_enabled: isEvent ? false : enabled,
        });
        return created;
      }
      if (!pocket) throw new Error(t("pocket.noPocketError"));
      const sqlChanged = definingSql !== initialSql;
      const incChanged = incrementalColumn !== initialIncrementalColumn || lookbackHours !== initialLookbackHours;
      const policyChanged = refreshPolicy !== initialRefreshPolicy;
      if (sqlChanged || incChanged || policyChanged) {
        const patchBody: PocketUpdate = {};
        if (sqlChanged) patchBody.defining_sql = definingSql;
        if (policyChanged) patchBody.refresh_policy = refreshPolicy;
        if (incChanged) {
          patchBody.incremental_column = incrementalColumn || null;
          patchBody.incremental_lookback_hours = incrementalColumn ? lookbackHours : null;
        }
        await pocketsApi.update(projectId, modelId, pocket.id, patchBody);
      }
      await pocketsApi.setPolicy(projectId, modelId, pocket.id, {
        cron_expression: isEvent ? null : (cron || null),
        is_enabled: isEvent ? false : enabled,
      });
      return pocket;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pockets", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["pocket-policy", projectId, modelId] });
      onClose();
    },
    onError: (err: unknown) => {
      const { message, violations } = extractErrorAndViolations(err, t);
      setSubmitError(message);
      setSubmitViolations(violations);
    },
  });

  const refreshNowMutation = useMutation({
    mutationFn: () => pocketsApi.refresh(projectId, modelId, pocket!.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pockets", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["pocket-runs", projectId, modelId, pocket?.id] });
    },
  });

  const canSave = useMemo(() => {
    if (mode === "create") {
      return Boolean(targetId && definingSql.trim());
    }
    return Boolean(pocket);
  }, [mode, pocket, targetId, definingSql]);

  const isDirty = useMemo(() => {
    if (saveMutation.isPending) return false;
    return (
      targetId !== initialTargetId ||
      definingSql !== initialSql ||
      cron !== initialCron ||
      enabled !== initialEnabled ||
      incrementalColumn !== initialIncrementalColumn ||
      lookbackHours !== initialLookbackHours
    );
  }, [targetId, initialTargetId, definingSql, initialSql, cron, initialCron, enabled, initialEnabled, incrementalColumn, initialIncrementalColumn, lookbackHours, initialLookbackHours, saveMutation.isPending]);

  async function handleClose() {
    if (!isDirty) {
      onClose();
      return;
    }
    const ok = await confirm({
      title: t("pocketTables.drawer.discardTitle"),
      message: t("pocketTables.drawer.discardMessage"),
      confirmLabel: t("pocketTables.drawer.discardButton"),
      destructive: true,
    });
    if (ok) onClose();
  }

  const isFailed = mode === "edit" && Boolean(pocket?.failure_reason || pocket?.status === "failed");

  const title = mode === "create"
    ? t("pocketTables.drawer.newTitle")
    : t("pocketTables.drawer.editTitle", { table: pocket?.physical_table_name ?? "" });

  return (
    <Drawer anchor="right" open={open} onClose={handleClose} sx={{ zIndex: (t) => t.zIndex.drawer + 3 }} PaperProps={{ sx: { width: 720 } }}>
      <Box display="flex" alignItems="center" px={2} py={1} borderBottom={1} borderColor="divider">
        <Typography variant="h6" flexGrow={1}>
          {title}
        </Typography>
        {mode === "edit" && pocket && (
          <Tooltip title={t("pocketTables.drawer.refreshNow")}>
            <span>
              <IconButton
                onClick={() => refreshNowMutation.mutate()}
                disabled={refreshNowMutation.isPending}
              >
                {refreshNowMutation.isPending ? <CircularProgress size={18} /> : <RefreshIcon />}
              </IconButton>
            </span>
          </Tooltip>
        )}
        <IconButton onClick={handleClose}>
          <CloseIcon />
        </IconButton>
      </Box>

      <Tabs value={tab} onChange={(_, v) => setTab(v as TabKey)} sx={{ px: 2 }}>
        <Tab value="query" label={t("pocketTables.drawer.tabQuery")} />
        <Tab value="schedule" label={t("pocketTables.drawer.tabSchedule")} />
        <Tab value="advanced" label={t("pocketTables.drawer.tabAdvanced")} disabled={mode === "create"} />
      </Tabs>

      <Box flexGrow={1} overflow="auto" px={2} py={2}>
        {tab === "query" && (
          <Stack spacing={1.5}>
            <FormControl size="small" fullWidth>
              <InputLabel>{t("pocketTables.drawer.targetLabel")}</InputLabel>
              <Select
                value={targetId}
                label={t("pocketTables.drawer.targetLabel")}
                onChange={(e) => setTargetId(String(e.target.value))}
                disabled={mode === "edit"}
              >
                {(targets.data ?? []).map((t) => (
                  <MenuItem key={t.id} value={t.id}>
                    {t.display_name}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>

            {isFailed && pocket?.failure_reason && (
              <Alert severity="error" variant="outlined" sx={{ py: 0.5 }}>
                {t("pocketTables.drawer.failedValidation", { error: pocket.failure_reason })}
              </Alert>
            )}

            <Alert severity="info" variant="outlined" sx={{ py: 0.5 }}>
              {t("pocketTables.drawer.sqlHelp")}
            </Alert>

            <SqlQueryEditor
              sql={definingSql}
              onSqlChange={setDefiningSql}
              onValidate={() => validateMutation.mutate()}
              onDryRun={() => dryRunMutation.mutate()}
              validating={validateMutation.isPending}
              dryRunning={dryRunMutation.isPending}
              readOnly={mode === "edit" && !isFailed}
              placeholder={t("pocketTables.drawer.sqlPlaceholder")}
              minHeight={180}
            />

            {validateResult && (
              <Alert
                severity={
                  !validateResult.ok
                    ? "error"
                    : validateResult.warning
                      ? "info"
                      : "success"
                }
                onClose={() => setValidateResult(null)}
              >
                {validateResult.ok ? (
                  <>
                    {t("pocketTables.drawer.validatedAt", { stage: validateResult.stage ?? "" })}
                    {validateResult.columns && validateResult.columns.length > 0 && (
                      <> {t("pocketTables.drawer.columns", { cols: validateResult.columns.join(", ") })}</>
                    )}
                    {validateResult.warning && <> {validateResult.warning}</>}
                  </>
                ) : validateResult.violations && validateResult.violations.length > 0 ? (
                  <>
                    <Box mb={0.5}>
                      {t("pocketTables.drawer.failedAt", { stage: validateResult.stage ?? "" })}
                    </Box>
                    <ViolationList items={validateResult.violations} />
                  </>
                ) : (
                  <>{t("pocketTables.drawer.failedAtWithError", { stage: validateResult.stage ?? "", error: validateResult.error ?? "" })}</>
                )}
              </Alert>
            )}

            {dryRunResult && (
              <Alert
                severity={dryRunResult.ok ? "success" : "error"}
                onClose={() => setDryRunResult(null)}
              >
                {dryRunResult.ok ? (
                  <>
                    {t("pocketTables.drawer.dryRunSuccess", { rows: dryRunResult.row_count?.toLocaleString() ?? "0", ms: String(dryRunResult.elapsed_ms ?? 0) })}
                  </>
                ) : (
                  <>{t("pocketTables.drawer.dryRunFailed", { error: dryRunResult.error ?? "" })}</>
                )}
              </Alert>
            )}
          </Stack>
        )}

        {tab === "schedule" && (
          <Stack spacing={1.5} data-testid="pocket-schedule-tab">
            {/* F-005-21: refresh trigger — scheduled cron vs source-drift event. */}
            <TextField
              select
              label={t("pocketTables.drawer.refreshTrigger")}
              size="small"
              fullWidth
              value={refreshPolicy}
              onChange={(e) => setRefreshPolicy(e.target.value === "event" ? "event" : "schedule")}
              helperText={
                refreshPolicy === "event"
                  ? t("pocketTables.drawer.refreshTriggerEventHelp")
                  : t("pocketTables.drawer.refreshTriggerScheduleHelp")
              }
            >
              <MenuItem value="schedule">{t("pocketTables.drawer.refreshTriggerSchedule")}</MenuItem>
              <MenuItem value="event">{t("pocketTables.drawer.refreshTriggerEvent")}</MenuItem>
            </TextField>
            <FormControlLabel
              control={
                <Switch
                  checked={enabled}
                  onChange={(_, v) => setEnabled(v)}
                  disabled={refreshPolicy === "event"}
                />
              }
              label={t("pocketTables.drawer.enableScheduled")}
            />
            <FrequencyPicker
              value={cronToPreset(cron)}
              onChange={(preset) => setCron(presetToCron(preset) ?? "")}
              disabled={refreshPolicy === "event" || !enabled}
            />

            <Typography variant="subtitle2" fontWeight={700} mt={2}>
              {t("pocketTables.drawer.rebuildMethod")}
            </Typography>
            <Typography variant="body2" color="text.secondary" mb={0.5}>
              {t("pocketTables.drawer.rebuildMethodHelp")}
            </Typography>
            <TextField
              label={t("pocketTables.drawer.incrementalColumn")}
              size="small"
              fullWidth
              value={incrementalColumn}
              onChange={(e) => setIncrementalColumn(e.target.value.trim())}
              placeholder={t("pocketTables.drawer.columnPlaceholder")}
              helperText={t("pocketTables.drawer.columnHelper")}
            />
            <TextField
              label={t("pocketTables.drawer.lookbackHours")}
              type="number"
              size="small"
              fullWidth
              value={lookbackHours}
              onChange={(e) => setLookbackHours(Math.max(1, Number(e.target.value) || 24))}
              disabled={!incrementalColumn}
              inputProps={{ min: 1 }}
            />
            <Alert severity="info" variant="outlined" sx={{ py: 0.5 }}>
              {incrementalColumn
                ? t("pocketTables.drawer.incrementalNote", { hours: String(lookbackHours), column: incrementalColumn })
                : t("pocketTables.drawer.fullRebuildNote")}
            </Alert>

            <Typography variant="subtitle2" fontWeight={700} mt={2}>
              {t("pocketTables.drawer.recentRebuilds")}
            </Typography>
            {runsQuery.isLoading ? (
              <CircularProgress size={18} />
            ) : (
              <RefreshRunHistory runs={runsQuery.data ?? []} limit={10} />
            )}
          </Stack>
        )}

        {tab === "advanced" && pocket && (
          <Stack spacing={1}>
            <KV label={t("pocketTables.drawer.kvStatus")} value={pocket.status} />
            <KV label={t("pocketTables.drawer.kvPhysicalTable")} value={pocket.physical_table_name} />
            <KV label={t("pocketTables.drawer.kvCreated")} value={new Date(pocket.created_at).toLocaleString()} />
            <KV
              label={t("pocketTables.drawer.kvLastRefresh")}
              value={pocket.last_refresh_at ? new Date(pocket.last_refresh_at).toLocaleString() : t("pocketTables.drawer.never")}
            />
            <KV label={t("pocketTables.drawer.kvRowCount")} value={pocket.row_count?.toLocaleString() ?? t("common.na")} />
            <KV label={t("pocketTables.drawer.kvStorage")} value={pocket.storage_bytes?.toLocaleString() ?? t("common.na")} />
            <KV label={t("pocketTables.drawer.kvHitCount")} value={String(pocket.hit_count)} />
          </Stack>
        )}
      </Box>

      {submitError && (
        <Alert severity="error" sx={{ mx: 2, mb: 1 }}>
          <Box mb={submitViolations && submitViolations.length > 0 ? 0.5 : 0}>
            {submitError}
          </Box>
          {submitViolations && submitViolations.length > 0 && (
            <ViolationList items={submitViolations} />
          )}
        </Alert>
      )}

      <Box display="flex" justifyContent="flex-end" gap={1} p={2} borderTop={1} borderColor="divider">
        <Button onClick={handleClose}>{t("common.cancel")}</Button>
        <Button
          variant="contained"
          onClick={() => saveMutation.mutate()}
          disabled={!canSave || saveMutation.isPending}
        >
          {saveMutation.isPending ? <CircularProgress size={16} /> : t("common.save")}
        </Button>
      </Box>
    </Drawer>
  );
}

function ViolationList({ items }: { items: PocketViolationItem[] }) {
  return (
    <Box component="ul" sx={{ m: 0, pl: 2.5 }}>
      {items.map((v, i) => (
        <li key={`${v.code}-${i}`}>
          <Typography variant="body2" component="span">
            <strong>{v.code}:</strong> {v.message}
          </Typography>
          {v.suggestion && (
            <Typography variant="caption" component="div" color="text.secondary">
              {v.suggestion}
            </Typography>
          )}
        </li>
      ))}
    </Box>
  );
}

function KV({ label, value }: { label: string; value: string }) {
  return (
    <Box display="flex" gap={2}>
      <Typography variant="body2" color="text.secondary" sx={{ width: 160 }}>
        {label}
      </Typography>
      <Typography variant="body2">{value}</Typography>
    </Box>
  );
}

function extractError(err: unknown, tFn: (key: string) => string): string {
  return extractErrorAndViolations(err, tFn).message;
}

function extractErrorAndViolations(err: unknown, tFn: (key: string) => string): {
  message: string;
  violations: PocketViolationItem[] | null;
} {
  const detail = (
    err as { response?: { data?: { detail?: unknown } } }
  )?.response?.data?.detail;
  if (detail && typeof detail === "object") {
    const obj = detail as { message?: string; violations?: PocketViolationItem[] };
    if (Array.isArray(obj.violations)) {
      return {
        message: obj.message || tFn("pocketTables.drawer.notValidSubset"),
        violations: obj.violations,
      };
    }
  }
  if (typeof detail === "string") return { message: detail, violations: null };
  if (err instanceof Error) return { message: err.message, violations: null };
  return { message: tFn("errors.requestFailed"), violations: null };
}

