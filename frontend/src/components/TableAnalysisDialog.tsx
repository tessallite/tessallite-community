import { useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControl,
  MenuItem,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import { dimensionsApi, measuresApi, modelTablesApi, tableAttributesApi } from "../api/client";
import type { DimensionCreate, MeasureCreate, TableAnalysis } from "../api/types";
import { useQueryClient } from "@tanstack/react-query";
import { useT } from "../i18n";

interface Props {
  open: boolean;
  onClose: () => void;
  projectId: string;
  modelId: string;
  sourceId: string;
  tableId: string;
  tableName: string | null | undefined;
}

const CONFIDENCE_COLOR: Record<string, "success" | "warning" | "error"> = {
  high: "success",
  medium: "warning",
  low: "error",
};

const ROLE_COLOR: Record<string, "primary" | "secondary" | "default" | "info"> = {
  measure: "primary",
  dimension: "secondary",
  date_key: "info",
  ignore: "default",
};

function toDisplayName(col: string): string {
  return col
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export default function TableAnalysisDialog({
  open,
  onClose,
  projectId,
  modelId,
  sourceId,
  tableId,
  tableName,
}: Props) {
  const t = useT();
  const qc = useQueryClient();
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<TableAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);

  // User overrides — populated after analysis, editable before Apply.
  const [typeOverride, setTypeOverride] = useState<string | null>(null);
  const [roleOverrides, setRoleOverrides] = useState<Record<string, string>>({});

  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [applySuccess, setApplySuccess] = useState(false);

  const TABLE_TYPE_OPTIONS = [
    { value: "fact", label: t("columnRole.fact") },
    { value: "dim_aggregate", label: t("columnRole.dimensionAggregate") },
    { value: "dim_detail", label: t("columnRole.dimensionDetail") },
  ];

  const COLUMN_ROLE_OPTIONS = [
    { value: "measure", label: t("columnRole.measure") },
    { value: "dimension", label: t("columnRole.dimension") },
    { value: "date_key", label: t("columnRole.dateKey") },
    { value: "ignore", label: t("columnRole.ignore") },
  ];

  async function runAnalysis() {
    setLoading(true);
    setError(null);
    setTypeOverride(null);
    setRoleOverrides({});
    setApplySuccess(false);
    setApplyError(null);
    try {
      const data = await modelTablesApi.analyze(projectId, modelId, sourceId, tableId);
      setResult(data);
    } catch (err: unknown) {
      setError((err as Error).message ?? t("tableAnalysis.analysisFailed"));
    } finally {
      setLoading(false);
    }
  }

  async function applyResult() {
    if (!result) return;
    setApplying(true);
    setApplyError(null);
    try {
      const finalType = typeOverride ?? result.suggested_table_type;
      await modelTablesApi.update(projectId, modelId, sourceId, tableId, {
        table_type: finalType as "fact" | "dim_aggregate" | "dim_detail",
      });

      const [existingMeasures, existingDims] = await Promise.all([
        measuresApi.list(projectId, modelId),
        dimensionsApi.list(projectId, modelId),
      ]);

      const existingMeasureCols = new Set(
        existingMeasures
          .filter((m) => m.source_table_id === tableId)
          .map((m) => (m.source_column_name ?? m.name).toLowerCase()),
      );
      const existingDimCols = new Set(
        existingDims
          .filter((d) => d.source_table_id === tableId)
          .map((d) => (d.source_column_name ?? d.name).toLowerCase()),
      );

      for (const s of result.column_suggestions) {
        const role = roleOverrides[s.column_id] ?? s.suggested_role;
        const colLower = s.column_name.toLowerCase();

        if (role === "measure" && !existingMeasureCols.has(colLower)) {
          const payload: MeasureCreate = {
            name: s.column_name,
            display_name: toDisplayName(s.column_name),
            source_table_id: tableId,
            source_column_name: s.column_name,
            default_agg: "sum",
          };
          await measuresApi.create(projectId, modelId, payload);
        } else if ((role === "dimension" || role === "date_key") && !existingDimCols.has(colLower)) {
          const payload: DimensionCreate = {
            name: s.column_name,
            display_name: toDisplayName(s.column_name),
            source_table_id: tableId,
            source_column_name: s.column_name,
            ...(role === "date_key" ? { is_time_dim: true } : {}),
          };
          await dimensionsApi.create(projectId, modelId, payload);
        } else if (role === "ignore") {
          await tableAttributesApi.updateColumn(projectId, modelId, tableId, s.column_id, {
            is_hidden: true,
          });
        }
      }

      qc.invalidateQueries({ queryKey: ["modelTables", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["tableAttributes", projectId, modelId, tableId] });
      qc.invalidateQueries({ queryKey: ["measures", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
      setApplySuccess(true);
    } catch (err: unknown) {
      setApplyError((err as Error).message ?? t("tableAnalysis.applyFailed"));
    } finally {
      setApplying(false);
    }
  }

  function handleClose() {
    setResult(null);
    setError(null);
    setTypeOverride(null);
    setRoleOverrides({});
    setApplySuccess(false);
    setApplyError(null);
    onClose();
  }

  const effectiveType = typeOverride ?? result?.suggested_table_type ?? "";

  return (
    <Dialog open={open} onClose={handleClose} maxWidth="md" fullWidth>
      <DialogTitle>{t("tableAnalysis.title", { tableName: tableName ?? "" })}</DialogTitle>

      <DialogContent dividers>
        {!result && !loading && !error && (
          <Box sx={{ py: 2, textAlign: "center" }}>
            <Typography variant="body2" color="text.secondary" gutterBottom>
              {t("tableAnalysis.description")}
            </Typography>
            <Button variant="contained" onClick={runAnalysis} sx={{ mt: 2 }}>
              {t("tableAnalysis.runAnalysis")}
            </Button>
          </Box>
        )}

        {loading && (
          <Box sx={{ py: 4, textAlign: "center" }}>
            <CircularProgress size={32} />
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
              {t("tableAnalysis.analysing")}
            </Typography>
          </Box>
        )}

        {error && (
          <Alert severity="error" sx={{ mb: 1 }}>{error}</Alert>
        )}

        {result && (
          <Box>
            <Box sx={{ display: "flex", alignItems: "center", gap: 1.5, mb: 1 }}>
              <Typography variant="subtitle2">{t("tableAnalysis.suggestedType")}</Typography>
              <FormControl size="small" sx={{ minWidth: 180 }}>
                <Select
                  value={effectiveType}
                  onChange={(e) => setTypeOverride(e.target.value)}
                  sx={{ height: 28, fontSize: 13 }}
                >
                  {TABLE_TYPE_OPTIONS.map((o) => (
                    <MenuItem key={o.value} value={o.value}>{o.label}</MenuItem>
                  ))}
                </Select>
              </FormControl>
              <Chip
                label={t("tableAnalysis.confidence", { confidence: result.confidence })}
                size="small"
                color={CONFIDENCE_COLOR[result.confidence] ?? "default"}
                variant="outlined"
              />
            </Box>

            <Typography variant="body2" sx={{ mb: 2 }}>
              {result.reasoning}
            </Typography>

            {result.potential_calendar_column && (
              <Typography variant="body2" sx={{ mb: 2 }}>
                <strong>{t("tableAnalysis.potentialCalendar")}</strong>{" "}
                <code>{result.potential_calendar_column}</code>
              </Typography>
            )}

            <Divider sx={{ mb: 2 }} />

            <Typography variant="subtitle2" gutterBottom>
              {t("tableAnalysis.columnSuggestions")}
            </Typography>
            <Typography variant="caption" color="text.secondary" display="block" mb={1}>
              {t("tableAnalysis.helpText")}
            </Typography>

            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>{t("tableAnalysis.columnHeader")}</TableCell>
                  <TableCell>{t("tableAnalysis.suggestedRoleHeader")}</TableCell>
                  <TableCell>{t("tableAnalysis.reasonHeader")}</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {result.column_suggestions.map((s) => {
                  const role = roleOverrides[s.column_id] ?? s.suggested_role;
                  return (
                    <TableRow key={s.column_id}>
                      <TableCell sx={{ fontFamily: "monospace", fontSize: "0.8rem" }}>
                        {s.column_name}
                      </TableCell>
                      <TableCell>
                        <FormControl size="small" sx={{ minWidth: 150 }}>
                          <Select
                            value={role}
                            onChange={(e) =>
                              setRoleOverrides((prev) => ({
                                ...prev,
                                [s.column_id]: e.target.value,
                              }))
                            }
                            sx={{ height: 26, fontSize: 12 }}
                            renderValue={(v) => (
                              <Chip
                                label={COLUMN_ROLE_OPTIONS.find((o) => o.value === v)?.label ?? v}
                                size="small"
                                color={ROLE_COLOR[v] ?? "default"}
                                variant="outlined"
                              />
                            )}
                          >
                            {COLUMN_ROLE_OPTIONS.map((o) => (
                              <MenuItem key={o.value} value={o.value}>{o.label}</MenuItem>
                            ))}
                          </Select>
                        </FormControl>
                      </TableCell>
                      <TableCell>
                        <Typography variant="caption">{s.reason}</Typography>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>

            {applyError && (
              <Alert severity="error" sx={{ mt: 2 }}>{applyError}</Alert>
            )}
            {applySuccess && (
              <Alert severity="success" sx={{ mt: 2 }}>
                {t("tableAnalysis.suggestionsApplied")}
              </Alert>
            )}
          </Box>
        )}
      </DialogContent>

      <DialogActions>
        {result && !applySuccess && (
          <>
            <Button size="small" onClick={runAnalysis} disabled={loading || applying}>
              {t("tableAnalysis.reRun")}
            </Button>
            <Button
              variant="contained"
              size="small"
              onClick={applyResult}
              disabled={applying}
              startIcon={applying ? <CircularProgress size={14} /> : undefined}
            >
              {applying ? t("tableAnalysis.applying") : t("tableAnalysis.applySuggestions")}
            </Button>
          </>
        )}
        {result && applySuccess && (
          <Button size="small" onClick={runAnalysis} disabled={loading}>
            {t("tableAnalysis.reRun")}
          </Button>
        )}
        <Button onClick={handleClose}>{t("common.close")}</Button>
      </DialogActions>
    </Dialog>
  );
}
