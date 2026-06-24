import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useT } from "../../../i18n";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  MenuItem,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import AutoFixHighIcon from "@mui/icons-material/AutoFixHigh";
import {
  dimensionsApi,
  measuresApi,
  modelTablesApi,
  tableAttributesApi,
} from "../../../api/client";
import { useTableAttributes, useDimensions, useMeasures } from "../../../api/hooks";
import type {
  Dimension,
  DimensionCreate,
  Measure,
  MeasureCreate,
  MeasureWarning,
  ModelTable,
  TableAttribute,
  TableAnalysis,
} from "../../../api/types";
import type { RenamePreviewRow } from "./AttributeRenameDialog";
import AttributeRenameDialog from "./AttributeRenameDialog";

type ColumnRole = "measure" | "dimension" | "date_key" | "ignore" | "none";

interface ColumnDraft {
  id: string;
  name: string;
  dataType: string;
  role: ColumnRole;
  originalRole: ColumnRole;
  isUserDefined: boolean;
}

const COLUMN_ROLE_OPTIONS: { value: ColumnRole; label: string }[] = [
  { value: "measure", label: "columnRole.measure" },
  { value: "dimension", label: "columnRole.dimension" },
  { value: "date_key", label: "columnRole.dateKey" },
  { value: "ignore", label: "columnRole.ignore" },
  { value: "none", label: "columnRole.none" },
];

const ROLE_STYLES: Record<ColumnRole, { color: string; bg: string }> = {
  measure:   { color: "#006C35", bg: "rgba(0,108,53,0.08)" },
  dimension: { color: "#3A5EA8", bg: "rgba(58,94,168,0.08)" },
  date_key:  { color: "#A67C00", bg: "rgba(166,124,0,0.08)" },
  ignore:    { color: "#5A6577", bg: "rgba(90,101,119,0.08)" },
  none:      { color: "#9E9E9E", bg: "transparent" },
};

