import { useMemo, useState } from "react";
import { useT } from "../../i18n";
import { useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Chip,
  CircularProgress,
  IconButton,
  ListItemText,
  MenuItem,
  Paper,
  Select,
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
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import { ui } from "../../theme/tokens";
import { dataTagsApi, personasApi } from "../../api/client";
import {
  useDataTags,
  useDimensions,
  useHierarchies,
  useMeasures,
  usePersonas,
} from "../../api/hooks";
import type {
  Persona,
  PersonaCreate,
  PersonaUpdate,
} from "../../api/types";
import { useConfirm } from "../Confirm";

interface EditorState {
  open: boolean;
  personaId: string | null;
  slug: string;
  name: string;
  description: string;
  measureIds: string[];
  dimensionIds: string[];
  hierarchyIds: string[];
  audienceRoles: string[];
  defaultFiltersJson: string;
  bypassRowSecurity: boolean;
  bypassRowSecurityInitial: boolean;
  includesHiddenColumns: boolean;
  restrictedTagIds: string[];
  // F-008-07: when loading the persona's current restrictions fails we
  // must not silently overwrite them with an empty set on save.
  restrictionsLoaded: boolean;
}

const EMPTY_EDITOR: EditorState = {
  open: false,
  personaId: null,
  slug: "",
  name: "",
  description: "",
  measureIds: [],
  dimensionIds: [],
  hierarchyIds: [],
  audienceRoles: [],
  defaultFiltersJson: "",
  bypassRowSecurity: false,
  bypassRowSecurityInitial: false,
  includesHiddenColumns: false,
  restrictedTagIds: [],
  restrictionsLoaded: true,
};

export default function PersonasPanel() {
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const t = useT();

  const personas = usePersonas(projectId!, modelId!);
  const measures = useMeasures(projectId!, modelId!);
  const dimensions = useDimensions(projectId!, modelId!);
  const hierarchies = useHierarchies(projectId!, modelId!);
  const dataTags = useDataTags(projectId!, modelId!);

  const [editor, setEditor] = useState<EditorState>(EMPTY_EDITOR);
  const [error, setError] = useState<string | null>(null);

  const refresh = () =>
    qc.invalidateQueries({ queryKey: ["personas", projectId, modelId] });

  // F-008-07: persona save and restriction save are one logical operation.
  // The mutation chains both calls so a restriction failure is surfaced in
  // the editor instead of dying in the developer console, and the create
  // path persists the ticked restrictions against the new persona id.
  const saveMutation = useMutation({
    mutationFn: async ({
      personaId,
      body,
      restrictedTagIds,
      saveRestrictions,
    }: {
      personaId: string | null;
      body: PersonaCreate;
      restrictedTagIds: string[];
      saveRestrictions: boolean;
    }) => {
      let targetId = personaId;
      if (targetId) {
        await personasApi.update(projectId!, modelId!, targetId, body as PersonaUpdate);
      } else {
        const created = await personasApi.create(projectId!, modelId!, body);
        targetId = created.id;
      }
      if (saveRestrictions) {
        try {
          await dataTagsApi.setPersonaRestrictions(
            projectId!, modelId!, targetId,
            { tag_ids: restrictedTagIds },
          );
        } catch (e: any) {
          // The persona itself saved — report the restriction failure
          // explicitly so the modeler knows the security control did
          // not persist.
          throw new Error(
            t("personas.restrictionsSaveFailed", {
              error: extractError(e) || t("errors.requestFailed"),
            }),
          );
        }
      }
    },
    onSuccess: () => {
      refresh();
      setEditor(EMPTY_EDITOR);
      setError(null);
    },
    onError: (e: any) => setError(extractError(e) || t("errors.requestFailed")),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) =>
      personasApi.delete(projectId!, modelId!, id),
    onSuccess: () => refresh(),
    onError: (e: any) => setError(extractError(e) || t("errors.requestFailed")),
  });

  function openCreate() {
    setError(null);
    setEditor({ ...EMPTY_EDITOR, open: true });
  }

  async function openEdit(p: Persona) {
    setError(null);
    let tagIds: string[] = [];
    let restrictionsLoaded = true;
    try {
      const restrictions = await dataTagsApi.getPersonaRestrictions(
        projectId!, modelId!, p.id,
      );
      tagIds = restrictions.map((r) => r.tag_id);
    } catch {
      // F-008-07: do NOT default to "no restrictions" — saving would
      // silently wipe the persona's column security. Disable the
      // restrictions section for this edit instead.
      restrictionsLoaded = false;
      setError(t("personas.restrictionsLoadFailed"));
    }
    setEditor({
      open: true,
      personaId: p.id,
      slug: p.slug,
      name: p.name,
      description: p.description ?? "",
      measureIds: p.included_measure_ids,
      dimensionIds: p.included_dimension_ids,
      hierarchyIds: p.included_hierarchy_ids,
      audienceRoles: p.audience_roles,
      defaultFiltersJson:
        Object.keys(p.default_filters || {}).length > 0
          ? JSON.stringify(p.default_filters, null, 2)
          : "",
      bypassRowSecurity: Boolean(p.bypass_row_security),
      bypassRowSecurityInitial: Boolean(p.bypass_row_security),
      includesHiddenColumns: Boolean(p.includes_hidden_columns),
      restrictedTagIds: tagIds,
      restrictionsLoaded,
    });
  }

  function buildBody(): PersonaCreate {
    let defaultFilters: Record<string, unknown> = {};
    const raw = editor.defaultFiltersJson.trim();
    if (raw) {
      try {
        defaultFilters = JSON.parse(raw);
      } catch (e) {
        throw new Error(t("personas.errorJsonInvalid"));
      }
    }
    return {
      slug: editor.slug.trim(),
      name: editor.name.trim(),
      description: editor.description.trim() || null,
      included_measure_ids: editor.measureIds,
      included_dimension_ids: editor.dimensionIds,
      included_hierarchy_ids: editor.hierarchyIds,
      audience_roles: editor.audienceRoles,
      default_filters: defaultFilters,
      bypass_row_security: editor.bypassRowSecurity,
      includes_hidden_columns: editor.includesHiddenColumns,
    };
  }

  async function handleSave() {
    setError(null);
    let body: PersonaCreate;
    try {
      body = buildBody();
    } catch (e: any) {
      setError(e.message);
      return;
    }
    if (!body.name) {
      setError(t("personas.errorNameRequired"));
      return;
    }
    if (!body.slug) {
      setError(t("personas.errorSlugRequired"));
      return;
    }
    if (!/^[a-z0-9_]+$/.test(body.slug)) {
      setError(t("personas.errorSlugInvalid"));
      return;
    }
    // Phase 8.C.1 — bypass flag is a governance switch. Confirm on every
    // save that newly enables it (including create with bypass=true), so
    // the operational consequence is spelled out to the modeler.
    const bypassTurnedOn =
      editor.bypassRowSecurity && !editor.bypassRowSecurityInitial;
    if (bypassTurnedOn) {
      const ok = await confirm({
        title: t("personas.bypassConfirmTitle"),
        message: t("personas.bypassConfirmMessage"),
        confirmLabel: t("personas.bypassConfirmButton"),
        destructive: true,
      });
      if (!ok) return;
    }
    saveMutation.mutate({
      personaId: editor.personaId,
      body,
      restrictedTagIds: editor.restrictedTagIds,
      // Skip the restriction write when the current restrictions could
      // not be loaded (would wipe them) — and on create, skip the extra
      // call when nothing was ticked.
      saveRestrictions:
        editor.restrictionsLoaded
        && (editor.personaId !== null || editor.restrictedTagIds.length > 0),
    });
  }

  async function handleDelete(p: Persona) {
    const ok = await confirm({
      title: t("personas.deleteConfirmTitle", { name: p.name }),
      message: t("personas.deleteConfirmMessage"),
      confirmLabel: t("personas.deleteConfirmButton"),
      destructive: true,
    });
    if (ok) deleteMutation.mutate(p.id);
  }

  const measureOptions = measures.data ?? [];
  const dimensionOptions = dimensions.data ?? [];
  const hierarchyOptions = hierarchies.data ?? [];

  const measureNameById = useMemo(() => {
    const m = new Map<string, string>();
    for (const x of measureOptions) m.set(x.id, x.name);
    return m;
  }, [measureOptions]);
  const dimensionNameById = useMemo(() => {
    const m = new Map<string, string>();
    for (const x of dimensionOptions) m.set(x.id, x.name);
    return m;
  }, [dimensionOptions]);
  const hierarchyNameById = useMemo(() => {
    const m = new Map<string, string>();
    for (const x of hierarchyOptions) m.set(x.id, x.name);
    return m;
  }, [hierarchyOptions]);

  return (
    <Box sx={{ p: 2 }}>
      <Box sx={{ display: "flex", alignItems: "center", mb: 2, gap: 1 }}>
        <Typography variant="h6" sx={{ flexGrow: 1 }}>
          {t("personas.title")}
        </Typography>
        <Button
          variant="contained"
          size="small"
          startIcon={<AddIcon />}
          onClick={openCreate}
        >
          {t("personas.newPersona")}
        </Button>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}

      {personas.isLoading ? (
        <CircularProgress size={20} />
      ) : (
        <TableContainer component={Paper} variant="outlined">
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>{t("personas.tableHeaderName")}</TableCell>
                <TableCell align="right">{t("personas.tableHeaderMeasures")}</TableCell>
                <TableCell align="right">{t("personas.tableHeaderDimensions")}</TableCell>
                <TableCell align="right">{t("personas.tableHeaderHierarchies")}</TableCell>
                <TableCell>{t("personas.tableHeaderAudienceRoles")}</TableCell>
                <TableCell>{t("personas.tableHeaderRowSecurity")}</TableCell>
                <TableCell align="right">{t("personas.tableHeaderActions")}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {(personas.data ?? []).map((p) => (
                <TableRow key={p.id} hover>
                  <TableCell>
                    <Typography variant="body2" fontWeight={500}>
                      {p.name}
                    </Typography>
                    {p.description && (
                      <Typography variant="caption" color="text.secondary">
                        {p.description}
                      </Typography>
                    )}
                  </TableCell>
                  <TableCell align="right">
                    {p.included_measure_ids.length || t("personas.any")}
                  </TableCell>
                  <TableCell align="right">
                    {p.included_dimension_ids.length || t("personas.any")}
                  </TableCell>
                  <TableCell align="right">
                    {p.included_hierarchy_ids.length || t("personas.any")}
                  </TableCell>
                  <TableCell>
                    <Stack direction="row" spacing={0.5} flexWrap="wrap">
                      {p.audience_roles.length === 0 ? (
                        <Typography variant="caption" color="text.secondary">
                          {t("personas.anyRole")}
                        </Typography>
                      ) : (
                        p.audience_roles.map((r) => (
                          <Chip key={r} label={r} size="small" sx={{ bgcolor: ui.mutedBg, color: ui.muted, fontWeight: 500 }} />
                        ))
                      )}
                    </Stack>
                  </TableCell>
                  <TableCell>
                    {p.bypass_row_security ? (
                      <Typography variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.redBg, color: ui.red, fontWeight: 600, fontSize: 11 }}>{t("personas.bypassed")}</Typography>
                    ) : (
                      <Typography variant="caption" color="text.secondary">
                        {t("personas.enforced")}
                      </Typography>
                    )}
                  </TableCell>
                  <TableCell align="right">
                    <Tooltip title={t("personas.editTooltip")}>
                      <IconButton size="small" onClick={() => openEdit(p)}>
                        <EditIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title={t("personas.deleteTooltip")}>
                      <IconButton
                        size="small"
                        onClick={() => handleDelete(p)}
                      >
                        <DeleteIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              ))}
              {(personas.data?.length ?? 0) === 0 && (
                <TableRow>
                  <TableCell colSpan={7} align="center">
                    <Typography variant="caption" color="text.secondary">
                      {t("personas.none")}
                    </Typography>
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      {editor.open && (
        <Paper variant="outlined" sx={{ mt: 2, p: 2 }}>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>
            {editor.personaId ? t("personas.editTitle") : t("personas.createTitle")}
          </Typography>

          <TextField
            label={t("personas.fieldName")}
            value={editor.name}
            onChange={(e) => setEditor({ ...editor, name: e.target.value })}
            size="small"
            fullWidth
            sx={{ mb: 1 }}
            required
          />
          <TextField
            label={t("personas.fieldSlug")}
            helperText={t("personas.fieldSlugHelp")}
            value={editor.slug}
            onChange={(e) =>
              setEditor({ ...editor, slug: e.target.value.toLowerCase() })
            }
            size="small"
            fullWidth
            sx={{ mb: 1 }}
            required
          />
          <TextField
            label={t("personas.fieldDescription")}
            value={editor.description}
            onChange={(e) =>
              setEditor({ ...editor, description: e.target.value })
            }
            size="small"
            fullWidth
            multiline
            minRows={1}
            maxRows={3}
            sx={{ mb: 2 }}
          />

          <ObjectMultiSelect
            label={t("personas.fieldMeasures")}
            options={measureOptions.map((m) => ({ id: m.id, label: m.name }))}
            value={editor.measureIds}
            onChange={(ids) => setEditor({ ...editor, measureIds: ids })}
            nameById={measureNameById}
          />
          <ObjectMultiSelect
            label={t("personas.fieldDimensions")}
            options={dimensionOptions.map((d) => ({ id: d.id, label: d.name }))}
            value={editor.dimensionIds}
            onChange={(ids) => setEditor({ ...editor, dimensionIds: ids })}
            nameById={dimensionNameById}
          />
          <ObjectMultiSelect
            label={t("personas.fieldHierarchies")}
            options={hierarchyOptions.map((h) => ({
              id: h.id,
              label: h.name,
            }))}
            value={editor.hierarchyIds}
            onChange={(ids) => setEditor({ ...editor, hierarchyIds: ids })}
            nameById={hierarchyNameById}
          />

          <Autocomplete
            multiple
            freeSolo
            size="small"
            options={[]}
            value={editor.audienceRoles}
            onChange={(_, value) =>
              setEditor({ ...editor, audienceRoles: value as string[] })
            }
            renderTags={(value, getTagProps) =>
              (value as string[]).map((option, index) => (
                <Chip
                  label={option}
                  size="small"
                  sx={{ bgcolor: ui.mutedBg, color: ui.muted }}
                  {...getTagProps({ index })}
                  key={option}
                />
              ))
            }
            renderInput={(params) => (
              <TextField
                {...params}
                label={t("personas.fieldAudienceRoles")}
                placeholder={t("personas.fieldAudienceRolesPlaceholder")}
                size="small"
                sx={{ mb: 2 }}
              />
            )}
          />

          <TextField
            label={t("personas.fieldDefaultFilters")}
            value={editor.defaultFiltersJson}
            onChange={(e) =>
              setEditor({ ...editor, defaultFiltersJson: e.target.value })
            }
            size="small"
            fullWidth
            multiline
            minRows={3}
            maxRows={8}
            placeholder={t("personas.fieldDefaultFiltersPlaceholder")}
            sx={{ mb: 2, fontFamily: "monospace" }}
          />

          <Paper
            variant="outlined"
            sx={{
              mb: 2,
              p: 1.5,
              borderColor: editor.bypassRowSecurity ? "error.main" : "divider",
              backgroundColor: editor.bypassRowSecurity
                ? "error.lighter"
                : "transparent",
            }}
          >
            <Typography
              variant="caption"
              color="error.main"
              sx={{ display: "block", mb: 0.5, fontWeight: 600 }}
            >
              {t("personas.bypassRowSecurityTitle")}
            </Typography>
            <Stack direction="row" spacing={1} alignItems="center">
              <input
                id="persona-bypass-row-security"
                type="checkbox"
                checked={editor.bypassRowSecurity}
                onChange={(e) =>
                  setEditor({
                    ...editor,
                    bypassRowSecurity: e.target.checked,
                  })
                }
              />
              <label htmlFor="persona-bypass-row-security">
                <Typography variant="body2">
                  {t("personas.bypassRowSecurityLabel")}
                </Typography>
              </label>
            </Stack>
            {editor.bypassRowSecurity && (
              <Alert severity="error" sx={{ mt: 1 }} variant="outlined">
                {t("personas.bypassRowSecurityWarning")}
              </Alert>
            )}
          </Paper>

          {dataTags.data && dataTags.data.length > 0 && (
            <Paper variant="outlined" sx={{ mb: 2, p: 1.5 }}>
              <Typography
                variant="caption"
                sx={{ display: "block", mb: 0.5, fontWeight: 600 }}
              >
                {t("personas.columnRestrictionsTitle")}
              </Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                {t("personas.columnRestrictionsDescription")}
              </Typography>
              {dataTags.data.map((tag) => {
                const restricted = editor.restrictedTagIds.includes(tag.id);
                return (
                  <Stack
                    key={tag.id}
                    direction="row"
                    alignItems="center"
                    spacing={1}
                    sx={{ mb: 0.5 }}
                  >
                    <input
                      id={`persona-restrict-tag-${tag.id}`}
                      type="checkbox"
                      checked={restricted}
                      disabled={!editor.restrictionsLoaded}
                      onChange={(e) => {
                        const next = e.target.checked
                          ? [...editor.restrictedTagIds, tag.id]
                          : editor.restrictedTagIds.filter((id) => id !== tag.id);
                        setEditor({ ...editor, restrictedTagIds: next });
                      }}
                    />
                    <label htmlFor={`persona-restrict-tag-${tag.id}`}>
                      <Typography variant="body2">
                        {tag.tag_name}
                        <Typography
                          component="span"
                          variant="body2"
                          color="text.secondary"
                        >
                          {" "}
                          {t("personas.columnCountLabel", { count: String(tag.columns.length) })}
                        </Typography>
                      </Typography>
                    </label>
                  </Stack>
                );
              })}
            </Paper>
          )}

          <Paper variant="outlined" sx={{ mb: 2, p: 1.5 }}>
            <Typography
              variant="caption"
              sx={{ display: "block", mb: 0.5, fontWeight: 600 }}
            >
              {t("personas.hiddenColumnsTitle")}
            </Typography>
            <Stack direction="row" spacing={1} alignItems="center">
              <input
                id="persona-includes-hidden-columns"
                type="checkbox"
                checked={editor.includesHiddenColumns}
                onChange={(e) =>
                  setEditor({
                    ...editor,
                    includesHiddenColumns: e.target.checked,
                  })
                }
              />
              <label htmlFor="persona-includes-hidden-columns">
                <Typography variant="body2">
                  {t("personas.hiddenColumnsLabel")}
                </Typography>
              </label>
            </Stack>
          </Paper>

          <Stack direction="row" spacing={1} justifyContent="flex-end">
            <Button onClick={() => setEditor(EMPTY_EDITOR)}>{t("common.cancel")}</Button>
            <Button
              variant="contained"
              onClick={handleSave}
              disabled={saveMutation.isPending}
            >
              {editor.personaId ? t("personas.saveButton") : t("personas.createButton")}
            </Button>
          </Stack>
        </Paper>
      )}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// ObjectMultiSelect — lightweight multi-select for the include-list fields.
// ---------------------------------------------------------------------------

interface MultiSelectOption {
  id: string;
  label: string;
}

function ObjectMultiSelect({
  label,
  options,
  value,
  onChange,
  nameById,
}: {
  label: string;
  options: MultiSelectOption[];
  value: string[];
  onChange: (ids: string[]) => void;
  nameById: Map<string, string>;
}) {
  return (
    <Box sx={{ mb: 1.5 }}>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
        {label}
      </Typography>
      <Select
        multiple
        size="small"
        fullWidth
        value={value}
        onChange={(e) => {
          const v = e.target.value;
          onChange(typeof v === "string" ? v.split(",") : (v as string[]));
        }}
        renderValue={(selected) => (
          <Stack direction="row" spacing={0.5} flexWrap="wrap">
            {(selected as string[]).map((id) => (
              <Chip key={id} label={nameById.get(id) ?? id} size="small" sx={{ bgcolor: ui.greenBg, color: ui.green }} />
            ))}
          </Stack>
        )}
      >
        {options.map((o) => (
          <MenuItem key={o.id} value={o.id}>
            <ListItemText primary={o.label} />
          </MenuItem>
        ))}
      </Select>
    </Box>
  );
}

// F-008-12: return only server-supplied text; the hardcoded English
// fallbacks moved out so callers route the empty case through
// t("errors.requestFailed").
function extractError(e: any): string {
  if (!e) return "";
  const detail = e?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail?.message) return detail.message;
  if (detail?.error_code) return `${detail.error_code}: ${detail.message ?? ""}`;
  return e?.message ?? "";
}
