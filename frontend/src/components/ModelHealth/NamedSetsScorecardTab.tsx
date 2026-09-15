import { useMemo, useState } from "react";
import {
  Alert,
  Box,
  Button,
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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import PlaylistAddCheckIcon from "@mui/icons-material/PlaylistAddCheck";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import { useNamedSets } from "../../api/hooks";
import type { NamedSet } from "../../api/types";
import HelpIconButton from "../HelpIconButton";
import { useT } from "../../i18n";
import { palette, ui } from "../../theme/tokens";
import { LIST_TYPE_LABELS, CERT_COLORS } from "../Panels/namedSetPresentation";

interface Props {
  projectId: string;
  modelId: string;
}

/**
 * Bug-9091 (R2-B02) viewer surface: a read-only view of the DEPLOYED named
 * sets, mirroring KpiScorecardTab's dashboard/define split for KPIs. Define
 * (NamedSetsPanel) stays the live authoring preview per the 2026-08-17
 * F-026-20/F-026-07 decision; this tab is the first real consumer that
 * passes deployedOnly=true.
 */
export default function NamedSetsScorecardTab({ projectId, modelId }: Props) {
  const t = useT();
  const [folderFilter, setFolderFilter] = useState<string>("__all__");
  const [certFilter, setCertFilter] = useState<string>("__all__");
  const [collapsedFolders, setCollapsedFolders] = useState<Set<string>>(
    new Set(),
  );

  // R3 (round-3 external review): a failed request must be shown as an
  // explicit, retryable error — not silently collapsed into the "no deployed
  // named sets" empty state, which would misrepresent an outage/permissions
  // failure as a legitimate "nothing deployed yet" finding. Mirrors
  // UsageAnalyticsTab's Bug-7459 pattern.
  const { data: namedSets, isLoading, isError, refetch } = useNamedSets(
    projectId,
    modelId,
    true,
  );

  const folderOptions = useMemo(() => {
    const set = new Set<string>();
    for (const ns of namedSets ?? []) {
      if (ns.display_folder) set.add(ns.display_folder);
    }
    return [...set].sort();
  }, [namedSets]);

  const certOptions = useMemo(() => {
    const set = new Set<string>();
    for (const ns of namedSets ?? []) {
      if (ns.certification_status) set.add(ns.certification_status);
    }
    return [...set].sort();
  }, [namedSets]);

  const filtered = useMemo(() => {
    let list = namedSets ?? [];
    if (folderFilter !== "__all__")
      list = list.filter((ns) => ns.display_folder === folderFilter);
    if (certFilter !== "__all__")
      list = list.filter((ns) => ns.certification_status === certFilter);
    return list;
  }, [namedSets, folderFilter, certFilter]);

  const grouped = useMemo(() => {
    const folders = new Map<string, NamedSet[]>();
    const ungrouped: NamedSet[] = [];
    for (const ns of filtered) {
      if (ns.display_folder) {
        const list = folders.get(ns.display_folder) || [];
        list.push(ns);
        folders.set(ns.display_folder, list);
      } else {
        ungrouped.push(ns);
      }
    }
    const result: { folder: string | null; sets: NamedSet[] }[] = [];
    for (const [folder, list] of folders) result.push({ folder, sets: list });
    if (ungrouped.length > 0) result.push({ folder: null, sets: ungrouped });
    return result;
  }, [filtered]);

  const toggleFolder = (folder: string) => {
    setCollapsedFolders((prev) => {
      const next = new Set(prev);
      if (next.has(folder)) next.delete(folder);
      else next.add(folder);
      return next;
    });
  };

  if (isLoading) {
    return (
      <Box sx={{ p: 4, textAlign: "center" }}>
        <CircularProgress sx={{ color: ui.green }} />
      </Box>
    );
  }

  if (isError) {
    return (
      <Box sx={{ p: 3 }}>
        <Alert
          severity="error"
          data-testid="named-sets-scorecard-load-error"
          sx={{ maxWidth: 600 }}
          action={
            <Button color="inherit" size="small" onClick={() => refetch()}>
              {t("common.retry")}
            </Button>
          }
        >
          {t("namedSetsScorecard.loadError")}
        </Alert>
      </Box>
    );
  }

  if (!namedSets || namedSets.length === 0) {
    return (
      <Box sx={{ p: 3 }}>
        <Alert severity="info" sx={{ maxWidth: 600 }}>
          <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 0.5 }}>
            {t("namedSetsScorecard.noSetsTitle")}
          </Typography>
          <Typography variant="body2">
            {t("namedSetsScorecard.noSetsDescription")}
          </Typography>
        </Alert>
      </Box>
    );
  }

  return (
    <Box sx={{ p: 2.5, maxWidth: 1400 }}>
      <Stack direction="row" alignItems="center" spacing={1.5} sx={{ mb: 2.5 }}>
        <PlaylistAddCheckIcon sx={{ color: ui.green, fontSize: 28 }} />
        <Typography
          variant="h6"
          fontWeight={700}
          sx={{ flex: 1, color: palette.charcoal }}
        >
          {t("namedSetsScorecard.title")}
        </Typography>
        <HelpIconButton href="/help/modelling/named-queries.html" />
      </Stack>

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
              {t("namedSetsScorecard.totalSets")}
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
              {filtered.length}
            </Typography>
          </Box>
          <Box sx={{ width: "1px", height: 36, bgcolor: palette.slateBorder }} />
          <Box sx={{ flex: 1 }} />

          {folderOptions.length > 0 && (
            <FormControl size="small" sx={{ minWidth: 150 }}>
              <InputLabel>{t("namedSetsScorecard.folderLabel")}</InputLabel>
              <Select
                value={folderFilter}
                label={t("namedSetsScorecard.folderLabel")}
                onChange={(e) => setFolderFilter(e.target.value)}
                sx={{ bgcolor: palette.white, borderRadius: 1 }}
              >
                <MenuItem value="__all__">
                  {t("namedSetsScorecard.folderAll")}
                </MenuItem>
                {folderOptions.map((f) => (
                  <MenuItem key={f} value={f}>
                    {f}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          )}

          {certOptions.length > 0 && (
            <FormControl size="small" sx={{ minWidth: 150 }}>
              <InputLabel>{t("namedSetsScorecard.certLabel")}</InputLabel>
              <Select
                value={certFilter}
                label={t("namedSetsScorecard.certLabel")}
                onChange={(e) => setCertFilter(e.target.value)}
                sx={{ bgcolor: palette.white, borderRadius: 1 }}
              >
                <MenuItem value="__all__">
                  {t("namedSetsScorecard.certAll")}
                </MenuItem>
                {certOptions.map((c) => (
                  <MenuItem key={c} value={c}>
                    {c}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          )}
        </Stack>
      </Paper>

      {grouped.map((group, gi) => (
        <Box key={gi} sx={{ mb: 3 }}>
          {group.folder && (
            <Stack
              direction="row"
              alignItems="center"
              spacing={0.5}
              sx={{ mb: 1.5, cursor: "pointer", "&:hover": { opacity: 0.8 } }}
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
                {group.folder} ({group.sets.length})
              </Typography>
            </Stack>
          )}
          <Collapse in={!group.folder || !collapsedFolders.has(group.folder)}>
            <Paper variant="outlined" sx={{ borderColor: palette.slateBorder }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>{t("namedSetsScorecard.colName")}</TableCell>
                    <TableCell>{t("namedSetsScorecard.colKind")}</TableCell>
                    <TableCell>
                      {t("namedSetsScorecard.colCertification")}
                    </TableCell>
                    <TableCell>
                      {t("namedSetsScorecard.colDescription")}
                    </TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {group.sets.map((ns) => (
                    <TableRow key={ns.id} hover>
                      <TableCell>
                        <Typography variant="body2" fontWeight={600}>
                          {ns.display_name || ns.name}
                        </Typography>
                      </TableCell>
                      <TableCell>
                        {LIST_TYPE_LABELS[ns.list_type ?? "advanced_mdx"]
                          ? t(LIST_TYPE_LABELS[ns.list_type ?? "advanced_mdx"])
                          : ns.list_type}
                      </TableCell>
                      <TableCell>
                        <Chip
                          size="small"
                          label={ns.certification_status}
                          color={CERT_COLORS[ns.certification_status] ?? "default"}
                        />
                      </TableCell>
                      <TableCell>
                        <Typography variant="body2" color="text.secondary">
                          {ns.description || "—"}
                        </Typography>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Paper>
          </Collapse>
        </Box>
      ))}
    </Box>
  );
}
