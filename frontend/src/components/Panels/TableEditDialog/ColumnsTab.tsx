import { Fragment, useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  CircularProgress,
  Stack,
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
import VisibilityIcon from "@mui/icons-material/Visibility";
import VisibilityOffIcon from "@mui/icons-material/VisibilityOff";
import { tableAttributesApi } from "../../../api/client";
import { useTableAttributes } from "../../../api/hooks";
import type { ModelTable, TableAttribute } from "../../../api/types";
import SyncColumnsButton from "./SyncColumnsButton";
import { useT } from "../../../i18n";

interface DraftColumn {
  id: string;
  name: string;
  data_type: string;
  display_name: string;
  description: string;
  is_hidden: boolean;
  is_primary_key: boolean;
  dirty: boolean;
}

interface Props {
  projectId: string;
  modelId: string;
  table: ModelTable;
  connectionId: string | null;
}

export default function ColumnsTab({ projectId, modelId, table, connectionId }: Props) {
  const t = useT();
  const qc = useQueryClient();
  const attributes = useTableAttributes(projectId, modelId, table.id);
  const [drafts, setDrafts] = useState<Record<string, DraftColumn>>({});
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => {
    if (!attributes.data) return;
    const next: Record<string, DraftColumn> = {};
    for (const a of attributes.data) {
      if (a.is_user_defined) continue;
      next[a.id] = {
        id: a.id,
        name: a.name,
        data_type: a.data_type,
        display_name: a.display_name ?? "",
        description: a.description ?? "",
        is_hidden: a.is_hidden ?? false,
        is_primary_key: a.is_primary_key ?? false,
        dirty: false,
      };
    }
    setDrafts(next);
  }, [attributes.data]);

  const physicalColumns: TableAttribute[] = useMemo(
    () => (attributes.data ?? []).filter((a) => !a.is_user_defined),
    [attributes.data],
  );

  const updateMutation = useMutation({
    mutationFn: async (col: DraftColumn) =>
      tableAttributesApi.updateColumn(projectId, modelId, table.id, col.id, {
        display_name: col.display_name.trim() ? col.display_name.trim() : null,
        description: col.description.trim() ? col.description.trim() : null,
        is_hidden: col.is_hidden,
        is_primary_key: col.is_primary_key,
      }),
    onSuccess: (_, col) => {
      setDrafts((prev) => ({ ...prev, [col.id]: { ...col, dirty: false } }));
      qc.invalidateQueries({ queryKey: ["tableAttributes", projectId, modelId, table.id] });
    },
    onError: (err: unknown) => {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        t("columns.saveFailed");
      setError(detail);
    },
  });

  function patchDraft(id: string, patch: Partial<DraftColumn>) {
    setDrafts((prev) => {
      const current = prev[id];
      if (!current) return prev;
      return { ...prev, [id]: { ...current, ...patch, dirty: true } };
    });
  }

  async function saveAllDirty() {
    setError(null);
    setSaving(true);
    const dirty = Object.values(drafts).filter((d) => d.dirty);
    for (const d of dirty) {
      try {
        await updateMutation.mutateAsync(d);
      } catch {
        break;
      }
    }
    setSaving(false);
  }

  const dirtyCount = Object.values(drafts).filter((d) => d.dirty).length;

  return (
    <Box sx={{ pt: 1 }}>
      <Typography variant="caption" color="text.secondary" display="block" mb={1}>
        {t("columns.description")}
      </Typography>
      {error && (
        <Alert severity="error" sx={{ mb: 1 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}
      {attributes.isLoading ? (
        <Box display="flex" justifyContent="center" py={3}>
          <CircularProgress size={24} />
        </Box>
      ) : physicalColumns.length === 0 ? (
        <Box sx={{ py: 2, textAlign: "center" }}>
          <Typography variant="body2" color="text.secondary" mb={1}>
            {t("columns.noPhysicalColumns")}
          </Typography>
          <SyncColumnsButton
            projectId={projectId}
            modelId={modelId}
            table={table}
            connectionId={connectionId}
            variant="contained"
          />
          {!connectionId && (
            <Typography variant="caption" color="warning.main" display="block" mt={1}>
              {t("columns.noConnection")}
            </Typography>
          )}
        </Box>
      ) : (
        <Stack spacing={1.5}>
          <TableContainer
            sx={{ border: 1, borderColor: "divider", borderRadius: 1, maxHeight: 420 }}
          >
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50" }}>
                    {t("columns.columnHeader")}
                  </TableCell>
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50", width: 80 }}>
                    {t("columns.typeHeader")}
                  </TableCell>
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50" }}>
                    {t("columns.displayNameHeader")}
                  </TableCell>
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50", width: 50, textAlign: "center" }}>
                    {t("columns.keyHeader")}
                  </TableCell>
                  <TableCell sx={{ fontWeight: 600, fontSize: "0.8rem", bgcolor: "grey.50", width: 50, textAlign: "center" }}>
                    <Tooltip title={t("columns.hiddenHeaderHelp")}>
                      <span>{t("columns.hiddenHeader")}</span>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {physicalColumns.map((col) => {
                  const draft = drafts[col.id];
                  if (!draft) return null;
                  const isExpanded = expandedId === col.id;
                  return (
                    <Fragment key={col.id}>
                      <TableRow
                        hover
                        onClick={() => setExpandedId(isExpanded ? null : col.id)}
                        sx={{
                          cursor: "pointer",
                          bgcolor: draft.dirty ? "rgba(0,108,53,0.03)" : undefined,
                          opacity: draft.is_hidden ? 0.5 : 1,
                        }}
                      >
                        <TableCell
                          sx={{
                            fontFamily: "monospace",
                            fontSize: "0.8rem",
                            py: 0.75,
                            textDecoration: draft.is_hidden ? "line-through" : "none",
                          }}
                        >
                          {col.name}
                        </TableCell>
                        <TableCell sx={{ fontSize: "0.75rem", color: "text.secondary", py: 0.75 }}>
                          {col.data_type}
                        </TableCell>
                        <TableCell sx={{ py: 0.5 }}>
                          <TextField
                            size="small"
                            fullWidth
                            variant="standard"
                            placeholder={col.name}
                            value={draft.display_name}
                            onClick={(e) => e.stopPropagation()}
                            onChange={(e) => patchDraft(col.id, { display_name: e.target.value })}
                            InputProps={{
                              disableUnderline: !draft.dirty,
                              sx: { fontSize: "0.8rem" },
                            }}
                          />
                        </TableCell>
                        <TableCell sx={{ textAlign: "center", py: 0.5 }}>
                          <Tooltip title={t("columns.primaryKeyTooltip")}>
                            <Checkbox
                              size="small"
                              checked={draft.is_primary_key}
                              onClick={(e) => e.stopPropagation()}
                              onChange={(e) => patchDraft(col.id, { is_primary_key: e.target.checked })}
                              inputProps={{ "aria-label": t("columns.declarePrimaryKey", { name: col.name }) }}
                            />
                          </Tooltip>
                        </TableCell>
                        <TableCell sx={{ textAlign: "center", py: 0.5 }}>
                          <Tooltip
                            title={draft.is_hidden ? t("columns.hiddenClickToShow") : t("columns.visibleClickToHide")}
                          >
                            <Checkbox
                              size="small"
                              checked={draft.is_hidden}
                              onClick={(e) => e.stopPropagation()}
                              onChange={(e) => patchDraft(col.id, { is_hidden: e.target.checked })}
                              icon={<VisibilityIcon sx={{ fontSize: 16 }} />}
                              checkedIcon={<VisibilityOffIcon sx={{ fontSize: 16 }} />}
                            />
                          </Tooltip>
                        </TableCell>
                      </TableRow>
                      {isExpanded && (
                        <TableRow>
                          <TableCell colSpan={5} sx={{ py: 1, bgcolor: "grey.50" }}>
                            <TextField
                              label={t("columns.businessDescription")}
                              size="small"
                              fullWidth
                              multiline
                              minRows={2}
                              maxRows={4}
                              value={draft.description}
                              onChange={(e) => patchDraft(col.id, { description: e.target.value })}
                              placeholder={t("columns.businessDescriptionPlaceholder")}
                            />
                          </TableCell>
                        </TableRow>
                      )}
                    </Fragment>
                  );
                })}
              </TableBody>
            </Table>
          </TableContainer>

          <Box display="flex" justifyContent="flex-end" gap={1}>
            <SyncColumnsButton
              projectId={projectId}
              modelId={modelId}
              table={table}
              connectionId={connectionId}
              label={t("columns.reSync")}
            />
            <Button
              variant="contained"
              size="small"
              disabled={dirtyCount === 0 || saving}
              onClick={saveAllDirty}
              startIcon={saving ? <CircularProgress size={14} color="inherit" /> : undefined}
            >
              {saving ? t("columns.saving") : t("columns.save")}
            </Button>
          </Box>
        </Stack>
      )}
    </Box>
  );
}
