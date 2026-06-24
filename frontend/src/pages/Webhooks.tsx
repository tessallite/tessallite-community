import { useCallback, useEffect, useRef, useState } from "react";
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
  Divider,
  Drawer,
  FormControlLabel,
  Checkbox,
  IconButton,
  InputAdornment,
  Paper,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tab,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import AddIcon from "@mui/icons-material/Add";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import DeleteIcon from "@mui/icons-material/DeleteOutline";
import EditIcon from "@mui/icons-material/EditOutlined";
import RefreshIcon from "@mui/icons-material/RefreshOutlined";
import ReplayIcon from "@mui/icons-material/ReplayOutlined";
import SendIcon from "@mui/icons-material/SendOutlined";
import VisibilityIcon from "@mui/icons-material/Visibility";
import VisibilityOffIcon from "@mui/icons-material/VisibilityOff";
import { webhooksApi } from "../api/client";
import type { WebhookEndpoint, WebhookDelivery } from "../api/types";
import HelpIconButton from "../components/HelpIconButton";
import { useT } from "../i18n";

function maskUrl(url: string): string {
  try {
    const u = new URL(url);
    return `${u.protocol}//${u.host}/***`;
  } catch {
    return "***";
  }
}

function statusColor(s: string): "success" | "error" | "warning" | "default" {
  if (s === "delivered") return "success";
  if (s === "dlq" || s === "failed") return "error";
  if (s === "pending") return "warning";
  return "default";
}

export default function Webhooks({ embedded }: { embedded?: boolean } = {}) {
  const t = useT();
  const qc = useQueryClient();
  const [tab, setTab] = useState(0);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<WebhookEndpoint | null>(null);
  const [historyEndpoint, setHistoryEndpoint] = useState<WebhookEndpoint | null>(null);
  const [secretDialog, setSecretDialog] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  return (
    <Box sx={{ p: embedded ? 0 : 3, maxWidth: 1100, mx: "auto" }}>
      {!embedded && (
      <>
      <Box display="flex" alignItems="center" gap={1} mb={1}>
        <IconButton href="/admin" size="small">
          <ArrowBackIcon />
        </IconButton>
        <Typography variant="h5" fontWeight={700}>
          {t("webhooks.title")}
        </Typography>
        <HelpIconButton href="/help/admin/webhooks.html" />
      </Box>
      <Typography variant="body2" color="text.secondary" mb={3}>
        {t("webhooks.description")}
      </Typography>
      </>
      )}

      <Box sx={{ mb: 3, p: 2, bgcolor: "grey.50", border: 1, borderColor: "divider", borderRadius: 1 }}>
        <Typography variant="subtitle2" gutterBottom>{t("webhooks.clientAppTitle")}</Typography>
        <Typography variant="caption" color="text.secondary" display="block" mb={1}>
          {t("webhooks.clientAppDescription")}
        </Typography>
        <Box display="flex" alignItems="center" gap={1}>
          <TextField
            size="small"
            value={window.location.origin}
            InputProps={{ readOnly: true, sx: { fontFamily: "monospace", fontSize: 13 } }}
            sx={{ flex: 1 }}
          />
          <Button size="small" onClick={() => navigator.clipboard.writeText(window.location.origin)}>
            {t("webhooks.copyButton")}
          </Button>
        </Box>
      </Box>

      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 2 }}>
        <Tab label={t("webhooks.tabEndpoints")} />
        <Tab label={t("webhooks.tabDlq")} />
      </Tabs>

      {tab === 0 && (
        <EndpointsTab
          onAdd={() => {
            setEditing(null);
            setDialogOpen(true);
          }}
          onEdit={(ep) => {
            setEditing(ep);
            setDialogOpen(true);
          }}
          onHistory={setHistoryEndpoint}
          onSecretRotated={(secret) => setSecretDialog(secret)}
        />
      )}

      {tab === 1 && <DlqTab />}

      <EndpointDialog
        open={dialogOpen}
        endpoint={editing}
        error={error}
        onClose={() => {
          setDialogOpen(false);
          setEditing(null);
          setError(null);
        }}
        onSaved={() => {
          setDialogOpen(false);
          setEditing(null);
          setError(null);
          qc.invalidateQueries({ queryKey: ["webhooks"] });
        }}
        onError={setError}
      />

      <DeliveryHistoryDrawer
        endpoint={historyEndpoint}
        onClose={() => setHistoryEndpoint(null)}
      />

      {secretDialog !== null && (
        <SecretRevealDialog
          secret={secretDialog}
          onClose={() => setSecretDialog(null)}
        />
      )}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Secret reveal dialog (masked by default, auto-hides after 30 s)
