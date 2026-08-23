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
import { useCanAuthorModel } from "../../auth/useCanAuthorModel";
import { dimensionsApi, modelTablesApi } from "../../api/client";
import { recordCreate, recordUpdate, recordDelete } from "../Builder/emitDrawerHistory";
import { useAllModelTables, useDimensions, useModelSourceStatistics, useSources, useTableAttributes } from "../../api/hooks";
import { useConfirm } from "../Confirm";
import { extractApiError } from "../../utils/extractApiError";
import { ui } from "../../theme/tokens";
import type { DimensionCreate } from "../../api/types";
import DimensionCalendarAssociation from "../Builder/DimensionCalendarAssociation";
import AttributeRelationshipsSection from "./AttributeRelationshipsSection";

const TIME_GRAINS = ["year", "quarter", "month", "week", "day", "hour", "minute"] as const;

/**
 * Map a persisted dimension to a create-shaped payload so undo/redo can restore
 * it (Bug-8227). Used to build the inverse op for a delete (re-create) and the
 * prior-values op for an update.
 */
function dimensionToPayload(d: import("../../api/types").Dimension): Record<string, unknown> {
  // Use ?? null (not ?? undefined) so undo actively resets a newly-set field
  // back to null via the PATCH (Fable review finding 3).
  return {
    name: d.name,
    display_name: d.display_name || d.name,
    description: d.description ?? null,
    display_folder: d.display_folder ?? null,
    source_table_id: d.source_table_id ?? null,
    source_column_name: d.source_column_name ?? null,
    display_column_name: d.display_column_name ?? null,
    data_type: d.data_type ?? null,
    user_defined_attribute_id: d.user_defined_attribute_id ?? null,
    is_time_dim: d.is_time_dim,
    time_grain: d.time_grain ?? null,
    calc_expression: d.calc_expression ?? null,
  };
}

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
  // Bug-5502: optional DISPLAY column (caption source) for a flat dimension.
  // Holds the source column NAME ("" = none) to match the create/update payload
  // field `display_column_name`.
  const [dimDisplayColumnName, setDimDisplayColumnName] = useState("");
  const [dimIsTime, setDimIsTime] = useState(false);
  const [dimTimeGrain, setDimTimeGrain] = useState("");
  const [dimCalcExpression, setDimCalcExpression] = useState("");
  const [dimCalendarId, setDimCalendarId] = useState<string | null>(null);
  const [dimCalendarType, setDimCalendarType] = useState<string | null>(null);
  const [dimHierarchyLevels, setDimHierarchyLevels] = useState<string[]>([]);
  // F-026-01: surfaces a failed calendar-association PATCH. The dimension row
  // itself has been written by the time this fires, so we keep the dialog open
  // and show a retryable error rather than reporting the combined save as
  // successful with the calendar binding silently dropped.
  //
  // Bug-8904 sibling: this used to be a bare boolean, so the panel rendered one
  // fixed sentence no matter WHY the PATCH was refused. The body-FK guards
  // answer 422 with a structured detail ({error_code, field, message}) that
  // names the actual reason — "that calendar belongs to another model" reads
  // very differently from a transport failure. Hold the server's own message
  // so the modeller is told what to fix, with the fixed sentence kept only as
  // the fallback for a failure that carried no message.
  const [calendarError, setCalendarError] = useState<string | null>(null);

  const canEdit = useCanAuthorModel();
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
  // Bug-5502: the display-column picker only applies to a flat (single-level)
  // dimension backed by a real source column — not time dims, calc expressions
  // or user-defined attributes (those have no distinct caption column to pick).
  const isFlatSourceDimension =
    !!selectedAttribute &&
    !selectedAttribute.is_user_defined &&
    !dimIsTime &&
    !dimCalcExpression.trim();

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
      // Bug-5502: send the display column only for an eligible flat dimension.
      // `null` clears it back to none; an unset/non-flat dim omits the field.
      display_column_name: isFlatSourceDimension
        ? dimDisplayColumnName || null
        : undefined,
      is_time_dim: dimIsTime,
      time_grain: dimIsTime && dimTimeGrain ? dimTimeGrain : undefined,
      calc_expression: dimCalcExpression.trim() || null,
    };
  }

  /** After creating/updating a dimension, persist the calendar association
   *  on the source model table if the user selected one (Bug-5245/5297).
   *
   *  F-026-01: this is a second, independent write for a single user intent
   *  ("this time dimension uses that calendar"). It must NOT be treated as
   *  best-effort — a discarded failure leaves the persisted model materially
   *  different from what the modeller configured. On failure it throws so the
   *  calling onSuccess handler can keep the dialog open and report the error
   *  instead of closing on a silently-partial save. */
  async function persistCalendarAssociation() {
    if (!dimCalendarId || !dimTableId || !selectedTable) return;
    await modelTablesApi.update(
      projectId!,
      modelId!,
      selectedTable.source_id,
      dimTableId,
      { calendar_table_id: dimCalendarId },
    );
    qc.invalidateQueries({ queryKey: ["modelTables"] });
  }

  const createDim = useMutation({
    mutationFn: async () => {
      const payload = buildDimensionPayload();
      const created = await dimensionsApi.create(projectId!, modelId!, payload);
      return { created, payload };
    },
    onSuccess: async ({ created, payload }) => {
      // Bug-8227: record the create so undo removes it / redo re-creates it.
      recordCreate("dimension", created.id, payload as unknown as Record<string, unknown>);
      qc.invalidateQueries({
        queryKey: ["dimensions", projectId, modelId],
      });
      try {
        await persistCalendarAssociation();
      } catch (err) {
        // Dimension row saved, calendar binding did not. Keep the dialog open
        // with a retryable error; do not report the combined save as done.
        // Promote to edit mode on the just-created id so a retry routes to
        // updateDim + persistCalendarAssociation, not another create (review
        // finding: duplicate-on-retry).
        setEditingDimId(created.id);
        setCalendarError(
          extractApiError(err, t("dimensions.calendarAssociationFailed")),
        );
        return;
      }
      setDialogOpen(false);
    },
  });

  const updateDim = useMutation({
    mutationFn: async () => {
      const payload = buildDimensionPayload();
      const prior = (dimensions.data ?? []).find((d) => d.id === editingDimId);
      const priorPayload = prior ? dimensionToPayload(prior) : null;
      await dimensionsApi.update(projectId!, modelId!, editingDimId!, payload);
      return { id: editingDimId!, payload, priorPayload };
    },
    onSuccess: async ({ id, payload, priorPayload }) => {
      if (priorPayload) {
        recordUpdate(
          "dimension",
          id,
          priorPayload,
          payload as unknown as Record<string, unknown>,
        );
      }
      qc.invalidateQueries({
        queryKey: ["dimensions", projectId, modelId],
      });
      try {
        await persistCalendarAssociation();
      } catch (err) {
        setCalendarError(
          extractApiError(err, t("dimensions.calendarAssociationFailed")),
        );
        return;
      }
      setDialogOpen(false);
      setEditingDimId(null);
    },
  });

  const deleteDim = useMutation({
    mutationFn: async (dim: import("../../api/types").Dimension) => {
      await dimensionsApi.delete(projectId!, modelId!, dim.id);
      return dim;
    },
    onSuccess: (dim) => {
      recordDelete(
        "dimension",
        dim.id,
        dimensionToPayload(dim),
      );
      qc.invalidateQueries({
        queryKey: ["dimensions", projectId, modelId],
      });
    },
  });

  // Attribute-relationship section binds to the PERSISTED dimension (not the
  // mid-edit form): a declaration pins the dimension's saved key column and its
  // detail must live in the saved source table. UDA/calc dims have no physical
  // key so they get no relationship section.
  const editingDim = editingDimId
    ? dimensions.data?.find((d) => d.id === editingDimId)
    : undefined;
  const editingDimSourceTableId =
    editingDim && editingDim.source_column_id
      ? editingDim.source_table_id
      : null;
  const editingDimKeyColumnName =
    editingDim && editingDim.source_column_id
      ? editingDim.source_column_name
      : null;

  const confirm = useConfirm();
  async function handleDeleteDim(dim: import("../../api/types").Dimension) {
    const ok = await confirm({
      title: t("dimensions.deleteTitle"),
      message: (
        <span>
          {t("dimensions.deleteMessage", { name: dim.name })}
        </span>
      ),
      confirmLabel: t("common.delete"),
    });
    if (ok) deleteDim.mutate(dim);
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
    setDimDisplayColumnName("");
    setDimIsTime(false);
    setDimTimeGrain("");
    setDimCalcExpression("");
    setDimCalendarId(null);
    setDimCalendarType(null);
    setDimHierarchyLevels([]);
    setCalendarError(null);
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
    // Bug-5502: restore the saved display column (by name) so the picker shows
    // the current caption source on edit, clearable back to none.
    setDimDisplayColumnName(dim.display_column_name ?? "");
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
    setCalendarError(null);
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
                      {d.detail_of_dimension_name && (
                        <Tooltip title={t("attributeRelationships.provenanceTooltip", { name: d.detail_of_dimension_name })}>
                          <Typography component="span" variant="caption" sx={{ display: "inline-flex", alignItems: "center", gap: 0.25, px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.goldBg, color: ui.goldDark, fontWeight: 500, fontSize: 11 }}>
                            {t("attributeRelationships.provenanceChip", { name: d.detail_of_dimension_name })}
                          </Typography>
                        </Tooltip>
                      )}
                      {(d.attribute_relationships?.length ?? 0) > 0 && !d.detail_of_dimension_name && (
                        <Tooltip title={t("attributeRelationships.pairMarkerTooltip", { count: d.attribute_relationships?.length ?? 0 })}>
                          <Typography component="span" variant="caption" sx={{ display: "inline-flex", alignItems: "center", gap: 0.25, px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.greenBg, color: ui.green, fontWeight: 500, fontSize: 11 }}>
                            {t("attributeRelationships.pairMarkerChip", { count: d.attribute_relationships?.length ?? 0 })}
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
                        onClick={() => handleDeleteDim(d)}
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

          {/* Bug-5502: optional display-column picker for flat dimensions. The
              key column above stays the identity; this picks a distinct caption
              column rendered to users. Clearable (a flat dim need not have one). */}
          {isFlatSourceDimension && (
            <FormControl fullWidth margin="dense" disabled={!dimTableId}>
              <InputLabel>{t("dimensions.displayColumnLabel")}</InputLabel>
              <Select
                value={dimDisplayColumnName}
                label={t("dimensions.displayColumnLabel")}
                onChange={(e) => setDimDisplayColumnName(e.target.value)}
              >
                <MenuItem value="">{t("common.none")}</MenuItem>
                {tableAttributes.data
                  ?.filter((attr) => !attr.is_user_defined)
                  .map((attr) => (
                    <MenuItem key={attr.id} value={attr.name}>
                      {attr.name}
                    </MenuItem>
                  ))}
              </Select>
              <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
                {t("dimensions.displayColumnHelp")}
              </Typography>
            </FormControl>
          )}

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

          {/* Derived-grain routing: declared key-to-detail relationships.
              Rendered for any persisted dimension. A relationship pins the
              dimension's current key column and needs a real detail column in
              the same source table, so the section itself explains (and blocks)
              when the dimension has no physical key column (UDA / calc dims). */}
          {editingDimId && (
            <AttributeRelationshipsSection
              projectId={projectId!}
              modelId={modelId!}
              dimensionId={editingDimId}
              sourceTableId={editingDimSourceTableId}
              keyColumnName={editingDimKeyColumnName}
              canEdit={canEdit}
            />
          )}

          {(createDim.isError || updateDim.isError) && (
            <Alert severity="error" sx={{ mt: 1 }}>
              {t(editingDimId ? "dimensions.updateFailed" : "dimensions.createFailed")}
            </Alert>
          )}
          {calendarError && (
            <Alert severity="error" sx={{ mt: 1 }}>
              {/* The fixed sentence states what the partial save left behind
                  (dimension written, calendar not linked) — the server cannot
                  know that. `calendarError` states WHY the server refused. Both
                  matter, so render both rather than letting the generic string
                  swallow the specific reason. */}
              {t("dimensions.calendarAssociationFailed")}
              {calendarError !== t("dimensions.calendarAssociationFailed") && (
                <Box component="span" sx={{ display: "block", mt: 0.5 }}>
                  {calendarError}
                </Box>
              )}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => {
              setCalendarError(null);
              return editingDimId ? updateDim.mutate() : createDim.mutate();
            }}
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
