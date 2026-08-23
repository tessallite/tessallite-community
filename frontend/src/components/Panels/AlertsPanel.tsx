import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/DeleteOutline";
import EditIcon from "@mui/icons-material/EditOutlined";
import SendIcon from "@mui/icons-material/Send";
import { notificationsApi } from "../../api/client";
import type {
  EventTypeOption,
  NotificationDelivery,
  NotificationRoute,
  NotificationRouteCreate,
} from "../../api/types";
import HelpIconButton from "../HelpIconButton";

const CHANNEL_TYPES = [
  { value: "email", label: "Email" },
  { value: "slack", label: "Slack" },
];

type RouteDialog =
  | { kind: "new" }
  | { kind: "edit"; route: NotificationRoute }
  | null;

export default function AlertsPanel() {
  const t = useT();
  const { projectId } = useParams<{ projectId: string }>();
  const qc = useQueryClient();
  const [dialog, setDialog] = useState<RouteDialog>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<{
    id: string;
    ok: boolean;
    message: string;
  } | null>(null);

  const routes = useQuery({
    queryKey: ["notification-routes", projectId],
    queryFn: () => notificationsApi.list(projectId!),
    enabled: Boolean(projectId),
  });

  // Single source of truth: the event vocabulary comes from the backend so
  // the picker can never offer an event the dispatcher would reject (422).
  const eventTypesQuery = useQuery({
    queryKey: ["notification-event-types", projectId],
    queryFn: () => notificationsApi.eventTypes(projectId!),
    enabled: Boolean(projectId),
  });
  const eventTypes = eventTypesQuery.data ?? [];

  const deliveries = useQuery({
    queryKey: ["notification-deliveries", projectId],
    queryFn: () => notificationsApi.deliveries(projectId!),
    enabled: Boolean(projectId),
  });

  const toggleEnabled = useMutation({
    mutationFn: ({
      routeId,
      enabled,
    }: {
      routeId: string;
      enabled: boolean;
    }) => notificationsApi.update(projectId!, routeId, { enabled }),
    onSuccess: () =>
      qc.invalidateQueries({
        queryKey: ["notification-routes", projectId],
      }),
  });

  const deleteRoute = useMutation({
    mutationFn: (routeId: string) =>
      notificationsApi.remove(projectId!, routeId),
    onSuccess: () =>
      qc.invalidateQueries({
        queryKey: ["notification-routes", projectId],
      }),
  });

  const handleTest = useCallback(
    async (route: NotificationRoute) => {
      if (!projectId) return;
      setTestingId(route.id);
      setTestResult(null);
      try {
        // Bug-5999: the API redacts a saved Slack route's webhook URL from
        // every response, so route.channel_config never carries it -- the
        // test-send must happen against the saved route id, not a config
        // payload the client cannot fully reconstruct.
        await notificationsApi.testRoute(projectId, route.id);
        setTestResult({ id: route.id, ok: true, message: t("alerts.testSent") });
      } catch (err: unknown) {
        const detail = (err as { response?: { data?: { detail?: string } } })
          ?.response?.data?.detail;
        setTestResult({
          id: route.id,
          ok: false,
          message: detail ?? t("alerts.testFailed"),
        });
      } finally {
        setTestingId(null);
      }
    },
    [projectId, t],
  );

  const eventLabel = (val: string) =>
    eventTypes.find((e) => e.value === val)?.label ?? val;
  const channelLabel = (val: string) => t(`alerts.channel.${val}`);

  function channelTarget(route: NotificationRoute): string {
    if (route.channel_type === "email") {
      const recipients = route.channel_config?.recipients;
      if (Array.isArray(recipients)) return recipients.join(", ");
      return "";
    }
    if (route.channel_type === "slack") {
      // Bug-5999: the API redacts the plaintext webhook URL (Bug-5945) and
      // exposes only a boolean flag, so show a masked indicator instead of
      // trying to read a field that is never present.
      return route.channel_config?.has_webhook_url
        ? t("alerts.webhookConfigured")
        : t("alerts.webhookNotConfigured");
    }
    return "";
  }

  return (
    <Box>
      <Box
        display="flex"
        alignItems="center"
        mb={1.5}
        gap={1}
      >
        <Typography variant="subtitle2" fontWeight={600} sx={{ mr: "auto" }}>
          {t("alerts.configurationTitle")}
        </Typography>
        <HelpIconButton href="/help/admin/alert-configuration.html" />
        <Button
          size="small"
          variant="contained"
          startIcon={<AddIcon fontSize="small" />}
          onClick={() => setDialog({ kind: "new" })}
        >
          {t("alerts.addRoute")}
        </Button>
      </Box>

      {routes.isLoading && (
        <Box sx={{ p: 2 }}>
          <CircularProgress size={18} />
        </Box>
      )}

      {routes.error && (
        <Alert severity="error" sx={{ mb: 1 }}>
          {t("alerts.failedToLoad")}
        </Alert>
      )}

      {!routes.isLoading && !routes.error && (routes.data ?? []).length === 0 && (
        <Alert severity="info" sx={{ mb: 1 }}>
          {t("alerts.noRoutesConfigured")}
        </Alert>
      )}

      {(routes.data ?? []).length > 0 && (
        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell sx={{ fontWeight: 700, fontSize: 12 }}>{t("alerts.eventHeader")}</TableCell>
                <TableCell sx={{ fontWeight: 700, fontSize: 12 }}>{t("alerts.channelHeader")}</TableCell>
                <TableCell sx={{ fontWeight: 700, fontSize: 12 }}>{t("alerts.targetHeader")}</TableCell>
                <TableCell sx={{ fontWeight: 700, fontSize: 12 }} align="center">
                  {t("alerts.enabledHeader")}
                </TableCell>
                <TableCell sx={{ fontWeight: 700, fontSize: 12 }} align="right">
                  {t("alerts.actionsHeader")}
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {(routes.data ?? []).map((route) => (
                <TableRow
                  key={route.id}
                  sx={{ "&:hover .row-actions": { opacity: 1 } }}
                >
                  <TableCell sx={{ fontSize: 12 }}>
                    {eventLabel(route.event_type)}
                  </TableCell>
                  <TableCell sx={{ fontSize: 12 }}>
                    {channelLabel(route.channel_type)}
                  </TableCell>
                  <TableCell sx={{ fontSize: 12 }}>
                    {channelTarget(route)}
                  </TableCell>
                  <TableCell align="center">
                    <Switch
                      size="small"
                      checked={route.enabled}
                      onChange={(_e, checked) =>
                        toggleEnabled.mutate({
                          routeId: route.id,
                          enabled: checked,
                        })
                      }
                    />
                  </TableCell>
                  <TableCell align="right">
                    <Box
                      className="row-actions"
                      sx={{ display: "inline-flex", gap: 0.25, opacity: 0 }}
                    >
                      <Tooltip title={t("alerts.sendTestAlert")}>
                        <span>
                          <IconButton
                            size="small"
                            onClick={() => handleTest(route)}
                            disabled={testingId === route.id}
                          >
                            {testingId === route.id ? (
                              <CircularProgress size={14} />
                            ) : (
                              <SendIcon sx={{ fontSize: 16 }} />
                            )}
                          </IconButton>
                        </span>
                      </Tooltip>
                      <Tooltip title={t("alerts.editRoute")}>
                        <IconButton
                          size="small"
                          onClick={() => setDialog({ kind: "edit", route })}
                        >
                          <EditIcon sx={{ fontSize: 16 }} />
                        </IconButton>
                      </Tooltip>
                      <Tooltip title={t("alerts.deleteRoute")}>
                        <IconButton
                          size="small"
                          onClick={() => deleteRoute.mutate(route.id)}
                        >
                          <DeleteIcon sx={{ fontSize: 16 }} />
                        </IconButton>
                      </Tooltip>
                    </Box>
                    {testResult?.id === route.id && (
                      <Typography
                        variant="caption"
                        color={testResult.ok ? "success.main" : "error.main"}
                        sx={{ display: "block", mt: 0.25 }}
                      >
                        {testResult.message}
                      </Typography>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      <Typography variant="subtitle2" sx={{ mt: 3, mb: 1 }}>
        {t("alerts.deliveriesTitle")}
      </Typography>
      {(deliveries.data ?? []).length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t("alerts.deliveriesEmpty")}
        </Typography>
      ) : (
        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>{t("alerts.eventTypeLabel")}</TableCell>
                <TableCell>{t("alerts.channelHeader")}</TableCell>
                <TableCell>{t("alerts.deliveriesStatus")}</TableCell>
                <TableCell>{t("alerts.deliveriesWhen")}</TableCell>
                <TableCell>{t("alerts.deliveriesError")}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {(deliveries.data as NotificationDelivery[]).map((d) => (
                <TableRow key={d.id}>
                  <TableCell>{d.event_type}</TableCell>
                  <TableCell>{t(`alerts.channel.${d.channel_type}`)}</TableCell>
                  <TableCell>{d.status}</TableCell>
                  <TableCell>{d.created_at ?? "—"}</TableCell>
                  <TableCell>{d.error_message ?? "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      <RouteDialogForm
        dialog={dialog}
        projectId={projectId!}
        eventTypes={eventTypes}
        onClose={() => setDialog(null)}
        onSaved={() => {
          setDialog(null);
          qc.invalidateQueries({
            queryKey: ["notification-routes", projectId],
          });
        }}
      />
    </Box>
  );
}

function RouteDialogForm({
  dialog,
  projectId,
  eventTypes,
  onClose,
  onSaved,
}: {
  dialog: RouteDialog;
  projectId: string;
  eventTypes: EventTypeOption[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const t = useT();
  const [eventType, setEventType] = useState("");
  const [channelType, setChannelType] = useState("email");
  const [recipients, setRecipients] = useState("");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Bug-5999: the API never returns a saved Slack route's plaintext webhook
  // URL (Bug-5945). Track whether one is already configured so the form can
  // let the user leave the field blank on edit instead of forcing re-entry.
  const [hasExistingWebhook, setHasExistingWebhook] = useState(false);

  useEffect(() => {
    setError(null);
    if (dialog?.kind === "edit") {
      const r = dialog.route;
      setEventType(r.event_type);
      setChannelType(r.channel_type);
      setEnabled(r.enabled);
      if (r.channel_type === "email") {
        const list = r.channel_config?.recipients;
        setRecipients(Array.isArray(list) ? list.join(", ") : "");
        setWebhookUrl("");
        setHasExistingWebhook(false);
      } else {
        // The webhook URL is a redacted secret -- never pre-fill it, only
        // note that one is already configured.
        setWebhookUrl("");
        setHasExistingWebhook(Boolean(r.channel_config?.has_webhook_url));
        setRecipients("");
      }
    } else if (dialog?.kind === "new") {
      setEventType("");
      setChannelType("email");
      setRecipients("");
      setWebhookUrl("");
      setEnabled(true);
      setHasExistingWebhook(false);
    }
  }, [dialog]);

  const save = useMutation({
    mutationFn: async () => {
      const channelConfig: Record<string, unknown> =
        channelType === "email"
          ? {
              recipients: recipients
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
            }
          : webhookUrl.trim()
            ? { webhook_url: webhookUrl.trim() }
            // Bug-5999: blank means "keep the already-configured secret";
            // the backend preserves the existing encrypted URL when this
            // key is absent from an edit request.
            : {};

      const body: NotificationRouteCreate = {
        event_type: eventType,
        channel_type: channelType,
        channel_config: channelConfig,
        enabled,
      };

      if (dialog?.kind === "edit") {
        await notificationsApi.update(projectId, dialog.route.id, body);
      } else {
        await notificationsApi.create(projectId, body);
      }
    },
    onSuccess: onSaved,
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail;
      setError(detail ?? (err instanceof Error ? err.message : t("alerts.saveFailed")));
    },
  });

  const isEdit = dialog?.kind === "edit";

  const isValid =
    eventType !== "" &&
    channelType !== "" &&
    (channelType === "email"
      ? recipients.trim().length > 0
      : webhookUrl.trim().length > 0 || (isEdit && hasExistingWebhook));

  return (
    <Dialog open={dialog !== null} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{isEdit ? t("alerts.editAlertRouteTitle") : t("alerts.newAlertRouteTitle")}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          <FormControl fullWidth size="small">
            <InputLabel>{t("alerts.eventTypeLabel")}</InputLabel>
            <Select
              value={eventType}
              label={t("alerts.eventTypeLabel")}
              onChange={(e) => setEventType(e.target.value)}
            >
              {eventTypes.map((et) => {
                const i18nKey = `alerts.eventType.${et.value}`;
                const translated = t(i18nKey);
                return (
                  <MenuItem key={et.value} value={et.value}>
                    {translated === i18nKey ? et.label : translated}
                  </MenuItem>
                );
              })}
            </Select>
          </FormControl>

          <FormControl fullWidth size="small">
            <InputLabel>{t("alerts.channelLabel")}</InputLabel>
            <Select
              value={channelType}
              label={t("alerts.channelLabel")}
              onChange={(e) => setChannelType(e.target.value)}
            >
              {CHANNEL_TYPES.map((ct) => (
                <MenuItem key={ct.value} value={ct.value}>
                  {t(`alerts.channel.${ct.value}`)}
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          {channelType === "email" && (
            <TextField
              label={t("alerts.recipientsLabel")}
              size="small"
              fullWidth
              value={recipients}
              onChange={(e) => setRecipients(e.target.value)}
              helperText={t("alerts.recipientsHelperText")}
            />
          )}

          {channelType === "slack" && (
            <TextField
              label={t("alerts.webhookUrlLabel")}
              size="small"
              fullWidth
              value={webhookUrl}
              onChange={(e) => setWebhookUrl(e.target.value)}
              placeholder={
                hasExistingWebhook ? t("alerts.webhookUrlEditPlaceholder") : undefined
              }
              helperText={
                hasExistingWebhook
                  ? t("alerts.webhookUrlEditHelperText")
                  : t("alerts.webhookUrlHelperText")
              }
            />
          )}

          <Stack direction="row" alignItems="center" spacing={1}>
            <Switch
              size="small"
              checked={enabled}
              onChange={(_e, checked) => setEnabled(checked)}
            />
            <Typography variant="body2">
              {enabled ? t("alerts.enabledStatus") : t("alerts.disabledStatus")}
            </Typography>
          </Stack>
        </Stack>

        {error && (
          <Alert severity="error" sx={{ mt: 1.5 }}>
            {error}
          </Alert>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t("alerts.cancel")}</Button>
        <Button
          variant="contained"
          disabled={!isValid || save.isPending}
          onClick={() => save.mutate()}
        >
          {save.isPending ? (
            <CircularProgress size={16} />
          ) : isEdit ? (
            t("alerts.save")
          ) : (
            t("alerts.create")
          )}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