// ---------------------------------------------------------------------------

const SECRET_REVEAL_TIMEOUT_MS = 30_000;

function SecretRevealDialog({ secret, onClose }: { secret: string; onClose: () => void }) {
  const t = useT();
  const [visible, setVisible] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => {
    if (visible) {
      clearTimer();
      timerRef.current = setTimeout(() => setVisible(false), SECRET_REVEAL_TIMEOUT_MS);
    }
    return clearTimer;
  }, [visible, clearTimer]);

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{t("webhooks.secretDialogTitle")}</DialogTitle>
      <DialogContent>
        <Alert severity="warning" sx={{ mb: 2 }}>
          {t("webhooks.secretWarning")}
        </Alert>
        <TextField
          fullWidth
          value={secret}
          type={visible ? "text" : "password"}
          InputProps={{
            readOnly: true,
            endAdornment: (
              <InputAdornment position="end">
                <Tooltip title={visible ? t("webhooks.hideSecretTooltip") : t("webhooks.revealSecretTooltip")}>
                  <IconButton
                    size="small"
                    onClick={() => setVisible((v) => !v)}
                    edge="end"
                  >
                    {visible ? <VisibilityOffIcon fontSize="small" /> : <VisibilityIcon fontSize="small" />}
                  </IconButton>
                </Tooltip>
              </InputAdornment>
            ),
          }}
          size="small"
          sx={{ fontFamily: "monospace" }}
        />
      </DialogContent>
      <DialogActions>
        <Button onClick={() => navigator.clipboard.writeText(secret)}>{t("webhooks.copySecretButton")}</Button>
        <Button onClick={onClose} variant="contained">{t("webhooks.doneButton")}</Button>
      </DialogActions>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Endpoints tab
// ---------------------------------------------------------------------------

