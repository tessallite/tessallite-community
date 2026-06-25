import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import PreviewIcon from "@mui/icons-material/Preview";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import { solidatusApi } from "../../api/client";
import { useT } from "../../i18n";
import type {
  SolidatusConnection,
  SolidatusExportPreviewResponse,
  SolidatusSyncRun,
} from "../../api/types";

type T = ReturnType<typeof useT>;
type Notice = { severity: "info" | "warning" | "error" | "success"; message: string } | null;

function statusLabel(t: T, status: string) {
  switch (status) {
    case "succeeded":
      return t("solidatus.statusSucceeded");
    case "failed":
      return t("solidatus.statusFailed");
    case "running":
      return t("solidatus.statusRunning");
    case "pending":
      return t("solidatus.statusPending");
    default:
      return t("solidatus.statusUnknown");
  }
}

function modeLabel(t: T, mode: string) {
  switch (mode) {
    case "dry_run":
      return t("solidatus.modeDryRun");
    case "push":
      return t("solidatus.modePush");
    default:
      return t("solidatus.modeUnknown");
  }
}

function StatusChip({ status, t }: { status: string; t: T }) {
  const color = status === "succeeded" ? "success" : status === "failed" ? "error" : "warning";
  return (
    <Chip
      size="small"
      label={statusLabel(t, status)}
      color={color as "success" | "error" | "warning"}
    />
  );
}

function warningMessages(preview: SolidatusExportPreviewResponse) {
  return preview.warnings
    .map((warning) => (typeof warning.message === "string" ? warning.message : null))
    .filter((message): message is string => Boolean(message));
}

function requestErrorMessage(t: T, fallbackKey: string) {
  return t(fallbackKey);
}

function syncErrorMessage(t: T, message: string | null | undefined) {
  if (!message) return t("solidatus.noErrorDetails");
  if (message.toLowerCase().includes("not implemented")) {
    return t("solidatus.syncFailureFallback");
  }
  return t("solidatus.syncFailureFallback");
}