function toDisplayName(col: string): string {
  return col
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Resolve a unique name for a new dimension or measure.
 *
 * Priority:
 *   1. col_name                     (no conflict)
 *   2. {alias}_{col_name}           (alias takes priority over physical name)
 *   3. {physical_name}_{col_name}
 *   4. {alias}_2_{col_name}, {alias}_3_{col_name}, …  (sequential fallback)
 *
 * takenNames is mutated by the caller after each reservation so that names
 * chosen earlier in the same save batch are not reused.
 */
function resolveUniqueName(
  colName: string,
  takenNames: Set<string>,
  alias: string,
  physicalName: string,
): string {
  const candidates = [
    colName,
    `${alias}_${colName}`,
    `${physicalName}_${colName}`,
  ];
  for (const c of candidates) {
    if (!takenNames.has(c.toLowerCase())) return c;
  }
  let n = 2;
  for (;;) {
    const c = `${alias}_${n}_${colName}`;
    if (!takenNames.has(c.toLowerCase())) return c;
    n++;
  }
}

interface Props {
  projectId: string;
  modelId: string;
  sourceId: string;
  table: ModelTable;
}

// Stored state between the preview check and the actual save execution.
interface PendingState {
  takenMeasureNames: Set<string>;
  takenDimNames: Set<string>;
  existingUdaMeasures: Set<string>;
  existingUdaDims: Set<string>;
  // Maps used to delete stale entities when a column's role changes.
  // Keys are source_column_name (lowercase) for this table only.
  measureIdByColName: Map<string, string>;
  dimIdByColName: Map<string, string>;
  // UDA-aware maps: keyed by user_defined_attribute_id → entity id.
  measureIdByUdaId: Map<string, string>;
  dimIdByUdaId: Map<string, string>;
}

export default function ClassificationTab({
  projectId,
  modelId,
  sourceId,
  table,
}: Props) {
  const t = useT();
  const qc = useQueryClient();
  const attributes = useTableAttributes(projectId, modelId, table.id);
  const measures = useMeasures(projectId, modelId);
  const dimensions = useDimensions(projectId, modelId);

  const [columns, setColumns] = useState<ColumnDraft[]>([]);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);
  const [analysisInfo, setAnalysisInfo] = useState<string | null>(null);
  const [measureWarnings, setMeasureWarnings] = useState<MeasureWarning[]>([]);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);

  // Pre-save rename preview (new-table conflict dialog).
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewRows, setPreviewRows] = useState<RenamePreviewRow[]>([]);
  const [previewTaken, setPreviewTaken] = useState<Set<string>>(new Set());
  const pendingRef = useRef<PendingState | null>(null);

  const allColumns = useMemo(
    () => attributes.data ?? [],
    [attributes.data],
  );

  const tableMeasures = useMemo(
    () => (measures.data ?? []).filter((m) => m.source_table_id === table.id),
    [measures.data, table.id],
  );
  const tableDimensions = useMemo(
    () => (dimensions.data ?? []).filter((d) => d.source_table_id === table.id),
    [dimensions.data, table.id],
  );

  function resolveRole(col: TableAttribute): ColumnRole {
    const colLower = col.name.toLowerCase();
    if (col.is_user_defined) {
      if (tableMeasures.some((m) => (m.user_defined_attribute_name ?? "").toLowerCase() === colLower))
        return "measure";
      const dim = tableDimensions.find(
        (d) => (d.user_defined_attribute_name ?? "").toLowerCase() === colLower,
      );
      if (dim) return dim.is_time_dim ? "date_key" : "dimension";
      return "none";
    }
    if (tableMeasures.some((m) => (m.source_column_name ?? "").toLowerCase() === colLower))
      return "measure";
    const dim = tableDimensions.find(
      (d) => (d.source_column_name ?? "").toLowerCase() === colLower,
    );
    if (dim) return dim.is_time_dim ? "date_key" : "dimension";
    if (col.is_hidden) return "ignore";
    return "none";
  }

  useEffect(() => {
    if (!allColumns.length) return;
    setColumns(
      allColumns.map((col: TableAttribute) => {
        const role = resolveRole(col);
        return {
          id: col.id,
          name: col.name,
          dataType: col.data_type,
          role,
          originalRole: role,
          isUserDefined: col.is_user_defined,
        };
      }),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allColumns, tableMeasures, tableDimensions]);

  function setColumnRole(colId: string, role: ColumnRole) {
    setColumns((prev) =>
      prev.map((c) => (c.id === colId ? { ...c, role } : c)),
    );
    setSaveSuccess(false);
  }

  async function runAutoClassify() {
    setAnalyzing(true);
    setAnalyzeError(null);
    setAnalysisInfo(null);
    setMeasureWarnings([]);
    setSaveSuccess(false);
    try {
      const result: TableAnalysis = await modelTablesApi.analyze(
        projectId, modelId, sourceId, table.id,
      );
      setColumns((prev) =>
        prev.map((col) => {
          const suggestion = result.column_suggestions.find(
            (s) => s.column_id === col.id,
          );
          return suggestion ? { ...col, role: suggestion.suggested_role as ColumnRole } : col;
        }),
      );
      setAnalysisInfo(
        t("classification.confidence", { confidence: result.confidence, reasoning: result.reasoning }),
      );
      if (result.measure_warnings?.length) {
        setMeasureWarnings(result.measure_warnings);
      }
    } catch (err: unknown) {
      setAnalyzeError((err as Error).message ?? t("classification.autoFailed"));
    } finally {
      setAnalyzing(false);
    }
  }

  const rolesChanged = columns.some((c) => c.role !== c.originalRole);
  const dirty = rolesChanged;

  // Physical name stripped of schema prefix (matches the backend convention).
  const physicalBase = table.physical_name.split(".").pop() ?? table.physical_name;

  /**
   * Execute the actual API save loop.
   *
   * nameOverrides: map from col.id → user-approved name. When present, the
   * override supersedes the resolver output for that column.
   */
  async function executeSave(
    nameOverrides: Map<string, string>,
    pending: PendingState,
  ) {
    const {
      takenMeasureNames,
      takenDimNames,
      existingUdaMeasures,
      existingUdaDims,
      measureIdByColName,
      dimIdByColName,
      measureIdByUdaId,
      dimIdByUdaId,
    } = pending;

    for (const col of columns) {
      if (col.role === col.originalRole) continue;
      const colLower = col.name.toLowerCase();

      if (col.role === "measure") {
        if (col.isUserDefined && existingUdaMeasures.has(colLower)) continue;
        const resolvedName = col.isUserDefined
          ? col.name
          : (nameOverrides.get(col.id) ?? resolveUniqueName(col.name, takenMeasureNames, table.alias, physicalBase));
        takenMeasureNames.add(resolvedName.toLowerCase());
        const payload: MeasureCreate = {
          name: resolvedName,
          display_name: toDisplayName(resolvedName),
          default_agg: "sum",
          ...(col.isUserDefined
            ? { user_defined_attribute_id: col.id }
            : { source_table_id: table.id, source_column_name: col.name }),
        };
        await measuresApi.create(projectId, modelId, payload);
        // Remove stale Dimension when the column was previously a dimension/date_key.
        if (col.originalRole === "dimension" || col.originalRole === "date_key") {
          const staleId = col.isUserDefined
            ? dimIdByUdaId.get(col.id)
            : dimIdByColName.get(colLower);
          if (staleId) await dimensionsApi.delete(projectId, modelId, staleId);
        }
      } else if (col.role === "dimension" || col.role === "date_key") {
        if (col.isUserDefined && existingUdaDims.has(colLower)) continue;
        const isTimeDim = col.role === "date_key";
        // Transitioning between dimension ↔ date_key: update is_time_dim, no recreate needed.
        if (col.originalRole === "dimension" || col.originalRole === "date_key") {
          const existingId = col.isUserDefined
            ? dimIdByUdaId.get(col.id)
            : dimIdByColName.get(colLower);
          if (existingId) {
            await dimensionsApi.update(projectId, modelId, existingId, { is_time_dim: isTimeDim });
            continue;
          }
        }
        const resolvedName = col.isUserDefined
          ? col.name
          : (nameOverrides.get(col.id) ?? resolveUniqueName(col.name, takenDimNames, table.alias, physicalBase));
        takenDimNames.add(resolvedName.toLowerCase());
        const payload: DimensionCreate = {
          name: resolvedName,
          display_name: toDisplayName(resolvedName),
          ...(col.isUserDefined
            ? { user_defined_attribute_id: col.id }
            : { source_table_id: table.id, source_column_name: col.name }),
          ...(isTimeDim ? { is_time_dim: true } : {}),
        };
        await dimensionsApi.create(projectId, modelId, payload);
        // Remove stale Measure when the column was previously a measure.
        if (col.originalRole === "measure") {
          const staleId = col.isUserDefined
            ? measureIdByUdaId.get(col.id)
            : measureIdByColName.get(colLower);
          if (staleId) await measuresApi.delete(projectId, modelId, staleId);
        }
      } else if (col.role === "ignore" && !col.isUserDefined) {
        await tableAttributesApi.updateColumn(
          projectId, modelId, table.id, col.id, { is_hidden: true },
        );
        // Remove stale classified entity when hiding a previously-classified column.
        if (col.originalRole === "measure") {
          const staleId = measureIdByColName.get(colLower);
          if (staleId) await measuresApi.delete(projectId, modelId, staleId);
        } else if (col.originalRole === "dimension" || col.originalRole === "date_key") {
          const staleId = dimIdByColName.get(colLower);
          if (staleId) await dimensionsApi.delete(projectId, modelId, staleId);
        }
      } else if (col.role === "none" && !col.isUserDefined) {
        // Unhide if previously hidden.
        if (col.originalRole === "ignore") {
          await tableAttributesApi.updateColumn(
            projectId, modelId, table.id, col.id, { is_hidden: false },
          );
        }
        // Remove stale classified entity when the column is reset to unclassified.
        if (col.originalRole === "measure") {
          const staleId = measureIdByColName.get(colLower);
          if (staleId) await measuresApi.delete(projectId, modelId, staleId);
        } else if (col.originalRole === "dimension" || col.originalRole === "date_key") {
          const staleId = dimIdByColName.get(colLower);
          if (staleId) await dimensionsApi.delete(projectId, modelId, staleId);
        }
      } else if (col.role === "none" && col.isUserDefined) {
        // Remove stale classified entity when resetting a UDA column to unclassified.
        if (col.originalRole === "measure") {
          const staleId = measureIdByUdaId.get(col.id);
          if (staleId) await measuresApi.delete(projectId, modelId, staleId);
        } else if (col.originalRole === "dimension" || col.originalRole === "date_key") {
          const staleId = dimIdByUdaId.get(col.id);
          if (staleId) await dimensionsApi.delete(projectId, modelId, staleId);
        }
      }
    }

    qc.invalidateQueries({ queryKey: ["modelTables", projectId, modelId] });
    qc.invalidateQueries({ queryKey: ["allModelTables", projectId, modelId] });
    qc.invalidateQueries({ queryKey: ["tableAttributes", projectId, modelId, table.id] });
    qc.invalidateQueries({ queryKey: ["measures", projectId, modelId] });
    qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
    qc.invalidateQueries({ queryKey: ["joins", projectId, modelId] });
    setSaveSuccess(true);
    setSaveError(null);
  }

  async function handleSave() {
    setSaving(true);
    setSaveError(null);
    setSaveSuccess(false);
    try {
      // Fetch fresh name sets — don't rely on the React Query cache.
      const [freshMeasures, freshDimensions] = await Promise.all([
        measuresApi.list(projectId, modelId),
        dimensionsApi.list(projectId, modelId),
      ]);

      const takenMeasureNames = new Set(freshMeasures.map((m) => m.name.toLowerCase()));
      const takenDimNames = new Set(freshDimensions.map((d) => d.name.toLowerCase()));
      const existingUdaMeasures = new Set(
        tableMeasures
          .map((m) => (m.user_defined_attribute_name ?? "").toLowerCase())
          .filter(Boolean),
      );
      const existingUdaDims = new Set(
        tableDimensions
          .map((d) => (d.user_defined_attribute_name ?? "").toLowerCase())
          .filter(Boolean),
      );

      const measureIdByColName = new Map<string, string>(
        freshMeasures
          .filter((m) => m.source_table_id === table.id && m.source_column_name)
          .map((m) => [m.source_column_name!.toLowerCase(), m.id]),
      );
      const dimIdByColName = new Map<string, string>(
        freshDimensions
          .filter((d) => d.source_table_id === table.id && d.source_column_name)
          .map((d) => [d.source_column_name!.toLowerCase(), d.id]),
      );

      // UDA-aware maps: user_defined_attribute_id → entity id
      const measureIdByUdaId = new Map<string, string>(
        freshMeasures
          .filter((m) => m.user_defined_attribute_id)
          .map((m) => [m.user_defined_attribute_id!, m.id]),
      );
      const dimIdByUdaId = new Map<string, string>(
        freshDimensions
          .filter((d) => d.user_defined_attribute_id)
          .map((d) => [d.user_defined_attribute_id!, d.id]),
      );

      const pending: PendingState = {
        takenMeasureNames: new Set(takenMeasureNames),
        takenDimNames: new Set(takenDimNames),
        existingUdaMeasures,
        existingUdaDims,
        measureIdByColName,
        dimIdByColName,
        measureIdByUdaId,
        dimIdByUdaId,
      };

      // Detect columns that would receive an auto-prefixed name.
      const conflicts: RenamePreviewRow[] = [];
      const checkTakenM = new Set(takenMeasureNames);
      const checkTakenD = new Set(takenDimNames);

      for (const col of columns) {
        if (col.role === col.originalRole) continue;
        if (col.isUserDefined) continue;

        if (col.role === "measure") {
          const resolved = resolveUniqueName(col.name, checkTakenM, table.alias, physicalBase);
          checkTakenM.add(resolved.toLowerCase());
          if (resolved !== col.name) {
            conflicts.push({
              type: "measure",
              id: col.id,
              source_column_name: col.name,
              current_name: "",
              suggested_name: resolved,
            });
          }
        } else if (col.role === "dimension" || col.role === "date_key") {
          const resolved = resolveUniqueName(col.name, checkTakenD, table.alias, physicalBase);
          checkTakenD.add(resolved.toLowerCase());
          if (resolved !== col.name) {
            conflicts.push({
              type: "dimension",
              id: col.id,
              source_column_name: col.name,
              current_name: "",
              suggested_name: resolved,
            });
          }
        }
      }

      if (conflicts.length > 0) {
        // Suspend the save; show the rename preview dialog.
        // taken set for the dialog = all model names minus the conflict cols (they're being created).
        const conflictIds = new Set(conflicts.map((c) => c.id));
        // For dialog validation, combine dim+measure name spaces (simpler for the user).
        const dialogTaken = new Set([
          ...freshMeasures.filter((m) => !conflictIds.has(m.id)).map((m) => m.name.toLowerCase()),
          ...freshDimensions.filter((d) => !conflictIds.has(d.id)).map((d) => d.name.toLowerCase()),
        ]);
        pendingRef.current = pending;
        setPreviewRows(conflicts);
        setPreviewTaken(dialogTaken);
        setPreviewOpen(true);
        setSaving(false);
        return;
      }

      await executeSave(new Map(), pending);
    } catch (err: unknown) {
      setSaveError(
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
          t("classification.saveFailed"),
      );
    } finally {
      setSaving(false);
    }
  }

  async function handlePreviewApply(
    approved: Array<{ type: string; id: string; name: string }>,
  ) {
    if (!pendingRef.current) return;
    const overrides = new Map(approved.map((r) => [r.id, r.name]));
    setPreviewOpen(false);
    setSaving(true);
    setSaveError(null);
    setSaveSuccess(false);
    try {
      await executeSave(overrides, pendingRef.current);
    } catch (err: unknown) {
      setSaveError(
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
          t("classification.saveFailed"),
      );
    } finally {
      setSaving(false);
      pendingRef.current = null;
    }
  }

  function handlePreviewCancel() {
    setPreviewOpen(false);
    pendingRef.current = null;
  }

  // takenNames for dialog — must be declared before any early return (Rules of Hooks).
  const stablePreviewTaken = useMemo(() => previewTaken, [previewTaken]);

  if (attributes.isLoading || measures.isLoading || dimensions.isLoading) {
    return (
      <Box display="flex" justifyContent="center" py={4}>
        <CircularProgress size={24} />
      </Box>
    );
  }

  return (
    <Stack spacing={2} sx={{ pt: 1 }}>
      {analyzeError && (
        <Alert severity="error" onClose={() => setAnalyzeError(null)}>
          {analyzeError}
        </Alert>
      )}
      {saveError && (
        <Alert severity="error" onClose={() => setSaveError(null)}>
          {saveError}
        </Alert>
      )}
      {saveSuccess && (
        <Alert severity="success" onClose={() => setSaveSuccess(false)}>
          {t("classification.saved")}
        </Alert>
      )}

      {/* Analysis info */}
      {analysisInfo && (
        <Typography variant="caption" color="text.secondary">
          {analysisInfo}
        </Typography>
      )}

      {/* Measure-vs-dimension warnings */}
      {measureWarnings.length > 0 && (
        <Alert severity="warning" onClose={() => setMeasureWarnings([])}>
          <Typography variant="subtitle2" gutterBottom>
            {measureWarnings.length !== 1
              ? t("classification.measureWarningPlural", { count: String(measureWarnings.length) })
              : t("classification.measureWarningSingular", { count: String(measureWarnings.length) })}
          </Typography>
          {measureWarnings.map((w) => (
            <Typography key={w.column_id} variant="caption" display="block" sx={{ ml: 1, mb: 0.5 }}>
              <strong>{w.column_name}</strong> ({w.severity}): {w.reason}
            </Typography>
          ))}
        </Alert>
      )}

      {/* Column classification */}
      <Box>
        <Box display="flex" alignItems="center" justifyContent="space-between" mb={1}>
          <Typography variant="subtitle2">{t("classification.title")}</Typography>
          <Button
            size="small"
            variant="outlined"
            startIcon={
              analyzing ? <CircularProgress size={14} /> : <AutoFixHighIcon fontSize="small" />
            }
            onClick={runAutoClassify}
            disabled={analyzing}
          >
            {analyzing ? t("classification.classifying") : t("classification.autoClassify")}
          </Button>
        </Box>
        <Typography variant="caption" color="text.secondary" display="block" mb={1}>
          {t("classification.description")}
        </Typography>

        {columns.length === 0 ? (
          <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
            {t("classification.noColumns")}
          </Typography>
        ) : (
          <TableContainer
            sx={{ border: 1, borderColor: "divider", borderRadius: 1, maxHeight: 380 }}
          >
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50" }}>
                    {t("classification.colColumn")}
                  </TableCell>
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50", width: 80 }}>
                    {t("classification.colType")}
                  </TableCell>
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50", width: 180 }}>
                    {t("classification.colRole")}
                  </TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {columns.map((col) => {
                  const changed = col.role !== col.originalRole;
                  const style = ROLE_STYLES[col.role];
                  return (
                    <TableRow
                      key={col.id}
                      sx={{
                        bgcolor: changed ? "rgba(0,108,53,0.03)" : undefined,
                        "&:hover": { bgcolor: "action.hover" },
                      }}
                    >
                      <TableCell
                        sx={{
                          fontFamily: "monospace",
                          fontSize: "0.8rem",
                          py: 0.75,
                          textDecoration: col.role === "ignore" ? "line-through" : "none",
                          color: col.isUserDefined
                            ? "secondary.main"
                            : col.role === "ignore"
                              ? "text.disabled"
                              : "text.primary",
                          fontStyle: col.isUserDefined ? "italic" : "normal",
                        }}
                      >
                        {col.isUserDefined ? t("classification.computedPrefix") : ""}{col.name}
                      </TableCell>
                      <TableCell
                        sx={{
                          fontSize: "0.75rem",
                          color: col.isUserDefined ? "secondary.main" : "text.secondary",
                          py: 0.75,
                        }}
                      >
                        {col.dataType}{col.isUserDefined ? t("classification.computedSuffix") : ""}
                      </TableCell>
                      <TableCell sx={{ py: 0.5 }}>
                        <Select
                          size="small"
                          value={col.role}
                          onChange={(e) => setColumnRole(col.id, e.target.value as ColumnRole)}
                          sx={{
                            height: 28,
                            fontSize: "0.75rem",
                            fontWeight: 600,
                            color: style.color,
                            bgcolor: style.bg,
                            minWidth: 140,
                            "& .MuiSelect-select": { py: 0.5 },
                          }}
                        >
                          {COLUMN_ROLE_OPTIONS
                            .filter((o) => !col.isUserDefined || o.value !== "ignore")
                            .map((o) => (
                              <MenuItem key={o.value} value={o.value} sx={{ fontSize: "0.8rem" }}>
                                {t(o.label)}
                              </MenuItem>
                            ))}
                        </Select>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TableContainer>
        )}
      </Box>

      {/* Save */}
      <Box display="flex" justifyContent="flex-end" pt={1}>
        <Button
          variant="contained"
          onClick={handleSave}
          disabled={!dirty || saving}
          startIcon={saving ? <CircularProgress size={14} color="inherit" /> : undefined}
        >
          {saving ? t("classification.saving") : t("classification.save")}
        </Button>
      </Box>

      {/* Pre-save name conflict preview */}
      <AttributeRenameDialog
        open={previewOpen}
        mode="new-table"
        rows={previewRows}
        takenNames={stablePreviewTaken}
        applying={saving}
        onApply={handlePreviewApply}
        onKeep={handlePreviewCancel}
        onRevert={handlePreviewCancel}
      />
    </Stack>
  );
}
