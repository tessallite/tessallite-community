import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
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
  FormControl,
  IconButton,
  InputLabel,
  Link,
  MenuItem,
  Select,
  Stack,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import SearchIcon from "@mui/icons-material/Search";
import {
  downstreamAssetsApi,
  impactScanApi,
} from "../../api/client";
import {
  useDownstreamAssets,
  useDownstreamAssetSummary,
  useGatewayQueryReferences,
} from "../../api/hooks";
import { useConfirm } from "../Confirm";
import { useT } from "../../i18n";
import type {
  AssetType,
  DownstreamAsset,
  DownstreamAssetCreate,
  DownstreamAssetUpdate,
} from "../../api/types";

const ASSET_TYPES: { value: AssetType; label: string }[] = [
  { value: "dashboard", label: "assetType.dashboard" },
  { value: "report", label: "assetType.report" },
  { value: "ml_job", label: "assetType.mlJob" },
  { value: "api", label: "assetType.api" },
  { value: "other", label: "assetType.other" },
];

const TYPE_COLORS: Record<AssetType, "primary" | "secondary" | "success" | "info" | "warning"> = {
  dashboard: "primary",
  report: "secondary",
  ml_job: "success",
  api: "info",
  other: "warning",
};

interface AssetFormState {
  asset_type: AssetType;
  asset_name: string;
  asset_url: string;
  owner: string;
  notes: string;
}

const EMPTY_FORM: AssetFormState = {
  asset_type: "dashboard",
  asset_name: "",
  asset_url: "",
  owner: "",
  notes: "",
};