export function SolidatusIntegrationPanel() {
  const t = useT();
  const { projectId, modelId } = useParams<{ projectId: string; modelId: string }>();

  const [configs, setConfigs] = useState<SolidatusConnection[]>([]);
  const [runs, setRuns] = useState<SolidatusSyncRun[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [preview, setPreview] = useState<SolidatusExportPreviewResponse | null>(null);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [formName, setFormName] = useState("");
  const [formUrl, setFormUrl] = useState("");
  const [formToken, setFormToken] = useState("");
  const [formWorkspace, setFormWorkspace] = useState("");
  const [formModelRef, setFormModelRef] = useState("");

  const pid = projectId ?? "";
  const mid = modelId ?? "";

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const [c, r] = await Promise.all([
        solidatusApi.listConfigs(pid, mid),
        solidatusApi.listRuns(pid, mid),
      ]);
      setConfigs(c);
      setRuns(r);
    } catch {
      setError(requestErrorMessage(t, "solidatus.loadFailed"));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { refresh(); }, [pid, mid]);

  function openCreate() {
    setEditingId(null);
    setFormName("");
    setFormUrl("");
    setFormToken("");
    setFormWorkspace("");
    setFormModelRef("");
    setDialogOpen(true);
  }

  function openEdit(c: SolidatusConnection) {
    setEditingId(c.id);
    setFormName(c.display_name);
    setFormUrl(c.base_url);
    setFormToken("");
    setFormWorkspace(c.workspace_id ?? "");
    setFormModelRef(c.model_ref ?? "");
    setDialogOpen(true);
  }

  async function saveConfig() {
    try {
      if (editingId) {
        const payload: Record<string, unknown> = {
          display_name: formName,
          base_url: formUrl,
        };
        if (formToken) payload.token = formToken;
        payload.workspace_id = formWorkspace || null;
        payload.model_ref = formModelRef || null;
        await solidatusApi.updateConfig(pid, mid, editingId, payload);
      } else {
        await solidatusApi.createConfig(pid, mid, {
          display_name: formName,
          base_url: formUrl,
          token: formToken,
          workspace_id: formWorkspace || null,
          model_ref: formModelRef || null,
        });
      }
      setDialogOpen(false);
      await refresh();
    } catch {
      setError(requestErrorMessage(t, "solidatus.saveFailed"));
    }
  }

  async function deleteConfig(id: string) {
    try {
      await solidatusApi.deleteConfig(pid, mid, id);
      await refresh();
    } catch {
      setError(requestErrorMessage(t, "solidatus.deleteFailed"));
    }
  }

  async function runValidate(connection: SolidatusConnection) {
    setLoading(true);
    setNotice(null);
    try {
      const result = await solidatusApi.validate(pid, mid, {
        connection_id: connection.id,
      });
      setError(null);
      setPreview({
        nodes_total: 0,
        edges_total: 0,
        by_type: {},
        warnings: result.warnings.map((w) => ({ message: w })),
      });
      // ok is tri-state: null => simulated (connector not contacted),
      // false => verified failure, true => verified pass. A simulated
      // result must never read as a green success.
      if (result.simulated || result.ok === null) {
        setNotice({
          severity: "info",
          message: t("solidatus.validationSimulated", { connection: connection.display_name }),
        });
      } else if (result.ok === false) {
        setNotice({
          severity: "warning",
          message: t("solidatus.validationReportedIssue", { connection: connection.display_name }),
        });
      } else {
        setNotice({
          severity: "success",
          message: t("solidatus.validationVerified", { connection: connection.display_name }),
        });
      }
    } catch {
      setError(requestErrorMessage(t, "solidatus.validateFailed"));
    } finally {
      setLoading(false);
    }
  }

  async function runPreview() {
    setLoading(true);
    setNotice(null);
    try {
      const result = await solidatusApi.exportPreview(pid, mid, {});
      setError(null);
      setPreview(result);
    } catch {
      setError(requestErrorMessage(t, "solidatus.previewFailed"));
    } finally {
      setLoading(false);
    }
  }

  async function runDryRun(connection: SolidatusConnection) {
    setLoading(true);
    setNotice(null);
    try {
      const result = await solidatusApi.sync(pid, mid, {
        connection_id: connection.id,
        mode: "dry_run",
      });
      const translatedStatus = statusLabel(t, result.status);
      if (result.status === "failed" || result.error_message) {
        setNotice({
          severity: "error",
          message: t("solidatus.syncReportedFailure", {
            connection: connection.display_name,
            status: translatedStatus,
            error: syncErrorMessage(t, result.error_message),
          }),
        });
        await refresh();
        return;
      }
      setNotice({
        severity: "info",
        message: t("solidatus.syncReportedStatus", {
          connection: connection.display_name,
          status: translatedStatus,
        }),
      });
      await refresh();
    } catch {
      setError(requestErrorMessage(t, "solidatus.syncFailed"));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Box sx={{ p: 2 }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between" mb={2}>
        <Typography variant="h6">{t("solidatus.title")}</Typography>
        <Button startIcon={<AddIcon />} onClick={openCreate} size="small">
          {t("solidatus.addConnection")}
        </Button>
      </Stack>

      <Alert severity="warning" sx={{ mb: 2 }}>
        {t("solidatus.stubWarning")}
      </Alert>

      {error && (
        <Alert severity="error" onClose={() => setError(null)} sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}

      {notice && (
        <Alert severity={notice.severity} onClose={() => setNotice(null)} sx={{ mb: 2 }}>
          {notice.message}
        </Alert>
      )}

      {loading && <CircularProgress size={24} sx={{ mb: 2 }} />}

      {configs.length === 0 && !loading && (
        <Typography color="text.secondary" sx={{ mb: 2 }}>
          {t("solidatus.noConnections")}
        </Typography>
      )}

      {configs.map((c) => (
        <Card key={c.id} data-testid={`solidatus-connection-${c.id}`} sx={{ mb: 2 }}>
          <CardContent>
            <Stack direction="row" justifyContent="space-between" alignItems="flex-start" gap={2}>
              <Box>
                <Typography variant="subtitle1">{c.display_name}</Typography>
                <Typography variant="body2" color="text.secondary">{c.base_url}</Typography>
                <Stack direction="row" gap={0.5} mt={0.5} flexWrap="wrap">
                  {c.workspace_id && (
                    <Chip
                      size="small"
                      label={t("solidatus.workspaceChip", { value: c.workspace_id })}
                    />
                  )}
                  {!c.is_active && <Chip size="small" label={t("solidatus.inactive")} color="default" />}
                </Stack>
              </Box>
              <Stack direction="row" gap={0.5}>
                <Tooltip title={t("solidatus.edit")}>
                  <IconButton size="small" onClick={() => openEdit(c)}>
                    <EditIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
                <Tooltip title={t("solidatus.delete")}>
                  <IconButton size="small" onClick={() => deleteConfig(c.id)}>
                    <DeleteIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
              </Stack>
            </Stack>

            {c.is_active && (
              <Stack direction="row" gap={1} mt={1.5} flexWrap="wrap">
                <Button
                  variant="outlined"
                  startIcon={<CheckCircleIcon />}
                  onClick={() => runValidate(c)}
                  disabled={loading}
                  size="small"
                  color="secondary"
                >
                  {t("solidatus.testConnection")}
                </Button>
                <Button
                  variant="outlined"
                  startIcon={<PreviewIcon />}
                  onClick={() => runPreview()}
                  disabled={loading}
                  size="small"
                >
                  {t("solidatus.preview")}
                </Button>
                <Button
                  variant="outlined"
                  startIcon={<PlayArrowIcon />}
                  onClick={() => runDryRun(c)}
                  disabled={loading}
                  size="small"
                >
                  {t("solidatus.dryRun")}
                </Button>
                <Tooltip title={t("solidatus.livePushUnavailableHelp")}>
                  <span>
                    <Button
                      variant="contained"
                      startIcon={<PlayArrowIcon />}
                      disabled
                      size="small"
                    >
                      {t("solidatus.requestSyncUnavailable")}
                    </Button>
                  </span>
                </Tooltip>
              </Stack>
            )}
          </CardContent>
        </Card>
      ))}

      {preview && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Typography variant="subtitle2" gutterBottom>
              {t("solidatus.previewResult")}
            </Typography>
            <Stack direction="row" gap={2} flexWrap="wrap">
              <Chip label={t("solidatus.nodesCount", { count: String(preview.nodes_total) })} />
              <Chip label={t("solidatus.edgesCount", { count: String(preview.edges_total) })} />
            </Stack>
            {Object.keys(preview.by_type).length > 0 && (
              <Box mt={1.5}>
                <Typography variant="caption" color="text.secondary">
                  {t("solidatus.breakdownHeading")}
                </Typography>
                <Stack direction="row" gap={1} flexWrap="wrap" mt={0.5}>
                  {Object.entries(preview.by_type)
                    .sort(([a], [b]) => a.localeCompare(b))
                    .map(([nodeType, count]) => (
                      <Chip
                        key={nodeType}
                        size="small"
                        variant="outlined"
                        label={`${nodeType}: ${count}`}
                      />
                    ))}
                </Stack>
              </Box>
            )}
            {warningMessages(preview).length > 0 && (
              <Stack gap={1} mt={1.5}>
                {warningMessages(preview).map((message) => (
                  <Alert key={message} severity="warning">
                    {message}
                  </Alert>
                ))}
              </Stack>
            )}
          </CardContent>
        </Card>
      )}

      {runs.length > 0 && (
        <>
          <Typography variant="subtitle2" gutterBottom>
            {t("solidatus.runHistory")}
          </Typography>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>{t("solidatus.status")}</TableCell>
                <TableCell>{t("solidatus.mode")}</TableCell>
                <TableCell>{t("solidatus.nodes")}</TableCell>
                <TableCell>{t("solidatus.edges")}</TableCell>
                <TableCell>{t("solidatus.started")}</TableCell>
                <TableCell>{t("solidatus.details")}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {runs.slice(0, 10).map((run) => (
                <TableRow key={run.id}>
                  <TableCell><StatusChip status={run.status} t={t} /></TableCell>
                  <TableCell>{modeLabel(t, run.mode)}</TableCell>
                  <TableCell>
                    {run.nodes_created > 0 && (
                      <Chip size="small" color="success" label={`+${run.nodes_created}`} sx={{ mr: 0.5 }} />
                    )}
                    {run.nodes_total}
                  </TableCell>
                  <TableCell>
                    {run.edges_created > 0 && (
                      <Chip size="small" color="success" label={`+${run.edges_created}`} sx={{ mr: 0.5 }} />
                    )}
                    {run.edges_total}
                  </TableCell>
                  <TableCell>{new Date(run.started_at).toLocaleString()}</TableCell>
                  <TableCell>{run.error_message ? syncErrorMessage(t, run.error_message) : ""}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}

      <Dialog open={dialogOpen} onClose={() => setDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>
          {editingId ? t("solidatus.editConnection") : t("solidatus.newConnection")}
        </DialogTitle>
        <DialogContent>
          <Stack gap={2} mt={1}>
            <TextField
              label={t("solidatus.displayName")}
              value={formName}
              onChange={(e) => setFormName(e.target.value)}
              fullWidth
              size="small"
            />
            <TextField
              label={t("solidatus.baseUrl")}
              value={formUrl}
              onChange={(e) => setFormUrl(e.target.value)}
              fullWidth
              size="small"
              placeholder={t("solidatus.baseUrlPlaceholder")}
            />
            <TextField
              label={t("solidatus.token")}
              value={formToken}
              onChange={(e) => setFormToken(e.target.value)}
              fullWidth
              size="small"
              type="password"
              placeholder={editingId ? t("solidatus.tokenUnchangedPlaceholder") : ""}
            />
            <TextField
              label={t("solidatus.workspaceId")}
              value={formWorkspace}
              onChange={(e) => setFormWorkspace(e.target.value)}
              fullWidth
              size="small"
            />
            <TextField
              label={t("solidatus.modelRef")}
              value={formModelRef}
              onChange={(e) => setFormModelRef(e.target.value)}
              fullWidth
              size="small"
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={saveConfig}
            disabled={!formName || !formUrl || (!editingId && !formToken)}
          >
            {t("common.save")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
