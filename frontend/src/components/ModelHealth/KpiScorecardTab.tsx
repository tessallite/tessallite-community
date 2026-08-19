import { useMemo, useState } from "react";
import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  Collapse,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Stack,
  Typography,
} from "@mui/material";
import AssessmentIcon from "@mui/icons-material/Assessment";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import WarningIcon from "@mui/icons-material/Warning";
import ErrorIcon from "@mui/icons-material/Error";
import HelpOutlineIcon from "@mui/icons-material/HelpOutline";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import { useKpis, useKpiBatchEvaluation, usePersonas } from "../../api/hooks";
import type { Kpi, KpiEvaluateResponse } from "../../api/types";
import HelpIconButton from "../HelpIconButton";
import { useT } from "../../i18n";
import { palette, ui } from "../../theme/tokens";
import KpiCard from "../KpiScorecard/KpiCard";
import { resolveKpiDisplayStatus } from "../KpiScorecard/statusUtils";

interface Props {
  projectId: string;
  modelId: string;
}

export default function KpiScorecardTab({ projectId, modelId }: Props) {
  const t = useT();
  const [filter, setFilter] = useState<"all" | "certified" | "deprecated">(
    "all",
  );
  // F-017-25: folder, indicator-type (leading/lagging) filters + persona switcher.
  const [folderFilter, setFolderFilter] = useState<string>("__all__");
  const [indicatorFilter, setIndicatorFilter] = useState<
    "all" | "leading" | "lagging" | "none"
  >("all");
  const [personaId, setPersonaId] = useState<string>("");

  // F-017-05 / F-103-03 (Bug-9091): the scorecard is a serving/viewer surface,
  // so it reads the DEPLOYED KPI set (deployed_only) — the same rows JDBC $KPIs
  // and XMLA MDSCHEMA_KPIS advertise. A certified-but-undeployed edit must not
  // change the executive card before Deploy. The model builder (KpisPanel) keeps
  // its own live list for authoring drafts.
  const { data: kpis, isLoading: kpisLoading } = useKpis(
    projectId, modelId, personaId || null, true,
  );
  const { data: personas } = usePersonas(projectId, modelId);
  const [collapsedFolders, setCollapsedFolders] = useState<Set<string>>(
    new Set(),
  );

  const allKpiIds = useMemo(() => (kpis ?? []).map((k) => k.id), [kpis]);

  const { data: batchResult, isLoading: evalLoading } = useKpiBatchEvaluation(
    projectId,
    modelId,
    allKpiIds,
    true,
    personaId || null,
  );

  // Distinct folders for the folder filter dropdown.
  const folderOptions = useMemo(() => {
    const set = new Set<string>();
    for (const k of kpis ?? []) {
      if (k.display_folder) set.add(k.display_folder);
    }
    return [...set].sort();
  }, [kpis]);

  const evalMap = useMemo(() => {
    const map: Record<string, KpiEvaluateResponse> = {};
    if (batchResult?.results) {
      for (const r of batchResult.results) {
        if (r.kpi_id) map[r.kpi_id] = r;
      }
    }
    return map;
  }, [batchResult]);

  const filteredKpis = useMemo(() => {
    if (!kpis) return [];
    let list = kpis;
    if (filter === "certified")
      list = list.filter((k) => k.certification_status === "certified");
    else if (filter === "deprecated")
      list = list.filter((k) => k.certification_status === "deprecated");
    // F-017-25: folder filter.
    if (folderFilter !== "__all__")
      list = list.filter((k) => k.display_folder === folderFilter);
    // F-017-25: indicator-type (leading/lagging/unclassified) filter.
    if (indicatorFilter !== "all")
      list = list.filter(
        (k) => (k.indicator_type ?? "none") === indicatorFilter,
      );
    return list;
  }, [kpis, filter, folderFilter, indicatorFilter]);

  const grouped = useMemo(() => {
    const folders = new Map<string, Kpi[]>();
    const ungrouped: Kpi[] = [];
    for (const kpi of filteredKpis) {
      if (kpi.display_folder) {
        const list = folders.get(kpi.display_folder) || [];
        list.push(kpi);
        folders.set(kpi.display_folder, list);
      } else {
        ungrouped.push(kpi);
      }
    }
    const result: { folder: string | null; kpis: Kpi[] }[] = [];
    for (const [folder, list] of folders) {
      result.push({ folder, kpis: list });
    }
    if (ungrouped.length > 0) {
      result.push({ folder: null, kpis: ungrouped });
    }
    return result;
  }, [filteredKpis]);

  const statusCounts = useMemo(() => {
    let good = 0,
      warning = 0,
      poor = 0,
      unknown = 0;
    for (const kpi of filteredKpis) {
      const ev = evalMap[kpi.id];
      const status = resolveKpiDisplayStatus(ev?.status, ev?.status_label);
      if (!ev || status === null || status === undefined) {
        unknown++;
        continue;
      }
      if (status === 1) good++;
      else if (status === 0) warning++;
      else poor++;
    }
    return { good, warning, poor, unknown };
  }, [filteredKpis, evalMap]);

  const toggleFolder = (folder: string) => {
    setCollapsedFolders((prev) => {
      const next = new Set(prev);
      if (next.has(folder)) next.delete(folder);
      else next.add(folder);
      return next;
    });
  };

  if (kpisLoading) {
    return (
      <Box sx={{ p: 4, textAlign: "center" }}>
        <CircularProgress sx={{ color: ui.green }} />
      </Box>
    );
  }

  if (!kpis || kpis.length === 0) {
    return (
      <Box sx={{ p: 3 }}>
        <Alert severity="info" sx={{ maxWidth: 600 }}>
          <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 0.5 }}>
            {t("kpiScorecard.noKpisTitle")}
          </Typography>
          <Typography variant="body2">
            {t("kpiScorecard.noKpisDescription")}
          </Typography>
        </Alert>
      </Box>
    );
  }

  return (
    <Box sx={{ p: 2.5, maxWidth: 1400 }}>
      {/* Header */}
      <Stack direction="row" alignItems="center" spacing={1.5} sx={{ mb: 2.5 }}>
        <AssessmentIcon sx={{ color: ui.green, fontSize: 28 }} />
        <Typography
          variant="h6"
          fontWeight={700}
          sx={{ flex: 1, color: palette.charcoal }}
        >
          {t("kpiScorecard.title")}
        </Typography>
        <HelpIconButton href="/help/concepts/kpis.html" />
      </Stack>

      {/* Summary bar */}
      <Paper
        variant="outlined"
        sx={{
          p: 2,
          mb: 3,
          borderColor: palette.slateBorder,
          borderRadius: 2,
          bgcolor: palette.mint,
        }}
      >
        <Stack direction="row" spacing={3} flexWrap="wrap" alignItems="center">
          <Box>
            <Typography
              variant="caption"
              sx={{
                color: palette.textSecondary,
                fontSize: 10,
                textTransform: "uppercase",
                letterSpacing: 0.5,
              }}
            >
              {t("kpiScorecard.totalKpis")}
            </Typography>
            <Typography
              variant="h4"
              fontWeight={700}
              sx={{
                fontVariantNumeric: "tabular-nums",
                color: palette.charcoal,
                lineHeight: 1.2,
              }}
            >
              {filteredKpis.length}
            </Typography>
          </Box>
          <Box sx={{ width: "1px", height: 36, bgcolor: palette.slateBorder }} />
          <Chip
            icon={<CheckCircleIcon sx={{ fontSize: 14 }} />}
            label={`${statusCounts.good} ${t("kpiScorecard.statusGood")}`}
            size="small"
            sx={{
              fontWeight: 600,
              bgcolor: ui.greenBg,
              color: ui.green,
              "& .MuiChip-icon": { color: ui.green },
            }}
          />
          <Chip
            icon={<WarningIcon sx={{ fontSize: 14 }} />}
            label={`${statusCounts.warning} ${t("kpiScorecard.statusWarning")}`}
            size="small"
            sx={{
              fontWeight: 600,
              bgcolor: ui.goldBg,
              color: ui.goldDark,
              "& .MuiChip-icon": { color: ui.goldDark },
            }}
          />
          <Chip
            icon={<ErrorIcon sx={{ fontSize: 14 }} />}
            label={`${statusCounts.poor} ${t("kpiScorecard.statusPoor")}`}
            size="small"
            sx={{
              fontWeight: 600,
              bgcolor: ui.redBg,
              color: ui.red,
              "& .MuiChip-icon": { color: ui.red },
            }}
          />
          {statusCounts.unknown > 0 && (
            <Chip
              icon={<HelpOutlineIcon sx={{ fontSize: 14 }} />}
              label={`${statusCounts.unknown} ${t("kpiScorecard.statusUnknown")}`}
              size="small"
              sx={{
                fontWeight: 600,
                bgcolor: ui.mutedBg,
                color: ui.muted,
                "& .MuiChip-icon": { color: ui.muted },
              }}
            />
          )}
          <Box sx={{ flex: 1 }} />
          <FormControl size="small" sx={{ minWidth: 140 }}>
            <InputLabel>{t("kpiScorecard.filterLabel")}</InputLabel>
            <Select
              value={filter}
              label={t("kpiScorecard.filterLabel")}
              onChange={(e) => setFilter(e.target.value as typeof filter)}
              sx={{ bgcolor: palette.white, borderRadius: 1 }}
            >
              <MenuItem value="all">{t("kpiScorecard.filterAll")}</MenuItem>
              <MenuItem value="certified">
                {t("kpiScorecard.filterCertified")}
              </MenuItem>
              <MenuItem value="deprecated">
                {t("kpiScorecard.filterDeprecated")}
              </MenuItem>
            </Select>
          </FormControl>

          {/* F-017-25: folder filter */}
          {folderOptions.length > 0 && (
            <FormControl size="small" sx={{ minWidth: 150 }}>
              <InputLabel>{t("kpiScorecard.folderLabel")}</InputLabel>
              <Select
                value={folderFilter}
                label={t("kpiScorecard.folderLabel")}
                onChange={(e) => setFolderFilter(e.target.value)}
                sx={{ bgcolor: palette.white, borderRadius: 1 }}
              >
                <MenuItem value="__all__">
                  {t("kpiScorecard.folderAll")}
                </MenuItem>
                {folderOptions.map((f) => (
                  <MenuItem key={f} value={f}>
                    {f}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          )}

          {/* F-017-25: indicator-type (leading / lagging) filter */}
          <FormControl size="small" sx={{ minWidth: 150 }}>
            <InputLabel>{t("kpiScorecard.indicatorLabel")}</InputLabel>
            <Select
              value={indicatorFilter}
              label={t("kpiScorecard.indicatorLabel")}
              onChange={(e) =>
                setIndicatorFilter(e.target.value as typeof indicatorFilter)
              }
              sx={{ bgcolor: palette.white, borderRadius: 1 }}
            >
              <MenuItem value="all">{t("kpiScorecard.indicatorAll")}</MenuItem>
              <MenuItem value="leading">
                {t("kpiScorecard.indicatorLeading")}
              </MenuItem>
              <MenuItem value="lagging">
                {t("kpiScorecard.indicatorLagging")}
              </MenuItem>
              <MenuItem value="none">
                {t("kpiScorecard.indicatorNone")}
              </MenuItem>
            </Select>
          </FormControl>

          {/* F-017-25: persona switcher — re-evaluates under the persona scope */}
          {personas && personas.length > 0 && (
            <FormControl size="small" sx={{ minWidth: 170 }}>
              <InputLabel>{t("kpiScorecard.personaLabel")}</InputLabel>
              <Select
                value={personaId}
                label={t("kpiScorecard.personaLabel")}
                onChange={(e) => setPersonaId(e.target.value)}
                sx={{ bgcolor: palette.white, borderRadius: 1 }}
              >
                <MenuItem value="">
                  {t("kpiScorecard.personaDefault")}
                </MenuItem>
                {personas.map((p) => (
                  <MenuItem key={p.id} value={p.id}>
                    {p.name}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          )}
        </Stack>
      </Paper>

      {/* KPI card grid */}
      {grouped.map((group, gi) => (
        <Box key={gi} sx={{ mb: 3 }}>
          {group.folder && (
            <Stack
              direction="row"
              alignItems="center"
              spacing={0.5}
              sx={{
                mb: 1.5,
                cursor: "pointer",
                "&:hover": { opacity: 0.8 },
              }}
              onClick={() => toggleFolder(group.folder!)}
            >
              <IconButton size="small" sx={{ p: 0 }}>
                {collapsedFolders.has(group.folder) ? (
                  <ExpandMoreIcon
                    sx={{ fontSize: 18, color: palette.textSecondary }}
                  />
                ) : (
                  <ExpandLessIcon
                    sx={{ fontSize: 18, color: palette.textSecondary }}
                  />
                )}
              </IconButton>
              <Typography
                variant="subtitle2"
                fontWeight={600}
                sx={{
                  textTransform: "uppercase",
                  fontSize: 11,
                  letterSpacing: 0.5,
                  color: palette.textSecondary,
                }}
              >
                {group.folder} ({group.kpis.length})
              </Typography>
            </Stack>
          )}
          <Collapse in={!group.folder || !collapsedFolders.has(group.folder)}>
            <Box
              sx={{
                display: "grid",
                gridTemplateColumns: {
                  xs: "1fr",
                  md: "repeat(auto-fit, minmax(420px, 1fr))",
                },
                gap: 2.5,
                alignItems: "stretch",
                justifyContent: "start",
              }}
            >
              {group.kpis.map((kpi) => (
                <KpiCard
                  key={kpi.id}
                  kpi={kpi}
                  evalData={evalMap[kpi.id] ?? null}
                  loading={evalLoading}
                />
              ))}
            </Box>
          </Collapse>
        </Box>
      ))}
    </Box>
  );
}