export default function ImpactPanel() {
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const t = useT();

  const [tab, setTab] = useState(0);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);
  const [form, setForm] = useState<AssetFormState>(EMPTY_FORM);

  const assets = useDownstreamAssets(projectId!, modelId!);
  const summary = useDownstreamAssetSummary(projectId!, modelId!);
  const queryRefs = useGatewayQueryReferences(projectId!, modelId!);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["downstream-assets", projectId, modelId] });
    qc.invalidateQueries({ queryKey: ["downstream-assets-summary", projectId, modelId] });
  };

  const createMut = useMutation({
    mutationFn: (data: DownstreamAssetCreate) =>
      downstreamAssetsApi.create(projectId!, modelId!, data),
    onSuccess: () => {
      invalidate();
      setDialogOpen(false);
    },
  });

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: DownstreamAssetUpdate }) =>
      downstreamAssetsApi.update(projectId!, modelId!, id, data),
    onSuccess: () => {
      invalidate();
      setDialogOpen(false);
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) =>
      downstreamAssetsApi.delete(projectId!, modelId!, id),
    onSuccess: invalidate,
  });

  const scanMut = useMutation({
    mutationFn: () => impactScanApi.scan(projectId!, modelId!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["gateway-query-refs", projectId, modelId] });
    },
  });

  const openCreate = () => {
    setEditId(null);
    setForm(EMPTY_FORM);
    setDialogOpen(true);
  };

  const openEdit = (a: DownstreamAsset) => {
    setEditId(a.id);
    setForm({
      asset_type: a.asset_type,
      asset_name: a.asset_name,
      asset_url: a.asset_url || "",
      owner: a.owner || "",
      notes: a.notes || "",
    });
    setDialogOpen(true);
  };

  const handleSave = () => {
    const payload = {
      asset_type: form.asset_type,
      asset_name: form.asset_name,
      asset_url: form.asset_url || null,
      owner: form.owner || null,
      notes: form.notes || null,
    };
    if (editId) {
      updateMut.mutate({ id: editId, data: payload });
    } else {
      createMut.mutate(payload as DownstreamAssetCreate);
    }
  };

  const handleDelete = async (a: DownstreamAsset) => {
    const ok = await confirm({
      title: t("impact.deleteConfirmTitle", { name: a.asset_name }),
      message: t("impact.deleteConfirmMessage"),
    });
    if (ok) deleteMut.mutate(a.id);
  };

  const summaryText = summary.data
    ? Object.entries(summary.data.by_type)
        .map(([type, c]) => c === 1 ? `${c} ${type}` : `${c} ${type}s`)
        .join(", ")
    : "";

  return (
    <Box sx={{ p: 2 }}>
      <Typography variant="h6" gutterBottom>
        {t("impact.title")}
      </Typography>

      {summary.data && summary.data.total > 0 && (
        <Alert severity="info" sx={{ mb: 2 }}>
          {t("impact.summaryMessage", {
            count: String(summary.data.total),
            label: summary.data.total !== 1 ? t("impact.summaryPlural") : t("impact.summaryPrefix"),
            summary: summaryText,
          })}
        </Alert>
      )}

      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 2 }}>
        <Tab label={t("impact.downstreamAssetsTab")} />
        <Tab label={t("impact.queryAuditTab")} />
      </Tabs>

      {tab === 0 && (
        <Box>
          <Stack direction="row" justifyContent="flex-end" sx={{ mb: 1 }}>
            <Button
              startIcon={<AddIcon />}
              variant="contained"
              size="small"
              onClick={openCreate}
            >
              {t("impact.addAssetButton")}
            </Button>
          </Stack>

          {assets.isLoading && <CircularProgress size={24} />}

          {assets.data && assets.data.length === 0 && (
            <Typography color="text.secondary" sx={{ py: 2 }}>
              {t("impact.noAssetsMessage")}
            </Typography>
          )}

          {assets.data && assets.data.length > 0 && (
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>{t("impact.typeHeader")}</TableCell>
                  <TableCell>{t("impact.nameHeader")}</TableCell>
                  <TableCell>{t("impact.ownerHeader")}</TableCell>
                  <TableCell>{t("impact.columnsHeader")}</TableCell>
                  <TableCell align="right">{t("impact.actionsHeader")}</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {assets.data.map((a) => (
                  <TableRow key={a.id}>
                    <TableCell>
                      <Chip
                        label={a.asset_type}
                        size="small"
                        color={TYPE_COLORS[a.asset_type] || "default"}
                      />
                    </TableCell>
                    <TableCell>
                      {a.asset_url ? (
                        <Link
                          href={a.asset_url}
                          target="_blank"
                          rel="noopener"
                          underline="hover"
                        >
                          {a.asset_name}
                        </Link>
                      ) : (
                        a.asset_name
                      )}
                    </TableCell>
                    <TableCell>{a.owner || t("common.na")}</TableCell>
                    <TableCell>{a.column_ids.length}</TableCell>
                    <TableCell align="right">
                      <Tooltip title={t("impact.editTooltip")}>
                        <IconButton size="small" onClick={() => openEdit(a)}>
                          <EditIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                      <Tooltip title={t("impact.deleteTooltip")}>
                        <IconButton
                          size="small"
                          onClick={() => handleDelete(a)}
                        >
                          <DeleteIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Box>
      )}

      {tab === 1 && (
        <Box>
          <Stack direction="row" justifyContent="flex-end" sx={{ mb: 1 }}>
            <Button
              startIcon={<SearchIcon />}
              variant="contained"
              size="small"
              onClick={() => scanMut.mutate()}
              disabled={scanMut.isPending}
            >
              {scanMut.isPending ? t("impact.scanningButton") : t("impact.runScanButton")}
            </Button>
          </Stack>

          {scanMut.isSuccess && (
            <Alert severity="success" sx={{ mb: 1 }}>
              {(() => {
                const tablesOne = scanMut.data.tables_matched === 1;
                const refsOne = scanMut.data.references_upserted === 1;
                const key = tablesOne
                  ? refsOne ? "impact.scanCompleteMessageOneTableOneRef" : "impact.scanCompleteMessageOneTableManyRef"
                  : refsOne ? "impact.scanCompleteMessageManyTableOneRef" : "impact.scanCompleteMessageManyTableManyRef";
                const params: Record<string, string> = {};
                if (!tablesOne) params.tablesMatched = String(scanMut.data.tables_matched);
                if (!refsOne) params.referencesUpserted = String(scanMut.data.references_upserted);
                return t(key, params);
              })()}
            </Alert>
          )}

          {queryRefs.isLoading && <CircularProgress size={24} />}

          {queryRefs.data && queryRefs.data.length === 0 && (
            <Typography color="text.secondary" sx={{ py: 2 }}>
              {t("impact.noReferencesMessage")}
            </Typography>
          )}

          {queryRefs.data && queryRefs.data.length > 0 && (
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>{t("impact.tableHeader")}</TableCell>
                  <TableCell>{t("impact.userHeader")}</TableCell>
                  <TableCell align="right">{t("impact.hitCountHeader")}</TableCell>
                  <TableCell>{t("impact.lastSeenHeader")}</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {queryRefs.data.map((r) => (
                  <TableRow key={r.id}>
                    <TableCell>{r.queried_table}</TableCell>
                    <TableCell>{r.query_user || t("common.na")}</TableCell>
                    <TableCell align="right">{r.hit_count}</TableCell>
                    <TableCell>
                      {new Date(r.last_seen_at).toLocaleDateString()}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Box>
      )}

      {/* Create / Edit dialog */}
      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>
          {editId ? t("impact.editAssetDialogTitle") : t("impact.addAssetDialogTitle")}
        </DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <FormControl fullWidth size="small">
              <InputLabel>{t("impact.typeFieldLabel")}</InputLabel>
              <Select
                value={form.asset_type}
                label={t("impact.typeFieldLabel")}
                onChange={(e) =>
                  setForm({ ...form, asset_type: e.target.value as AssetType })
                }
              >
                {ASSET_TYPES.map((assetType) => (
                  <MenuItem key={assetType.value} value={assetType.value}>
                    {t(assetType.label)}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            <TextField
              label={t("impact.nameFieldLabel")}
              size="small"
              fullWidth
              required
              value={form.asset_name}
              onChange={(e) => setForm({ ...form, asset_name: e.target.value })}
            />
            <TextField
              label={t("impact.urlFieldLabel")}
              size="small"
              fullWidth
              value={form.asset_url}
              onChange={(e) => setForm({ ...form, asset_url: e.target.value })}
            />
            <TextField
              label={t("impact.ownerFieldLabel")}
              size="small"
              fullWidth
              value={form.owner}
              onChange={(e) => setForm({ ...form, owner: e.target.value })}
            />
            <TextField
              label={t("impact.notesFieldLabel")}
              size="small"
              fullWidth
              multiline
              rows={2}
              value={form.notes}
              onChange={(e) => setForm({ ...form, notes: e.target.value })}
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>{t("impact.cancelButton")}</Button>
          <Button
            variant="contained"
            onClick={handleSave}
            disabled={
              !form.asset_name || createMut.isPending || updateMut.isPending
            }
          >
            {editId ? t("impact.updateButton") : t("impact.createButton")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
