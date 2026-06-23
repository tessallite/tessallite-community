import { useState } from "react";
import { useT } from "../../i18n";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Collapse,
  IconButton,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import EditIcon from "@mui/icons-material/Edit";
import DeleteIcon from "@mui/icons-material/Delete";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import { aggregatesApi } from "../../api/client";
import type { AggregateDefinition, AggregateROI } from "../../api/types";
import { ui, statusColor } from "../../theme/tokens";
import { RefreshTriggerButton, RefreshRunHistory } from "../Refresh";
import { cronToLabel } from "../Refresh/FrequencyPicker";

type TFn = (key: string, vars?: Record<string, string>) => string;

function friendlyStatus(status: string, isStale: boolean, t: TFn): { label: string; severity: string } {
  if (isStale) return { label: t("aggCard.statusOutdated"), severity: "stale" };
  switch (status) {
    case "active":
      return { label: t("aggCard.statusActive"), severity: "active" };
    case "disabled":
      return { label: t("aggCard.statusDisabled"), severity: "default" };
    case "invalid":
      return { label: t("aggCard.statusNeedsAttention"), severity: "invalid" };
    case "retired":
      return { label: t("aggCard.statusRetired"), severity: "retired" };
    case "pending":
      return { label: t("aggCard.statusPending"), severity: "warning" };
    default:
      return { label: status, severity: "default" };
  }
}

function invalidHint(reason: string | null, t: TFn): string {
  if (!reason) return t("aggCard.hintDefault");
  const r = reason.toLowerCase();
  if (r.includes("measure") && (r.includes("deleted") || r.includes("no longer exists")))
    return t("aggCard.hintRestoreMeasure");
  if (r.includes("grain dimension") && r.includes("no longer exists"))
    return t("aggCard.hintRestoreDimension");
  if (r.includes("missing column") || r.includes("missing table"))
    return t("aggCard.hintRestoreColumn");
  if (r.includes("no longer reachable"))
    return t("aggCard.hintRestoreJoin");
  if (r.includes("no tables"))
    return t("aggCard.hintAddTables");
  return t("aggCard.hintDefault");
}

interface Props {
  agg: AggregateDefinition;
  projectId: string;
  modelId: string;
  violationCount?: number;
  roi?: AggregateROI;
  personaName?: string;
  scheduleCron?: string | null;
  onEdit: () => void;
  onDelete: () => void;
}

