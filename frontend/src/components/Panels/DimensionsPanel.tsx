import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  FormControlLabel,
  IconButton,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import AccessTimeIcon from "@mui/icons-material/AccessTime";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { canEditModelConfig } from "../../auth/currentUser";
import { dimensionsApi, modelTablesApi } from "../../api/client";
import { useAllModelTables, useDimensions, useModelSourceStatistics, useSources, useTableAttributes } from "../../api/hooks";
import { useConfirm } from "../Confirm";
import { useBuilderStore } from "../../store/builderStore";
import { ui } from "../../theme/tokens";
import type { DimensionCreate } from "../../api/types";
import DimensionCalendarAssociation from "../Builder/DimensionCalendarAssociation";

const TIME_GRAINS = ["year", "quarter", "month", "week", "day", "hour", "minute"] as const;

export default function DimensionsPanel() {
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const qc = useQueryClient();
  const t = useT();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingDimId, setEditingDimId] = useState<string | null>(null);
  const [dimName, setDimName] = useState("");
  const [dimDisplay, setDimDisplay] = useState("");
  const [dimDescription, setDimDescription] = useState("");
  const [dimFolder, setDimFolder] = useState("");
  const [dimTableId, setDimTableId] = useState("");
  const [dimAttrId, setDimAttrId] = useState("");
  const [pendingDimAttrName, setPendingDimAttrName] = useState<string | null>(null);
  const [dimIsTime, setDimIsTime] = useState(false);
  const [dimTimeGrain, setDimTimeGrain] = useState("");
  const [dimCalcExpression, setDimCalcExpression] = useState("");
  const [dimCalendarId, setDimCalendarId] = useState<string | null>(null);
  const [dimCalendarType, setDimCalendarType] = useState<string | null>(null);
  const [dimHierarchyLevels, setDimHierarchyLevels] = useState<string[]>([]);

  const storeReadOnly = useBuilderStore((s) => s.readOnly);
  const canEdit = canEditModelConfig() && !storeReadOnly;
  const dimensions = useDimensions(projectId!, modelId!);
  const sources = useSources(projectId!, modelId!);
  const sourceIds = (sources.data ?? []).map((s) => s.id);
  const allTables = useAllModelTables(projectId!, modelId!, sourceIds);
  const { columnStatsMap } = useModelSourceStatistics(projectId!, modelId!);
  const tableAttributes = useTableAttributes(projectId!, modelId!, dimTableId);
  const selectedAttribute = (tableAttributes.data ?? []).find((a) => a.id === dimAttrId);
  const selectedTable = allTables.data?.find((t) => t.id === dimTableId);
  const isDateAttribute = selectedAttribute
    ? /date|timestamp|datetime/i.test(selectedAttribute.data_type)
    : false;

  useEffect(() => {
    if (!pendingDimAttrName || !tableAttributes.data) return;
    const match = tableAttributes.data.find(
      (a) => !a.is_user_defined && a.name === pendingDimAttrName,
    );
    if (match) {
      setDimAttrId(match.id);
    }
    setPendingDimAttrName(null);
  }, [pendingDimAttrName, tableAttributes.data]);

  function buildDimensionPayload(): DimensionCreate {
    return {
      name: dimName,
      display_name: dimDisplay || dimName,
      description: dimDescription.trim() ? dimDescription.trim() : null,
      display_folder: dimFolder.trim() ? dimFolder.trim() : null,
      source_table_id:
        selectedAttribute && !selectedAttribute.is_user_defined
          ? dimTableId || undefined
          : undefined,
      source_column_name:
        selectedAttribute && !selectedAttribute.is_user_defined
          ? selectedAttribute.name
          : undefined,
      user_defined_attribute_id:
        selectedAttribute && selectedAttribute.is_user_defined
          ? dimAttrId
          : undefined,
      is_time_dim: dimIsTime,
      time_grain: dimIsTime && dimTimeGrain ? dimTimeGrain : undefined,
      calc_expression: dimCalcExpression.trim() || null,
    };
  }

  /** After creating/updating a dimension, persist the calendar association
   *  on the source model table if the user selected one (Bug-5245/5297). */
  async function persistCalendarAssociation() {
    if (!dimCalendarId || !dimTableId || !selectedTable) return;
    try {
      await modelTablesApi.update(
        projectId!,
        modelId!,
        selectedTable.source_id,
        dimTableId,
        { calendar_table_id: dimCalendarId },
      );
      qc.invalidateQueries({ queryKey: ["modelTables"] });
    } catch {
      // Calendar binding is best-effort; the dimension itself has been saved.
    }
  }

  const createDim = useMutation({
    mutationFn: () => dimensionsApi.create(projectId!, modelId!, buildDimensionPayload()),
    onSuccess: async () => {
      await persistCalendarAssociation();
      qc.invalidateQueries({
        queryKey: ["dimensions", projectId, modelId],
      });
      setDialogOpen(false);
    },
  });

  const updateDim = useMutation({
    mutationFn: () =>
      dimensionsApi.update(projectId!, modelId!, editingDimId!, buildDimensionPayload()),
    onSuccess: async () => {
      await persistCalendarAssociation();
      qc.invalidateQueries({
        queryKey: ["dimensions", projectId, modelId],
      });
      setDialogOpen(false);
      setEditingDimId(null);
    },
  });

  const deleteDim = useMutation({
    mutationFn: (id: string) =>
      dimensionsApi.delete(projectId!, modelId!, id),
    onSuccess: () =>
      qc.invalidateQueries({
        queryKey: ["dimensions", projectId, modelId],
      }),
  });

  const confirm = useConfirm();
  async function handleDeleteDim(id: string, name: string) {
    const ok = await confirm({
      title: t("dimensions.deleteTitle"),
      message: (
        <span>
          {t("dimensions.deleteMessage", { name })}
        </span>
      ),
      confirmLabel: t("common.delete"),
    });
    if (ok) deleteDim.mutate(id);
  }

  function tableLabel(tableId: string | null) {
    if (!tableId) return null;
    const t = allTables.data?.find((t) => t.id === tableId);
    return t?.alias ?? t?.display_name ?? null;
  }

  function openDialog() {
    setEditingDimId(null);
    setDimName("");
    setDimDisplay("");
    setDimDescription("");
    setDimFolder("");
    setDimTableId("");
    setDimAttrId("");
    setPendingDimAttrName(null);
    setDimIsTime(false);
    setDimTimeGrain("");
    setDimCalcExpression("");
    setDimCalendarId(null);
    setDimCalendarType(null);
    setDimHierarchyLevels([]);
    setDialogOpen(true);
  }

  function openEditDialog(dimId: string) {
    const dim = dimensions.data?.find((d) => d.id === dimId);
    if (!dim) return;
    setEditingDimId(dim.id);
    setDimName(dim.name);
    setDimDisplay(dim.display_name && dim.display_name !== dim.name ? dim.display_name : "");
    setDimDescription(dim.description ?? "");
    setDimFolder(dim.display_folder ?? "");
    setDimTableId(dim.source_table_id ?? "");
    if (dim.user_defined_attribute_id) {
      setDimAttrId(dim.user_defined_attribute_id);
      setPendingDimAttrName(null);
    } else {
      setDimAttrId("");
      setPendingDimAttrName(dim.source_column_name ?? null);
    }
    setDimIsTime(dim.is_time_dim);
    setDimTimeGrain(dim.time_grain ?? "");
    setDimCalcExpression(dim.calc_expression ?? "");
    // Bug-5245/5297: restore calendar association state so the picker shows the
    // current binding on edit, not blank.  selectedTable is derived from
    // dimTableId which we just set above, but it won't be available until the
    // next render.  Read directly from allTables instead.
    const editTable = allTables.data?.find((t) => t.id === (dim.source_table_id ?? ""));
    setDimCalendarId(editTable?.calendar_table_id ?? null);
    setDimCalendarType(null);
    setDimHierarchyLevels([]);
    setDialogOpen(true);
  }

  return (
    <Box>
      <Box display="flex" alignItems="center" mb={1.5}>
        <Typography variant="body2" color="text.secondary" sx={{ flex: 1 }}>
          {t("dimensions.description")}
        </Typography>
        {canEdit && (
          <Button
            size="small"
            variant="contained"
            startIcon={<AddIcon />}
            onClick={openDialog}
            sx={{ ml: 1, whiteSpace: "nowrap" }}
          >
            {t("dimensions.add")}
          </Button>
        )}
      </Box>

      {dimensions.isLoading ? (
        <CircularProgress size={20} />
      ) : (
        <TableContainer component={Paper} variant="outlined">
          <Table size="small">
            <TableHead>
              <TableRow sx={{ bgcolor: ui.tableHeaderBg }}>
                <TableCell><strong>{t("dimensions.name")}</strong></TableCell>
                <TableCell><strong>{t("dimensions.source")}</strong></TableCell>
                <TableCell><strong>{t("dimensions.type")}</strong></TableCell>
                <TableCell align="right"><strong>{t("dimensions.distinct")}</strong></TableCell>
                <TableCell><strong>{t("dimensions.time")}</strong></TableCell>
                {canEdit && <TableCell />}
              </TableRow>
            </TableHead>
            <TableBody>
              {dimensions.data?.map((d) => {
                const colStats = d.source_column_id ? columnStatsMap[d.source_column_id] : undefined;
                return (
                <TableRow key={d.id}>
                  <TableCell>
                    <Typography variant="body2" fontWeight={500}>
                      {d.name}
                    </Typography>
                    {d.display_name !== d.name && (
                      <Typography variant="caption" color="text.secondary">
                        {d.display_name}
                      </Typography>
                    )}
                  </TableCell>
                  <TableCell>
                    {d.calc_expression ? (
                      <Typography variant="caption" sx={{ fontStyle: "italic", color: ui.purple, fontSize: 11 }}>calc</Typography>
                    ) : d.source_column_name || d.user_defined_attribute_name ? (
                      <Typography variant="caption" sx={{ fontFamily: "monospace", fontSize: 11, color: d.user_defined_attribute_name ? ui.purple : ui.muted }}>
                        {`${tableLabel(d.source_table_id) ?? "?"}.${d.user_defined_attribute_name ?? d.source_column_name}`}
                      </Typography>
                    ) : (
                      <Typography variant="caption" color="text.secondary">--</Typography>
                    )}
                  </TableCell>
                  <TableCell>
                    <Typography variant="caption" color="text.secondary" sx={{ fontSize: 11 }}>
                      {colStats?.data_type ?? d.data_type ?? "--"}
                    </Typography>
                  </TableCell>
                  <TableCell align="right">
                    {colStats?.distinct_count != null ? (
                      <Tooltip title={`${colStats.distinct_count.toLocaleString()} distinct of ${(colStats.row_count ?? 0).toLocaleString()} rows`}>
                        <Typography variant="caption" sx={{ fontSize: 11 }}>
                          {colStats.distinct_count.toLocaleString()}
                          {colStats.row_count && colStats.row_count > 0
                            ? ` (${((colStats.distinct_count / colStats.row_count) * 100).toFixed(1)}%)`
                            : ""}
                        </Typography>
                      </Tooltip>
                    ) : (
                      <Typography variant="caption" color="text.secondary">--</Typography>
                    )}
                  </TableCell>
                  <TableCell>
                    <Box display="flex" gap={0.5} alignItems="center" flexWrap="wrap">
                      {d.is_time_dim && (
                        <Typography component="span" variant="caption" sx={{ display: "inline-flex", alignItems: "center", gap: 0.25, px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.goldBg, color: ui.goldDark, fontWeight: 500, fontSize: 11 }}>
                          <AccessTimeIcon sx={{ fontSize: 12 }} /> {d.time_grain ?? "time"}
                        </Typography>
                      )}
                      {d.high_cardinality && (
                        <Tooltip title="High cardinality — over 50% distinct values relative to row count">
                          <Typography component="span" variant="caption" sx={{ display: "inline-flex", alignItems: "center", gap: 0.25, px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: "warning.lighter", color: "warning.dark", fontWeight: 500, fontSize: 11 }}>
                            <WarningAmberIcon sx={{ fontSize: 12 }} /> high cardinality
                          </Typography>
                        </Tooltip>
                      )}
                    </Box>
                  </TableCell>
                  {canEdit && (
                  <TableCell align="right">
                    <Tooltip title={t("common.edit")}>
                      <IconButton
                        size="small"
                        onClick={() => openEditDialog(d.id)}
                      >
                        <EditIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title={t("common.delete")}>
                      <IconButton
                        size="small"
                        onClick={() => handleDeleteDim(d.id, d.name)}
                      >
                        <DeleteIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  </TableCell>
                  )}
                </TableRow>
                );
              })}
              {dimensions.data?.length === 0 && (
                <TableRow>
                  <TableCell colSpan={canEdit ? 6 : 5}>
                    <Typography variant="body2" color="text.secondary">
                      {t("dimensions.none")}
                    </Typography>
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>{editingDimId ? t("dimensions.edit") : t("dimensions.add")}</DialogTitle>
        <DialogContent>
          <TextField
            label={t("dimensions.nameLabel")}
            fullWidth
            margin="normal"
            value={dimName}
            onChange={(e) => setDimName(e.target.value)}
            autoFocus
          />
          <TextField
            label={t("common.displayName")}
            fullWidth
            margin="normal"
            value={dimDisplay}
            onChange={(e) => setDimDisplay(e.target.value)}
          />
          <TextField
            label={t("dimensions.businessDescription")}
            fullWidth
            margin="normal"
            multiline
            minRows={2}
            maxRows={6}
            value={dimDescription}
            onChange={(e) => setDimDescription(e.target.value)}
            placeholder={t("dimensions.businessDescriptionPlaceholder")}
          />
          <TextField
            label={t("dimensions.displayFolder")}
            fullWidth
            margin="normal"
            value={dimFolder}
            onChange={(e) => setDimFolder(e.target.value)}
            placeholder={t("dimensions.displayFolderPlaceholder")}
          />

          <Typography variant="subtitle2" sx={{ mt: 2, mb: 0.5 }}>
            {t("dimensions.sourceColumn")}
          </Typography>
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("dimensions.tableLabel")}</InputLabel>
            <Select
              value={dimTableId}
              label={t("dimensions.tableLabel")}
              onChange={(e) => {
                setDimTableId(e.target.value);
                setDimAttrId("");
                setPendingDimAttrName(null);
              }}
            >
              <MenuItem value="">{t("common.none")}</MenuItem>
              {allTables.data?.map((t) => (
                <MenuItem key={t.id} value={t.id}>
                  {t.alias ?? t.display_name} ({t.physical_name})
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth margin="dense" disabled={!dimTableId}>
            <InputLabel>{t("dimensions.attributeLabel")}</InputLabel>
            <Select
              value={dimAttrId}
              label={t("dimensions.attributeLabel")}
              onChange={(e) => setDimAttrId(e.target.value)}
            >
              <MenuItem value="">{t("common.none")}</MenuItem>
              {tableAttributes.data?.map((attr) => (
                <MenuItem key={attr.id} value={attr.id}>
                  {attr.is_user_defined ? `fx ${attr.name}` : attr.name}
                </MenuItem>
              ))}
              {tableAttributes.isLoading && (
                <MenuItem value="" disabled>
                  <CircularProgress size={14} sx={{ mr: 1 }} /> {t("dimensions.loadingAttributes")}
                </MenuItem>
              )}
            </Select>
          </FormControl>

          <Typography variant="subtitle2" sx={{ mt: 2, mb: 0.5 }}>
            {t("dimensions.formulaExpression")}
          </Typography>
          <TextField
            label={t("dimensions.sqlExpression")}
            fullWidth
            margin="dense"
            multiline
            minRows={2}
            maxRows={6}
            value={dimCalcExpression}
            onChange={(e) => setDimCalcExpression(e.target.value)}
            placeholder={t("dimensions.sqlExpressionPlaceholder")}
            helperText={t("dimensions.sqlExpressionHelp")}
            inputProps={{ style: { fontFamily: "monospace", fontSize: 13 } }}
          />

          <Box sx={{ mt: 2 }}>
            <FormControlLabel
              control={
                <Switch
                  checked={dimIsTime}
                  onChange={(e) => setDimIsTime(e.target.checked)}
                />
              }
              label={t("dimensions.timeDimension")}
            />
            {dimIsTime && (
              <FormControl fullWidth margin="dense">
                <InputLabel>{t("dimensions.timeGrainLabel")}</InputLabel>
                <Select
                  value={dimTimeGrain}
                  label={t("dimensions.timeGrainLabel")}
                  onChange={(e) => setDimTimeGrain(e.target.value)}
                >
                  {TIME_GRAINS.map((g) => (
                    <MenuItem key={g} value={g}>
                      {g}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
            )}
          </Box>

          <DimensionCalendarAssociation
            projectId={projectId!}
            modelId={modelId!}
            sourceId={selectedTable?.source_id ?? ""}
            visible={isDateAttribute || dimIsTime}
            factTable={selectedTable?.physical_name}
            initialCalendarId={dimCalendarId}
            factDateColumn={
              selectedAttribute && !selectedAttribute.is_user_defined
                ? selectedAttribute.name
                : undefined
            }
            onCalendarSelect={(calId, calType) => {
              setDimCalendarId(calId);
              setDimCalendarType(calType);
            }}
            onHierarchyLevels={(levels) => {
              setDimHierarchyLevels(levels);
            }}
          />

          {(createDim.isError || updateDim.isError) && (
            <Alert severity="error" sx={{ mt: 1 }}>
              {t(editingDimId ? "dimensions.updateFailed" : "dimensions.createFailed")}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => (editingDimId ? updateDim.mutate() : createDim.mutate())}
            disabled={!dimName || createDim.isPending || updateDim.isPending}
          >
            {createDim.isPending || updateDim.isPending ? (
              <CircularProgress size={18} />
            ) : editingDimId ? t("common.save") : t("common.add")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
