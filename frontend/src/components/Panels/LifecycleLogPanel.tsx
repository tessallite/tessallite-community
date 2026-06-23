import { useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Card,
  CardContent,
  CircularProgress,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import { optimizerApiClient } from "../../api/client";
import type {
  AggregateLifecycleEvent,
  AggregateLifecycleResponse,
} from "../../api/types";
import { statusColor } from "../../theme/tokens";
import { useT } from "../../i18n";

const EVENT_TYPES = [
  "all",
  "created",
  "approved",
  "validated",
  "retired",
  "retired_unused",
  "retired_idle",
  "purged",
  "refresh_failed",
] as const;

type EventFilter = (typeof EVENT_TYPES)[number];

function shortId(id: string | null): string {
  if (!id) return "—";
  return id.slice(0, 8);
}

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function formatPayload(payload: Record<string, unknown>): string {
  if (!payload || Object.keys(payload).length === 0) return "—";
  return Object.entries(payload)
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(", ");
}

export default function LifecycleLogPanel() {
  const t = useT();
  const { modelId } = useParams<{ modelId: string }>();
  const [eventType, setEventType] = useState<EventFilter>("all");

  const query = useQuery<AggregateLifecycleResponse>({
    queryKey: ["aggregate-lifecycle", modelId, eventType],
    queryFn: () =>
      optimizerApiClient.getAggregateLifecycle(modelId!, {
        eventType: eventType === "all" ? undefined : eventType,
      }),
    enabled: Boolean(modelId),
  });

  const events: AggregateLifecycleEvent[] = useMemo(
    () => query.data?.events ?? [],
    [query.data],
  );

  if (!modelId) {
    return (
      <Box sx={{ p: 2 }}>
        <Alert severity="error">{t("lifecycleLog.noModelError")}</Alert>
      </Box>
    );
  }

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <Box display="flex" alignItems="center" gap={1}>
        <Typography variant="body2" color="text.secondary" flexGrow={1}>
          {t("lifecycleLog.description")}
        </Typography>
        <FormControl size="small" sx={{ minWidth: 180 }}>
          <InputLabel id="event-type-label">{t("lifecycleLog.eventTypeLabel")}</InputLabel>
          <Select
            labelId="event-type-label"
            label={t("lifecycleLog.eventTypeLabel")}
            value={eventType}
            onChange={(e) => setEventType(e.target.value as EventFilter)}
          >
            {EVENT_TYPES.map((ev) => (
              <MenuItem key={ev} value={ev}>
                {t(`lifecycleLog.eventType.${ev}`)}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      </Box>

      {query.isLoading && (
        <Box sx={{ p: 4, display: "flex", justifyContent: "center" }}>
          <CircularProgress size={22} />
        </Box>
      )}

      {query.isError && (
        <Alert severity="error">
          {t("lifecycleLog.loadError", {
            error: String((query.error as Error)?.message ?? "unknown error"),
          })}
        </Alert>
      )}

      {!query.isLoading && !query.isError && events.length === 0 && (
        <Alert severity="info">{t("lifecycleLog.noEvents")}</Alert>
      )}

      {events.length > 0 && (
        <Card variant="outlined">
          <CardContent sx={{ p: 0, "&:last-child": { pb: 0 } }}>
            <TableContainer>
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    <TableCell sx={{ fontWeight: 600 }}>{t("lifecycleLog.whenHeader")}</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>{t("lifecycleLog.eventHeader")}</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>{t("lifecycleLog.aggregateHeader")}</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>{t("lifecycleLog.reasonHeader")}</TableCell>
                    <TableCell sx={{ fontWeight: 600 }}>{t("lifecycleLog.detailHeader")}</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {events.map((ev) => (
                    <TableRow key={ev.id} hover>
                      <TableCell sx={{ whiteSpace: "nowrap" }}>
                        {formatTimestamp(ev.occurred_at)}
                      </TableCell>
                      <TableCell>
                        {(() => { const sc = statusColor(ev.event_type); return (
                          <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, fontWeight: 500, fontSize: 11, bgcolor: sc.bg, color: sc.fg }}>{t(`lifecycleLog.eventType.${ev.event_type}`)}</Typography>
                        ); })()}
                      </TableCell>
                      <TableCell sx={{ fontFamily: "monospace" }}>
                        {shortId(ev.aggregate_id)}
                      </TableCell>
                      <TableCell>{ev.reason ?? t("common.na")}</TableCell>
                      <TableCell sx={{ maxWidth: 480 }}>
                        <Typography variant="caption" color="text.secondary">
                          {formatPayload(ev.payload)}
                        </Typography>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          </CardContent>
        </Card>
      )}
    </Box>
  );
}
