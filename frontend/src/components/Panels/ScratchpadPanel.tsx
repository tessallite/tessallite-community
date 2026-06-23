import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  IconButton,
  MenuItem,
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
import { scratchpadApi } from "../../api/client";
import type {
  ScratchpadMeasure,
  ScratchpadMeasureCreate,
  ScratchpadMeasureUpdate,
} from "../../api/types";
import { useConfirm } from "../Confirm";

// The supported scratchpad data types — kept in sync with the server-side
// SCRATCHPAD_DATA_TYPES set in scratchpad_measures.py. Rendering a select over
// this fixed list prevents an unknown free-text value showing a raw i18n key
// (F-029-14).
const SCRATCHPAD_DATA_TYPES = [
  "numeric",
  "integer",
  "string",
  "boolean",
  "date",
  "timestamp",
] as const;

export default function ScratchpadPanel() {
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const t = useT();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<ScratchpadMeasure | null>(null);
  const [form, setForm] = useState<ScratchpadMeasureCreate>({
    name: "",
    expression: "",
    data_type: "numeric",
    format: null,
    display_name: null,
  });

  const queryKey = ["scratchpad-measures", projectId, modelId];

  const measuresQuery = useQuery({
    queryKey,
    queryFn: () => scratchpadApi.list(projectId!, modelId!),
    enabled: Boolean(projectId && modelId),
  });

  const createMut = useMutation({
    mutationFn: (data: ScratchpadMeasureCreate) =>
      scratchpadApi.create(projectId!, modelId!, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey });
      closeDialog();
    },
  });

  const updateMut = useMutation({
    mutationFn: (args: { id: string; data: ScratchpadMeasureUpdate }) =>
      scratchpadApi.update(projectId!, modelId!, args.id, args.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey });
      closeDialog();
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) =>
      scratchpadApi.delete(projectId!, modelId!, id),
    onSuccess: () => qc.invalidateQueries({ queryKey }),
  });

  function openCreate() {
    setEditing(null);
    setForm({ name: "", expression: "", data_type: "numeric", format: null, display_name: null });
    setDialogOpen(true);
  }

  function openEdit(m: ScratchpadMeasure) {
    setEditing(m);
    setForm({
      name: m.name,
      expression: m.expression,
      data_type: m.data_type,
      format: m.format,
      display_name: m.display_name,
    });
    setDialogOpen(true);
  }

  function closeDialog() {
    setDialogOpen(false);
    setEditing(null);
  }

  function handleSave() {
    if (editing) {
      updateMut.mutate({ id: editing.id, data: form });
    } else {
      createMut.mutate(form);
    }
  }

  async function handleDelete(m: ScratchpadMeasure) {
    const ok = await confirm({
      title: t("scratchpad.deleteConfirmTitle"),
      message: t("scratchpad.deleteConfirmMessage", { name: m.display_name || m.name }),
    });
    if (ok) deleteMut.mutate(m.id);
  }

  const error = createMut.error ?? updateMut.error;
  const errorMsg =
    error && typeof error === "object" && "response" in error
      ? ((error as { response?: { data?: { detail?: string } } }).response?.data?.detail ?? t("scratchpad.saveFailed"))
      : error
        ? String(error)
        : null;

  return (
    <Box sx={{ p: 2 }}>
      <Box sx={{ display: "flex", alignItems: "center", mb: 2 }}>
        <Typography variant="h6" sx={{ flex: 1 }}>
          {t("scratchpad.title")}
        </Typography>
        <Button
          size="small"
          startIcon={<AddIcon />}
          variant="contained"
          onClick={openCreate}
        >
          {t("scratchpad.newButton")}
        </Button>
      </Box>

      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        {t("scratchpad.description")}
      </Typography>

      {measuresQuery.isLoading && (
        <Box sx={{ textAlign: "center", py: 4 }}>
          <CircularProgress size={24} />
        </Box>
      )}

      {measuresQuery.data && measuresQuery.data.length === 0 && (
        <Alert severity="info">
          {t("scratchpad.emptyMessage")}
        </Alert>
      )}

      {measuresQuery.data && measuresQuery.data.length > 0 && (
        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>{t("scratchpad.nameHeader")}</TableCell>
                <TableCell>{t("scratchpad.expressionHeader")}</TableCell>
                <TableCell>{t("scratchpad.typeHeader")}</TableCell>
                <TableCell align="right">{t("scratchpad.actionsHeader")}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {measuresQuery.data.map((m) => (
                <TableRow key={m.id}>
                  <TableCell>
                    <Typography variant="body2" fontWeight={600}>
                      {m.display_name || m.name}
                    </Typography>
                    {m.display_name && (
                      <Typography variant="caption" color="text.secondary">
                        {m.name}
                      </Typography>
                    )}
                  </TableCell>
                  <TableCell>
                    <Typography
                      variant="body2"
                      sx={{ fontFamily: "monospace", fontSize: 12 }}
                    >
                      {m.expression}
                    </Typography>
                  </TableCell>
                  <TableCell>{t(`scratchpad.dataType.${m.data_type}`)}</TableCell>
                  <TableCell align="right">
                    <Tooltip title={t("scratchpad.editTooltip")}>
                      <IconButton size="small" onClick={() => openEdit(m)}>
                        <EditIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title={t("scratchpad.deleteTooltip")}>
                      <IconButton size="small" onClick={() => handleDelete(m)}>
                        <DeleteIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      <Dialog open={dialogOpen} onClose={closeDialog} maxWidth="sm" fullWidth>
        <DialogTitle>
          {editing ? t("scratchpad.editDialogTitle") : t("scratchpad.createDialogTitle")}
        </DialogTitle>
        <DialogContent sx={{ display: "flex", flexDirection: "column", gap: 2, pt: "8px !important" }}>
          <TextField
            label={t("scratchpad.nameLabel")}
            size="small"
            required
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            helperText={t("scratchpad.nameHelperText")}
          />
          <TextField
            label={t("scratchpad.displayNameLabel")}
            size="small"
            value={form.display_name ?? ""}
            onChange={(e) =>
              setForm({ ...form, display_name: e.target.value || null })
            }
          />
          <TextField
            label={t("scratchpad.expressionLabel")}
            size="small"
            required
            multiline
            minRows={2}
            value={form.expression}
            onChange={(e) => setForm({ ...form, expression: e.target.value })}
            helperText={t("scratchpad.expressionHelperText")}
            InputProps={{ sx: { fontFamily: "monospace", fontSize: 13 } }}
          />
          <TextField
            label={t("scratchpad.dataTypeLabel")}
            size="small"
            select
            value={form.data_type}
            onChange={(e) => setForm({ ...form, data_type: e.target.value })}
          >
            {SCRATCHPAD_DATA_TYPES.map((dt) => (
              <MenuItem key={dt} value={dt}>
                {t(`scratchpad.dataType.${dt}`)}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            label={t("scratchpad.formatLabel")}
            size="small"
            value={form.format ?? ""}
            onChange={(e) =>
              setForm({ ...form, format: e.target.value || null })
            }
            helperText={t("scratchpad.formatHelperText")}
          />
          {errorMsg && <Alert severity="error">{errorMsg}</Alert>}
        </DialogContent>
        <DialogActions>
          <Button onClick={closeDialog}>{t("scratchpad.cancelButton")}</Button>
          <Button
            variant="contained"
            onClick={handleSave}
            disabled={
              !form.name.trim() ||
              !form.expression.trim() ||
              createMut.isPending ||
              updateMut.isPending
            }
          >
            {createMut.isPending || updateMut.isPending ? (
              <CircularProgress size={16} />
            ) : editing ? (
              t("scratchpad.saveButton")
            ) : (
              t("scratchpad.createButton")
            )}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
