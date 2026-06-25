import { useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Collapse,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  IconButton,
  InputLabel,
  List,
  ListItem,
  ListItemText,
  MenuItem,
  Select,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import { dataTagsApi, tableAttributesApi } from "../../api/client";
import { useAllModelTables, useDataTags, useSources } from "../../api/hooks";
import { useConfirm } from "../Confirm";
import { useT } from "../../i18n";
import type {
  DataTag,
  DataTagCreate,
  DataTagUpdate,
  TableAttribute,
} from "../../api/types";

interface TagFormState {
  tag_name: string;
  description: string;
  // F-008-08: column assignment — the whole point of a data tag.
  columnIds: string[];
  // column_id -> "table.column" labels for the assigned-column chips.
  columnLabels: Record<string, string>;
}

const EMPTY_FORM: TagFormState = {
  tag_name: "",
  description: "",
  columnIds: [],
  columnLabels: {},
};

export default function DataTagsPanel() {
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const t = useT();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);
  const [form, setForm] = useState<TagFormState>(EMPTY_FORM);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  // F-008-17: surface list-level mutation errors (e.g. a failed delete) that
  // happen while the dialog is closed.
  const [panelError, setPanelError] = useState<string | null>(null);
  const [pickerTableId, setPickerTableId] = useState<string>("");

  const tags = useDataTags(projectId!, modelId!);
  const sources = useSources(projectId!, modelId!);
  const sourceIds = useMemo(
    () => (sources.data ?? []).map((s) => s.id),
    [sources.data],
  );
  const allTables = useAllModelTables(projectId!, modelId!, sourceIds);

  const pickerColumns = useQuery({
    queryKey: ["tableAttributes", projectId, modelId, pickerTableId],
    queryFn: () => tableAttributesApi.list(projectId!, modelId!, pickerTableId),
    enabled: !!projectId && !!modelId && !!pickerTableId && dialogOpen,
  });

  const pickerTable = (allTables.data ?? []).find((tb) => tb.id === pickerTableId);
  const pickerTableLabel = pickerTable
    ? pickerTable.alias || pickerTable.physical_name
    : "";

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["data-tags", projectId, modelId] });
  };

  const createMut = useMutation({
    mutationFn: (data: DataTagCreate) =>
      dataTagsApi.create(projectId!, modelId!, data),
    onSuccess: () => {
      invalidate();
      setDialogOpen(false);
      setFormError(null);
    },
    onError: (e: unknown) =>
      setFormError(
        t("dataTags.saveFailed", { error: extractError(e) || t("errors.requestFailed") }),
      ),
  });

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: DataTagUpdate }) =>
      dataTagsApi.update(projectId!, modelId!, id, data),
    onSuccess: () => {
      invalidate();
      setDialogOpen(false);
      setFormError(null);
    },
    onError: (e: unknown) =>
      setFormError(
        t("dataTags.saveFailed", { error: extractError(e) || t("errors.requestFailed") }),
      ),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => dataTagsApi.delete(projectId!, modelId!, id),
    onSuccess: () => {
      invalidate();
      setPanelError(null);
    },
    // F-008-17: a delete failure must surface, not vanish silently.
    onError: (e: unknown) =>
      setPanelError(
        t("dataTags.saveFailed", { error: extractError(e) || t("errors.requestFailed") }),
      ),
  });

  const openCreate = () => {
    setEditId(null);
    setForm(EMPTY_FORM);
    setFormError(null);
    setPickerTableId("");
    setDialogOpen(true);
  };

  const openEdit = (tag: DataTag) => {
    setEditId(tag.id);
    const labels: Record<string, string> = {};
    for (const c of tag.columns) {
      labels[c.column_id] = `${c.table_name}.${c.column_name}`;
    }
    setForm({
      tag_name: tag.tag_name,
      description: tag.description || "",
      columnIds: tag.columns.map((c) => c.column_id),
      columnLabels: labels,
    });
    setFormError(null);
    setPickerTableId("");
    setDialogOpen(true);
  };

  const toggleColumn = (attr: TableAttribute) => {
    setForm((prev) => {
      const present = prev.columnIds.includes(attr.id);
      const columnIds = present
        ? prev.columnIds.filter((id) => id !== attr.id)
        : [...prev.columnIds, attr.id];
      const columnLabels = { ...prev.columnLabels };
      if (present) {
        delete columnLabels[attr.id];
      } else {
        columnLabels[attr.id] = `${pickerTableLabel}.${attr.name}`;
      }
      return { ...prev, columnIds, columnLabels };
    });
  };

  const removeColumn = (columnId: string) => {
    setForm((prev) => {
      const columnLabels = { ...prev.columnLabels };
      delete columnLabels[columnId];
      return {
        ...prev,
        columnIds: prev.columnIds.filter((id) => id !== columnId),
        columnLabels,
      };
    });
  };

  const handleSave = () => {
    const payload = {
      tag_name: form.tag_name,
      description: form.description || null,
      column_ids: form.columnIds,
    };
    if (editId) {
      updateMut.mutate({ id: editId, data: payload });
    } else {
      createMut.mutate(payload as DataTagCreate);
    }
  };

  const handleDelete = async (tag: DataTag) => {
    const ok = await confirm({
      title: t("dataTags.deleteConfirmTitle", { name: tag.tag_name }),
      message: t("dataTags.deleteMessage"),
    });
    if (ok) deleteMut.mutate(tag.id);
  };

  const totalColumns = tags.data
    ? tags.data.reduce((s, t) => s + t.columns.length, 0)
    : 0;

  return (
    <Box sx={{ p: 2 }}>
      <Typography variant="h6" gutterBottom>
        {t("dataTags.title")}
      </Typography>

      {panelError && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setPanelError(null)}>
          {panelError}
        </Alert>
      )}

      {tags.data && tags.data.length > 0 && (
        <Alert severity="info" sx={{ mb: 2 }}>
          {(() => {
            const tagCount = tags.data.length;
            const colCount = totalColumns;
            const tOne = tagCount === 1;
            const cOne = colCount === 1;
            let key: string;
            if (tOne && cOne) key = "dataTags.summaryOneTagOneCol";
            else if (tOne) key = "dataTags.summaryOneTagManyCol";
            else if (cOne) key = "dataTags.summaryManyTagOneCol";
            else key = "dataTags.summaryManyTagManyCol";
            const params: Record<string, string> = {};
            if (!tOne) params.tagCount = String(tagCount);
            if (!cOne) params.columnCount = String(colCount);
            return t(key, params);
          })()}
        </Alert>
      )}

      <Stack direction="row" justifyContent="flex-end" sx={{ mb: 1 }}>
        <Button
          startIcon={<AddIcon />}
          variant="contained"
          size="small"
          onClick={openCreate}
        >
          {t("dataTags.addButton")}
        </Button>
      </Stack>

      {tags.isLoading && <CircularProgress size={24} />}

      {tags.data && tags.data.length === 0 && (
        <Typography color="text.secondary" sx={{ py: 2 }}>
          {t("dataTags.noTags")}
        </Typography>
      )}

      {tags.data &&
        tags.data.map((tag) => (
          <Box
            key={tag.id}
            sx={{
              border: "1px solid",
              borderColor: "divider",
              borderRadius: 1,
              mb: 1,
              px: 2,
              py: 1,
            }}
          >
            <Box display="flex" alignItems="center">
              <IconButton
                size="small"
                onClick={() =>
                  setExpanded(expanded === tag.id ? null : tag.id)
                }
              >
                {expanded === tag.id ? (
                  <ExpandLessIcon fontSize="small" />
                ) : (
                  <ExpandMoreIcon fontSize="small" />
                )}
              </IconButton>
              <Typography fontWeight={600} sx={{ flexGrow: 1, ml: 1 }}>
                {tag.tag_name}
              </Typography>
              <Chip
                label={t(tag.columns.length === 1 ? "dataTags.columnCountSingular" : "dataTags.columnCountPlural", tag.columns.length !== 1 ? { count: String(tag.columns.length) } : {})}
                size="small"
                sx={{ mr: 1 }}
              />
              <Tooltip title={t("common.edit")}>
                <IconButton size="small" onClick={() => openEdit(tag)}>
                  <EditIcon fontSize="small" />
                </IconButton>
              </Tooltip>
              <Tooltip title={t("common.delete")}>
                <IconButton size="small" onClick={() => handleDelete(tag)}>
                  <DeleteIcon fontSize="small" />
                </IconButton>
              </Tooltip>
            </Box>
            {tag.description && (
              <Typography
                variant="body2"
                color="text.secondary"
                sx={{ ml: 5 }}
              >
                {tag.description}
              </Typography>
            )}
            <Collapse in={expanded === tag.id}>
              {tag.columns.length === 0 ? (
                <Typography
                  variant="body2"
                  color="text.secondary"
                  sx={{ ml: 5, mt: 1 }}
                >
                  {t("dataTags.noColumnsAssigned")}
                </Typography>
              ) : (
                <List dense sx={{ ml: 4 }}>
                  {tag.columns.map((c) => (
                    <ListItem key={c.column_id} disablePadding>
                      <ListItemText
                        primary={`${c.table_name}.${c.column_name}`}
                        primaryTypographyProps={{ variant: "body2" }}
                      />
                    </ListItem>
                  ))}
                </List>
              )}
            </Collapse>
          </Box>
        ))}

      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>
          {editId ? t("dataTags.editDialogTitle") : t("dataTags.createDialogTitle")}
        </DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            {formError && (
              <Alert severity="error" onClose={() => setFormError(null)}>
                {formError}
              </Alert>
            )}
            <TextField
              label={t("dataTags.tagNameLabel")}
              size="small"
              fullWidth
              required
              value={form.tag_name}
              onChange={(e) => setForm({ ...form, tag_name: e.target.value })}
              placeholder={t("dataTags.tagNamePlaceholder")}
            />
            <TextField
              label={t("dataTags.descriptionLabel")}
              size="small"
              fullWidth
              multiline
              rows={2}
              value={form.description}
              onChange={(e) =>
                setForm({ ...form, description: e.target.value })
              }
              placeholder={t("dataTags.descriptionPlaceholder")}
            />

            {/* F-008-08: column picker — assign the columns this tag covers. */}
            <Box>
              <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
                {t("dataTags.columnsSectionTitle")}
              </Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                {t("dataTags.columnsSectionDescription")}
              </Typography>

              {form.columnIds.length > 0 && (
                <Stack
                  direction="row"
                  spacing={0.5}
                  flexWrap="wrap"
                  useFlexGap
                  sx={{ mb: 1 }}
                >
                  {form.columnIds.map((id) => (
                    <Chip
                      key={id}
                      label={form.columnLabels[id] ?? id}
                      size="small"
                      onDelete={() => removeColumn(id)}
                    />
                  ))}
                </Stack>
              )}

              <FormControl size="small" fullWidth sx={{ mb: 1 }}>
                <InputLabel id="data-tag-table-picker-label">
                  {t("dataTags.selectTableLabel")}
                </InputLabel>
                <Select
                  labelId="data-tag-table-picker-label"
                  value={pickerTableId}
                  label={t("dataTags.selectTableLabel")}
                  onChange={(e) => setPickerTableId(String(e.target.value))}
                >
                  {(allTables.data ?? []).map((tb) => (
                    <MenuItem key={tb.id} value={tb.id}>
                      {tb.alias || tb.physical_name}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>

              {pickerTableId && pickerColumns.isLoading && (
                <CircularProgress size={18} />
              )}
              {pickerTableId && pickerColumns.data && (
                <Box
                  sx={{
                    border: "1px solid",
                    borderColor: "divider",
                    borderRadius: 1,
                    maxHeight: 180,
                    overflowY: "auto",
                    px: 1.5,
                    py: 1,
                  }}
                >
                  {pickerColumns.data.filter((a) => !a.is_user_defined).length === 0 ? (
                    <Typography variant="body2" color="text.secondary">
                      {t("dataTags.noColumnsInTable")}
                    </Typography>
                  ) : (
                    pickerColumns.data
                      .filter((a) => !a.is_user_defined)
                      .map((attr) => (
                        <Stack
                          key={attr.id}
                          direction="row"
                          alignItems="center"
                          spacing={1}
                          sx={{ mb: 0.25 }}
                        >
                          <input
                            type="checkbox"
                            id={`tag-col-${attr.id}`}
                            checked={form.columnIds.includes(attr.id)}
                            onChange={() => toggleColumn(attr)}
                          />
                          <label htmlFor={`tag-col-${attr.id}`}>
                            <Typography variant="body2">
                              {attr.name}
                              <Typography
                                component="span"
                                variant="caption"
                                color="text.secondary"
                              >
                                {" "}
                                {attr.data_type}
                              </Typography>
                            </Typography>
                          </label>
                        </Stack>
                      ))
                  )}
                </Box>
              )}
            </Box>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={handleSave}
            disabled={
              !form.tag_name || createMut.isPending || updateMut.isPending
            }
          >
            {editId ? t("dataTags.updateButton") : t("dataTags.createButton")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

function extractError(e: unknown): string {
  const err = e as {
    response?: { data?: { detail?: string | { message?: string } } };
    message?: string;
  };
  const detail = err?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  return err?.message ?? "";
}
