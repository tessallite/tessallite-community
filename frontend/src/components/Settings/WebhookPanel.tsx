import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  Divider,
  FormControlLabel,
  FormGroup,
  Stack,
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
import {
  agentApi,
  type AgentConfig,
  type WebhookDlqRow,
  type WebhookEventType,
} from "../../api/agentApi";
import { useT } from "../../i18n";

const WILDCARD = "*";

type Props = {
  projectId: string;
  draft: AgentConfig;
  update: <K extends keyof AgentConfig>(key: K, value: AgentConfig[K]) => void;
  webhookSecretRotated: boolean;
};

/**
 * Bug-8411 — the agent webhook settings surface.
 *
 * The backend has had rotate-secret, the DLQ list, retry and discard since
 * Phase C2, and the API client has wrapped all four since then, but nothing
 * in the SPA ever called them: the Webhook section rendered a single URL
 * field, so a modeller could configure a receiver and then had no way to
 * rotate its signing secret, choose which events it gets, or see (let alone
 * recover) an event that failed to deliver.
 *
 * Lives in its own file rather than inside ProjectAgentTabs.tsx, which is
 * already ~1100 lines against this repo's 200-400 line target.
 */
export default function WebhookPanel({
  projectId,
  draft,
  update,
  webhookSecretRotated,
}: Props) {
  const t = useT();
  const qc = useQueryClient();
  const [revealedSecret, setRevealedSecret] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const hasWebhook = Boolean((draft.webhook_url ?? "").trim());

  const eventTypesQuery = useQuery<WebhookEventType[]>({
    queryKey: ["agent-webhook-event-types", projectId],
    queryFn: () => agentApi.listWebhookEventTypes(projectId),
    enabled: Boolean(projectId),
    staleTime: Infinity, // A static catalogue; refetching it per render is waste.
  });

  const dlqQuery = useQuery<WebhookDlqRow[]>({
    queryKey: ["agent-webhook-dlq", projectId],
    queryFn: () => agentApi.listWebhookDlq(projectId),
    enabled: Boolean(projectId) && hasWebhook,
  });

  const onActionError = (err: { response?: { data?: { detail?: string } } }) =>
    setActionError(err.response?.data?.detail ?? t("agent.webhook.actionFailed"));

  const rotateMut = useMutation({
    mutationFn: () => agentApi.rotateWebhookSecret(projectId),
    onSuccess: (data) => {
      setActionError(null);
      setRevealedSecret(data.signing_secret);
    },
    onError: onActionError,
  });

  const retryMut = useMutation({
    mutationFn: (dlqId: string) => agentApi.retryWebhookDlq(projectId, dlqId),
    onSuccess: () => {
      setActionError(null);
      qc.invalidateQueries({ queryKey: ["agent-webhook-dlq", projectId] });
    },
    onError: onActionError,
  });

  const discardMut = useMutation({
    mutationFn: (dlqId: string) => agentApi.discardWebhookDlq(projectId, dlqId),
    onSuccess: () => {
      setActionError(null);
      qc.invalidateQueries({ queryKey: ["agent-webhook-dlq", projectId] });
    },
    onError: onActionError,
  });

  // A null filter list means "every event" on the backend (a project that
  // predates the column), so render it the same way the dispatcher reads it
  // rather than showing an empty, misleading set of checkboxes.
  const filters = useMemo(
    () => draft.webhook_event_filters ?? [WILDCARD],
    [draft.webhook_event_filters],
  );
  const allEvents = filters.includes(WILDCARD);
  const eventTypes = eventTypesQuery.data ?? [];

  const setAllEvents = (checked: boolean) => {
    // Deselecting "all events" pre-selects every event individually rather
    // than leaving an empty list: the backend rejects an empty subscription
    // outright (an empty list must never be read as "everything" — that was
    // Bug-7330 on the platform-wide webhook), and an empty checkbox grid
    // would make the Save button fail with a validation error the user did
    // not ask for.
    update(
      "webhook_event_filters",
      checked ? [WILDCARD] : eventTypes.map((e) => e.value),
    );
  };

  const toggleEvent = (value: string, checked: boolean) => {
    const next = checked
      ? [...filters.filter((f) => f !== WILDCARD), value]
      : filters.filter((f) => f !== value && f !== WILDCARD);
    update("webhook_event_filters", next);
  };

  const noEventsSelected = !allEvents && filters.length === 0;

  // `useT` returns the key itself when a message is missing. Fall back to the
  // backend-supplied label so a newly added event type that has no i18n key
  // yet renders as readable text instead of a raw dotted key.
  const eventLabel = (evt: WebhookEventType) => {
    const key = `agent.webhook.event.${evt.value}`;
    const translated = t(key);
    return translated === key ? evt.label : translated;
  };

  return (
    <Stack spacing={2}>
      <Typography variant="subtitle2">{t("agent.setup.webhookHeading")}</Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.webhookHelp")}
      </Typography>

      <TextField
        label={t("agent.setup.webhookUrl")}
        size="small"
        value={draft.webhook_url ?? ""}
        onChange={(e) => update("webhook_url", e.target.value || null)}
        helperText={t("agent.setup.webhookHelp")}
      />

      {webhookSecretRotated && (
        <Alert severity="warning">
          {t("agent.webhook.secretRotatedNotice")}
        </Alert>
      )}

      {actionError && (
        <Alert severity="error" onClose={() => setActionError(null)}>
          {actionError}
        </Alert>
      )}

      {/* ---------------------------------------------------------------- */}
      {/* Signing secret                                                    */}
      {/* ---------------------------------------------------------------- */}
      <Divider />
      <Typography variant="subtitle2">
        {t("agent.webhook.signingSecretHeading")}
      </Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.webhook.signingSecretHelp")}
      </Typography>
      <Box display="flex" alignItems="center" gap={1}>
        <Tooltip
          title={hasWebhook ? "" : t("agent.webhook.rotateNeedsUrl")}
          disableHoverListener={hasWebhook}
        >
          <span>
            <Button
              size="small"
              variant="outlined"
              disabled={!hasWebhook || rotateMut.isPending}
              onClick={() => rotateMut.mutate()}
            >
              {rotateMut.isPending ? (
                <CircularProgress size={16} />
              ) : (
                t("agent.webhook.rotateSecret")
              )}
            </Button>
          </span>
        </Tooltip>
      </Box>
      {revealedSecret && (
        <Alert severity="warning" onClose={() => setRevealedSecret(null)}>
          <Typography variant="body2" gutterBottom>
            {t("agent.webhook.secretShownOnce")}
          </Typography>
          <Box display="flex" alignItems="center" gap={1}>
            <TextField
              size="small"
              value={revealedSecret}
              InputProps={{
                readOnly: true,
                sx: { fontFamily: "monospace", fontSize: 13 },
              }}
              fullWidth
            />
            <Button
              size="small"
              onClick={() => navigator.clipboard.writeText(revealedSecret)}
            >
              {t("agent.setup.copy")}
            </Button>
          </Box>
        </Alert>
      )}

      {/* ---------------------------------------------------------------- */}
      {/* Event subscription                                                */}
      {/* ---------------------------------------------------------------- */}
      <Divider />
      <Typography variant="subtitle2">
        {t("agent.webhook.eventsHeading")}
      </Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.webhook.eventsHelp")}
      </Typography>
      {eventTypesQuery.isError && (
        <Alert severity="error">{t("agent.webhook.eventsLoadFailed")}</Alert>
      )}
      <FormGroup>
        <FormControlLabel
          control={
            <Checkbox
              size="small"
              checked={allEvents}
              // Until the catalogue has loaded there is nothing to expand
              // "all events" INTO, so unticking it would produce an empty
              // subscription the backend rejects. Keep it locked instead of
              // letting the user create an unsaveable state.
              disabled={eventTypes.length === 0}
              onChange={(e) => setAllEvents(e.target.checked)}
            />
          }
          label={
            <Typography variant="body2">
              {t("agent.webhook.allEvents")}
            </Typography>
          }
        />
        {eventTypes.map((evt) => (
          <FormControlLabel
            key={evt.value}
            sx={{ ml: 2 }}
            control={
              <Checkbox
                size="small"
                disabled={allEvents}
                checked={allEvents || filters.includes(evt.value)}
                onChange={(e) => toggleEvent(evt.value, e.target.checked)}
              />
            }
            label={
              <Typography variant="body2">{eventLabel(evt)}</Typography>
            }
          />
        ))}
      </FormGroup>
      {noEventsSelected && (
        <Alert severity="warning">{t("agent.webhook.noEventsSelected")}</Alert>
      )}

      {/* ---------------------------------------------------------------- */}
      {/* Dead-letter queue                                                 */}
      {/* ---------------------------------------------------------------- */}
      <Divider />
      <Typography variant="subtitle2">{t("agent.webhook.dlqHeading")}</Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.webhook.dlqHelp")}
      </Typography>
      {!hasWebhook && (
        <Typography variant="body2" color="text.secondary">
          {t("agent.webhook.dlqNeedsUrl")}
        </Typography>
      )}
      {hasWebhook && dlqQuery.isLoading && <CircularProgress size={20} />}
      {hasWebhook && dlqQuery.isError && (
        <Alert severity="error">{t("agent.webhook.dlqLoadFailed")}</Alert>
      )}
      {hasWebhook && dlqQuery.data && dlqQuery.data.length === 0 && (
        <Typography variant="body2" color="text.secondary">
          {t("agent.webhook.dlqEmpty")}
        </Typography>
      )}
      {hasWebhook && dlqQuery.data && dlqQuery.data.length > 0 && (
        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>{t("agent.webhook.dlqEvent")}</TableCell>
                <TableCell>{t("agent.webhook.dlqDestination")}</TableCell>
                <TableCell align="right">
                  {t("agent.webhook.dlqAttempts")}
                </TableCell>
                <TableCell>{t("agent.webhook.dlqLastError")}</TableCell>
                <TableCell>{t("agent.webhook.dlqLastAttempt")}</TableCell>
                <TableCell align="right">
                  {t("agent.webhook.dlqActions")}
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {dlqQuery.data.map((row) => (
                <TableRow key={row.id}>
                  <TableCell>
                    <Chip size="small" label={row.event_type} />
                  </TableCell>
                  {/* Bug-8350 — only the sanitised scheme://host[:port] hint
                      is ever returned; the full URL may embed a bearer token
                      in its path or query and is never sent to the client. */}
                  <TableCell sx={{ fontFamily: "monospace", fontSize: 12 }}>
                    {row.target_host ?? "-"}
                  </TableCell>
                  <TableCell align="right">{row.attempt_count}</TableCell>
                  <TableCell
                    sx={{ maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis" }}
                  >
                    <Tooltip title={row.last_error ?? ""}>
                      <span>
                        {row.last_status_code
                          ? `${row.last_status_code} — ${row.last_error ?? ""}`
                          : row.last_error ?? "-"}
                      </span>
                    </Tooltip>
                  </TableCell>
                  <TableCell>
                    {row.last_attempted_at
                      ? new Date(row.last_attempted_at).toLocaleString()
                      : "-"}
                  </TableCell>
                  <TableCell align="right">
                    <Stack direction="row" spacing={1} justifyContent="flex-end">
                      <Button
                        size="small"
                        disabled={retryMut.isPending}
                        onClick={() => retryMut.mutate(row.id)}
                      >
                        {t("agent.webhook.dlqRetry")}
                      </Button>
                      <Button
                        size="small"
                        color="error"
                        disabled={discardMut.isPending}
                        onClick={() => discardMut.mutate(row.id)}
                      >
                        {t("agent.webhook.dlqDiscard")}
                      </Button>
                    </Stack>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
      {hasWebhook && (
        <Box>
          <Button
            size="small"
            onClick={() =>
              qc.invalidateQueries({ queryKey: ["agent-webhook-dlq", projectId] })
            }
          >
            {t("agent.webhook.dlqRefresh")}
          </Button>
        </Box>
      )}
    </Stack>
  );
}
