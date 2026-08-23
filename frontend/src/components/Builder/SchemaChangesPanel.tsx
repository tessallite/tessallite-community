import { useMemo } from "react";
import { useT } from "../../i18n";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  Stack,
  Typography,
} from "@mui/material";
import { useParams } from "react-router-dom";
import { schemaChangesApi } from "../../api/client";

export default function SchemaChangesPanel() {
  const t = useT();
  const { projectId, modelId } = useParams<{ projectId: string; modelId: string }>();
  const qc = useQueryClient();

  const events = useQuery({
    queryKey: ["schema-changes", projectId, modelId],
    queryFn: () => schemaChangesApi.list(projectId!, modelId!),
    enabled: !!projectId && !!modelId,
  });

  const acknowledge = useMutation({
    mutationFn: (eventId: string) =>
      schemaChangesApi.acknowledge(projectId!, modelId!, eventId),
    onSuccess: () =>
      qc.invalidateQueries({
        queryKey: ["schema-changes", projectId, modelId],
      }),
  });

  const unresolved = useMemo(
    () => (events.data ?? []).filter((e) => !e.acknowledged_at),
    [events.data],
  );
  const resolved = useMemo(
    () => (events.data ?? []).filter((e) => e.acknowledged_at),
    [events.data],
  );

  if (events.isLoading) return <CircularProgress size={20} sx={{ m: 2 }} />;

  return (
    <Box sx={{ p: 2 }}>
      <Stack direction="row" alignItems="center" gap={1} mb={1}>
        <Typography variant="subtitle2" fontWeight={700}>
          {t("schemaChanges.title")}
        </Typography>
        {unresolved.length > 0 && (
          <Chip label={unresolved.length} color="warning" size="small" />
        )}
      </Stack>

      {unresolved.length === 0 && resolved.length === 0 && (
        <Typography variant="body2" color="text.secondary">
          {t("schemaChanges.noDrift")}
        </Typography>
      )}

      {unresolved.map((e) => (
        <Alert
          key={e.id}
          severity={e.is_breaking ? "error" : "warning"}
          sx={{ mb: 1 }}
          action={
            <Button
              size="small"
              onClick={() => acknowledge.mutate(e.id)}
              disabled={acknowledge.isPending}
            >
              {t("schemaChanges.acknowledge")}
            </Button>
          }
        >
          <strong>{e.table_name ?? t("schemaChanges.unknownTable")}</strong>{" "}
            <Chip
            label={t(`schemaChanges.${e.change_type}`)}
            size="small"
            sx={{ ml: 0.5 }}
          />
          {Boolean(
            (e.detail as Record<string, unknown> | null | undefined)?.column_name
              ?? (e.detail as Record<string, unknown> | null | undefined)?.column,
          ) && (
            <Typography variant="caption" display="block">
              {t("schemaChanges.column", {
                name: String(
                  (e.detail as Record<string, unknown>).column_name
                    ?? (e.detail as Record<string, unknown>).column,
                ),
              })}
            </Typography>
          )}
          {e.detected_at && (
            <Typography variant="caption" color="text.secondary">
              {new Date(e.detected_at).toLocaleString()}
            </Typography>
          )}
        </Alert>
      ))}

      {resolved.length > 0 && (
        <>
          <Divider sx={{ my: 1 }} />
          <Typography variant="caption" color="text.secondary">
            {t("schemaChanges.acknowledged", { count: String(resolved.length) })}
          </Typography>
        </>
      )}
    </Box>
  );
}