function EndpointsTab({
  onAdd,
  onEdit,
  onHistory,
  onSecretRotated,
}: {
  onAdd: () => void;
  onEdit: (ep: WebhookEndpoint) => void;
  onHistory: (ep: WebhookEndpoint) => void;
  onSecretRotated: (secret: string) => void;
}) {
  const t = useT();
  const qc = useQueryClient();
  const { data: endpoints = [], isLoading } = useQuery({
    queryKey: ["webhooks"],
    queryFn: webhooksApi.list,
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => webhooksApi.delete(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["webhooks"] }),
  });

  const toggleMut = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      webhooksApi.update(id, { is_active: active }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["webhooks"] }),
  });

  const testMut = useMutation({
    mutationFn: (id: string) => webhooksApi.test(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["webhooks"] }),
  });

  const rotateMut = useMutation({
    mutationFn: (id: string) => webhooksApi.rotateSecret(id),
    onSuccess: (data) => onSecretRotated(data.signing_secret),
  });

  return (
    <>
      <Box display="flex" justifyContent="flex-end" mb={2}>
        <Button startIcon={<AddIcon />} variant="contained" size="small" onClick={onAdd}>
          {t("webhooks.addEndpoint")}
        </Button>
      </Box>

      <TableContainer component={Paper} variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell sx={{ fontWeight: 700 }}>{t("webhooks.colName")}</TableCell>
              <TableCell sx={{ fontWeight: 700 }}>{t("webhooks.colUrl")}</TableCell>
              <TableCell sx={{ fontWeight: 700 }}>{t("webhooks.colEvents")}</TableCell>
              <TableCell sx={{ fontWeight: 700 }}>{t("webhooks.colActive")}</TableCell>
              <TableCell width={220} />
            </TableRow>
          </TableHead>
          <TableBody>
            {isLoading && (
              <TableRow>
                <TableCell colSpan={5} align="center">
                  <CircularProgress size={18} />
                </TableCell>
              </TableRow>
            )}
            {!isLoading && endpoints.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} align="center">
                  {t("webhooks.noEndpoints")}
                </TableCell>
              </TableRow>
            )}
            {endpoints.map((ep: WebhookEndpoint) => (
              <TableRow key={ep.id}>
                <TableCell>{ep.name}</TableCell>
                <TableCell>
                  <Tooltip title={ep.url}>
                    <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: 12 }}>
                      {maskUrl(ep.url)}
                    </Typography>
                  </Tooltip>
                </TableCell>
                <TableCell>
                  {ep.event_filters.includes("*") ? (
                    <Chip label={t("webhooks.allEvents")} size="small" variant="outlined" />
                  ) : (
                    ep.event_filters.map((f) => (
                      <Chip key={f} label={f} size="small" variant="outlined" sx={{ mr: 0.5, mb: 0.5 }} />
                    ))
                  )}
                </TableCell>
                <TableCell>
                  <Switch
                    size="small"
                    checked={ep.is_active}
                    onChange={(e) =>
                      toggleMut.mutate({ id: ep.id, active: e.target.checked })
                    }
                  />
                </TableCell>
                <TableCell>
                  <Tooltip title={t("webhooks.testDeliveryTooltip")}>
                    <IconButton
                      size="small"
                      onClick={() => testMut.mutate(ep.id)}
                      disabled={testMut.isPending}
                    >
                      <SendIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t("webhooks.deliveryHistoryTooltip")}>
                    <IconButton size="small" onClick={() => onHistory(ep)}>
                      <RefreshIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t("webhooks.rotateSecretTooltip")}>
                    <IconButton
                      size="small"
                      onClick={() => rotateMut.mutate(ep.id)}
                      disabled={rotateMut.isPending}
                    >
                      <ReplayIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t("webhooks.editTooltip")}>
                    <IconButton size="small" onClick={() => onEdit(ep)}>
                      <EditIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t("webhooks.deleteTooltip")}>
                    <IconButton
                      size="small"
                      onClick={() => deleteMut.mutate(ep.id)}
                    >
                      <DeleteIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
    </>
  );
}

// ---------------------------------------------------------------------------
// DLQ tab
// ---------------------------------------------------------------------------

