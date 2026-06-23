import { useCallback, useEffect, useMemo, useState } from "react";
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
  IconButton,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import AutoFixHighIcon from "@mui/icons-material/AutoFixHigh";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import VerifiedIcon from "@mui/icons-material/Verified";
import BlockIcon from "@mui/icons-material/Block";
import TrendingUpIcon from "@mui/icons-material/TrendingUp";
import TrendingDownIcon from "@mui/icons-material/TrendingDown";
import TrendingFlatIcon from "@mui/icons-material/TrendingFlat";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import WarningIcon from "@mui/icons-material/Warning";
import ErrorIcon from "@mui/icons-material/Error";

import StarIcon from "@mui/icons-material/Star";
import StarBorderIcon from "@mui/icons-material/StarBorder";
import AccessTimeIcon from "@mui/icons-material/AccessTime";

import { kpisApi, measuresApi, preferencesApi } from "../../api/client";
import type { Kpi, KpiEvaluateResponse } from "../../api/types";
import { useDimensions, useUserPreferences } from "../../api/hooks";
import { canEditModelConfig, isTenantAdmin } from "../../auth/currentUser";
import { useBuilderStore } from "../../store/builderStore";
import { useConfirm } from "../Confirm";
import TemplateGalleryDialog from "./TemplateGalleryDialog";
import type { KpiTemplate } from "./templates";
import { KpiWizard } from "../KpiWizard";
import type { KpiTemplateInit } from "../KpiWizard";
import { KpiBusinessBuilderDialog } from "../KpiBusinessBuilder/KpiBusinessBuilderDialog";
import { palette, ui } from "../../theme/tokens";
import { resolveKpiDisplayStatus } from "../KpiScorecard/statusUtils";

const CERT_COLORS: Record<string, "success" | "warning" | "default" | "info"> = {
  certified: "success",
  shared: "info",
  draft: "default",
  deprecated: "warning",
};

// Derive a technical name (slug) from a free-text display name.
export function slugify(s: string): string {
  return (
    s
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "") || "kpi"
  );
}

function StatusIcon({ status }: { status: number | null }) {
  if (status === null) return null;
  if (status >= 1) return <CheckCircleIcon fontSize="small" sx={{ color: ui.green }} />;
  if (status === 0) return <WarningIcon fontSize="small" sx={{ color: ui.goldDark }} />;
  return <ErrorIcon fontSize="small" sx={{ color: ui.red }} />;
}

function TrendIcon({ trend }: { trend: number | null }) {
  if (trend === null) return null;
  if (trend >= 1) return <TrendingUpIcon fontSize="small" sx={{ color: ui.green }} />;
  if (trend === 0) return <TrendingFlatIcon fontSize="small" sx={{ color: ui.muted }} />;
  return <TrendingDownIcon fontSize="small" sx={{ color: ui.red }} />;
}