export default function AggregateCard({
  agg,
  projectId,
  modelId,
  violationCount = 0,
  roi,
  personaName,
  scheduleCron,
  onEdit,
  onDelete,
}: Props) {
  const t = useT();
  const [showHistory, setShowHistory] = useState(false);
  const [feedback, setFeedback] = useState<{ ok: boolean; msg: string } | null>(null);

  const status = friendlyStatus(agg.status, agg.is_stale, t);
  const colors = statusColor(status.severity);
  // Derived health. Prefer the backend-derived `health` (which is freshness-
  // aware: an active-but-stale/never-refreshed aggregate is unhealthy because
  // the query-router won't route it until rebuilt). Fall back to status + stale
  // for older cached rows: healthy = disabled, or active-and-not-stale.
  const health: "healthy" | "unhealthy" =
    agg.health ??
    (agg.status === "disabled" || (agg.status === "active" && !agg.is_stale)
      ? "healthy"
      : "unhealthy");
  // F-010-18 — a predictive aggregate whose grain later saw real query traffic
  // gets stamped by the feedback sweep; surface that as a "Validated" badge.
  const isValidatedPredictive =
    agg.creation_reason === "predictive" && Boolean(agg.predictive_validated_at);

  const runsQuery = useQuery({
    queryKey: ["aggregate-runs", projectId, modelId, agg.id],
    queryFn: () => aggregatesApi.getRuns(projectId, modelId, agg.id),
    enabled: showHistory,
  });

  return (
    <Card variant="outlined" data-testid={`aggregate-card-${agg.id}`}>
      <CardContent sx={{ py: 1, "&:last-child": { pb: 1 } }}>
        {/* Row 1: name + status + actions */}
        <Box display="flex" alignItems="center" gap={1}>
          <Typography variant="body2" fontWeight={600} noWrap sx={{ flexGrow: 1 }}>
            {agg.physical_table_name}
          </Typography>
          {violationCount > 0 && (
            <Tooltip title={t("aggCard.dataIssues", { count: String(violationCount) })}>
              <Chip
                label={violationCount}
                size="small"
                color="error"
                variant="outlined"
                data-testid={`agg-violations-${agg.id}`}
              />
            </Tooltip>
          )}
          {roi && (
            <Tooltip title={t("aggCard.usageScore", { score: String(roi.roi_score), hits: String(roi.hit_count) })}>
              <Chip
                label={t("aggCard.score", { score: String(roi.roi_score) })}
                size="small"
                color={roi.roi_score >= 1 ? "success" : "default"}
                variant="outlined"
              />
            </Tooltip>
          )}
          {isValidatedPredictive && (
            <Tooltip
              title={t("aggCard.predictiveValidatedTooltip", {
                date: new Date(agg.predictive_validated_at!).toLocaleString(),
              })}
            >
              <Chip
                label={t("aggCard.predictiveValidated")}
                size="small"
                color="success"
                variant="outlined"
                data-testid={`agg-predictive-validated-${agg.id}`}
                sx={{ fontSize: 10, height: 18 }}
              />
            </Tooltip>
          )}
          <Tooltip
            title={t(health === "healthy" ? "aggCard.healthHealthy" : "aggCard.healthUnhealthy")}
          >
            <Chip
              label={status.label}
              size="small"
              variant="outlined"
              data-testid={`agg-health-${agg.id}`}
              data-health={health}
              aria-label={`${status.label} (${health})`}
              sx={{
                color: colors.fg,
                borderColor: colors.fg,
                bgcolor: colors.bg,
                fontWeight: 600,
                fontSize: 10,
                height: 20,
              }}
            />
          </Tooltip>
          {personaName && (
            <Chip
              label={personaName}
              size="small"
              variant="outlined"
              sx={{ fontSize: 10, height: 18 }}
            />
          )}
          <Tooltip title={t("aggCard.editTooltip")}>
            <IconButton size="small" onClick={onEdit} data-testid={`agg-edit-${agg.id}`}>
              <EditIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        </Box>

        {/* Row 2: grain + measures */}
        <Typography variant="caption" color="text.secondary" display="block" mt={0.25}>
          {t("aggCard.groupedBy", { grain: agg.grain.join(", ") })}
        </Typography>
        {agg.measure_names && agg.measure_names.length > 0 && (
          <Typography variant="caption" color="text.secondary" display="block">
            {t("aggCard.measures", { names: agg.measure_names.join(", ") })}
          </Typography>
        )}

        {/* Row 3: schedule info */}
        {scheduleCron !== undefined && (
          <Typography variant="caption" color="text.secondary" display="block">
            {t("aggCard.rebuilds", { schedule: cronToLabel(scheduleCron) })}
          </Typography>
        )}

        {/* Invalid state */}
        {agg.status === "invalid" && agg.invalid_reason && (
          <Box
            sx={{
              mt: 1,
              p: 1,
              borderRadius: 1,
              bgcolor: ui.redBg,
              border: "1px solid",
              borderColor: "error.light",
            }}
          >
            <Typography variant="caption" fontWeight={600} color="error.main" display="block">
              {agg.invalid_reason}
            </Typography>
            <Typography variant="caption" color="text.secondary" display="block" mt={0.25}>
              {invalidHint(agg.invalid_reason, t)}
            </Typography>
          </Box>
        )}

        {/* Action buttons */}
        <Stack direction="row" spacing={0.5} mt={1} alignItems="center">
          <RefreshTriggerButton
            entityId={agg.id}
            modelId={modelId}
            projectId={projectId}
            entityType="aggregate"
            mode="full"
            label={t("aggCard.rebuildNow")}
            size="small"
            variant="outlined"
            onResult={(ok, msg) => setFeedback({ ok, msg })}
          />
          <Button
            size="small"
            onClick={() => setShowHistory(!showHistory)}
            endIcon={showHistory ? <ExpandLessIcon /> : <ExpandMoreIcon />}
            data-testid={`agg-history-toggle-${agg.id}`}
          >
            {t("aggCard.history")}
          </Button>
          <Box flexGrow={1} />
          <Tooltip title={t("aggCard.deleteTooltip")}>
            <IconButton size="small" onClick={onDelete} data-testid={`agg-delete-${agg.id}`}>
              <DeleteIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        </Stack>

        {/* Feedback */}
        {feedback && (
          <Alert
            severity={feedback.ok ? "success" : "warning"}
            sx={{ mt: 0.75 }}
            onClose={() => setFeedback(null)}
          >
            {feedback.msg}
          </Alert>
        )}

        {/* Collapsible run history */}
        <Collapse in={showHistory}>
          <Box sx={{ mt: 1 }}>
            {runsQuery.isLoading ? (
              <Typography variant="caption" color="text.secondary">{t("aggCard.loading")}</Typography>
            ) : (
              <RefreshRunHistory runs={runsQuery.data ?? []} limit={5} />
            )}
          </Box>
        </Collapse>
      </CardContent>
    </Card>
  );
}
