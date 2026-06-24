import { useState } from "react";
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
  IconButton,
  List,
  ListItem,
  ListItemButton,
  ListItemText,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import { savedQueriesApi } from "../../api/client";
import { useSavedQueries } from "../../api/hooks";
import type { SavedQuery, SavedQueryCreate } from "../../api/types";
import { useBuilderStore } from "../../store/builderStore";
import { useConfirm } from "../Confirm";

/** Extract the API ``detail`` message, falling back to a translated default. */
function errorDetail(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "response" in err) {
    const detail = (err as { response?: { data?: { detail?: string } } })
      .response?.data?.detail;
    if (detail) return detail;
  }
  return fallback;
}

export default function SavedQueriesPanel() {
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const t = useT();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const queries = useSavedQueries(projectId!, modelId!);
  const openPanel = useBuilderStore((s) => s.openPanel);
  const setPendingSql = useBuilderStore((s) => s.setPendingSql);

  const [createOpen, setCreateOpen] = useState(false);
  const [editItem, setEditItem] = useState<SavedQuery | null>(null);
  const [form, setForm] = useState({ name: "", description: "", query_text: "" });
  const [error, setError] = useState<string | null>(null);

  const createMutation = useMutation({
    mutationFn: (data: SavedQueryCreate) =>
      savedQueriesApi.create(projectId!, modelId!, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["savedQueries", projectId, modelId] });
      setCreateOpen(false);
      setForm({ name: "", description: "", query_text: "" });
    },
    onError: (err: unknown) => setError(errorDetail(err, t("savedQueries.saveFailed"))),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, ...data }: { id: string; name?: string; description?: string; query_text?: string }) =>
      savedQueriesApi.update(projectId!, modelId!, id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["savedQueries", projectId, modelId] });
      setEditItem(null);
    },
    onError: (err: unknown) => setError(errorDetail(err, t("savedQueries.saveFailed"))),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) =>
      savedQueriesApi.delete(projectId!, modelId!, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["savedQueries", projectId, modelId] });
    },
    onError: (err: unknown) => setError(errorDetail(err, t("savedQueries.deleteFailed"))),
  });

  function handleRun(q: SavedQuery) {
    setPendingSql(q.query_text);
    openPanel("query");
  }

  async function handleDelete(q: SavedQuery) {
    const ok = await confirm({
      title: t("savedQueries.deleteConfirmTitle"),
      message: t("savedQueries.deleteConfirmMessage", { name: q.name }),
    });
    if (ok) deleteMutation.mutate(q.id);
  }

  if (queries.isLoading) {
    return (
      <Box display="flex" justifyContent="center" py={4}>
        <CircularProgress size={24} />
      </Box>
    );
  }

  const items = queries.data ?? [];

  return (
    <Stack spacing={2} sx={{ p: 2 }}>
      <Box display="flex" alignItems="center" justifyContent="space-between">
        <Typography variant="h6" fontWeight={600}>
          {t("savedQueries.title")}
        </Typography>
        <Button
          size="small"
          variant="contained"
          startIcon={<AddIcon />}
          onClick={() => {
            setForm({ name: "", description: "", query_text: "" });
            setCreateOpen(true);
          }}
        >
          {t("savedQueries.new")}
        </Button>
      </Box>

      {error && (
        <Alert severity="error" onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {items.length === 0 ? (
        <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
          {t("savedQueries.noQueries")}
        </Typography>
      ) : (
        <List disablePadding>
          {items.map((q) => (
            <ListItem
              key={q.id}
              disablePadding
              secondaryAction={
                <Stack direction="row" spacing={0.5}>
                  <Tooltip title={t("savedQueries.openInQueryPanel")}>
                    <IconButton size="small" onClick={() => handleRun(q)}>
                      <PlayArrowIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t("savedQueries.copySql")}>
                    <IconButton
                      size="small"
                      onClick={() => navigator.clipboard.writeText(q.query_text)}
                    >
                      <ContentCopyIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t("common.edit")}>
                    <IconButton
                      size="small"
                      onClick={() => {
                        setEditItem(q);
                        setForm({
                          name: q.name,
                          description: q.description ?? "",
                          query_text: q.query_text,
                        });
                      }}
                    >
                      <EditIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t("common.delete")}>
                    <IconButton
                      size="small"
                      onClick={() => handleDelete(q)}
                      disabled={deleteMutation.isPending}
                    >
                      <DeleteIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                </Stack>
              }
            >
              <ListItemButton sx={{ pr: 18 }}>
                <ListItemText
                  primary={q.name}
                  secondary={
                    <>
                      {q.description && (
                        <Typography variant="caption" display="block" color="text.secondary">
                          {q.description}
                        </Typography>
                      )}
                      <Typography variant="caption" color="text.disabled">
                        {q.query_type.toUpperCase()} &middot; {t("savedQueries.by")} {q.created_by} &middot;{" "}
                        {new Date(q.updated_at).toLocaleDateString()}
                      </Typography>
                    </>
                  }
                  primaryTypographyProps={{ fontWeight: 500, fontSize: "0.9rem" }}
                />
              </ListItemButton>
            </ListItem>
          ))}
        </List>
      )}

      {/* Create dialog */}
      <Dialog open={createOpen} onClose={() => setCreateOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{t("savedQueries.saveQueryDialogTitle")}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label={t("savedQueries.nameLabel")}
              size="small"
              fullWidth
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            />
            <TextField
              label={t("savedQueries.descriptionLabel")}
              size="small"
              fullWidth
              value={form.description}
              onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
            />
            <TextField
              label={t("savedQueries.sqlLabel")}
              size="small"
              fullWidth
              multiline
              minRows={4}
              maxRows={12}
              value={form.query_text}
              onChange={(e) => setForm((f) => ({ ...f, query_text: e.target.value }))}
              InputProps={{ sx: { fontFamily: "monospace", fontSize: "0.85rem" } }}
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCreateOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            disabled={!form.name.trim() || !form.query_text.trim() || createMutation.isPending}
            onClick={() =>
              createMutation.mutate({
                name: form.name.trim(),
                description: form.description.trim() || undefined,
                query_text: form.query_text.trim(),
              })
            }
          >
            {createMutation.isPending ? t("savedQueries.saving") : t("common.save")}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Edit dialog */}
      <Dialog open={!!editItem} onClose={() => setEditItem(null)} maxWidth="sm" fullWidth>
        <DialogTitle>{t("savedQueries.editQueryDialogTitle")}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label={t("savedQueries.nameLabel")}
              size="small"
              fullWidth
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            />
            <TextField
              label={t("savedQueries.descriptionLabel")}
              size="small"
              fullWidth
              value={form.description}
              onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
            />
            <TextField
              label={t("savedQueries.sqlLabel")}
              size="small"
              fullWidth
              multiline
              minRows={4}
              maxRows={12}
              value={form.query_text}
              onChange={(e) => setForm((f) => ({ ...f, query_text: e.target.value }))}
              InputProps={{ sx: { fontFamily: "monospace", fontSize: "0.85rem" } }}
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setEditItem(null)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            disabled={!form.name.trim() || !form.query_text.trim() || updateMutation.isPending}
            onClick={() => {
              if (!editItem) return;
              updateMutation.mutate({
                id: editItem.id,
                name: form.name.trim(),
                description: form.description.trim() || undefined,
                query_text: form.query_text.trim(),
              });
            }}
          >
            {updateMutation.isPending ? t("savedQueries.saving") : t("savedQueries.update")}
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