export default function KpisPanel() {
  const t = useT();
  const { projectId = "", modelId = "" } = useParams();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const storeReadOnly = useBuilderStore((s) => s.readOnly);
  const canEdit = canEditModelConfig() && !storeReadOnly;

  const [templateGalleryOpen, setTemplateGalleryOpen] = useState(false);
  const [evalData, setEvalData] = useState<Record<string, KpiEvaluateResponse>>({});
  const [evalLoading, setEvalLoading] = useState<Record<string, boolean>>({});

  const { data: prefs, refetch: refetchPrefs } = useUserPreferences(projectId, modelId);
  const favouriteIds = useMemo(() => new Set(prefs?.favourites?.kpi ?? []), [prefs]);
  const recentIds = useMemo(() => prefs?.recently_used?.kpi ?? [], [prefs]);

  const toggleFavourite = useCallback(
    async (kpiId: string) => {
      await preferencesApi.toggleFavourite(projectId, modelId, { entity_type: "kpi", entity_id: kpiId });
      refetchPrefs();
    },
    [projectId, modelId, refetchPrefs],
  );

  const recordRecentlyUsed = useCallback(
    (kpiId: string) => {
      preferencesApi.recordRecentlyUsed(projectId, modelId, { entity_type: "kpi", entity_id: kpiId })
        .then(() => refetchPrefs())
        .catch(() => {});
    },
    [projectId, modelId, refetchPrefs],
  );

  const queryKey = ["kpis", projectId, modelId];

  const { data: kpis = [], isLoading } = useQuery({
    queryKey,
    queryFn: () => kpisApi.list(projectId, modelId),
    enabled: Boolean(projectId && modelId),
  });

  const { data: measures = [] } = useQuery({
    queryKey: ["measures", projectId, modelId],
    queryFn: () => measuresApi.list(projectId, modelId),
    enabled: Boolean(projectId && modelId),
  });

  const { data: dimensions = [] } = useDimensions(projectId, modelId);

  // Business builder state
  const [bbOpen, setBbOpen] = useState(false);
  const [bbEditKpi, setBbEditKpi] = useState<Kpi | null>(null);

  // V2 wizard state
  const [v2WizardOpen, setV2WizardOpen] = useState(false);
  const [v2EditKpi, setV2EditKpi] = useState<Kpi | null>(null);
  const [v2Template, setV2Template] = useState<KpiTemplateInit | null>(null);

  const deleteMut = useMutation({
    mutationFn: (id: string) => kpisApi.delete(projectId, modelId, id),
    onSuccess: () => qc.invalidateQueries({ queryKey }),
  });

  const isAdmin = isTenantAdmin();

  const handleEvaluate = useCallback(
    async (kpiId: string) => {
      setEvalLoading((prev) => ({ ...prev, [kpiId]: true }));
      try {
        const result = await kpisApi.evaluate(projectId, modelId, kpiId);
        setEvalData((prev) => ({ ...prev, [kpiId]: result }));
      } catch {
        setEvalData((prev) => ({
          ...prev,
          [kpiId]: {
            kpi_id: null,
            value: null,
            value_str: null,
            target: null,
            goal: null,
            status: null,
            status_label: null,
            status_color: null,
            trend: null,
            trend_label: null,
            trend_pct: null,
            formatted_value: null,
            formatted_target: null,
            formatted_goal: null,
            formatted_variance: null,
            trend_series: null,
            evaluation_ms: null,
            compiled_expression: null,
            compiled_scope: null,
          },
        }));
      } finally {
        setEvalLoading((prev) => ({ ...prev, [kpiId]: false }));
      }
    },
    [projectId, modelId],
  );

  useEffect(() => {
    for (const kpi of kpis) {
      if (!evalData[kpi.id] && !evalLoading[kpi.id]) {
        handleEvaluate(kpi.id);
      }
    }
  }, [kpis, evalData, evalLoading, handleEvaluate]);

  function openCreate() {
    setBbEditKpi(null);
    setBbOpen(true);
  }

  function openAdvancedCreate() {
    setV2EditKpi(null);
    setV2Template(null);
    setV2WizardOpen(true);
  }

  function applyKpiTemplate(template: KpiTemplate) {
    setV2EditKpi(null);
    setV2Template({
      name: template.name,
      display_name: template.display_name,
      description: template.description,
      display_folder: template.display_folder,
      expression: template.expression,
      kpi_type: template.kpi_type,
      direction: template.direction,
      status_graphic: template.status_graphic,
      trend_graphic: template.trend_graphic,
    });
    setV2WizardOpen(true);
  }

  function openEdit(kpi: Kpi) {
    recordRecentlyUsed(kpi.id);
    if (kpi.business_definition) {
      setBbEditKpi(kpi);
      setBbOpen(true);
    } else {
      setV2EditKpi(kpi);
      setV2Template(null);
      setV2WizardOpen(true);
    }
  }

  async function handleDelete(kpi: Kpi) {
    const ok = await confirm({
      title: t("kpis.deleteConfirm"),
      message: t("kpis.deleteMessage", { name: kpi.display_name || kpi.name }),
      confirmLabel: t("common.delete"),
    });
    if (ok) deleteMut.mutate(kpi.id);
  }

  return (
    <Box sx={{ p: 2, overflow: "auto" }}>
      <Box display="flex" alignItems="center" justifyContent="space-between" mb={2}>
        <Typography variant="subtitle1" fontWeight={700}>
          {t("kpis.title")}
        </Typography>
        {canEdit && (
          <Box display="flex" gap={1}>
            <Button size="small" startIcon={<AutoFixHighIcon />} variant="outlined" onClick={() => setTemplateGalleryOpen(true)}>
              {t("kpis.fromTemplate")}
            </Button>
            <Button size="small" variant="outlined" onClick={openAdvancedCreate}>
              {t("kpis.advancedKpi")}
            </Button>
            <Button size="small" startIcon={<AddIcon />} variant="contained" onClick={openCreate}>
              {t("kpis.add")}
            </Button>
          </Box>
        )}
      </Box>

      <Typography variant="body2" color="text.secondary" mb={2}>
        {t("kpis.description")}
      </Typography>

      {isLoading && <CircularProgress size={20} />}

      {!isLoading && kpis.length === 0 && (
        <Typography variant="body2" color="text.secondary">
          {t("kpis.empty")}
        </Typography>
      )}

      {(() => {
        const favKpis = kpis.filter((k: Kpi) => favouriteIds.has(k.id));
        const recentKpis = kpis.filter((k: Kpi) => !favouriteIds.has(k.id) && recentIds.includes(k.id));
        const otherKpis = kpis.filter((k: Kpi) => !favouriteIds.has(k.id) && !recentIds.includes(k.id));
        const sections: { label: string | null; items: Kpi[]; icon?: typeof StarIcon }[] = [];
        if (favKpis.length > 0) sections.push({ label: t("kpis.favourites"), items: favKpis, icon: StarIcon });
        if (recentKpis.length > 0) sections.push({ label: t("kpis.recentlyUsed"), items: recentKpis, icon: AccessTimeIcon });
        if (otherKpis.length > 0 || sections.length === 0) {
          sections.push({ label: sections.length > 0 ? t("kpis.allKpis") : null, items: otherKpis });
        }
        return sections.map((section, si) => (
          <Box key={si} sx={{ mb: 2 }}>
            {section.label && (
              <Stack direction="row" alignItems="center" spacing={0.5} sx={{ mb: 1 }}>
                {section.icon && (() => { const SIcon = section.icon; return <SIcon sx={{ fontSize: 16, color: "text.secondary" }} />; })()}
                <Typography variant="caption" fontWeight={600} color="text.secondary" sx={{ textTransform: "uppercase", fontSize: 11 }}>
                  {section.label}
                </Typography>
              </Stack>
            )}
            <Stack spacing={1.5}>
              {section.items.map((kpi: Kpi) => {
          const ev = evalData[kpi.id];
          const evLoading = evalLoading[kpi.id];
          const isFav = favouriteIds.has(kpi.id);
          return (
            <Card key={kpi.id} variant="outlined" sx={{ borderColor: palette.slateBorder, borderRadius: 2, "&:hover": { borderColor: ui.green, boxShadow: 1 }, transition: "border-color 0.2s, box-shadow 0.2s" }}>
              <CardContent sx={{ py: 1.5, "&:last-child": { pb: 1.5 } }}>
                <Box display="flex" alignItems="center" justifyContent="space-between">
                  <Box display="flex" alignItems="center" gap={0.5}>
                    <Tooltip title={isFav ? t("kpis.removeFromFavourites") : t("kpis.addToFavourites")}>
                      <IconButton size="small" onClick={() => toggleFavourite(kpi.id)} sx={{ p: 0.25 }}>
                        {isFav ? <StarIcon sx={{ fontSize: 18, color: "#f9a825" }} /> : <StarBorderIcon sx={{ fontSize: 18, color: "text.secondary" }} />}
                      </IconButton>
                    </Tooltip>
                    <Box>
                    <Typography variant="subtitle2">
                      {kpi.display_name || kpi.name}
                    </Typography>
                    {kpi.display_name && (
                      <Typography variant="caption" color="text.secondary" fontFamily="monospace">
                        {kpi.name}
                      </Typography>
                    )}
                    </Box>
                  </Box>
                  <Box display="flex" alignItems="center" gap={0.5}>
                    <Chip label={kpi.status_graphic} size="small" variant="outlined" />
                    {kpi.certification_status !== "draft" && (
                      <Chip
                        icon={kpi.certification_status === "certified" ? <VerifiedIcon /> : undefined}
                        label={kpi.certification_status}
                        size="small"
                        color={CERT_COLORS[kpi.certification_status] ?? "default"}
                      />
                    )}
                    {canEdit && (
                      <>
                        <Button size="small" startIcon={<EditIcon />} onClick={() => openEdit(kpi)}>
                          {t("common.edit")}
                        </Button>
                        <Button size="small" startIcon={<DeleteIcon />} onClick={() => handleDelete(kpi)}>
                          {t("common.delete")}
                        </Button>
                      </>
                    )}
                  </Box>
                </Box>
                {kpi.description && (
                  <Typography variant="body2" color="text.secondary" mt={0.5}>
                    {kpi.description}
                  </Typography>
                )}
                {(kpi.kpi_type || kpi.weight !== null) && (
                <Box display="flex" gap={2} mt={1} flexWrap="wrap" alignItems="center">
                  {kpi.kpi_type && (
                    <Typography variant="caption" color="text.secondary">
                      {t("kpis.type")}: {kpi.kpi_type}
                    </Typography>
                  )}
                  {kpi.weight !== null && (
                    <Typography variant="caption" color="text.secondary">
                      {t("kpis.weight")}: {kpi.weight}
                    </Typography>
                  )}
                </Box>
                )}
                {/* Live evaluate data */}
                {evLoading && (
                  <Box mt={1}>
                    <CircularProgress size={14} />
                  </Box>
                )}
                {ev && !evLoading && (
                  <Box display="flex" gap={2} mt={1} flexWrap="wrap" alignItems="center">
                    {ev.formatted_value && (
                      <Chip
                        size="small"
                        label={`${t("kpiScorecard.valueLabel")}: ${ev.formatted_value}`}
                        variant="outlined"
                      />
                    )}
                    {ev.formatted_goal && (
                      <Chip
                        size="small"
                        label={`${t("kpiScorecard.goalLabel")}: ${ev.formatted_goal}`}
                        variant="outlined"
                      />
                    )}
                    {ev.status_label && (() => {
                      // Custom (absolute_value) bands can be authored in either
                      // direction, so the numeric status int is unreliable. Trust
                      // the matched band's own colour and label-derived status.
                      const dispStatus = resolveKpiDisplayStatus(ev.status, ev.status_label);
                      const statusColor =
                        ev.status_color ||
                        (dispStatus === 1 ? ui.green : dispStatus === 0 ? ui.goldDark : dispStatus === -1 ? ui.red : ui.muted);
                      return (
                        <Chip
                          size="small"
                          icon={<StatusIcon status={dispStatus} />}
                          label={ev.status_label}
                          variant="outlined"
                          sx={{ borderColor: statusColor, color: statusColor }}
                        />
                      );
                    })()}
                    {ev.trend !== null && ev.trend !== undefined && ev.trend_label && (
                      <Chip
                        size="small"
                        icon={<TrendIcon trend={ev.trend} />}
                        label={ev.trend_label}
                        variant="outlined"
                      />
                    )}
                  </Box>
                )}
                {kpi.certification_status === "deprecated" && (
                  <Alert severity="warning" sx={{ mt: 1, py: 0 }} icon={<BlockIcon fontSize="small" />}>
                    {t("kpis.deprecatedWarning")}
                  </Alert>
                )}
                {kpi.business_definition?._compiled?.summary && (
                  <Typography variant="caption" color="text.secondary" mt={0.5} display="block" fontStyle="italic">
                    {kpi.business_definition._compiled.summary}
                  </Typography>
                )}
                {kpi.display_folder && (
                  <Typography variant="caption" color="text.secondary" mt={0.5} display="block">
                    {t("kpis.folder")}: {kpi.display_folder}
                  </Typography>
                )}
              </CardContent>
            </Card>
          );
        })}
            </Stack>
          </Box>
        ));
      })()}

      <KpiBusinessBuilderDialog
        open={bbOpen}
        onClose={() => { setBbOpen(false); setBbEditKpi(null); }}
        onSaved={() => qc.invalidateQueries({ queryKey })}
        projectId={projectId}
        modelId={modelId}
        measures={measures}
        dimensions={dimensions}
        editKpi={bbEditKpi}
        onOpenAdvanced={(kpi) => {
          setBbOpen(false);
          setBbEditKpi(null);
          setV2EditKpi(kpi);
          setV2Template(null);
          setV2WizardOpen(true);
        }}
      />

      <KpiWizard
        open={v2WizardOpen}
        onClose={() => { setV2WizardOpen(false); setV2Template(null); }}
        onSaved={() => qc.invalidateQueries({ queryKey })}
        projectId={projectId}
        modelId={modelId}
        measures={measures}
        dimensions={dimensions}
        kpis={kpis}
        editKpi={v2EditKpi}
        initialTemplate={v2Template}
        isAdmin={isAdmin}
        canEdit={canEdit}
      />

      <TemplateGalleryDialog
        open={templateGalleryOpen}
        onClose={() => setTemplateGalleryOpen(false)}
        entityType="kpi"
        onApplyKpi={applyKpiTemplate}
      />
    </Box>
  );
}
