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
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import PreviewIcon from "@mui/icons-material/Preview";
import { collibraApi } from "../../api/client";
import { useT } from "../../i18n";
import type {
  CollibraConnection,
  CollibraExportPreviewResponse,
  CollibraSyncRun,
} from "../../api/types";

type T = ReturnType<typeof useT>;
type Notice = { severity: "info" | "warning" | "error" | "success"; message: string } | null;

function statusLabel(t: T, status: string) {
  switch (status) {
    case "succeeded":
      return t("collibra.statusSucceeded");
    case "failed":
      return t("collibra.statusFailed");
    case "running":
      return t("collibra.statusRunning");
    case "pending":
      return t("collibra.statusPending");
    default:
      return t("collibra.statusUnknown");
  }
}

function modeLabel(t: T, mode: string) {
  switch (mode) {
    case "dry_run":
      return t("collibra.modeDryRun");
    case "push":
      return t("collibra.modePush");
    default:
      return t("collibra.modeUnknown");
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

function warningMessages(preview: CollibraExportPreviewResponse) {
  return preview.warnings
    .map((warning) => (typeof warning.message === "string" ? warning.message : null))
    .filter((message): message is string => Boolean(message));
}

function requestErrorMessage(t: T, fallbackKey: string) {
  return t(fallbackKey);
}

function syncErrorMessage(t: T, message: string | null | undefined) {
  if (!message) return t("collibra.noErrorDetails");
  if (message.toLowerCase().includes("not implemented")) {
    return t("collibra.syncFailureFallback");
  }
  return t("collibra.syncFailureFallback");
}

export function CollibraIntegrationPanel() {
  const t = useT();
  const { projectId, modelId } = useParams<{ projectId: string; modelId: string }>();

  const [configs, setConfigs] = useState<CollibraConnection[]>([]);
  const [runs, setRuns] = useState<CollibraSyncRun[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [preview, setPreview] = useState<CollibraExportPreviewResponse | null>(null);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [formName, setFormName] = useState("");
  const [formUrl, setFormUrl] = useState("");
  const [formToken, setFormToken] = useState("");
  const [formCommunity, setFormCommunity] = useState("");
  const [formDomain, setFormDomain] = useState("");

  const pid = projectId ?? "";
  const mid = modelId ?? "";

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const [c, r] = await Promise.all([
        collibraApi.listConfigs(pid, mid),
        collibraApi.listRuns(pid, mid),
      ]);
      setConfigs(c);
      setRuns(r);
    } catch {
      setError(requestErrorMessage(t, "collibra.loadFailed"));
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
    setFormCommunity("");
    setFormDomain("");
    setDialogOpen(true);
  }

  function openEdit(c: CollibraConnection) {
    setEditingId(c.id);
    setFormName(c.display_name);
    setFormUrl(c.base_url);
    setFormToken("");
    setFormCommunity(c.community_id ?? "");
    setFormDomain(c.domain_id ?? "");
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
        payload.community_id = formCommunity || null;
        payload.domain_id = formDomain || null;
        await collibraApi.updateConfig(pid, mid, editingId, payload);
      } else {
        await collibraApi.createConfig(pid, mid, {
          display_name: formName,
          base_url: formUrl,
          token: formToken,
          community_id: formCommunity || null,
          domain_id: formDomain || null,
        });
      }
      setDialogOpen(false);
      await refresh();
    } catch {
      setError(requestErrorMessage(t, "collibra.saveFailed"));
    }
  }

  async function deleteConfig(id: string) {
    try {
      await collibraApi.deleteConfig(pid, mid, id);
      await refresh();
    } catch {
      setError(requestErrorMessage(t, "collibra.deleteFailed"));
    }
  }

  async function runValidate(connection: CollibraConnection) {
    setLoading(true);
    setNotice(null);
    try {
      const result = await collibraApi.validate(pid, mid, {
        connection_id: connection.id,
      });
      setError(null);
      setPreview({
        assets_total: 0,
        relations_total: 0,
        attributes_total: 0,
        responsibilities_total: 0,
        by_asset_type: {},
        warnings: result.warnings.map((w) => ({ message: w })),
      });
      // ok is tri-state: null => simulated (connector not contacted),
      // false => verified failure, true => verified pass. A simulated
      // result must never read as a green success.
      if (result.simulated || result.ok === null) {
        setNotice({
          severity: "info",
          message: t("collibra.validationSimulated", { connection: connection.display_name }),
        });
      } else if (result.ok === false) {
        setNotice({
          severity: "warning",
          message: t("collibra.validationReportedIssue", { connection: connection.display_name }),
        });
      } else {
        setNotice({
          severity: "success",
          message: t("collibra.validationVerified", { connection: connection.display_name }),
        });
      }
    } catch {
      setError(requestErrorMessage(t, "collibra.validateFailed"));
    } finally {
      setLoading(false);
    }
  }

  async function runPreview() {
    setLoading(true);
    setNotice(null);
    try {
      const result = await collibraApi.exportPreview(pid, mid, {});
      setError(null);
      setPreview(result);
    } catch {
      setError(requestErrorMessage(t, "collibra.previewFailed"));
    } finally {
      setLoading(false);
    }
  }

  async function runDryRun(connection: CollibraConnection) {
    setLoading(true);
    setNotice(null);
    try {
      const result = await collibraApi.sync(pid, mid, {
        connection_id: connection.id,
        dry_run: true,
      });
      const translatedStatus = statusLabel(t, result.status);
      if (result.status === "failed" || result.error_message) {
        setNotice({
          severity: "error",
          message: t("collibra.syncReportedFailure", {
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
        message: t("collibra.syncReportedStatus", {
          connection: connection.display_name,
          status: translatedStatus,
        }),
      });
      await refresh();
    } catch {
      setError(requestErrorMessage(t, "collibra.syncFailed"));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Box sx={{ p: 2 }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between" mb={2}>
        <Typography variant="h6">{t("collibra.title")}</Typography>
        <Button startIcon={<AddIcon />} onClick={openCreate} size="small">
          {t("collibra.addConnection")}
        </Button>
      </Stack>

      <Alert severity="warning" sx={{ mb: 2 }}>
        {t("collibra.stubWarning")}
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
          {t("collibra.noConnections")}
        </Typography>
      )}

      {configs.map((c) => (
        <Card key={c.id} data-testid={`collibra-connection-${c.id}`} sx={{ mb: 2 }}>
          <CardContent>
            <Stack direction="row" justifyContent="space-between" alignItems="flex-start" gap={2}>
              <Box>
                <Typography variant="subtitle1">{c.display_name}</Typography>
                <Typography variant="body2" color="text.secondary">{c.base_url}</Typography>
                <Stack direction="row" gap={0.5} mt={0.5} flexWrap="wrap">
                  {c.community_id && (
                    <Chip
                      size="small"
                      label={t("collibra.communityChip", { value: c.community_id })}
                    />
                  )}
                  {c.domain_id && (
                    <Chip
                      size="small"
                      label={t("collibra.domainChip", { value: c.domain_id })}
                    />
                  )}
                  {!c.is_active && <Chip size="small" label={t("collibra.inactive")} color="default" />}
                </Stack>
              </Box>
              <Stack direction="row" gap={0.5}>
                <Tooltip title={t("collibra.edit")}>
                  <IconButton size="small" onClick={() => openEdit(c)}>
                    <EditIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
                <Tooltip title={t("collibra.delete")}>
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
                  {t("collibra.testConnection")}
                </Button>
                <Button
                  variant="outlined"
                  startIcon={<PreviewIcon />}
                  onClick={() => runPreview()}
                  disabled={loading}
                  size="small"
                >
                  {t("collibra.preview")}
                </Button>
                <Button
                  variant="outlined"
                  startIcon={<PlayArrowIcon />}
                  onClick={() => runDryRun(c)}
                  disabled={loading}
                  size="small"
                >
                  {t("collibra.dryRun")}
                </Button>
                <Tooltip title={t("collibra.livePushUnavailableHelp")}>
                  <span>
                    <Button
                      variant="contained"
                      startIcon={<PlayArrowIcon />}
                      disabled
                      size="small"
                    >
                      {t("collibra.requestSyncUnavailable")}
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
              {t("collibra.previewResult")}
            </Typography>
            <Stack direction="row" gap={2} flexWrap="wrap">
              <Chip label={t("collibra.assetsCount", { count: String(preview.assets_total) })} />
              <Chip label={t("collibra.relationsCount", { count: String(preview.relations_total) })} />
              {preview.attributes_total > 0 && (
                <Chip
                  label={t("collibra.attributesCount", {
                    count: String(preview.attributes_total),
                  })}
                />
              )}
              {preview.responsibilities_total > 0 && (
                <Chip
                  label={t("collibra.responsibilitiesCount", {
                    count: String(preview.responsibilities_total),
                  })}
                />
              )}
            </Stack>
            {Object.keys(preview.by_asset_type).length > 0 && (
              <Box mt={1.5}>
                <Typography variant="caption" color="text.secondary">
                  {t("collibra.breakdownHeading")}
                </Typography>
                <Stack direction="row" gap={1} flexWrap="wrap" mt={0.5}>
                  {Object.entries(preview.by_asset_type)
                    .sort(([a], [b]) => a.localeCompare(b))
                    .map(([assetType, count]) => (
                      <Chip
                        key={assetType}
                        size="small"
                        variant="outlined"
                        label={`${assetType}: ${count}`}
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
            {t("collibra.runHistory")}
          </Typography>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>{t("collibra.status")}</TableCell>
                <TableCell>{t("collibra.mode")}</TableCell>
                <TableCell>{t("collibra.assets")}</TableCell>
                <TableCell>{t("collibra.relations")}</TableCell>
                <TableCell>{t("collibra.started")}</TableCell>
                <TableCell>{t("collibra.details")}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {runs.slice(0, 10).map((run) => (
                <TableRow key={run.id}>
                  <TableCell><StatusChip status={run.status} t={t} /></TableCell>
                  <TableCell>{modeLabel(t, run.mode)}</TableCell>
                  <TableCell>
                    {run.assets_created > 0 && (
                      <Chip size="small" color="success" label={`+${run.assets_created}`} sx={{ mr: 0.5 }} />
                    )}
                    {run.assets_total}
                  </TableCell>
                  <TableCell>
                    {run.relations_created > 0 && (
                      <Chip size="small" color="success" label={`+${run.relations_created}`} sx={{ mr: 0.5 }} />
                    )}
                    {run.relations_total}
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
          {editingId ? t("collibra.editConnection") : t("collibra.newConnection")}
        </DialogTitle>
        <DialogContent>
          <Stack gap={2} mt={1}>
            <TextField
              label={t("collibra.displayName")}
              value={formName}
              onChange={(e) => setFormName(e.target.value)}
              fullWidth
              size="small"
            />
            <TextField
              label={t("collibra.baseUrl")}
              value={formUrl}
              onChange={(e) => setFormUrl(e.target.value)}
              fullWidth
              size="small"
              placeholder={t("collibra.baseUrlPlaceholder")}
            />
            <TextField
              label={t("collibra.token")}
              value={formToken}
              onChange={(e) => setFormToken(e.target.value)}
              fullWidth
              size="small"
              type="password"
              placeholder={editingId ? t("collibra.tokenUnchangedPlaceholder") : ""}
            />
            <TextField
              label={t("collibra.communityId")}
              value={formCommunity}
              onChange={(e) => setFormCommunity(e.target.value)}
              fullWidth
              size="small"
            />
            <TextField
              label={t("collibra.domainId")}
              value={formDomain}
              onChange={(e) => setFormDomain(e.target.value)}
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
