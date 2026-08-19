/**
 * Impact Analysis panel (Bug-7787, Phase 4, spec section 11.2).
 *
 * A two-pane workbench for exploring model-side dependency impact:
 * - Left: object selector and dependent list grouped by severity.
 * - Right: selected impact detail with dependency path, effect, and field.
 * - Summary strip showing hard/soft/cascade counts.
 * - Operation selector: Inspect / Delete preview.
 */
import { useCallback, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import {
  Alert,
  Autocomplete,
  Box,
  Chip,
  CircularProgress,
  Divider,
  FormControl,
  InputLabel,
  List,
  ListItemButton,
  ListItemText,
  MenuItem,
  Paper,
  Select,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { useImpactCatalogue, useImpactQuery } from "../../api/hooks";
import { useT } from "../../i18n";
import type {
  ImpactCatalogueItem,
  ImpactItem,
  ImpactOperation,
  ImpactQueryRequest,
} from "../../api/types_domains/model_impact";

// Severity styling: hard = error, soft = warning, info = info.
const SEVERITY_COLOR: Record<string, "error" | "warning" | "info" | "default"> = {
  hard_break: "error",
  soft_degrade: "warning",
  informational: "info",
};

const SEVERITY_LABEL: Record<string, string> = {
  hard_break: "impactAnalysis.severity.hardBreak",
  soft_degrade: "impactAnalysis.severity.softDegrade",
  informational: "impactAnalysis.severity.informational",
};

export default function ImpactAnalysisPanel() {
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const t = useT();

  const [search, setSearch] = useState("");
  const [selectedObject, setSelectedObject] = useState<ImpactCatalogueItem | null>(null);
  const [operation, setOperation] = useState<ImpactOperation>("inspect");
  const [selectedImpact, setSelectedImpact] = useState<ImpactItem | null>(null);

  // Catalogue query — fetches the searchable object list.
  const catalogue = useImpactCatalogue(projectId!, modelId!, {
    search: search || undefined,
    limit: 100,
  });

  // Build the impact query request when an object is selected.
  const queryBody: ImpactQueryRequest | null = useMemo(() => {
    if (!selectedObject) return null;
    return {
      target: {
        object_type: selectedObject.object_type,
        object_id: selectedObject.object_id,
      },
      operation,
    };
  }, [selectedObject, operation]);

  // Impact query — runs when an object is selected.
  const impact = useImpactQuery(projectId!, modelId!, queryBody);

  const handleObjectSelect = useCallback(
    (_: unknown, value: ImpactCatalogueItem | null) => {
      setSelectedObject(value);
      setSelectedImpact(null);
    },
    [],
  );

  // Group impacts by severity.
  const grouped = useMemo(() => {
    if (!impact.data) return { hard: [], soft: [], info: [] };
    const hard: ImpactItem[] = [];
    const soft: ImpactItem[] = [];
    const info: ImpactItem[] = [];
    for (const item of impact.data.impacts) {
      if (item.severity === "hard_break") hard.push(item);
      else if (item.severity === "soft_degrade") soft.push(item);
      else info.push(item);
    }
    return { hard, soft, info };
  }, [impact.data]);

  return (
    <Box sx={{ p: 2 }}>
      <Typography variant="h6" gutterBottom>
        {t("impactAnalysis.title")}
      </Typography>

      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        {t("impactAnalysis.description")}
      </Typography>

      {/* Object selector + operation */}
      <Stack direction="row" spacing={2} sx={{ mb: 2 }} alignItems="flex-start">
        <Autocomplete
          sx={{ flex: 1 }}
          size="small"
          options={catalogue.data?.items ?? []}
          getOptionLabel={(opt) => `${opt.display_name} (${opt.object_type})`}
          groupBy={(opt) => opt.object_type}
          loading={catalogue.isLoading}
          value={selectedObject}
          onChange={handleObjectSelect}
          onInputChange={(_, value) => setSearch(value)}
          renderInput={(params) => (
            <TextField
              {...params}
              label={t("impactAnalysis.objectSearchLabel")}
              placeholder={t("impactAnalysis.objectSearchPlaceholder")}
            />
          )}
          isOptionEqualToValue={(opt, val) => opt.object_id === val.object_id}
        />
        <FormControl size="small" sx={{ minWidth: 140 }}>
          <InputLabel>{t("impactAnalysis.operationLabel")}</InputLabel>
          <Select
            value={operation}
            label={t("impactAnalysis.operationLabel")}
            onChange={(e) => {
              setOperation(e.target.value as ImpactOperation);
              setSelectedImpact(null);
            }}
          >
            <MenuItem value="inspect">{t("impactAnalysis.operationInspect")}</MenuItem>
            <MenuItem value="delete">{t("impactAnalysis.operationDelete")}</MenuItem>
          </Select>
        </FormControl>
      </Stack>

      {/* Summary strip */}
      {impact.data && (
        <Stack direction="row" spacing={2} sx={{ mb: 2 }}>
          <Chip
            label={`${t("impactAnalysis.summary.hardBreaks")}: ${impact.data.summary.hard_break}`}
            color={impact.data.summary.hard_break > 0 ? "error" : "default"}
            variant={impact.data.summary.hard_break > 0 ? "filled" : "outlined"}
            size="small"
          />
          <Chip
            label={`${t("impactAnalysis.summary.softChanges")}: ${impact.data.summary.soft_degrade}`}
            color={impact.data.summary.soft_degrade > 0 ? "warning" : "default"}
            variant={impact.data.summary.soft_degrade > 0 ? "filled" : "outlined"}
            size="small"
          />
          <Chip
            label={`${t("impactAnalysis.summary.cascadeDeleted")}: ${impact.data.summary.cascade_deleted}`}
            color="default"
            variant="outlined"
            size="small"
          />
          <Chip
            label={`${t("impactAnalysis.summary.total")}: ${impact.data.summary.total}`}
            color="default"
            variant="outlined"
            size="small"
          />
        </Stack>
      )}

      {/* Guard decision alert */}
      {impact.data && impact.data.guard.decision === "blocked" && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {t("impactAnalysis.guard.blocked")}
        </Alert>
      )}
      {impact.data && impact.data.guard.decision === "blocked_unresolved" && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {t("impactAnalysis.guard.blockedUnresolved")}
        </Alert>
      )}
      {impact.data && impact.data.guard.decision === "acknowledgement_required" && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          {t("impactAnalysis.guard.acknowledgementRequired")}
        </Alert>
      )}
      {impact.data && impact.data.guard.decision === "allowed" && impact.data.summary.total === 0 && (
        <Alert severity="success" sx={{ mb: 2 }}>
          {t("impactAnalysis.guard.noDependents")}
        </Alert>
      )}

      {/* Loading state */}
      {impact.isLoading && selectedObject && (
        <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
          <CircularProgress size={28} />
        </Box>
      )}

      {/* No object selected state */}
      {!selectedObject && (
        <Typography color="text.secondary" sx={{ py: 4, textAlign: "center" }}>
          {t("impactAnalysis.selectObjectPrompt")}
        </Typography>
      )}

      {/* Two-pane layout */}
      {impact.data && impact.data.summary.total > 0 && (
        <Stack direction="row" spacing={2} sx={{ minHeight: 300 }}>
          {/* Left pane: dependent list */}
          <Paper variant="outlined" sx={{ flex: 1, overflow: "auto", maxHeight: 500 }}>
            {grouped.hard.length > 0 && (
              <>
                <Typography
                  variant="subtitle2"
                  sx={{ px: 1.5, pt: 1, color: "error.main" }}
                >
                  {t("impactAnalysis.group.hardBreaks", { count: String(grouped.hard.length) })}
                </Typography>
                <ImpactList
                  items={grouped.hard}
                  selected={selectedImpact}
                  onSelect={setSelectedImpact}
                  t={t}
                />
              </>
            )}
            {grouped.soft.length > 0 && (
              <>
                <Divider />
                <Typography
                  variant="subtitle2"
                  sx={{ px: 1.5, pt: 1, color: "warning.main" }}
                >
                  {t("impactAnalysis.group.softChanges", { count: String(grouped.soft.length) })}
                </Typography>
                <ImpactList
                  items={grouped.soft}
                  selected={selectedImpact}
                  onSelect={setSelectedImpact}
                  t={t}
                />
              </>
            )}
            {grouped.info.length > 0 && (
              <>
                <Divider />
                <Typography
                  variant="subtitle2"
                  sx={{ px: 1.5, pt: 1, color: "info.main" }}
                >
                  {t("impactAnalysis.group.informational", { count: String(grouped.info.length) })}
                </Typography>
                <ImpactList
                  items={grouped.info}
                  selected={selectedImpact}
                  onSelect={setSelectedImpact}
                  t={t}
                />
              </>
            )}
          </Paper>

          {/* Right pane: selected impact detail */}
          <Paper variant="outlined" sx={{ flex: 1, p: 2, overflow: "auto", maxHeight: 500 }}>
            {selectedImpact ? (
              <ImpactDetail item={selectedImpact} t={t} />
            ) : (
              <Typography color="text.secondary" sx={{ py: 4, textAlign: "center" }}>
                {t("impactAnalysis.selectImpactPrompt")}
              </Typography>
            )}
          </Paper>
        </Stack>
      )}
    </Box>
  );
}