function DlqTab() {
  const t = useT();
  const qc = useQueryClient();
  const { data: entries = [], isLoading } = useQuery({
    queryKey: ["webhooks-dlq"],
    queryFn: () => webhooksApi.dlq(),
  });

  const retryMut = useMutation({
    mutationFn: (id: string) => webhooksApi.retryDlq(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["webhooks-dlq"] }),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => webhooksApi.deleteDlq(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["webhooks-dlq"] }),
  });

  return (
    <TableContainer component={Paper} variant="outlined">
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell sx={{ fontWeight: 700 }}>{t("webhooks.dlqColEventType")}</TableCell>
            <TableCell sx={{ fontWeight: 700 }}>{t("webhooks.dlqColStatus")}</TableCell>
            <TableCell sx={{ fontWeight: 700 }}>{t("webhooks.dlqColAttempts")}</TableCell>
            <TableCell sx={{ fontWeight: 700 }}>{t("webhooks.dlqColResponse")}</TableCell>
            <TableCell sx={{ fontWeight: 700 }}>{t("webhooks.dlqColError")}</TableCell>
            <TableCell sx={{ fontWeight: 700 }}>{t("webhooks.dlqColCreated")}</TableCell>
            <TableCell width={100} />
          </TableRow>
        </TableHead>
        <TableBody>
          {isLoading && (
            <TableRow>
              <TableCell colSpan={7} align="center">
                <CircularProgress size={18} />
              </TableCell>
            </TableRow>
          )}
          {!isLoading && entries.length === 0 && (
            <TableRow>
              <TableCell colSpan={7} align="center">
                {t("webhooks.noDlqEntries")}
              </TableCell>
            </TableRow>
          )}
          {entries.map((d: WebhookDelivery) => (
            <TableRow key={d.id}>
              <TableCell>{d.event_type}</TableCell>
              <TableCell>
                <Chip label={d.status} size="small" color={statusColor(d.status)} />
              </TableCell>
              <TableCell>{d.attempts}</TableCell>
              <TableCell>{d.response_code ?? t("common.na")}</TableCell>
              <TableCell>
                <Typography variant="body2" sx={{ fontSize: 12, maxWidth: 200 }} noWrap>
                  {d.error_message ?? t("common.na")}
                </Typography>
              </TableCell>
              <TableCell>
                {d.created_at ? new Date(d.created_at).toLocaleString() : t("common.na")}
              </TableCell>
              <TableCell>
                <Tooltip title={t("webhooks.retryTooltip")}>
                  <IconButton
                    size="small"
                    onClick={() => retryMut.mutate(d.id)}
                    disabled={retryMut.isPending}
                  >
                    <ReplayIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
                <Tooltip title={t("webhooks.deleteTooltip")}>
                  <IconButton
                    size="small"
                    onClick={() => deleteMut.mutate(d.id)}
                  >
                    <DeleteIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

// ---------------------------------------------------------------------------
// Create / Edit dialog
// ---------------------------------------------------------------------------

function EndpointDialog({
  open,
  endpoint,
  error,
  onClose,
  onSaved,
  onError,
}: {
  open: boolean;
  endpoint: WebhookEndpoint | null;
  error: string | null;
  onClose: () => void;
  onSaved: () => void;
  onError: (e: string) => void;
}) {
  const t = useT();
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [allEvents, setAllEvents] = useState(true);
  const [selectedEvents, setSelectedEvents] = useState<Set<string>>(new Set());

  // Single source of truth: the subscribable event names come from the
  // backend catalogue, so the picker can never offer an event the backend
  // does not emit.
  const { data: eventTypes = [] } = useQuery({
    queryKey: ["webhook-event-types"],
    queryFn: () => webhooksApi.eventTypes(),
    enabled: open,
  });

  const isEdit = endpoint !== null;

  const handleOpen = () => {
    if (endpoint) {
      setName(endpoint.name);
      setUrl(endpoint.url);
      const isAll = endpoint.event_filters.includes("*");
      setAllEvents(isAll);
      setSelectedEvents(new Set(isAll ? [] : endpoint.event_filters));
    } else {
      setName("");
      setUrl("");
      setAllEvents(true);
      setSelectedEvents(new Set());
    }
  };

  const saveMut = useMutation({
    mutationFn: async () => {
      const filters = allEvents ? ["*"] : Array.from(selectedEvents);
      if (endpoint) {
        await webhooksApi.update(endpoint.id, {
          name,
          url,
          event_filters: filters,
        });
      } else {
        await webhooksApi.create({ name, url, event_filters: filters });
      }
    },
    onSuccess: onSaved,
    onError: (err: any) => {
      onError(err?.response?.data?.detail ?? t("common.saveFailed"));
    },
  });

  const valid = name.trim() !== "" && url.trim() !== "" && (allEvents || selectedEvents.size > 0);

  return (
    <Dialog
      open={open}
      onClose={onClose}
      maxWidth="sm"
      fullWidth
      TransitionProps={{ onEnter: handleOpen }}
    >
      <DialogTitle>{isEdit ? t("webhooks.dialogEditTitle") : t("webhooks.dialogAddTitle")}</DialogTitle>
      <DialogContent>
        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}
        <TextField
          label={t("webhooks.nameLabel")}
          fullWidth
          margin="normal"
          value={name}
          onChange={(e) => setName(e.target.value)}
          autoFocus
        />
        <TextField
          label={t("webhooks.urlLabel")}
          fullWidth
          margin="normal"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder={t("webhooks.urlPlaceholder")}
        />
        <Divider sx={{ my: 2 }} />
        <Typography variant="subtitle2" gutterBottom>
          {t("webhooks.eventFiltersTitle")}
        </Typography>
        <FormControlLabel
          control={
            <Checkbox checked={allEvents} onChange={(e) => setAllEvents(e.target.checked)} />
          }
          label={t("webhooks.allEventsLabel")}
        />
        {!allEvents && (
          <Box sx={{ pl: 2, display: "flex", flexDirection: "column" }}>
            {eventTypes.map((et) => {
              const i18nKey = `webhooks.event.${et.value.replace(/\./g, "_")}`;
              const translated = t(i18nKey);
              return (
                <FormControlLabel
                  key={et.value}
                  control={
                    <Checkbox
                      size="small"
                      checked={selectedEvents.has(et.value)}
                      onChange={(e) => {
                        const next = new Set(selectedEvents);
                        if (e.target.checked) next.add(et.value);
                        else next.delete(et.value);
                        setSelectedEvents(next);
                      }}
                    />
                  }
                  label={translated === i18nKey ? et.label : translated}
                  sx={{ "& .MuiTypography-root": { fontSize: 13 } }}
                />
              );
            })}
          </Box>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t("webhooks.cancelButton")}</Button>
        <Button
          variant="contained"
          disabled={!valid || saveMut.isPending}
          onClick={() => saveMut.mutate()}
        >
          {saveMut.isPending ? <CircularProgress size={16} /> : isEdit ? t("webhooks.updateButton") : t("webhooks.createButton")}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Delivery history drawer
// ---------------------------------------------------------------------------

function DeliveryHistoryDrawer({
  endpoint,
  onClose,
}: {
  endpoint: WebhookEndpoint | null;
  onClose: () => void;
}) {
  const t = useT();
  const { data: deliveries = [], isLoading } = useQuery({
    queryKey: ["webhook-deliveries", endpoint?.id],
    queryFn: () => webhooksApi.deliveries(endpoint!.id),
    enabled: endpoint !== null,
  });

  return (
    <Drawer anchor="right" open={endpoint !== null} onClose={onClose}>
      <Box sx={{ width: 480, p: 2 }}>
        <Typography variant="h6" gutterBottom>
          {t("webhooks.deliveryHistoryTitle", { name: endpoint?.name ?? "" })}
        </Typography>
        {isLoading ? (
          <CircularProgress size={18} />
        ) : deliveries.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            {t("webhooks.noDeliveries")}
          </Typography>
        ) : (
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell sx={{ fontWeight: 700 }}>{t("webhooks.deliveryColEvent")}</TableCell>
                <TableCell sx={{ fontWeight: 700 }}>{t("webhooks.deliveryColStatus")}</TableCell>
                <TableCell sx={{ fontWeight: 700 }}>{t("webhooks.deliveryColCode")}</TableCell>
                <TableCell sx={{ fontWeight: 700 }}>{t("webhooks.deliveryColAttempts")}</TableCell>
                <TableCell sx={{ fontWeight: 700 }}>{t("webhooks.deliveryColTime")}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {deliveries.map((d: WebhookDelivery) => (
                <TableRow key={d.id}>
                  <TableCell>{d.event_type}</TableCell>
                  <TableCell>
                    <Chip label={d.status} size="small" color={statusColor(d.status)} />
                  </TableCell>
                  <TableCell>{d.response_code ?? t("common.na")}</TableCell>
                  <TableCell>{d.attempts}</TableCell>
                  <TableCell>
                    {d.created_at ? new Date(d.created_at).toLocaleString() : t("common.na")}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Box>
    </Drawer>
  );
}
