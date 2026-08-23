import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import RefreshIcon from "@mui/icons-material/Refresh";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import VerifiedIcon from "@mui/icons-material/Verified";
import BlockIcon from "@mui/icons-material/Block";

import { namedQueriesApi } from "../../api/client";
import {
  useNamedQueries,
  useNamedQueryAnalytics,
  useNamedQueryCaps,
} from "../../api/hooks";
import { useT } from "../../i18n";
import type {
  NamedQuery,
  NamedQueryCreate,
  NamedQueryValidateResponse,
} from "../../api/types";
import { extractApiError } from "../../utils/extractApiError";
import {
  namedQueryHealth,
  namedQueryHealthColor,
} from "../../utils/namedQueryHealth";
import { recordCreate, recordUpdate } from "../Builder/emitDrawerHistory";

/** Bug-7960 mirror: reference name pattern — must match backend
 *  _REFERENCE_NAME_RE semantics (letters/underscore then letters/digits/_). */
const REFERENCE_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Shared query-key prefix so list query and refresh invalidation agree
 *  (Bug-7963 mirror). */
export const NAMED_QUERIES_QUERY_KEY_PREFIX = "namedQueries";

interface NqFormState {
  name: string;
  display_name: string;
  description: string;
  display_folder: string;
  definition_sql: string;
  row_cap: string;
  column_cap: string;
  // F-026-01 / F-101-07: refresh schedule (cron). The persisted policy row is
  // a separate snapshot-owned resource; the form mirrors it here.
  refresh_enabled: boolean;
  refresh_cron: string;
}

function toFormState(nq: NamedQuery | null): NqFormState {
  return {
    name: nq?.name ?? "",
    display_name: nq?.display_name ?? "",
    description: nq?.description ?? "",
    display_folder: nq?.display_folder ?? "",
    definition_sql: nq?.definition_sql ?? "",
    row_cap: nq?.row_cap != null ? String(nq.row_cap) : "",
    column_cap: nq?.column_cap != null ? String(nq.column_cap) : "",
    refresh_enabled: nq?.refresh_policy?.is_enabled ?? false,
    refresh_cron: nq?.refresh_policy?.cron_expression ?? "",
  };
}

/** Persisted NQ -> create/update-shaped payload for undo/redo restore
 *  (Bug-8227 mirror). Exported for the hosting panel's delete-undo. */
export function namedQueryToPayload(nq: NamedQuery): Record<string, unknown> {
  return {
    name: nq.name,
    display_name: nq.display_name ?? null,
    description: nq.description ?? null,
    display_folder: nq.display_folder ?? null,
    definition_sql: nq.definition_sql,
    row_cap: nq.row_cap ?? null,
    column_cap: nq.column_cap ?? null,
    // F-026-01: carry the schedule so an undo of a delete/create restores it.
    refresh_policy: nq.refresh_policy?.is_enabled ? "schedule" : "manual",
    refresh_cron: nq.refresh_policy?.cron_expression ?? null,
    refresh_policy_enabled: nq.refresh_policy?.is_enabled ?? null,
  };
}

/** The create/validate 400 detail is an object {message, errors}; surface both. */
function nqErrorMessage(err: unknown, fallback: string): string {
  const data = (err as { response?: { data?: unknown } })?.response?.data;
  if (data && typeof data === "object" && "detail" in data) {
    const detail = (data as { detail: unknown }).detail;
    if (typeof detail === "string" && detail) return detail;
    if (detail && typeof detail === "object") {
      const d = detail as { message?: string; errors?: string[] };
      const errors =
        Array.isArray(d.errors) && d.errors.length > 0 ? d.errors.join("; ") : null;
      const parts = [d.message, errors].filter(Boolean) as string[];
      if (parts.length > 0) return parts.join(" ");
    }
  }
  return extractApiError(err, fallback);
}

function parseCapField(raw: string): number | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const num = Number(trimmed);
  return Number.isFinite(num) && num >= 1 ? Math.trunc(num) : null;
}

function formatAnalyticsMilliseconds(value: number): string {
  return new Intl.NumberFormat(undefined, {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }).format(value);
}

function formatAnalyticsBytes(value: number): string {
  return new Intl.NumberFormat(undefined, {
    maximumFractionDigits: 1,
  }).format(value);
}