// --- Sub-components ---

function ImpactList({
  items,
  selected,
  onSelect,
  t,
}: {
  items: ImpactItem[];
  selected: ImpactItem | null;
  onSelect: (item: ImpactItem) => void;
  t: (key: string, params?: Record<string, string>) => string;
}) {
  return (
    <List dense disablePadding>
      {items.map((item) => (
        <ListItemButton
          key={item.impact_id}
          selected={selected?.impact_id === item.impact_id}
          onClick={() => onSelect(item)}
          sx={{ py: 0.5 }}
        >
          <ListItemText
            primary={item.object.display_name}
            secondary={`${item.object.object_type} — ${t(SEVERITY_LABEL[item.severity] ?? item.severity)}`}
            primaryTypographyProps={{ variant: "body2" }}
            secondaryTypographyProps={{ variant: "caption" }}
          />
          <Chip
            label={t(SEVERITY_LABEL[item.severity] ?? item.severity)}
            color={SEVERITY_COLOR[item.severity] ?? "default"}
            size="small"
            variant="outlined"
            sx={{ ml: 1 }}
          />
        </ListItemButton>
      ))}
    </List>
  );
}

function ImpactDetail({
  item,
  t,
}: {
  item: ImpactItem;
  t: (key: string, params?: Record<string, string>) => string;
}) {
  return (
    <Stack spacing={1.5}>
      <Typography variant="subtitle1">{item.object.display_name}</Typography>
      <Stack direction="row" spacing={1}>
        <Chip
          label={t(SEVERITY_LABEL[item.severity] ?? item.severity)}
          color={SEVERITY_COLOR[item.severity] ?? "default"}
          size="small"
        />
        <Chip label={item.object.object_type} size="small" variant="outlined" />
      </Stack>

      <Box>
        <Typography variant="caption" color="text.secondary">
          {t("impactAnalysis.detail.effect")}
        </Typography>
        <Typography variant="body2">{item.effect}</Typography>
      </Box>

      <Box>
        <Typography variant="caption" color="text.secondary">
          {t("impactAnalysis.detail.deletePolicy")}
        </Typography>
        <Typography variant="body2">{item.delete_policy}</Typography>
      </Box>

      <Box>
        <Typography variant="caption" color="text.secondary">
          {t("impactAnalysis.detail.depth")}
        </Typography>
        <Typography variant="body2">{item.min_depth}</Typography>
      </Box>

      {item.direct && (
        <Chip label={t("impactAnalysis.detail.direct")} size="small" color="primary" variant="outlined" />
      )}

      {/* Dependency path */}
      {item.paths.length > 0 && (
        <Box>
          <Typography variant="caption" color="text.secondary">
            {t("impactAnalysis.detail.dependencyPath")}
          </Typography>
          {item.paths.map((path, idx) => (
            <Typography key={idx} variant="body2" sx={{ fontFamily: "monospace", fontSize: 11 }}>
              {path.nodes.join(" -> ")}
            </Typography>
          ))}
        </Box>
      )}

      {/* Reason */}
      {item.reason_key && (
        <Box>
          <Typography variant="caption" color="text.secondary">
            {t("impactAnalysis.detail.reason")}
          </Typography>
          <Typography variant="body2">{t(item.reason_key, item.reason_params)}</Typography>
        </Box>
      )}
    </Stack>
  );
}
