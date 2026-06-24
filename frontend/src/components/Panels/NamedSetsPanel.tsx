import { useCallback, useEffect, useMemo, useState } from "react";
import { useT } from "../../i18n";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  MenuItem,
  Stack,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tabs,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import AutoFixHighIcon from "@mui/icons-material/AutoFixHigh";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import HistoryIcon from "@mui/icons-material/History";
import RestoreIcon from "@mui/icons-material/Restore";
import PreviewIcon from "@mui/icons-material/Visibility";
import VerifiedIcon from "@mui/icons-material/Verified";
import BlockIcon from "@mui/icons-material/Block";
import StarIcon from "@mui/icons-material/Star";
import StarBorderIcon from "@mui/icons-material/StarBorder";
import AccessTimeIcon from "@mui/icons-material/AccessTime";

import { namedSetsApi, dimensionsApi, preferencesApi, queryRouterApiClient } from "../../api/client";
import type {
  BuilderDefinition,
  Dimension,
  NamedSet,
  NamedSetCreate,
  NamedSetPreviewResponse,
  NamedSetValidateResponse,
  VersionEntry,
} from "../../api/types";
import { useUserPreferences } from "../../api/hooks";
import { canEditModelConfig, isTenantAdmin } from "../../auth/currentUser";
import { useBuilderStore } from "../../store/builderStore";
import { useConfirm } from "../Confirm";
import EntityImpactSummary from "./EntityImpactSummary";
import TemplateGalleryDialog from "./TemplateGalleryDialog";
import type { NamedSetTemplate } from "./templates";

type DialogMode = "create" | "edit";
type DialogTab = "basics" | "rule" | "scope" | "preview" | "history";
type ListType = "fixed" | "dynamic_top_n" | "filtered" | "advanced_mdx";

interface FormState {
  name: string;
  display_name: string;
  description: string;
  display_folder: string;
  scope: number;
  expression: string;
  dimensions: string;
  list_type: ListType;
  certification_status: string;
  // Fixed members builder
  fixed_dimension: string;
  fixed_hierarchy: string;
  fixed_members: string[];
  // Top N builder
  topn_entity: string;
  topn_count: string;
  topn_measure: string;
  topn_direction: "top" | "bottom";
  // Filter builder
  filter_entity: string;
  filter_conditions: { field: string; operator: string; value: string }[];
  filter_logic: "AND" | "OR";
}

const EMPTY: FormState = {
  name: "",
  display_name: "",
  description: "",
  display_folder: "",
  scope: 2,
  expression: "",
  dimensions: "",
  list_type: "fixed",
  certification_status: "draft",
  fixed_dimension: "",
  fixed_hierarchy: "",
  fixed_members: [],
  topn_entity: "",
  topn_count: "10",
  topn_measure: "",
  topn_direction: "top",
  filter_entity: "",
  filter_conditions: [{ field: "", operator: ">", value: "" }],
  filter_logic: "AND",
};

const SCOPE_OPTIONS = [
  { value: 1, label: "namedSets.scopeSession" },
  { value: 2, label: "namedSets.scopeGlobal" },
];

const LIST_TYPE_OPTIONS: { value: ListType; label: string }[] = [
  { value: "fixed", label: "namedSets.listTypeFixed" },
  { value: "dynamic_top_n", label: "namedSets.listTypeTopN" },
  { value: "filtered", label: "namedSets.listTypeFiltered" },
  { value: "advanced_mdx", label: "namedSets.listTypeMdx" },
];

const LIST_TYPE_LABELS: Record<string, string> = {
  fixed: "namedSets.labelFixed",
  dynamic_top_n: "namedSets.labelDynamic",
  filtered: "namedSets.labelFiltered",
  advanced_mdx: "namedSets.labelMdx",
};

const CERT_COLORS: Record<string, "success" | "warning" | "default" | "info"> = {
  certified: "success",
  shared: "info",
  draft: "default",
  deprecated: "warning",
};

const FILTER_OPERATORS = [
  { value: ">", label: ">" },
  { value: ">=", label: ">=" },
  { value: "<", label: "<" },
  { value: "<=", label: "<=" },
  { value: "=", label: "=" },
  { value: "!=", label: "!=" },
];

function buildBuilderDefinition(form: FormState): BuilderDefinition | null {
  switch (form.list_type) {
    case "fixed":
      if (!form.fixed_dimension || form.fixed_members.length === 0) return null;
      return {
        type: "fixedMembers",
        dimension: form.fixed_dimension,
        hierarchy: form.fixed_hierarchy || form.fixed_dimension,
        members: form.fixed_members,
      };
    case "dynamic_top_n":
      if (!form.topn_entity || !form.topn_count || !form.topn_measure) return null;
      return {
        type: "topN",
        entity: form.topn_entity,
        count: Number(form.topn_count),
        measure: form.topn_measure,
        direction: form.topn_direction,
      };
    case "filtered":
      if (!form.filter_entity || form.filter_conditions.length === 0) return null;
      return {
        type: "filter",
        entity: form.filter_entity,
        conditions: form.filter_conditions
          .filter((c) => c.field && c.value)
          .map((c) => ({
            field: c.field,
            operator: c.operator,
            value: isNaN(Number(c.value)) ? c.value : Number(c.value),
          })),
        logic: form.filter_logic,
      };
    default:
      return null;
  }
}