interface NamedQueryEditorProps {
  open: boolean;
  mode: "create" | "edit";
  /** The row being edited (null in create mode). */
  initial: NamedQuery | null;
  projectId: string;
  modelId: string;
  /** Modeller-tier gate: false renders the editor read-only. */
  canEdit: boolean;
  needsSaveOrDeploy: boolean;
  onClose: () => void;
}

/**
 * Named Query editor — the strategy §12 surface for authoring a model-bound
 * SQL definition, validating it (output columns + shape), refreshing the
 * materialised result, and reading its health (fresh / stale / failed).
 *
 * Lives as its own dialog component so the Named Sets panel stays the
 * kind-selector host while this editor keeps its own form lifecycle.
 */
export default function NamedQueryEditor({
  open,
  mode,
  initial,
  projectId,
  modelId,
  canEdit,
  needsSaveOrDeploy,
  onClose,
}: NamedQueryEditorProps) {
  const t = useT();
  const qc = useQueryClient();
  const isEdit = mode === "edit" && initial !== null;

  const [form, setForm] = useState<NqFormState>(() => toFormState(initial));
  const [error, setError] = useState<string | null>(null);
  const [validation, setValidation] = useState<NamedQueryValidateResponse | null>(null);
  const [validating, setValidating] = useState(false);
  const [refreshPendingRunId, setRefreshPendingRunId] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const editSnapshotRef = useRef<string>("");

  // Re-sync the form whenever the dialog opens on a different target row.
  useEffect(() => {
    if (!open) return;
    const next = toFormState(initial);
    setForm(next);
    setError(null);
    setValidation(null);
    setValidating(false);
    setRefreshPendingRunId(null);
    setRefreshError(null);
    editSnapshotRef.current = JSON.stringify(next);
  }, [open, initial]);

  // Live server row: after a refresh the list refetches, and the health badge
  // here must follow the artifact lifecycle instead of a stale open-time copy.
  const { data: namedQueries } = useNamedQueries(projectId, modelId);
  const serverNq = useMemo(() => {
    if (!initial) return null;
    return namedQueries.find((nq) => nq.id === initial.id) ?? initial;
  }, [namedQueries, initial]);

  const caps = useNamedQueryCaps(projectId);

  const health = useMemo(
    () => (serverNq ? namedQueryHealth(serverNq) : null),
    [serverNq],
  );
  const analytics = useNamedQueryAnalytics(
    projectId,
    modelId,
    isEdit ? serverNq?.id : null,
  );

  const effectiveRowCap = serverNq?.row_cap ?? caps.maxRows;
  const effectiveColumnCap = serverNq?.column_cap ?? caps.maxColumns;

  const queryKey = [NAMED_QUERIES_QUERY_KEY_PREFIX, projectId, modelId];

  const invalidateList = useCallback(() => {
    qc.invalidateQueries({ queryKey });
  }, [qc, queryKey]);

  // Refresh-run polling: while the run we just queued is still
  // queued/running, poll the run list; on completion invalidate the list so
  // the health badge and last-refreshed caption settle.
  const { data: runs } = useQuery({
    queryKey: [NAMED_QUERIES_QUERY_KEY_PREFIX, projectId, modelId, "runs", initial?.id],
    queryFn: () => namedQueriesApi.listRuns(projectId, modelId, initial!.id),
    enabled: Boolean(projectId && modelId && initial && refreshPendingRunId),
    refetchInterval: (query) => {
      const latest = query.state.data?.[0];
      if (
        latest &&
        latest.id === refreshPendingRunId &&
        (latest.status === "queued" || latest.status === "running")
      ) {
        return 2000;
      }
      return false;
    },
  });

  useEffect(() => {
    const latest = runs?.[0];
    if (latest && latest.id === refreshPendingRunId) {
      if (latest.status === "completed" || latest.status === "failed") {
        setRefreshPendingRunId(null);
        invalidateList();
      }
    }
  }, [runs, refreshPendingRunId, invalidateList]);

  // Bug-7942 mirror: dirty detection disables Refresh (refresh runs the SAVED
  // definition, never unsaved edits).
  const formDirty = useMemo(() => {
    if (!isEdit) return false;
    return JSON.stringify(form) !== editSnapshotRef.current;
  }, [form, isEdit]);

  const createMut = useMutation({
    mutationFn: async (data: NamedQueryCreate) => {
      const created = await namedQueriesApi.create(projectId, modelId, data);
      return { created, data };
    },
    onSuccess: ({ created, data }) => {
      // Bug-8227 mirror: undo removes the created row; redo re-creates it.
      recordCreate("namedQuery", created.id, data as unknown as Record<string, unknown>);
      invalidateList();
      onClose();
    },
    onError: (err: unknown) =>
      setError(nqErrorMessage(err, t("namedQueries.errorCreate"))),
  });

  const updateMut = useMutation({
    mutationFn: async ({ id, data }: { id: string; data: Record<string, unknown> }) => {
      const prior = initial ? namedQueryToPayload(initial) : null;
      await namedQueriesApi.update(projectId, modelId, id, data);
      // F-026-01: PATCH on the Named Query ignores schedule fields, so persist
      // the cron through the dedicated policy resource — but only when it
      // actually changed, so an unscheduled NQ never gains a spurious row.
      const desiredEnabled = form.refresh_enabled;
      const desiredCron = form.refresh_cron.trim() || null;
      const priorEnabled = initial?.refresh_policy?.is_enabled ?? false;
      const priorCron = initial?.refresh_policy?.cron_expression ?? null;
      if (desiredEnabled !== priorEnabled || desiredCron !== priorCron) {
        await namedQueriesApi.putPolicy(projectId, modelId, id, {
          cron_expression: desiredCron,
          is_enabled: desiredEnabled,
        });
      }
      return { id, data, prior };
    },
    onSuccess: ({ id, data, prior }) => {
      if (prior) recordUpdate("namedQuery", id, prior, data);
      invalidateList();
      onClose();
    },
    onError: (err: unknown) =>
      setError(nqErrorMessage(err, t("namedQueries.errorUpdate"))),
  });

  const refreshMut = useMutation({
    mutationFn: (id: string) => namedQueriesApi.refresh(projectId, modelId, id),
    onSuccess: (run) => {
      setRefreshError(null);
      setRefreshPendingRunId(run.id);
      invalidateList();
    },
    onError: (err: unknown) =>
      setRefreshError(nqErrorMessage(err, t("namedQueries.errorRefresh"))),
  });

  const handleValidate = useCallback(async () => {
    setValidating(true);
    setValidation(null);
    try {
      const result = await namedQueriesApi.validate(projectId, modelId, {
        definition_sql: form.definition_sql,
      });
      setValidation(result);
    } catch (err: unknown) {
      setValidation(null);
      setError(nqErrorMessage(err, t("namedQueries.errorValidate")));
    } finally {
      setValidating(false);
    }
  }, [projectId, modelId, form.definition_sql, t]);

  function buildPayload(): Record<string, unknown> {
    const payload: Record<string, unknown> = {
      definition_sql: form.definition_sql,
      display_name: form.display_name || null,
      description: form.description || null,
      display_folder: form.display_folder || null,
    };
    const rowCap = parseCapField(form.row_cap);
    const colCap = parseCapField(form.column_cap);
    if (mode === "create") {
      payload.name = form.name;
      payload.row_cap = rowCap;
      payload.column_cap = colCap;
      // F-026-01: send the refresh schedule inline on create. The backend
      // inserts the NamedQueryRefreshPolicy row only when policy == "schedule".
      if (form.refresh_enabled && form.refresh_cron.trim()) {
        payload.refresh_policy = "schedule";
        payload.refresh_cron = form.refresh_cron.trim();
        payload.refresh_policy_enabled = true;
      }
    } else {
      if (rowCap !== null) payload.row_cap = rowCap;
      if (colCap !== null) payload.column_cap = colCap;
    }
    return payload;
  }

  function handleSave() {
    if (mode === "create") {
      createMut.mutate(buildPayload() as unknown as NamedQueryCreate);
    } else if (initial) {
      updateMut.mutate({ id: initial.id, data: buildPayload() });
    }
  }

  const nameValid = form.name.trim().length > 0;
  const nameFormatOk =
    !isEdit && form.name.length > 0 ? REFERENCE_NAME_RE.test(form.name) : true;
  const sqlValid = form.definition_sql.trim().length > 0;
  // F-026-01: a scheduled refresh must carry a cron; enabling with a blank cron
  // is not a valid policy (backend rejects it on create; PUT would store a
  // useless enabled-but-cronless row).
  const scheduleValid = !form.refresh_enabled || form.refresh_cron.trim().length > 0;
  const isPending = createMut.isPending || updateMut.isPending;
  const saveDisabled =
    !canEdit || isPending || !nameValid || !nameFormatOk || !sqlValid || !scheduleValid;

  const usedColumns = validation?.output_columns?.length ?? 0;

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ pb: 0.5 }}>
        <Box display="flex" alignItems="center" gap={1}>
          <span>
            {mode === "create" ? t("namedQueries.add") : t("namedQueries.editNamedQuery")}
          </span>
          <Chip
            label={t("namedQueries.kindChip")}
            size="small"
            variant="outlined"
            color="info"
          />
        </Box>
      </DialogTitle>
      <DialogContent dividers sx={{ minHeight: 340 }}>
        {error && (
          <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
            {error}
          </Alert>
        )}

        <TextField
          label={t("namedQueries.name")}
          fullWidth
          margin="normal"
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          disabled={!canEdit || isEdit}
          placeholder={t("namedQueries.namePlaceholder")}
          error={form.name.length > 0 && (!nameValid || !nameFormatOk)}
          helperText={
            form.name.length > 0 && !nameFormatOk
              ? t("namedQueries.refNameInvalid")
              : form.name.length > 0 && nameFormatOk && !isEdit
                ? t("namedQueries.refNameUsage", { name: form.name })
                : undefined
          }
        />
        <TextField
          label={t("namedQueries.displayName")}
          fullWidth
          margin="normal"
          value={form.display_name}
          onChange={(e) => setForm({ ...form, display_name: e.target.value })}
          disabled={!canEdit}
          placeholder={t("namedQueries.displayNamePlaceholder")}
        />
        <TextField
          label={t("namedQueries.descriptionLabel")}
          fullWidth
          margin="normal"
          multiline
          rows={2}
          value={form.description}
          onChange={(e) => setForm({ ...form, description: e.target.value })}
          disabled={!canEdit}
        />
        <TextField
          label={t("namedQueries.displayFolder")}
          fullWidth
          margin="normal"
          value={form.display_folder}
          onChange={(e) => setForm({ ...form, display_folder: e.target.value })}
          disabled={!canEdit}
          placeholder={t("namedQueries.displayFolderPlaceholder")}
        />
        <TextField
          label={t("namedQueries.definitionSql")}
          fullWidth
          margin="normal"
          multiline
          rows={5}
          value={form.definition_sql}
          onChange={(e) => setForm({ ...form, definition_sql: e.target.value })}
          disabled={!canEdit}
          placeholder={t("namedQueries.definitionSqlPlaceholder")}
          helperText={t("namedQueries.definitionSqlHelp")}
          inputProps={{
            style: { fontFamily: "monospace" },
            "data-testid": "nq-definition-sql",
          }}
        />

        {/* Caps overrides + effective caps count */}
        <Stack direction="row" spacing={2} mt={1}>
          <TextField
            label={t("namedQueries.rowCapField")}
            size="small"
            type="number"
            value={form.row_cap}
            onChange={(e) => setForm({ ...form, row_cap: e.target.value })}
            disabled={!canEdit}
            helperText={t("namedQueries.rowCapHelp")}
            inputProps={{ min: 1, "data-testid": "nq-row-cap" }}
          />
          <TextField
            label={t("namedQueries.columnCapField")}
            size="small"
            type="number"
            value={form.column_cap}
            onChange={(e) => setForm({ ...form, column_cap: e.target.value })}
            disabled={!canEdit}
            helperText={t("namedQueries.columnCapHelp")}
            inputProps={{ min: 1, "data-testid": "nq-column-cap" }}
          />
        </Stack>
        <Typography
          variant="caption"
          color="text.secondary"
          display="block"
          mt={1}
          data-testid="nq-caps-count"
        >
          {effectiveColumnCap !== null
            ? t("namedQueries.capsCount", {
                used: String(usedColumns),
                cap: String(effectiveColumnCap),
              })
            : t("namedQueries.capsCountNoCap", { used: String(usedColumns) })}
          {effectiveRowCap !== null
            ? " · " + t("namedQueries.rowCapCount", { cap: String(effectiveRowCap) })
            : " · " + t("namedQueries.rowCapSystemDefault")}
        </Typography>

        {/* F-026-01 / F-101-07: refresh schedule (cron). Materialised Named
            Queries auto-refresh only when a schedule is attached here. */}
        <Box mt={2} data-testid="nq-schedule-area">
          <FormControlLabel
            control={
              <Switch
                checked={form.refresh_enabled}
                onChange={(e) =>
                  setForm({ ...form, refresh_enabled: e.target.checked })
                }
                disabled={!canEdit}
                data-testid="nq-schedule-enabled"
              />
            }
            label={t("namedQueries.scheduleEnable")}
          />
          <TextField
            label={t("namedQueries.scheduleCron")}
            fullWidth
            margin="dense"
            size="small"
            value={form.refresh_cron}
            onChange={(e) => setForm({ ...form, refresh_cron: e.target.value })}
            disabled={!canEdit || !form.refresh_enabled}
            placeholder={t("namedQueries.scheduleCronPlaceholder")}
            error={form.refresh_enabled && !form.refresh_cron.trim()}
            helperText={
              form.refresh_enabled && !form.refresh_cron.trim()
                ? t("namedQueries.scheduleCronRequired")
                : t("namedQueries.scheduleCronHelp")
            }
            inputProps={{
              style: { fontFamily: "monospace" },
              "data-testid": "nq-schedule-cron",
            }}
          />
        </Box>

        {/* Validate step — shows output columns + shape */}
        <Box mt={2} data-testid="nq-validation-area">
          <Box display="flex" alignItems="center" gap={1}>
            <Button
              variant="outlined"
              size="small"
              startIcon={
                validating ? <CircularProgress size={14} /> : <VerifiedIcon />
              }
              onClick={handleValidate}
              disabled={!canEdit || validating || !sqlValid}
              data-testid="nq-validate-btn"
            >
              {t("namedQueries.validate")}
            </Button>
            {validation?.is_valid && (
              <Typography variant="body2" color="success.main" data-testid="nq-validation-ok">
                {t("namedQueries.validationValid", {
                  shape: validation.shape ?? "projection",
                  count: String(usedColumns),
                })}
              </Typography>
            )}
          </Box>

          {validation && !validation.is_valid && (
            <Alert severity="error" sx={{ mt: 1 }} data-testid="nq-validation-invalid">
              {t("namedQueries.validationInvalid")}
              {validation.errors.length > 0 && (
                <Box component="ul" sx={{ m: 0, pl: 2, mt: 0.5 }}>
                  {validation.errors.map((err, idx) => (
                    <li key={idx}>{err}</li>
                  ))}
                </Box>
              )}
            </Alert>
          )}

          {validation?.is_valid && validation.output_columns.length > 0 && (
            <Box mt={1}>
              <Typography variant="subtitle2">
                {t("namedQueries.outputColumns")}
              </Typography>
              <Table size="small" data-testid="nq-output-columns">
                <TableHead>
                  <TableRow>
                    <TableCell>{t("namedQueries.columnName")}</TableCell>
                    <TableCell>{t("namedQueries.columnType")}</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {validation.output_columns.map((col, idx) => (
                    <TableRow key={`${col.name}-${idx}`}>
                      <TableCell>
                        <Typography variant="caption" fontFamily="monospace">
                          {col.name}
                        </Typography>
                      </TableCell>
                      <TableCell>{col.type}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              {validation.shape && (
                <Typography variant="caption" color="text.secondary" display="block" mt={0.5}>
                  {t("namedQueries.shape", { shape: validation.shape })}
                </Typography>
              )}
            </Box>
          )}
        </Box>

        {/* Refresh + health — edit mode only (an unsaved NQ has no row) */}
        {isEdit && serverNq && health && (
          <Box mt={2} data-testid="nq-health-area">
            <Box display="flex" alignItems="center" gap={1} flexWrap="wrap">
              <Tooltip
                title={formDirty ? t("namedQueries.refreshDirtyTooltip") : ""}
                placement="top"
              >
                <span>
                  <Button
                    variant="contained"
                    size="small"
                    startIcon={
                      refreshMut.isPending || refreshPendingRunId ? (
                        <CircularProgress size={14} />
                      ) : (
                        <RefreshIcon />
                      )
                    }
                    onClick={() => refreshMut.mutate(serverNq.id)}
                    disabled={!canEdit || formDirty || refreshMut.isPending || !!refreshPendingRunId}
                    data-testid="nq-refresh-btn"
                  >
                    {t("namedQueries.refresh")}
                  </Button>
                </span>
              </Tooltip>
              <Chip
                label={t(
                  health.status === "fresh"
                    ? "namedQueries.healthFresh"
                    : health.status === "failed"
                      ? "namedQueries.healthFailed"
                      : "namedQueries.healthStale",
                )}
                size="small"
                color={namedQueryHealthColor(health.status)}
                data-testid="nq-health-badge"
              />
              {needsSaveOrDeploy && (
                <Tooltip title={t("namedQueries.pendingDeploy")}>
                  <span
                    tabIndex={0}
                    role="img"
                    aria-label={t("namedQueries.pendingDeploy")}
                    style={{ display: "inline-flex" }}
                    data-testid="nq-pending-deploy"
                  >
                    <WarningAmberIcon fontSize="small" color="warning" />
                  </span>
                </Tooltip>
              )}
              <Typography variant="caption" color="text.secondary" data-testid="nq-last-refreshed">
                {serverNq.artifact?.last_refresh_at
                  ? t("namedQueries.lastRefreshed", {
                      time: new Date(serverNq.artifact.last_refresh_at).toLocaleString(),
                    })
                  : t("namedQueries.neverRefreshed")}
              </Typography>
              {serverNq.artifact?.row_count != null && (
                <Typography variant="caption" color="text.secondary" data-testid="nq-row-count">
                  {t("namedQueries.rowCount", { count: String(serverNq.artifact.row_count) })}
                </Typography>
              )}
            </Box>
            {health.status !== "fresh" && (
              <Alert
                severity={health.status === "failed" ? "error" : "warning"}
                icon={<BlockIcon fontSize="small" />}
                sx={{ mt: 1, py: 0 }}
                data-testid="nq-health-reason"
              >
                {health.detail ?? (health.reasonKey ? t(health.reasonKey) : "")}
              </Alert>
            )}
            {refreshPendingRunId && (
              <Alert severity="info" sx={{ mt: 1 }} data-testid="nq-refresh-queued">
                {t("namedQueries.refreshQueued")}
              </Alert>
            )}
            {refreshError && (
              <Alert
                severity="error"
                sx={{ mt: 1 }}
                onClose={() => setRefreshError(null)}
                data-testid="nq-refresh-error"
              >
                {refreshError}
              </Alert>
            )}
            {analytics.isError && (
              <Alert severity="warning" sx={{ mt: 1 }} data-testid="nq-analytics-error">
                {t("namedQueries.analyticsUnavailable")}
              </Alert>
            )}
            {analytics.data && (
              <Box mt={1} data-testid="nq-analytics">
                <Typography variant="subtitle2">
                  {t("namedQueries.analyticsTitle")}
                </Typography>
                <Typography variant="caption" color="text.secondary" display="block">
                  {t("namedQueries.analyticsSummary", {
                    fallback: String(analytics.data.fallback_queries),
                    total: String(analytics.data.total_queries),
                    rate: analytics.data.fallback_rate.toFixed(1),
                  })}
                </Typography>
                {analytics.data.avg_fallback_execution_ms != null && (
                  <Typography variant="caption" color="text.secondary" display="block">
                    {t("namedQueries.analyticsAvgExecution", {
                      value: formatAnalyticsMilliseconds(
                        analytics.data.avg_fallback_execution_ms,
                      ),
                    })}
                  </Typography>
                )}
                {analytics.data.avg_fallback_bytes_processed != null && (
                  <Typography variant="caption" color="text.secondary" display="block">
                    {t("namedQueries.analyticsAvgBytes", {
                      value: formatAnalyticsBytes(
                        analytics.data.avg_fallback_bytes_processed,
                      ),
                    })}
                  </Typography>
                )}
                {analytics.data.fallback_reasons.length > 0 && (
                  <Typography variant="caption" color="text.secondary" display="block">
                    {t("namedQueries.analyticsReasons", {
                      reasons: analytics.data.fallback_reasons
                        .map((entry) => `${entry.reason} (${entry.count})`)
                        .join(", "),
                    })}
                  </Typography>
                )}
                {analytics.data.recommendation === "repair_named_query_materialisation" && (
                  <Alert severity="warning" sx={{ mt: 1, py: 0 }} data-testid="nq-analytics-recommendation">
                    {t("namedQueries.analyticsRecommendation")}
                  </Alert>
                )}
              </Box>
            )}
          </Box>
        )}

        {mode === "create" && (
          <Alert severity="info" sx={{ mt: 2 }} data-testid="nq-save-to-refresh">
            {t("namedQueries.saveToRefresh")}
          </Alert>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t("common.cancel")}</Button>
        <Button
          variant="contained"
          disabled={saveDisabled}
          onClick={handleSave}
          data-testid="nq-save-btn"
        >
          {isPending ? (
            <CircularProgress size={16} />
          ) : mode === "create" ? (
            t("namedQueries.create")
          ) : (
            t("namedQueries.update")
          )}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
