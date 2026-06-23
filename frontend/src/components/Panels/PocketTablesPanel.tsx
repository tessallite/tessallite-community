import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";

import { Divider } from "@mui/material";

import { dataQualityApi, pocketsApi } from "../../api/client";
import { usePockets } from "../../api/hooks";
import type { PocketDefinition } from "../../api/types";
import { canEditModelConfig } from "../../auth/currentUser";
import { useBuilderStore } from "../../store/builderStore";
import { useConfirm } from "../Confirm";
import { RefreshTriggerButton } from "../Refresh";
import PocketDrawer from "./PocketDrawer";
import PocketSuggestionsPanel from "./PocketSuggestionsPanel";

const STATUS_COLOR: Record<string, "success" | "default" | "error" | "warning" | "info"> = {
  fresh: "success",
  invalidating: "info",
  stale: "warning",
  failed: "error",
};

export default function PocketTablesPanel() {
  const { projectId, modelId } = useParams<{ projectId: string; modelId: string }>();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const t = useT();

  const pockets = usePockets(projectId!, modelId!);
  const storeReadOnly = useBuilderStore((s) => s.readOnly);
  const canManagePockets = canEditModelConfig() && !storeReadOnly;

  const { data: pocketViolations = {} } = useQuery<Record<string, number>>({
    queryKey: ["pocket-violations", projectId, modelId],
    queryFn: () => dataQualityApi.pocketViolationSummary(projectId!, modelId!),
    staleTime: 60_000,
  });

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerMode, setDrawerMode] = useState<"create" | "edit">("create");
  const [selected, setSelected] = useState<PocketDefinition | null>(null);
  // F-005-13: SQL to prefill the create drawer when acting on a suggestion.
  const [initialSql, setInitialSql] = useState<string>("");

  // F-005-08: pocket payoff metrics (hit ratio, time saved, storage, evictions).
  const { data: metrics } = useQuery({
    queryKey: ["metrics", projectId, modelId],
    queryFn: () => pocketsApi.getMetrics(projectId!, modelId!),
    enabled: Boolean(projectId && modelId),
    staleTime: 30_000,
  });

  // F-005-22: map pocket_id → matched_since_refresh so each fresh-but-idle
  // pocket gets a "no matches since refresh" badge, and surface the model-level
  // top skip reason when at least one fresh pocket is going unused.
  const matchedSinceRefresh = new Map<string, boolean>(
    (metrics?.top_pockets ?? []).map((p) => [p.pocket_id, p.matched_since_refresh !== false]),
  );

  const deletePocket = useMutation({
    mutationFn: (pocketId: string) => pocketsApi.delete(projectId!, modelId!, pocketId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pockets", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["metrics", projectId, modelId] });
    },
  });

  function openCreate() {
    setDrawerMode("create");
    setSelected(null);
    setInitialSql("");
    setDrawerOpen(true);
  }

  // F-005-13: open the create drawer prefilled with a suggestion's model-subset
  // SQL. On save it goes through the normal validated create path (predicates
  // re-derived server-side), so the suggestion stops being display-only.
  function openCreateFromSuggestion(definingSql: string) {
    setDrawerMode("create");
    setSelected(null);
    setInitialSql(definingSql);
    setDrawerOpen(true);
  }

  function openEdit(pocket: PocketDefinition) {
    setDrawerMode("edit");
    setSelected(pocket);
    setDrawerOpen(true);
  }

  async function handleDelete(pocket: PocketDefinition) {
    const ok = await confirm({
      title: t("pocketTables.deleteConfirmTitle"),
      message: (
        <span>
          {t("pocketTables.deleteMessage", { table: pocket.physical_table_name })}
        </span>
      ),
      confirmLabel: t("pocketTables.deleteConfirm"),
    });
    if (ok) deletePocket.mutate(pocket.id);
  }

  return (
    <Box>
      <Box display="flex" gap={1} mb={1.5}>
        <Box flexGrow={1} />
        {canManagePockets && (
          <Button size="small" variant="contained" startIcon={<AddIcon />} onClick={openCreate}>
            {t("pocketTables.createButton")}
          </Button>
        )}
      </Box>

      {metrics && metrics.total_pockets > 0 && (
        <Card variant="outlined" sx={{ mb: 1 }}>
          <CardContent sx={{ py: 1.5 }}>
            <Box display="flex" flexWrap="wrap" gap={3}>
              <MetricTile
                label={t("pocketTables.metrics.hitRatio")}
                value={`${(metrics.pocket_hit_rate * 100).toFixed(1)}%`}
              />
              <MetricTile
                label={t("pocketTables.metrics.timeSaved")}
                value={fmtMs(metrics.pocket_time_saved_ms)}
              />
              <MetricTile
                label={t("pocketTables.metrics.storage")}
                value={fmtBytes(metrics.pocket_storage_bytes)}
              />
              <MetricTile
                label={t("pocketTables.metrics.evictions24h")}
                value={String(metrics.pocket_evictions_24h)}
              />
            </Box>
          </CardContent>
        </Card>
      )}

      <Alert severity="info" sx={{ mb: 1 }}>
        {t("pocketTables.infoAlert")}
      </Alert>
      {/* F-005-22: warn when fresh pockets are not matching any traffic. */}
      {metrics && (metrics.zero_match_fresh_pockets ?? 0) > 0 && (
        <Alert severity="warning" sx={{ mb: 1 }}>
          {t("pocketTables.zeroMatchAlert", {
            count: String(metrics.zero_match_fresh_pockets ?? 0),
          })}
          {metrics.top_skip_reason
            ? " " +
              t("pocketTables.zeroMatchSkipReason", {
                reason: metrics.top_skip_reason,
                count: String(metrics.top_skip_count ?? 0),
              })
            : ""}
        </Alert>
      )}
      {!canManagePockets && (
        <Alert severity="warning" sx={{ mb: 1 }}>
          {t("pocketTables.readOnlyAlert")}
        </Alert>
      )}

      {pockets.isLoading ? (
        <CircularProgress size={20} />
      ) : (pockets.data?.length ?? 0) === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t("pocketTables.emptyMessage")}
        </Typography>
      ) : (
        <Stack spacing={1}>
          {(pockets.data ?? []).map((pocket) => (
            <Card key={pocket.id} variant="outlined">
              <CardContent sx={{ py: 1 }}>
                <Box display="flex" alignItems="center" gap={1}>
                  <Box flexGrow={1}>
                    <Typography variant="body2" fontWeight={600}>
                      {pocket.physical_table_name}
                    </Typography>
                  </Box>

                  <Chip
                    label={pocket.status}
                    size="small"
                    color={STATUS_COLOR[pocket.status] ?? "default"}
                  />
                  {/* F-005-22: flag a fresh pocket that has not matched since refresh. */}
                  {pocket.status === "fresh" &&
                    matchedSinceRefresh.get(pocket.id) === false && (
                      <Tooltip title={t("pocketTables.noMatchTooltip")}>
                        <Chip
                          label={t("pocketTables.noMatchBadge")}
                          size="small"
                          color="warning"
                          variant="outlined"
                        />
                      </Tooltip>
                    )}
                  <Chip label={t("pocketTables.hitsLabel", { count: pocket.hit_count.toString() })} size="small" variant="outlined" />
                  {pocketViolations[pocket.id] > 0 && (
                    <Tooltip title={t("pocketTables.violationsTooltip", { count: pocketViolations[pocket.id].toString() })}>
                      <Chip
                        label={pocketViolations[pocket.id]}
                        size="small"
                        color="error"
                        variant="outlined"
                      />
                    </Tooltip>
                  )}

                  <Button
                    size="small"
                    startIcon={<EditIcon />}
                    onClick={() => openEdit(pocket)}
                    disabled={!canManagePockets}
                  >
                    {t("common.edit")}
                  </Button>
                  <RefreshTriggerButton
                    entityId={pocket.id}
                    modelId={modelId!}
                    projectId={projectId!}
                    entityType="pocket"
                    label={t("pocketTables.rebuildButton")}
                    size="small"
                    variant="text"
                  />
                  <Button
                    size="small"
                    startIcon={<DeleteIcon />}
                    onClick={() => void handleDelete(pocket)}
                    disabled={!canManagePockets || deletePocket.isPending}
                  >
                    {t("common.delete")}
                  </Button>
                </Box>

                {pocket.failure_reason && (
                  <Typography variant="caption" color="error" display="block" mt={0.5}>
                    {pocket.failure_reason}
                  </Typography>
                )}

                {pocket.predicates?.length ? (
                  <Box display="flex" flexWrap="wrap" gap={0.5} mt={0.5}>
                    {pocket.predicates.map((p) => {
                      const val = (p.value_json as { value?: unknown })?.value;
                      const label = Array.isArray(val)
                        ? `${p.column_name} ${p.operator} [${val.join(", ")}]`
                        : `${p.column_name} ${p.operator} ${val === null || val === undefined ? t("common.nullValue") : String(val)}`;
                      return <Chip key={p.id} size="small" variant="outlined" label={label} />;
                    })}
                  </Box>
                ) : null}

                <Typography variant="caption" color="text.secondary" display="block" mt={0.5}>
                  {t("pocketTables.lastRefreshLabel")}: {pocket.last_refresh_at ? new Date(pocket.last_refresh_at).toLocaleString() : t("pocketTables.neverRefreshed")}
                </Typography>
              </CardContent>
            </Card>
          ))}
        </Stack>
      )}

      <Divider sx={{ my: 3 }} />

      <PocketSuggestionsPanel
        modelId={modelId!}
        onCreate={canManagePockets ? openCreateFromSuggestion : undefined}
      />

      <PocketDrawer
        open={drawerOpen}
        mode={drawerMode}
        projectId={projectId!}
        modelId={modelId!}
        pocket={selected}
        prefillSql={initialSql}
        onClose={() => setDrawerOpen(false)}
      />
    </Box>
  );
}

function MetricTile({ label, value }: { label: string; value: string }) {
  return (
    <Box>
      <Typography variant="caption" color="text.secondary" display="block">
        {label}
      </Typography>
      <Typography variant="h6" fontWeight={600}>
        {value}
      </Typography>
    </Box>
  );
}

function fmtBytes(b: number): string {
  if (b >= 1_073_741_824) return `${(b / 1_073_741_824).toFixed(1)} GB`;
  if (b >= 1_048_576) return `${(b / 1_048_576).toFixed(1)} MB`;
  if (b >= 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${b} B`;
}

function fmtMs(ms: number): string {
  if (ms >= 3_600_000) return `${(ms / 3_600_000).toFixed(1)} h`;
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)} min`;
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)} s`;
  return `${ms} ms`;
}