export default function NamedSetsPanel() {
  const { projectId = "", modelId = "" } = useParams();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const storeReadOnly = useBuilderStore((s) => s.readOnly);
  const canEdit = canEditModelConfig() && !storeReadOnly;
  const t = useT();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogMode, setDialogMode] = useState<DialogMode>("create");
  const [dialogTab, setDialogTab] = useState<DialogTab>("basics");
  const [editId, setEditId] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [templateGalleryOpen, setTemplateGalleryOpen] = useState(false);
  const [validation, setValidation] = useState<NamedSetValidateResponse | null>(null);
  const [previewData, setPreviewData] = useState<NamedSetPreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [memberOptions, setMemberOptions] = useState<string[]>([]);
  const [membersLoading, setMembersLoading] = useState(false);

  const { data: prefs, refetch: refetchPrefs } = useUserPreferences(projectId, modelId);
  const favouriteIds = useMemo(() => new Set(prefs?.favourites?.named_set ?? []), [prefs]);
  const recentIds = useMemo(() => prefs?.recently_used?.named_set ?? [], [prefs]);

  const toggleFavourite = useCallback(
    async (nsId: string) => {
      await preferencesApi.toggleFavourite(projectId, modelId, { entity_type: "named_set", entity_id: nsId });
      refetchPrefs();
    },
    [projectId, modelId, refetchPrefs],
  );

  const recordRecentlyUsed = useCallback(
    (nsId: string) => {
      preferencesApi.recordRecentlyUsed(projectId, modelId, { entity_type: "named_set", entity_id: nsId })
        .then(() => refetchPrefs())
        .catch(() => {});
    },
    [projectId, modelId, refetchPrefs],
  );

  const queryKey = ["namedSets", projectId, modelId];

  const { data: sets = [], isLoading } = useQuery({
    queryKey,
    queryFn: () => namedSetsApi.list(projectId, modelId),
    enabled: Boolean(projectId && modelId),
  });

  const { data: dims = [] } = useQuery({
    queryKey: ["dimensions", projectId, modelId],
    queryFn: () => dimensionsApi.list(projectId, modelId),
    enabled: Boolean(projectId && modelId),
  });

  const createMut = useMutation({
    mutationFn: (data: NamedSetCreate) =>
      namedSetsApi.create(projectId, modelId, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey });
      closeDialog();
    },
    onError: (err: any) =>
      setError(err?.response?.data?.detail ?? t("namedSets.errorCreate")),
  });

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Record<string, unknown> }) =>
      namedSetsApi.update(projectId, modelId, id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey });
      closeDialog();
    },
    onError: (err: any) =>
      setError(err?.response?.data?.detail ?? t("namedSets.errorUpdate")),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => namedSetsApi.delete(projectId, modelId, id),
    onSuccess: () => qc.invalidateQueries({ queryKey }),
  });

  const certifyMut = useMutation({
    mutationFn: (id: string) => namedSetsApi.certify(projectId, modelId, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey });
      closeDialog();
    },
    onError: (err: any) =>
      setError(err?.response?.data?.detail ?? t("namedSets.errorCertify")),
  });

  const deprecateMut = useMutation({
    mutationFn: ({ id, replacementId }: { id: string; replacementId?: string }) =>
      namedSetsApi.deprecate(projectId, modelId, id, { replacement_id: replacementId || null }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey });
      closeDialog();
    },
    onError: (err: any) =>
      setError(err?.response?.data?.detail ?? t("namedSets.errorDeprecate")),
  });

  const revertMut = useMutation({
    mutationFn: ({ id, versionNumber }: { id: string; versionNumber: number }) =>
      namedSetsApi.revert(projectId, modelId, id, versionNumber),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey });
      setVersions([]);
      closeDialog();
    },
    onError: (err: any) =>
      setError(err?.response?.data?.detail ?? t("namedSets.errorRevert")),
  });

  const [versions, setVersions] = useState<VersionEntry[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [deprecateReplacementId, setDeprecateReplacementId] = useState("");
  const isAdmin = isTenantAdmin();

  const loadVersions = useCallback(
    async (nsId: string) => {
      setVersionsLoading(true);
      try {
        const v = await namedSetsApi.versions(projectId, modelId, nsId);
        setVersions(v);
      } catch {
        setVersions([]);
      } finally {
        setVersionsLoading(false);
      }
    },
    [projectId, modelId],
  );

  useEffect(() => {
    if (dialogTab === "history" && editId) {
      loadVersions(editId);
    }
  }, [dialogTab, editId, loadVersions]);

  useEffect(() => {
    if (!form.fixed_dimension || form.list_type !== "fixed" || !modelId) {
      setMemberOptions([]);
      return;
    }
    let cancelled = false;
    setMembersLoading(true);
    queryRouterApiClient
      .discoverMembers(modelId, form.fixed_dimension)
      .then((res) => {
        if (!cancelled) {
          setMemberOptions(res.members.map((m) => m.name || m.key));
        }
      })
      .catch(() => {
        if (!cancelled) setMemberOptions([]);
      })
      .finally(() => {
        if (!cancelled) setMembersLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [form.fixed_dimension, form.list_type, modelId]);

  function openCreate() {
    setForm(EMPTY);
    setDialogMode("create");
    setDialogTab("basics");
    setEditId(null);
    setError(null);
    setValidation(null);
    setPreviewData(null);
    setVersions([]);
    setDeprecateReplacementId("");
    setDialogOpen(true);
  }

  function applyNamedSetTemplate(template: NamedSetTemplate) {
    setForm({
      ...EMPTY,
      name: template.name,
      display_name: template.display_name,
      description: template.description,
      display_folder: template.display_folder,
      expression: template.set_expression,
      list_type: template.builder_type,
    });
    setDialogMode("create");
    setDialogTab("basics");
    setEditId(null);
    setError(null);
    setValidation(null);
    setPreviewData(null);
    setVersions([]);
    setDeprecateReplacementId("");
    setDialogOpen(true);
  }

  function openEdit(ns: NamedSet) {
    const bd = ns.builder_definition;
    setForm({
      name: ns.name,
      display_name: ns.display_name ?? "",
      description: ns.description ?? "",
      display_folder: ns.display_folder ?? "",
      scope: ns.scope,
      expression: ns.expression,
      dimensions: ns.dimensions ?? "",
      list_type: (ns.list_type as ListType) ?? "advanced_mdx",
      certification_status: ns.certification_status ?? "draft",
      fixed_dimension: bd?.dimension ?? "",
      fixed_hierarchy: bd?.hierarchy ?? "",
      fixed_members: (bd?.members as string[]) ?? [],
      topn_entity: bd?.entity ?? "",
      topn_count: bd?.count != null ? String(bd.count) : "10",
      topn_measure: bd?.measure ?? "",
      topn_direction: bd?.direction ?? "top",
      filter_entity: bd?.entity ?? "",
      filter_conditions:
        (bd?.conditions as { field: string; operator: string; value: string }[]) ??
        [{ field: "", operator: ">", value: "" }],
      filter_logic: (bd?.logic as "AND" | "OR") ?? "AND",
    });
    setDialogMode("edit");
    setDialogTab("basics");
    setEditId(ns.id);
    setError(null);
    setValidation(null);
    setPreviewData(null);
    setVersions([]);
    setDeprecateReplacementId("");
    setDialogOpen(true);
    recordRecentlyUsed(ns.id);
  }

  function closeDialog() {
    setDialogOpen(false);
    setError(null);
    setValidation(null);
    setPreviewData(null);
  }

  function handleSave() {
    const builderDef = buildBuilderDefinition(form);
    const payload: Record<string, unknown> = {
      name: form.name,
      display_name: form.display_name || undefined,
      description: form.description || undefined,
      display_folder: form.display_folder || undefined,
      scope: form.scope,
      dimensions: form.dimensions || undefined,
      list_type: form.list_type,
    };
    if (builderDef) {
      payload.builder_definition = builderDef;
    } else {
      payload.expression = form.expression;
    }

    if (dialogMode === "create") {
      createMut.mutate(payload as unknown as NamedSetCreate);
    } else if (editId) {
      if (form.certification_status) {
        payload.certification_status = form.certification_status;
      }
      updateMut.mutate({ id: editId, data: payload });
    }
  }

  const handleValidate = useCallback(async () => {
    try {
      const builderDef = buildBuilderDefinition(form);
      const data = builderDef
        ? { builder_definition: builderDef }
        : { expression: form.expression };
      const result = await namedSetsApi.validate(projectId, modelId, data);
      setValidation(result);
    } catch {
      setValidation(null);
    }
  }, [form, projectId, modelId]);

  const handlePreview = useCallback(async () => {
    setPreviewLoading(true);
    try {
      if (editId) {
        const result = await namedSetsApi.preview(projectId, modelId, editId);
        setPreviewData(result);
      } else {
        const bd = buildBuilderDefinition(form);
        const result = await namedSetsApi.previewByDefinition(projectId, modelId, {
          builder_definition: bd ?? undefined,
          expression: !bd ? form.expression || undefined : undefined,
        });
        setPreviewData(result);
      }
    } catch {
      setPreviewData(null);
    } finally {
      setPreviewLoading(false);
    }
  }, [editId, projectId, modelId, form]);

  useEffect(() => {
    if (dialogTab === "preview") {
      handlePreview();
    }
  }, [dialogTab, handlePreview]);

  async function handleDelete(ns: NamedSet) {
    const ok = await confirm({
      title: t("namedSets.deleteConfirm"),
      message: t("namedSets.deleteMessage", { name: ns.display_name || ns.name }),
      confirmLabel: t("namedSets.delete"),
    });
    if (ok) deleteMut.mutate(ns.id);
  }

  const isPending = createMut.isPending || updateMut.isPending;
  const nameValid = form.name.trim().length > 0;
  const hasRule =
    form.list_type === "advanced_mdx"
      ? form.expression.trim().length > 0
      : buildBuilderDefinition(form) !== null;

  function addCondition() {
    setForm({
      ...form,
      filter_conditions: [
        ...form.filter_conditions,
        { field: "", operator: ">", value: "" },
      ],
    });
  }

  function removeCondition(idx: number) {
    setForm({
      ...form,
      filter_conditions: form.filter_conditions.filter((_, i) => i !== idx),
    });
  }

  function updateCondition(
    idx: number,
    key: "field" | "operator" | "value",
    value: string,
  ) {
    const updated = [...form.filter_conditions];
    updated[idx] = { ...updated[idx], [key]: value };
    setForm({ ...form, filter_conditions: updated });
  }

  return (
    <Box sx={{ p: 2, overflow: "auto" }}>
      <Box display="flex" alignItems="center" justifyContent="space-between" mb={2}>
        <Typography variant="subtitle1" fontWeight={700}>
          {t("namedSets.title")}
        </Typography>
        {canEdit && (
          <Box display="flex" gap={1}>
            <Button size="small" startIcon={<AutoFixHighIcon />} variant="outlined" onClick={() => setTemplateGalleryOpen(true)}>
              {t("namedSets.fromTemplate")}
            </Button>
            <Button size="small" startIcon={<AddIcon />} variant="contained" onClick={openCreate}>
              {t("namedSets.add")}
            </Button>
          </Box>
        )}
      </Box>

      <Typography variant="body2" color="text.secondary" mb={2}>
        {t("namedSets.description")}
      </Typography>

      {isLoading && <CircularProgress size={20} />}

      {!isLoading && sets.length === 0 && (
        <Typography variant="body2" color="text.secondary">
          {t("namedSets.none")}
        </Typography>
      )}

      {(() => {
        const favSets = sets.filter((s: NamedSet) => favouriteIds.has(s.id));
        const recentSets = sets.filter((s: NamedSet) => !favouriteIds.has(s.id) && recentIds.includes(s.id));
        const otherSets = sets.filter((s: NamedSet) => !favouriteIds.has(s.id) && !recentIds.includes(s.id));
        const sections: { label: string | null; items: NamedSet[]; icon?: typeof StarIcon }[] = [];
        if (favSets.length > 0) sections.push({ label: t("namedSets.favourites"), items: favSets, icon: StarIcon });
        if (recentSets.length > 0) sections.push({ label: t("namedSets.recentlyUsed"), items: recentSets, icon: AccessTimeIcon });
        if (otherSets.length > 0 || sections.length === 0) {
          sections.push({ label: sections.length > 0 ? t("namedSets.allNamedSets") : null, items: otherSets });
        }
        return sections.map((section, si) => (
          <Box key={si} sx={{ mb: 2 }}>
            {section.label && (
              <Stack direction="row" alignItems="center" spacing={0.5} sx={{ mb: 1 }}>
                {section.icon && (() => { const SIcon = section.icon; return <SIcon sx={{ fontSize: 16, color: "text.secondary" }} />; })()}
                <Typography variant="caption" fontWeight={600} color="text.secondary" sx={{ textTransform: "uppercase", fontSize: 11 }}>
                  {section.label}
                </Typography>
              </Stack>
            )}
            <Stack spacing={1.5}>
              {section.items.map((ns: NamedSet) => {
          const isFav = favouriteIds.has(ns.id);
          return (
          <Card key={ns.id} variant="outlined">
            <CardContent sx={{ py: 1.5, "&:last-child": { pb: 1.5 } }}>
              <Box display="flex" alignItems="center" justifyContent="space-between">
                <Box display="flex" alignItems="center" gap={0.5}>
                  <Tooltip title={isFav ? t("namedSets.removeFromFavourites") : t("namedSets.addToFavourites")}>
                    <IconButton size="small" onClick={() => toggleFavourite(ns.id)} sx={{ p: 0.25 }}>
                      {isFav ? <StarIcon sx={{ fontSize: 18, color: "#f9a825" }} /> : <StarBorderIcon sx={{ fontSize: 18, color: "text.secondary" }} />}
                    </IconButton>
                  </Tooltip>
                  <Box>
                  <Typography variant="subtitle2">
                    {ns.display_name || ns.name}
                  </Typography>
                  {ns.display_name && (
                    <Typography variant="caption" color="text.secondary" fontFamily="monospace">
                      {ns.name}
                    </Typography>
                  )}
                  </Box>
                </Box>
                <Box display="flex" alignItems="center" gap={0.5}>
                  <Chip
                    label={LIST_TYPE_LABELS[ns.list_type ?? "advanced_mdx"] ? t(LIST_TYPE_LABELS[ns.list_type ?? "advanced_mdx"]) : ns.list_type}
                    size="small"
                    variant="outlined"
                  />
                  <Chip
                    label={ns.scope === 2 ? t("namedSets.scopeGlobal") : t("namedSets.scopeSession")}
                    size="small"
                    variant="outlined"
                    color={ns.scope === 2 ? "primary" : "default"}
                  />
                  {ns.certification_status !== "draft" && (
                    <Chip
                      icon={ns.certification_status === "certified" ? <VerifiedIcon /> : undefined}
                      label={ns.certification_status}
                      size="small"
                      color={CERT_COLORS[ns.certification_status] ?? "default"}
                    />
                  )}
                  {canEdit && (
                    <>
                      <Button size="small" startIcon={<EditIcon />} onClick={() => openEdit(ns)}>
                        {t("namedSets.edit")}
                      </Button>
                      <Button size="small" startIcon={<DeleteIcon />} onClick={() => handleDelete(ns)}>
                        {t("namedSets.delete")}
                      </Button>
                    </>
                  )}
                </Box>
              </Box>
              {ns.description && (
                <Typography variant="body2" color="text.secondary" mt={0.5}>
                  {ns.description}
                </Typography>
              )}
              {ns.certification_status === "deprecated" && (
                <Alert severity="warning" sx={{ mt: 1, py: 0 }} icon={<BlockIcon fontSize="small" />}>
                  {t("namedSets.deprecatedMessage")}
                </Alert>
              )}
              {ns.display_folder && (
                <Typography variant="caption" color="text.secondary" mt={0.5} display="block">
                  {t("namedSets.folder", { folder: ns.display_folder })}
                </Typography>
              )}
            </CardContent>
          </Card>
          );
        })}
            </Stack>
          </Box>
        ));
      })()}

      {/* ---- Tabbed Dialog ---- */}
      <Dialog open={dialogOpen} onClose={closeDialog} maxWidth="md" fullWidth>
        <DialogTitle sx={{ pb: 0.5 }}>
          {dialogMode === "create" ? t("namedSets.addNamedSet") : t("namedSets.editNamedSet")}
        </DialogTitle>
        <Box sx={{ borderBottom: 1, borderColor: "divider", px: 3 }}>
          <Tabs value={dialogTab} onChange={(_, v) => setDialogTab(v as DialogTab)}>
            <Tab value="basics" label={t("namedSets.tabBasics")} />
            <Tab value="rule" label={t("namedSets.tabListRule")} />
            <Tab value="scope" label={t("namedSets.tabScope")} />
            <Tab value="preview" label={t("namedSets.tabPreview")} />
            <Tab value="history" label={t("namedSets.tabHistory")} disabled={dialogMode === "create"} icon={<HistoryIcon />} iconPosition="start" />
          </Tabs>
        </Box>
        <DialogContent dividers sx={{ minHeight: 340 }}>
          {error && (
            <Alert severity="error" sx={{ mb: 2 }}>
              {error}
            </Alert>
          )}

          {/* Tab 1: Basics */}
          <Box sx={{ display: dialogTab === "basics" ? "block" : "none" }}>
            <TextField
              label={t("namedSets.name")}
              fullWidth
              margin="normal"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              disabled={dialogMode === "edit"}
              placeholder={t("namedSets.namePlaceholder")}
              error={form.name.length > 0 && !nameValid}
            />
            <TextField
              label={t("namedSets.displayName")}
              fullWidth
              margin="normal"
              value={form.display_name}
              onChange={(e) => setForm({ ...form, display_name: e.target.value })}
              placeholder={t("namedSets.displayNamePlaceholder")}
            />
            <TextField
              label={t("namedSets.descriptionLabel")}
              fullWidth
              margin="normal"
              multiline
              rows={2}
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
            <TextField
              label={t("namedSets.displayFolder")}
              fullWidth
              margin="normal"
              value={form.display_folder}
              onChange={(e) => setForm({ ...form, display_folder: e.target.value })}
              placeholder={t("namedSets.displayFolderPlaceholder")}
            />
          </Box>

          {/* Tab 2: List Rule */}
          <Box sx={{ display: dialogTab === "rule" ? "block" : "none" }}>
            <TextField
              select
              label={t("namedSets.listType")}
              fullWidth
              margin="normal"
              value={form.list_type}
              onChange={(e) => setForm({ ...form, list_type: e.target.value as ListType })}
            >
              {LIST_TYPE_OPTIONS.map((o) => (
                <MenuItem key={o.value} value={o.value}>
                  {t(`namedSets.listType.${o.value}`)}
                </MenuItem>
              ))}
            </TextField>

            {/* Fixed Members Builder */}
            {form.list_type === "fixed" && (
              <Box mt={2}>
                <TextField
                  select
                  label={t("namedSets.dimension")}
                  fullWidth
                  margin="normal"
                  value={form.fixed_dimension}
                  onChange={(e) =>
                    setForm({ ...form, fixed_dimension: e.target.value, fixed_hierarchy: e.target.value })
                  }
                >
                  <MenuItem value="">{t("namedSets.selectPlaceholder")}</MenuItem>
                  {dims.map((d: Dimension) => (
                    <MenuItem key={d.id} value={d.name}>
                      {d.display_name || d.name}
                    </MenuItem>
                  ))}
                </TextField>
                <Autocomplete
                  multiple
                  freeSolo
                  loading={membersLoading}
                  options={memberOptions}
                  value={form.fixed_members}
                  onChange={(_, newValue) =>
                    setForm({ ...form, fixed_members: newValue as string[] })
                  }
                  renderInput={(params) => (
                    <TextField
                      {...params}
                      label={t("namedSets.members")}
                      margin="normal"
                      placeholder={memberOptions.length ? t("namedSets.selectMembers") : t("namedSets.selectDimensionFirst")}
                      helperText={memberOptions.length ? t("namedSets.membersHelp") : t("namedSets.membersHelpSelectDim")}
                    />
                  )}
                />
              </Box>
            )}

            {/* Top N Builder */}
            {form.list_type === "dynamic_top_n" && (
              <Box mt={2}>
                <TextField
                  select
                  label={t("namedSets.entity")}
                  fullWidth
                  margin="normal"
                  value={form.topn_entity}
                  onChange={(e) => setForm({ ...form, topn_entity: e.target.value })}
                >
                  <MenuItem value="">{t("namedSets.selectPlaceholder")}</MenuItem>
                  {dims.map((d: Dimension) => (
                    <MenuItem key={d.id} value={d.name}>
                      {d.display_name || d.name}
                    </MenuItem>
                  ))}
                </TextField>
                <TextField
                  label={t("namedSets.count")}
                  type="number"
                  fullWidth
                  margin="normal"
                  value={form.topn_count}
                  onChange={(e) => setForm({ ...form, topn_count: e.target.value })}
                  inputProps={{ min: 1 }}
                />
                <TextField
                  label={t("namedSets.measure")}
                  fullWidth
                  margin="normal"
                  value={form.topn_measure}
                  onChange={(e) => setForm({ ...form, topn_measure: e.target.value })}
                  placeholder={t("namedSets.measurePlaceholder")}
                  helperText={t("namedSets.measureHelp")}
                />
                <ToggleButtonGroup
                  value={form.topn_direction}
                  exclusive
                  onChange={(_, v) => v && setForm({ ...form, topn_direction: v })}
                  size="small"
                  sx={{ mt: 1 }}
                >
                  <ToggleButton value="top">{t("namedSets.topHighest")}</ToggleButton>
                  <ToggleButton value="bottom">{t("namedSets.bottomLowest")}</ToggleButton>
                </ToggleButtonGroup>
              </Box>
            )}

            {/* Filter Builder */}
            {form.list_type === "filtered" && (
              <Box mt={2}>
                <TextField
                  select
                  label={t("namedSets.entity")}
                  fullWidth
                  margin="normal"
                  value={form.filter_entity}
                  onChange={(e) => setForm({ ...form, filter_entity: e.target.value })}
                >
                  <MenuItem value="">{t("namedSets.selectPlaceholder")}</MenuItem>
                  {dims.map((d: Dimension) => (
                    <MenuItem key={d.id} value={d.name}>
                      {d.display_name || d.name}
                    </MenuItem>
                  ))}
                </TextField>
                <ToggleButtonGroup
                  value={form.filter_logic}
                  exclusive
                  onChange={(_, v) => v && setForm({ ...form, filter_logic: v })}
                  size="small"
                  sx={{ mt: 1, mb: 1 }}
                >
                  <ToggleButton value="AND">{t("namedSets.allConditions")}</ToggleButton>
                  <ToggleButton value="OR">{t("namedSets.anyCondition")}</ToggleButton>
                </ToggleButtonGroup>
                {form.filter_conditions.map((cond, idx) => (
                  <Box key={idx} display="flex" gap={1} alignItems="center" mt={1}>
                    <TextField
                      label={t("namedSets.measure")}
                      size="small"
                      value={cond.field}
                      onChange={(e) => updateCondition(idx, "field", e.target.value)}
                      sx={{ flex: 2 }}
                    />
                    <TextField
                      select
                      label={t("namedSets.operator")}
                      size="small"
                      value={cond.operator}
                      onChange={(e) => updateCondition(idx, "operator", e.target.value)}
                      sx={{ width: 80 }}
                    >
                      {FILTER_OPERATORS.map((o) => (
                        <MenuItem key={o.value} value={o.value}>
                          {o.label}
                        </MenuItem>
                      ))}
                    </TextField>
                    <TextField
                      label={t("namedSets.value")}
                      size="small"
                      value={cond.value}
                      onChange={(e) => updateCondition(idx, "value", e.target.value)}
                      sx={{ flex: 1 }}
                    />
                    <Button
                      size="small"
                      color="error"
                      onClick={() => removeCondition(idx)}
                      disabled={form.filter_conditions.length <= 1}
                    >
                      {t("namedSets.remove")}
                    </Button>
                  </Box>
                ))}
                <Button size="small" onClick={addCondition} sx={{ mt: 1 }}>
                  {t("namedSets.addCondition")}
                </Button>
              </Box>
            )}

            {/* Advanced MDX */}
            {form.list_type === "advanced_mdx" && (
              <TextField
                label={t("namedSets.mdxExpression")}
                fullWidth
                margin="normal"
                multiline
                rows={4}
                value={form.expression}
                onChange={(e) => setForm({ ...form, expression: e.target.value })}
                placeholder={t("namedSets.mdxPlaceholder")}
                helperText={t("namedSets.mdxHelp")}
              />
            )}

            <Box mt={2}>
              <Button
                variant="outlined"
                size="small"
                onClick={handleValidate}
              >
                {t("namedSets.validate")}
              </Button>
              {validation && (
                <Alert
                  severity={validation.is_valid ? "success" : "error"}
                  sx={{ mt: 1 }}
                >
                  {validation.is_valid
                    ? t("namedSets.expressionValid")
                    : validation.errors.join("; ")}
                  {validation.explanation && (
                    <Typography variant="caption" display="block" mt={0.5}>
                      {validation.explanation}
                    </Typography>
                  )}
                  {validation.warnings.length > 0 && (
                    <Typography variant="caption" display="block" color="warning.main" mt={0.5}>
                      {t("namedSets.warnings", { warnings: validation.warnings.join("; ") })}
                    </Typography>
                  )}
                  {validation.estimated_cost_band && (
                    <Typography variant="caption" display="block" mt={0.5}>
                      {t("namedSets.estimatedCost", { cost: validation.estimated_cost_band })}
                    </Typography>
                  )}
                </Alert>
              )}
            </Box>
          </Box>

          {/* Tab 3: Scope & Governance */}
          <Box sx={{ display: dialogTab === "scope" ? "block" : "none" }}>
            <TextField
              select
              label={t("namedSets.scope")}
              fullWidth
              margin="normal"
              value={form.scope}
              onChange={(e) => setForm({ ...form, scope: Number(e.target.value) })}
              helperText={t("namedSets.scopeHelp")}
            >
              {SCOPE_OPTIONS.map((o) => (
                <MenuItem key={o.value} value={o.value}>
                  {t(`namedSets.scope.${o.value}`)}
                </MenuItem>
              ))}
            </TextField>
            <TextField
              label={t("namedSets.dimensions")}
              fullWidth
              margin="normal"
              value={form.dimensions}
              onChange={(e) => setForm({ ...form, dimensions: e.target.value })}
              placeholder={t("namedSets.dimensionsPlaceholder")}
              helperText={t("namedSets.dimensionsHelp")}
            />
            <TextField
              select
              label={t("namedSets.certificationStatus")}
              fullWidth
              margin="normal"
              value={form.certification_status}
              onChange={(e) => setForm({ ...form, certification_status: e.target.value })}
              helperText={!isAdmin ? t("namedSets.certificationHelp") : undefined}
            >
              <MenuItem value="draft">{t("namedSets.certStatus.draft")}</MenuItem>
              <MenuItem value="shared">{t("namedSets.certStatus.shared")}</MenuItem>
              {isAdmin && <MenuItem value="certified">{t("namedSets.certStatus.certified")}</MenuItem>}
              {isAdmin && <MenuItem value="deprecated">{t("namedSets.certStatus.deprecated")}</MenuItem>}
            </TextField>
          </Box>

          {/* Tab 4: Preview */}
          <Box sx={{ display: dialogTab === "preview" ? "block" : "none" }}>
            <Box display="flex" alignItems="center" gap={1} mb={2}>
              <Button
                variant="outlined"
                startIcon={<PreviewIcon />}
                onClick={handlePreview}
                disabled={previewLoading}
              >
                {previewLoading ? <CircularProgress size={16} /> : t("namedSets.refreshPreview")}
              </Button>
              {previewData?.explanation && (
                <Typography variant="body2" color="text.secondary">
                  {previewData.explanation}
                </Typography>
              )}
            </Box>
            {previewData?.warnings && previewData.warnings.length > 0 && (
              <Typography variant="caption" color="warning.main" mb={1} display="block">
                {t("namedSets.warnings", { warnings: previewData.warnings.join("; ") })}
              </Typography>
            )}
            {previewData && previewData.items.length > 0 ? (
              <>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>{t("namedSets.previewNumber")}</TableCell>
                      <TableCell>{t("namedSets.previewMember")}</TableCell>
                      <TableCell>{t("namedSets.previewKey")}</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {previewData.items.map((item) => (
                      <TableRow key={item.ordinal}>
                        <TableCell>{item.ordinal}</TableCell>
                        <TableCell>{item.caption}</TableCell>
                        <TableCell>
                          <Typography variant="caption" fontFamily="monospace">
                            {item.key}
                          </Typography>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                {previewData.truncated && (
                  <Typography variant="caption" color="warning.main" mt={1} display="block">
                    {t("namedSets.truncated")}
                  </Typography>
                )}
                <Typography variant="caption" color="text.secondary" mt={0.5} display="block">
                  {t("namedSets.total", { count: String(previewData.total_count) })}
                </Typography>
              </>
            ) : (
              !previewLoading && (
                <Typography variant="body2" color="text.secondary">
                  {t("namedSets.noPreviewData")}
                </Typography>
              )
            )}
          </Box>

          {/* Tab 5: History */}
          <Box sx={{ display: dialogTab === "history" ? "block" : "none" }}>
            {editId && (form.certification_status === "shared" || form.certification_status === "certified") && (
              <EntityImpactSummary
                entityType="named_set"
                entityId={editId}
                entityName={form.display_name || form.name}
                projectId={projectId}
                modelId={modelId}
              />
            )}
            {isAdmin && editId && (
              <Box display="flex" gap={1} mb={2} flexWrap="wrap">
                {form.certification_status !== "certified" && (
                  <Button
                    variant="outlined"
                    color="success"
                    size="small"
                    startIcon={<VerifiedIcon />}
                    disabled={certifyMut.isPending}
                    onClick={() => editId && certifyMut.mutate(editId)}
                  >
                    {certifyMut.isPending ? <CircularProgress size={14} /> : t("namedSets.certify")}
                  </Button>
                )}
                {form.certification_status !== "deprecated" && (
                  <Box display="flex" gap={1} alignItems="center">
                    <Button
                      variant="outlined"
                      color="warning"
                      size="small"
                      startIcon={<BlockIcon />}
                      disabled={deprecateMut.isPending}
                      onClick={() =>
                        editId &&
                        deprecateMut.mutate({
                          id: editId,
                          replacementId: deprecateReplacementId || undefined,
                        })
                      }
                    >
                      {deprecateMut.isPending ? <CircularProgress size={14} /> : t("namedSets.deprecate")}
                    </Button>
                    <TextField
                      select
                      size="small"
                      label={t("namedSets.replacement")}
                      value={deprecateReplacementId}
                      onChange={(e) => setDeprecateReplacementId(e.target.value)}
                      sx={{ minWidth: 180 }}
                    >
                      <MenuItem value="">{t("namedSets.replacementNone")}</MenuItem>
                      {sets
                        .filter((s: NamedSet) => s.id !== editId)
                        .map((s: NamedSet) => (
                          <MenuItem key={s.id} value={s.id}>
                            {s.display_name || s.name}
                          </MenuItem>
                        ))}
                    </TextField>
                  </Box>
                )}
              </Box>
            )}
            {!isAdmin && editId && form.certification_status !== "shared" && form.certification_status !== "certified" && canEdit && (
              <Alert severity="info" sx={{ mb: 2 }}>
                {t("namedSets.certificationRequest")}
              </Alert>
            )}
            <Typography variant="subtitle2" mb={1}>
              {t("namedSets.versionHistory")}
            </Typography>
            {versionsLoading && <CircularProgress size={20} />}
            {!versionsLoading && versions.length === 0 && (
              <Typography variant="body2" color="text.secondary">
                {t("namedSets.noVersionHistory")}
              </Typography>
            )}
            {versions.length > 0 && (
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>{t("namedSets.historyVersion")}</TableCell>
                    <TableCell>{t("namedSets.historyChangedBy")}</TableCell>
                    <TableCell>{t("namedSets.historyDate")}</TableCell>
                    <TableCell>{t("namedSets.historySummary")}</TableCell>
                    <TableCell />
                  </TableRow>
                </TableHead>
                <TableBody>
                  {versions.map((v) => (
                    <TableRow key={v.id}>
                      <TableCell>{v.version_number}</TableCell>
                      <TableCell>{v.changed_by ?? t("common.na")}</TableCell>
                      <TableCell>
                        <Tooltip title={v.changed_at}>
                          <span>{new Date(v.changed_at).toLocaleDateString()}</span>
                        </Tooltip>
                      </TableCell>
                      <TableCell>{v.change_summary ?? t("common.na")}</TableCell>
                      <TableCell>
                        {canEdit && (
                          <Button
                            size="small"
                            startIcon={<RestoreIcon />}
                            disabled={revertMut.isPending}
                            onClick={() =>
                              editId &&
                              revertMut.mutate({
                                id: editId,
                                versionNumber: v.version_number,
                              })
                            }
                          >
                            {t("namedSets.revert")}
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </Box>
        </DialogContent>
        <DialogActions>
          <Button onClick={closeDialog}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            disabled={isPending || !nameValid || !hasRule}
            onClick={handleSave}
            sx={{ display: dialogTab === "history" ? "none" : undefined }}
          >
            {isPending ? <CircularProgress size={16} /> : dialogMode === "create" ? t("namedSets.create") : t("namedSets.update")}
          </Button>
        </DialogActions>
      </Dialog>

      <TemplateGalleryDialog
        open={templateGalleryOpen}
        onClose={() => setTemplateGalleryOpen(false)}
        entityType="named_set"
        onApplyNamedSet={applyNamedSetTemplate}
      />
    </Box>
  );
}
