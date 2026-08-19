import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  List,
  ListItem,
  ListItemText,
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
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import HistoryIcon from "@mui/icons-material/History";
import RefreshIcon from "@mui/icons-material/Refresh";
import RestoreIcon from "@mui/icons-material/Restore";
import PreviewIcon from "@mui/icons-material/Visibility";
import VerifiedIcon from "@mui/icons-material/Verified";
import BlockIcon from "@mui/icons-material/Block";
import StarIcon from "@mui/icons-material/Star";
import StarBorderIcon from "@mui/icons-material/StarBorder";
import AccessTimeIcon from "@mui/icons-material/AccessTime";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";

import { namedSetsApi, namedQueriesApi, dimensionsApi, preferencesApi, queryRouterApiClient } from "../../api/client";
import { rowSecurityDeniedAll } from "../../utils/rowSecurity";
import {
  namedQueryHealth,
  namedQueryHealthColor,
  type NamedQueryHealthStatus,
} from "../../utils/namedQueryHealth";
import type {
  BuilderDefinition,
  Dimension,
  NamedQuery,
  NamedSet,
  NamedSetCreate,
  NamedSetPreviewResponse,
  NamedSetValidateResponse,
  VersionEntry,
} from "../../api/types";
import { useNamedQueries, useUserPreferences } from "../../api/hooks";
import { isTenantAdmin } from "../../auth/currentUser";
import { useBuilderStore } from "../../store/builderStore";
import { useModelNeedsSaveOrDeploy } from "../../store/useModelEditorStore";
import { useConfirm } from "../Confirm";
import { recordCreate, recordUpdate, recordDelete } from "../Builder/emitDrawerHistory";
import EntityImpactSummary from "./EntityImpactSummary";
import TemplateGalleryDialog from "./TemplateGalleryDialog";
import NamedQueryEditor, { namedQueryToPayload } from "./NamedQueryEditor";
import type { NamedSetTemplate } from "./templates";

type DialogMode = "create" | "edit";
type DialogTab = "basics" | "rule" | "scope" | "preview" | "history";
type ListType = "fixed" | "dynamic_top_n" | "filtered" | "advanced_mdx" | "sql_fixed";
/** Top-level kind selector: MDX Named Sets, Tessallite Named Lists, and
 *  Named Queries (record sets) — strategy §12. */
type ListKind = "mdx" | "tessallite" | "named_query";
/** Definition type within a Tessallite Named List. */
type TessDefinitionType = "fixedMembers" | "topN" | "filter" | "sql_query";

/** Bug-7963: shared query key constant so list query and refresh
 *  invalidation use the same key. Exported so the canvas-history registry
 *  (F-026-05) can assert its invalidation key stays aligned with this prefix. */
export const NAMED_SETS_QUERY_KEY_PREFIX = "namedSets";

/** Bug-7960: reference name pattern — must match backend _REFERENCE_NAME_RE. */
const REFERENCE_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

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
  // Fixed members builder (MDX)
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
  // Tessallite Named List fields
  tess_definition_type: TessDefinitionType;
  tess_dimension: string;
  tess_column_id: string;
  tess_data_type: "string" | "number";
  tess_members: (string | number)[];
  tess_sql_query: string;
  tess_last_refreshed_at: string | null;
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
  tess_definition_type: "fixedMembers",
  tess_dimension: "",
  tess_column_id: "",
  tess_data_type: "string",
  tess_members: [],
  tess_sql_query: "",
  tess_last_refreshed_at: null,
};

const SCOPE_OPTIONS = [1, 2] as const;

const MDX_LIST_TYPE_OPTIONS: ListType[] = [
  "fixed",
  "dynamic_top_n",
  "filtered",
  "advanced_mdx",
];

const LIST_TYPE_LABELS: Record<string, string> = {
  fixed: "namedSets.labelFixed",
  dynamic_top_n: "namedSets.labelDynamic",
  filtered: "namedSets.labelFiltered",
  advanced_mdx: "namedSets.labelMdx",
  sql_fixed: "namedSets.labelSqlFixed",
};

/** Returns the kind for a given list_type. */
function listTypeToKind(listType: string | null): ListKind {
  return listType === "sql_fixed" ? "tessallite" : "mdx";
}

/** Returns the effective path badge label i18n key. */
function pathBadgeKey(listType: string | null): string {
  return listType === "sql_fixed"
    ? "namedSets.pathBadgeSql"
    : "namedSets.pathBadgeXmla";
}

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

/** Map a persisted named set to a create-shaped payload for undo/redo restore
 *  (Bug-8227). */
function namedSetToPayload(ns: NamedSet): Record<string, unknown> {
  // Use ?? null (not ?? undefined) so undo actively resets a newly-set field
  // back to null via the PATCH (Fable review finding 3).
  return {
    name: ns.name,
    display_name: ns.display_name ?? null,
    description: ns.description ?? null,
    display_folder: ns.display_folder ?? null,
    scope: ns.scope,
    dimensions: ns.dimensions ?? null,
    list_type: ns.list_type ?? null,
    // Preserve certification_status so a certified/deprecated named set is
    // restored to its prior governance state on delete-undo (review finding 5).
    certification_status: ns.certification_status ?? null,
    ...(ns.builder_definition
      ? { builder_definition: ns.builder_definition }
      : { expression: ns.expression }),
  };
}

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
    case "sql_fixed": {
      const defType = form.tess_definition_type;
      if (defType === "fixedMembers") {
        if (!form.tess_dimension || form.tess_members.length === 0) return null;
        return {
          type: "fixedMembers",
          dimension: form.tess_dimension,
          column_id: form.tess_column_id || undefined,
          data_type: form.tess_data_type,
          members: form.tess_members,
        };
      }
      if (defType === "topN") {
        if (!form.topn_entity || !form.topn_count || !form.topn_measure) return null;
        return {
          type: "topN",
          entity: form.topn_entity,
          count: Number(form.topn_count),
          measure: form.topn_measure,
          direction: form.topn_direction,
          data_type: form.tess_data_type,
          members: form.tess_members,
          // Bug-7948: store dimension + column_id per spec 6.2.
          dimension: form.topn_entity,
          column_id: form.tess_column_id || undefined,
        };
      }
      if (defType === "filter") {
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
          data_type: form.tess_data_type,
          members: form.tess_members,
          // Bug-7948: store dimension + column_id per spec 6.2.
          dimension: form.filter_entity,
          column_id: form.tess_column_id || undefined,
        };
      }
      if (defType === "sql_query") {
        if (!form.tess_sql_query.trim()) return null;
        return {
          type: "sql_query",
          query: form.tess_sql_query,
          data_type: form.tess_data_type,
          members: form.tess_members,
          // Bug-7948: dimension/column_id optional for sql_query (not determinable).
          dimension: undefined,
          column_id: undefined,
        };
      }
      return null;
    }
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
  const canEdit = !storeReadOnly;  // Bug-8784: backend caller_can_author is authoritative
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
  // Bug-8453 / R4 finding 3: the empty picker is a permissions outcome.
  const [membersRowSecurityDenied, setMembersRowSecurityDenied] =
    useState(false);
  const [membersLoading, setMembersLoading] = useState(false);
  /** Drawer-level filter: which kind of named sets to show in the list. */
  const [drawerKind, setDrawerKind] = useState<ListKind>("mdx");
  /** Dialog-level kind: scoped to the create/edit dialog lifecycle. */
  const [dialogKind, setDialogKind] = useState<ListKind>("mdx");
  const [tessMemberInput, setTessMemberInput] = useState("");
  const [tessPasteInput, setTessPasteInput] = useState("");
  const [tessMemberError, setTessMemberError] = useState<string | null>(null);
  const [tessPasteError, setTessPasteError] = useState<string | null>(null);
  const [copyFeedback, setCopyFeedback] = useState(false);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const needsSaveOrDeploy = useModelNeedsSaveOrDeploy();
  const [refreshLoading, setRefreshLoading] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  // Bug-7942: track the initial edit form state to detect dirty (unsaved) changes.
  const editFormSnapshotRef = useRef<string>("");

  // Clean up copy-feedback timer on unmount
  useEffect(() => () => {
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
  }, []);

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

  const queryKey = [NAMED_SETS_QUERY_KEY_PREFIX, projectId, modelId];

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

  // --- Named Queries (strategy §12) — list + editor dialog state ---
  const { data: namedQueries = [], isLoading: nqLoading } = useNamedQueries(projectId, modelId);
  const NQ_QUERY_KEY = ["namedQueries", projectId, modelId];
  const [nqEditorOpen, setNqEditorOpen] = useState(false);
  const [nqEditorMode, setNqEditorMode] = useState<"create" | "edit">("create");
  const [nqEditorInitial, setNqEditorInitial] = useState<NamedQuery | null>(null);

  const deleteNqMut = useMutation({
    mutationFn: async (nq: NamedQuery) => {
      await namedQueriesApi.delete(projectId, modelId, nq.id);
      return nq;
    },
    onSuccess: (nq) => {
      // Bug-8227 mirror: undo re-creates the deleted Named Query.
      recordDelete("namedQuery", nq.id, namedQueryToPayload(nq));
      qc.invalidateQueries({ queryKey: NQ_QUERY_KEY });
    },
  });

  async function handleDeleteNq(nq: NamedQuery) {
    const ok = await confirm({
      title: t("namedQueries.deleteConfirm"),
      message: t("namedQueries.deleteMessage", { name: nq.display_name || nq.name }),
      confirmLabel: t("namedQueries.delete"),
    });
    if (ok) deleteNqMut.mutate(nq);
  }

  function openCreateNq() {
    setNqEditorMode("create");
    setNqEditorInitial(null);
    setNqEditorOpen(true);
  }

  function openEditNq(nq: NamedQuery) {
    setNqEditorMode("edit");
    setNqEditorInitial(nq);
    setNqEditorOpen(true);
  }

  function closeNqEditor() {
    setNqEditorOpen(false);
  }

  function nqHealthLabel(status: NamedQueryHealthStatus): string {
    return t(
      status === "fresh"
        ? "namedQueries.healthFresh"
        : status === "failed"
          ? "namedQueries.healthFailed"
          : "namedQueries.healthStale",
    );
  }

  const createMut = useMutation({
    mutationFn: async (data: NamedSetCreate) => {
      const created = await namedSetsApi.create(projectId, modelId, data);
      return { created, data };
    },
    onSuccess: ({ created, data }) => {
      // Bug-8227: record the create so undo removes it / redo re-creates it.
      recordCreate("namedSet", created.id, data as unknown as Record<string, unknown>);
      qc.invalidateQueries({ queryKey });
      closeDialog();
    },
    onError: (err: any) =>
      setError(err?.response?.data?.detail ?? t("namedSets.errorCreate")),
  });

  const updateMut = useMutation({
    mutationFn: async ({ id, data }: { id: string; data: Record<string, unknown> }) => {
      const prior = sets.find((s) => s.id === id);
      const priorPayload = prior ? namedSetToPayload(prior) : null;
      await namedSetsApi.update(projectId, modelId, id, data);
      return { id, data, priorPayload };
    },
    onSuccess: ({ id, data, priorPayload }) => {
      if (priorPayload) {
        recordUpdate("namedSet", id, priorPayload, data);
      }
      qc.invalidateQueries({ queryKey });
      closeDialog();
    },
    onError: (err: any) =>
      setError(err?.response?.data?.detail ?? t("namedSets.errorUpdate")),
  });

  const deleteMut = useMutation({
    mutationFn: async (ns: NamedSet) => {
      await namedSetsApi.delete(projectId, modelId, ns.id);
      return ns;
    },
    onSuccess: (ns) => {
      recordDelete("namedSet", ns.id, namedSetToPayload(ns));
      qc.invalidateQueries({ queryKey });
    },
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
    // R5 finding F8: reset per fetch. Set only in .then(), the flag lingered
    // from a denied dimension onto the next one's failed fetch.
    setMembersRowSecurityDenied(false);
    setMembersLoading(true);
    queryRouterApiClient
      .discoverMembers(modelId, form.fixed_dimension)
      .then((res) => {
        if (!cancelled) {
          // Bug-8453 / R4 finding 3: a row-security deny-all returns zero
          // members. Rendering that as an empty picker tells the modeller the
          // dimension has no members, which is a statement about the model
          // rather than about their own access.
          setMembersRowSecurityDenied(rowSecurityDeniedAll(res));
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

  function openCreate(kind: ListKind = "mdx") {
    const initial = kind === "tessallite"
      ? { ...EMPTY, list_type: "sql_fixed" as ListType }
      : EMPTY;
    setForm(initial);
    setDialogKind(kind);
    setDialogMode("create");
    setDialogTab("basics");
    setEditId(null);
    setError(null);
    setValidation(null);
    setPreviewData(null);
    setVersions([]);
    setDeprecateReplacementId("");
    setTessMemberInput("");
    setTessPasteInput("");
    setTessMemberError(null);
    setTessPasteError(null);
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
    // Templates are MDX-only; reset kind and Tessallite state
    setDialogKind("mdx");
    setDialogMode("create");
    setDialogTab("basics");
    setEditId(null);
    setError(null);
    setValidation(null);
    setPreviewData(null);
    setVersions([]);
    setDeprecateReplacementId("");
    setTessMemberInput("");
    setTessPasteInput("");
    setTessMemberError(null);
    setTessPasteError(null);
    setDialogOpen(true);
  }

  function openEdit(ns: NamedSet) {
    const bd = ns.builder_definition;
    const isSqlFixed = ns.list_type === "sql_fixed";
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
      fixed_dimension: isSqlFixed ? "" : (bd?.dimension ?? ""),
      fixed_hierarchy: isSqlFixed ? "" : (bd?.hierarchy ?? ""),
      fixed_members: isSqlFixed ? [] : ((bd?.members as string[]) ?? []),
      topn_entity: bd?.entity ?? "",
      topn_count: bd?.count != null ? String(bd.count) : "10",
      topn_measure: bd?.measure ?? "",
      topn_direction: bd?.direction ?? "top",
      filter_entity: bd?.entity ?? "",
      filter_conditions:
        (bd?.conditions as { field: string; operator: string; value: string }[]) ??
        [{ field: "", operator: ">", value: "" }],
      filter_logic: (bd?.logic as "AND" | "OR") ?? "AND",
      tess_definition_type: isSqlFixed ? ((bd?.type as TessDefinitionType) ?? "fixedMembers") : "fixedMembers",
      tess_dimension: isSqlFixed && bd?.type === "fixedMembers" ? (bd?.dimension ?? "") : "",
      tess_column_id: isSqlFixed ? (bd?.column_id ?? "") : "",
      tess_data_type: isSqlFixed ? (bd?.data_type ?? "string") : "string",
      tess_members: isSqlFixed ? ((bd?.members as (string | number)[]) ?? []) : [],
      tess_sql_query: isSqlFixed && bd?.type === "sql_query" ? (bd?.query ?? "") : "",
      tess_last_refreshed_at: isSqlFixed
        ? (ns.trust_meta?.last_refreshed_at ?? bd?.last_refreshed_at ?? null)
        : null,
    });
    setDialogKind(listTypeToKind(ns.list_type));
    setDialogMode("edit");
    setDialogTab("basics");
    setEditId(ns.id);
    setError(null);
    setValidation(null);
    setPreviewData(null);
    setVersions([]);
    setDeprecateReplacementId("");
    setTessMemberInput("");
    setTessPasteInput("");
    setTessMemberError(null);
    setTessPasteError(null);
    setDialogOpen(true);
    // Bug-7942: snapshot the initial form state for dirty detection.
    editFormSnapshotRef.current = JSON.stringify({
      name: ns.name,
      display_name: ns.display_name ?? "",
      description: ns.description ?? "",
      display_folder: ns.display_folder ?? "",
      scope: ns.scope,
      expression: ns.expression,
      dimensions: ns.dimensions ?? "",
      list_type: (ns.list_type as ListType) ?? "advanced_mdx",
      certification_status: ns.certification_status ?? "draft",
      fixed_dimension: isSqlFixed ? "" : (bd?.dimension ?? ""),
      fixed_hierarchy: isSqlFixed ? "" : (bd?.hierarchy ?? ""),
      fixed_members: isSqlFixed ? [] : ((bd?.members as string[]) ?? []),
      topn_entity: bd?.entity ?? "",
      topn_count: bd?.count != null ? String(bd.count) : "10",
      topn_measure: bd?.measure ?? "",
      topn_direction: bd?.direction ?? "top",
      filter_entity: bd?.entity ?? "",
      filter_conditions:
        (bd?.conditions as { field: string; operator: string; value: string }[]) ??
        [{ field: "", operator: ">", value: "" }],
      filter_logic: (bd?.logic as "AND" | "OR") ?? "AND",
      tess_definition_type: isSqlFixed ? ((bd?.type as TessDefinitionType) ?? "fixedMembers") : "fixedMembers",
      tess_dimension: isSqlFixed && bd?.type === "fixedMembers" ? (bd?.dimension ?? "") : "",
      tess_column_id: isSqlFixed ? (bd?.column_id ?? "") : "",
      tess_data_type: isSqlFixed ? (bd?.data_type ?? "string") : "string",
      tess_members: isSqlFixed ? ((bd?.members as (string | number)[]) ?? []) : [],
      tess_sql_query: isSqlFixed && bd?.type === "sql_query" ? (bd?.query ?? "") : "",
      tess_last_refreshed_at: isSqlFixed
        ? (ns.trust_meta?.last_refreshed_at ?? bd?.last_refreshed_at ?? null)
        : null,
    });
    recordRecentlyUsed(ns.id);
  }

  function closeDialog() {
    setDialogOpen(false);
    setError(null);
    setValidation(null);
    setPreviewData(null);
    setTessMemberInput("");
    setTessPasteInput("");
    setTessMemberError(null);
    setTessPasteError(null);
    setCopyFeedback(false);
    setRefreshLoading(false);
    setRefreshError(null);
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
    // Tessallite lists compute preview client-side (IN fragment); skip server call
    if (dialogTab === "preview" && dialogKind !== "tessallite") {
      handlePreview();
    }
  }, [dialogTab, handlePreview, dialogKind]);

  async function handleDelete(ns: NamedSet) {
    const ok = await confirm({
      title: t("namedSets.deleteConfirm"),
      message: t("namedSets.deleteMessage", { name: ns.display_name || ns.name }),
      confirmLabel: t("namedSets.delete"),
    });
    if (ok) deleteMut.mutate(ns);
  }

  const isPending = createMut.isPending || updateMut.isPending;
  const nameValid = form.name.trim().length > 0;
  const isTessallite = dialogKind === "tessallite";
  const isDynamicTess = isTessallite && form.tess_definition_type !== "fixedMembers";
  // Bug-7942: detect dirty form for Refresh button disable.
  const formDirty = useMemo(() => {
    if (dialogMode !== "edit") return false;
    return JSON.stringify(form) !== editFormSnapshotRef.current;
  }, [form, dialogMode]);
  const hasRule =
    form.list_type === "advanced_mdx"
      ? form.expression.trim().length > 0
      : buildBuilderDefinition(form) !== null;

  async function handleRefresh() {
    if (!editId) return;
    setRefreshLoading(true);
    setRefreshError(null);
    try {
      const updated = await namedSetsApi.refresh(projectId, modelId, editId);
      const bd = updated.builder_definition;
      setForm((prev) => ({
        ...prev,
        tess_members: (bd?.members as (string | number)[]) ?? [],
        tess_last_refreshed_at:
          updated.trust_meta?.last_refreshed_at ?? bd?.last_refreshed_at ?? null,
      }));
      qc.invalidateQueries({ queryKey });
    } catch (err: unknown) {
      const detail =
        err && typeof err === "object" && "response" in err
          ? ((err as { response?: { data?: { detail?: string } } }).response?.data?.detail ?? String(err))
          : String(err);
      setRefreshError(detail);
    } finally {
      setRefreshLoading(false);
    }
  }

  // --- Tessallite Named List member editor logic ---
  /** Validate and add a single member value. Returns error message or null. */
  function validateAndAddTessMember(raw: string): string | null {
    const trimmed = raw.trim();
    if (!trimmed) return t("namedSets.memberEditorEmpty");
    if (form.tess_members.length >= namedSetsApi._cachedMemberCap) {
      return t("namedSets.memberEditorCapReached", { cap: String(namedSetsApi._cachedMemberCap) });
    }
    if (form.tess_data_type === "number") {
      const num = Number(trimmed);
      if (isNaN(num) || !isFinite(num)) return t("namedSets.memberEditorInvalidNumber", { value: trimmed });
      if (form.tess_members.includes(num)) {
        return t("namedSets.memberEditorDuplicate", { value: trimmed });
      }
      setForm((prev) => ({ ...prev, tess_members: [...prev.tess_members, num] }));
      return null;
    }
    // string type
    if (form.tess_members.includes(trimmed)) {
      return t("namedSets.memberEditorDuplicate", { value: trimmed });
    }
    setForm((prev) => ({ ...prev, tess_members: [...prev.tess_members, trimmed] }));
    return null;
  }

  function handleTessMemberKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      const err = validateAndAddTessMember(tessMemberInput);
      setTessMemberError(err);
      if (!err) setTessMemberInput("");
    }
  }

  function handleTessMemberAdd() {
    const err = validateAndAddTessMember(tessMemberInput);
    setTessMemberError(err);
    if (!err) setTessMemberInput("");
  }

  function handleTessPasteImport() {
    const raw = tessPasteInput.trim();
    if (!raw) return;
    // Split by commas or newlines
    const tokens = raw.split(/[,\n\r]+/).map((s) => s.trim()).filter(Boolean);
    const errors: string[] = [];
    const rejected: string[] = [];
    const newMembers = [...form.tess_members];
    for (const token of tokens) {
      if (newMembers.length >= namedSetsApi._cachedMemberCap) {
        errors.push(t("namedSets.memberEditorCapReached", { cap: String(namedSetsApi._cachedMemberCap) }));
        rejected.push(token);
        continue;
      }
      if (form.tess_data_type === "number") {
        const num = Number(token);
        if (isNaN(num) || !isFinite(num)) {
          errors.push(t("namedSets.memberEditorInvalidNumber", { value: token }));
          rejected.push(token);
          continue;
        }
        if (!newMembers.includes(num)) newMembers.push(num);
      } else {
        if (!newMembers.includes(token)) newMembers.push(token);
      }
    }
    setForm((prev) => ({ ...prev, tess_members: newMembers }));
    // Keep rejected tokens so the user can correct them; clear on full success
    setTessPasteInput(rejected.length > 0 ? rejected.join(", ") : "");
    setTessPasteError(errors.length > 0 ? errors.join("; ") : null);
  }

  function removeTessMember(idx: number) {
    setForm((prev) => ({
      ...prev,
      tess_members: prev.tess_members.filter((_, i) => i !== idx),
    }));
  }

  /** Build the IN(...) preview fragment for Tessallite lists. */
  const tessInFragment = useMemo(() => {
    if (form.list_type !== "sql_fixed" || form.tess_members.length === 0) return "";
    if (form.tess_data_type === "number") {
      return `IN (${form.tess_members.join(", ")})`;
    }
    return `IN (${form.tess_members.map((m) => `'${String(m).replace(/'/g, "''")}'`).join(", ")})`;
  }, [form.list_type, form.tess_members, form.tess_data_type]);

  const tessUsageSnippet = useMemo(() => {
    if (form.list_type !== "sql_fixed") return "";
    let dimName = "<dimension_name>";
    if (form.tess_definition_type === "fixedMembers") {
      dimName = form.tess_dimension || "<dimension_name>";
    } else if (form.tess_definition_type === "topN") {
      dimName = form.topn_entity || "<dimension_name>";
    } else if (form.tess_definition_type === "filter") {
      dimName = form.filter_entity || "<dimension_name>";
    }
    const listName = form.name || "<ListName>";
    return `WHERE ${dimName} IN (@${listName})`;
  }, [form.list_type, form.tess_definition_type, form.tess_dimension, form.topn_entity, form.filter_entity, form.name]);

  async function handleCopyToClipboard(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopyFeedback(true);
      if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
      copyTimerRef.current = setTimeout(() => setCopyFeedback(false), 1500);
    } catch {
      // Fallback: silently fail
    }
  }

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
            {drawerKind === "mdx" && (
              <Button size="small" startIcon={<AutoFixHighIcon />} variant="outlined" onClick={() => setTemplateGalleryOpen(true)}>
                {t("namedSets.fromTemplate")}
              </Button>
            )}
            <Button
              size="small"
              startIcon={<AddIcon />}
              variant="contained"
              onClick={() => (drawerKind === "named_query" ? openCreateNq() : openCreate(drawerKind))}
            >
              {t("namedSets.add")}
            </Button>
          </Box>
        )}
      </Box>

      <Typography variant="body2" color="text.secondary" mb={2}>
        {t("namedSets.description")}
      </Typography>

      {!isLoading && (
        <Box sx={{ mb: 2 }}>
          <ToggleButtonGroup
            value={drawerKind}
            exclusive
            onChange={(_, v) => { if (v) setDrawerKind(v); }}
            size="small"
            aria-label={t("namedSets.kindSelectorLabel")}
            data-testid="kind-selector"
          >
            <ToggleButton value="mdx">{t("namedSets.kindMdx")}</ToggleButton>
            <ToggleButton value="tessallite">{t("namedSets.kindTessallite")}</ToggleButton>
            <ToggleButton value="named_query" data-testid="kind-named-queries">
              {t("namedSets.kindNamedQueries")}
            </ToggleButton>
          </ToggleButtonGroup>
        </Box>
      )}

      {isLoading && <CircularProgress size={20} />}

      {!isLoading && drawerKind !== "named_query" && sets.length === 0 && (
        <Typography variant="body2" color="text.secondary">
          {t("namedSets.none")}
        </Typography>
      )}

      {drawerKind === "named_query" ? (
        <Box data-testid="nq-list">
          {nqLoading && <CircularProgress size={20} />}
          {!nqLoading && namedQueries.length === 0 && (
            <Typography variant="body2" color="text.secondary" data-testid="nq-none">
              {t("namedQueries.none")}
            </Typography>
          )}
          <Stack spacing={1.5}>
            {namedQueries.map((nq: NamedQuery) => {
              const health = namedQueryHealth(nq);
              return (
                <Card key={nq.id} variant="outlined">
                  <CardContent sx={{ py: 1.5, "&:last-child": { pb: 1.5 } }}>
                    <Box display="flex" alignItems="center" justifyContent="space-between">
                      <Box>
                        <Typography variant="subtitle2">
                          {nq.display_name || nq.name}
                        </Typography>
                        {nq.display_name && (
                          <Typography variant="caption" color="text.secondary" fontFamily="monospace">
                            @{nq.name}
                          </Typography>
                        )}
                      </Box>
                      <Box display="flex" alignItems="center" gap={0.5}>
                        <Chip
                          label={t("namedQueries.kindChip")}
                          size="small"
                          variant="outlined"
                          color="info"
                        />
                        <Chip
                          label={t("namedSets.pathBadgeSql")}
                          size="small"
                          variant="filled"
                          color="info"
                          data-testid={`nq-channel-${nq.id}`}
                        />
                        <Chip
                          label={nqHealthLabel(health.status)}
                          size="small"
                          color={namedQueryHealthColor(health.status)}
                          data-testid={`nq-health-${nq.id}`}
                        />
                        {nq.certification_status !== "draft" && (
                          <Chip
                            icon={nq.certification_status === "certified" ? <VerifiedIcon /> : undefined}
                            label={nq.certification_status}
                            size="small"
                            color={CERT_COLORS[nq.certification_status] ?? "default"}
                          />
                        )}
                        {needsSaveOrDeploy && (
                          <Tooltip title={t("namedQueries.pendingDeploy")}>
                            <span
                              tabIndex={0}
                              role="img"
                              aria-label={t("namedQueries.pendingDeploy")}
                              style={{ display: "inline-flex" }}
                              data-testid={`nq-pending-deploy-${nq.id}`}
                            >
                              <WarningAmberIcon fontSize="small" color="warning" />
                            </span>
                          </Tooltip>
                        )}
                        {canEdit && (
                          <Button size="small" startIcon={<EditIcon />} onClick={() => openEditNq(nq)}>
                            {t("namedSets.edit")}
                          </Button>
                        )}
                        {isAdmin && canEdit && (
                          <Button size="small" startIcon={<DeleteIcon />} onClick={() => handleDeleteNq(nq)}>
                            {t("namedSets.delete")}
                          </Button>
                        )}
                      </Box>
                    </Box>
                    {nq.description && (
                      <Typography variant="body2" color="text.secondary" mt={0.5}>
                        {nq.description}
                      </Typography>
                    )}
                    {health.status !== "fresh" && (
                      <Alert
                        severity={health.status === "failed" ? "error" : "warning"}
                        sx={{ mt: 1, py: 0 }}
                        icon={<BlockIcon fontSize="small" />}
                      >
                        {health.detail ?? (health.reasonKey ? t(health.reasonKey) : "")}
                      </Alert>
                    )}
                    <Typography variant="caption" color="text.secondary" mt={0.5} display="block">
                      {nq.artifact?.last_refresh_at
                        ? t("namedQueries.lastRefreshed", {
                            time: new Date(nq.artifact.last_refresh_at).toLocaleString(),
                          })
                        : t("namedQueries.neverRefreshed")}
                      {nq.artifact?.row_count != null
                        ? ` · ${t("namedQueries.rowCount", { count: String(nq.artifact.row_count) })}`
                        : ""}
                    </Typography>
                    {nq.display_folder && (
                      <Typography variant="caption" color="text.secondary" mt={0.5} display="block">
                        {t("namedSets.folder", { folder: nq.display_folder })}
                      </Typography>
                    )}
                  </CardContent>
                </Card>
              );
            })}
          </Stack>
        </Box>
      ) : (
      (() => {
        const kindFiltered = sets.filter((s: NamedSet) => listTypeToKind(s.list_type) === drawerKind);
        if (kindFiltered.length === 0 && sets.length > 0) {
          return (
            <Typography variant="body2" color="text.secondary" data-testid="kind-empty">
              {t("namedSets.noneForKind")}
            </Typography>
          );
        }
        const favSets = kindFiltered.filter((s: NamedSet) => favouriteIds.has(s.id));
        const recentSets = kindFiltered.filter((s: NamedSet) => !favouriteIds.has(s.id) && recentIds.includes(s.id));
        const otherSets = kindFiltered.filter((s: NamedSet) => !favouriteIds.has(s.id) && !recentIds.includes(s.id));
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
                    label={t(pathBadgeKey(ns.list_type))}
                    size="small"
                    variant="filled"
                    color={ns.list_type === "sql_fixed" ? "info" : "default"}
                    data-testid={`path-badge-${ns.id}`}
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
                  {ns.list_type === "sql_fixed" && needsSaveOrDeploy && (
                    <Tooltip title={t("namedSets.pendingDeploy")}>
                      <span tabIndex={0} role="img" aria-label={t("namedSets.pendingDeploy")} style={{ display: "inline-flex" }}>
                        <WarningAmberIcon
                          fontSize="small"
                          color="warning"
                          data-testid={`pending-deploy-${ns.id}`}
                        />
                      </span>
                    </Tooltip>
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
      })()
      )}

      {/* ---- Tabbed Dialog ---- */}
      <Dialog open={dialogOpen} onClose={closeDialog} maxWidth="md" fullWidth>
        <DialogTitle sx={{ pb: 0.5 }}>
          <Box display="flex" alignItems="center" gap={1}>
            <span>
              {dialogMode === "create"
                ? (dialogKind === "tessallite" ? t("namedSets.addNamedList") : t("namedSets.addNamedSet"))
                : (dialogKind === "tessallite" ? t("namedSets.editNamedList") : t("namedSets.editNamedSet"))
              }
            </span>
            {dialogMode === "edit" && (
              <Chip
                label={dialogKind === "tessallite" ? t("namedSets.editingKindTessallite") : t("namedSets.editingKindMdx")}
                size="small"
                variant="outlined"
                color={dialogKind === "tessallite" ? "info" : "default"}
              />
            )}
          </Box>
        </DialogTitle>
        {/* Kind selector — only in create mode (edit infers from the existing list_type) */}
        {dialogMode === "create" && (
          <Box sx={{ px: 3, pb: 1 }}>
            <ToggleButtonGroup
              value={dialogKind}
              exclusive
              onChange={(_, v) => {
                if (!v) return;
                setDialogKind(v);
                setTessMemberError(null);
                setTessPasteError(null);
                setTessMemberInput("");
                setValidation(null);
                setPreviewData(null);
                if (v === "tessallite") {
                  setForm((prev) => ({ ...prev, list_type: "sql_fixed" as ListType }));
                } else {
                  setForm((prev) => ({ ...prev, list_type: "fixed" as ListType }));
                }
              }}
              size="small"
              aria-label={t("namedSets.kindSelectorLabel")}
              data-testid="dialog-kind-selector"
            >
              <ToggleButton value="mdx">{t("namedSets.kindMdx")}</ToggleButton>
              <ToggleButton value="tessallite">{t("namedSets.kindTessallite")}</ToggleButton>
            </ToggleButtonGroup>
          </Box>
        )}
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
              placeholder={isTessallite ? "e.g. top_products" : t("namedSets.namePlaceholder")}
              error={form.name.length > 0 && (!nameValid || (isTessallite && !REFERENCE_NAME_RE.test(form.name)))}
              helperText={
                isTessallite && form.name.length > 0 && !REFERENCE_NAME_RE.test(form.name)
                  ? t("namedSets.refNameInvalid")
                  : isTessallite && form.name.length > 0
                    ? t("namedSets.refNameUsage", { name: form.name })
                    : undefined
              }
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
            {/* MDX kind: show the list type selector and MDX builders */}
            {!isTessallite && (
              <>
                <TextField
                  select
                  label={t("namedSets.listType")}
                  fullWidth
                  margin="normal"
                  value={form.list_type}
                  onChange={(e) => setForm({ ...form, list_type: e.target.value as ListType })}
                >
                  {MDX_LIST_TYPE_OPTIONS.map((lt) => (
                    <MenuItem key={lt} value={lt}>
                      {t(`namedSets.listType.${lt}`)}
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
                          error={membersRowSecurityDenied}
                          helperText={
                            membersRowSecurityDenied
                              ? t("query.rowSecurityDeniedBody")
                              : memberOptions.length
                                ? t("namedSets.membersHelp")
                                : t("namedSets.membersHelpSelectDim")
                          }
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
              </>
            )}

            {/* Tessallite Named List: definition type + member editor */}
            {isTessallite && (
              <Box mt={1} data-testid="tess-member-editor">
                {/* Definition type selector */}
                <TextField
                  select
                  label={t("namedSets.definitionType")}
                  fullWidth
                  margin="normal"
                  value={form.tess_definition_type}
                  onChange={(e) => {
                    const newType = e.target.value as TessDefinitionType;
                    // Bug-7950: clear ALL type-specific fields, not just members.
                    setForm((prev) => ({
                      ...prev,
                      tess_definition_type: newType,
                      tess_members: [],
                      tess_last_refreshed_at: null,
                      tess_column_id: "",
                      topn_entity: "",
                      topn_count: "10",
                      topn_measure: "",
                      topn_direction: "top",
                      filter_entity: "",
                      filter_conditions: [{ field: "", operator: ">", value: "" }],
                      filter_logic: "AND",
                      tess_sql_query: "",
                    }));
                    setTessMemberError(null);
                    setRefreshError(null);
                  }}
                  data-testid="tess-definition-type"
                >
                  <MenuItem value="fixedMembers">{t("namedSets.defTypeFixed")}</MenuItem>
                  <MenuItem value="topN">{t("namedSets.defTypeTopN")}</MenuItem>
                  <MenuItem value="filter">{t("namedSets.defTypeFilter")}</MenuItem>
                  <MenuItem value="sql_query">{t("namedSets.defTypeSqlQuery")}</MenuItem>
                </TextField>

                {/* Data type selector (all definition types) */}
                <TextField
                  select
                  label={t("namedSets.dataType")}
                  fullWidth
                  margin="normal"
                  value={form.tess_data_type}
                  onChange={(e) => {
                    const newType = e.target.value as "string" | "number";
                    setForm({ ...form, tess_data_type: newType, tess_members: [] });
                    setTessMemberError(null);
                    setTessMemberInput("");
                  }}
                >
                  <MenuItem value="string">{t("namedSets.dataTypeString")}</MenuItem>
                  <MenuItem value="number">{t("namedSets.dataTypeNumber")}</MenuItem>
                </TextField>

                {/* --- fixedMembers: dimension selector + manual entry --- */}
                {form.tess_definition_type === "fixedMembers" && (
                  <>
                    <TextField
                      select
                      label={t("namedSets.selectDimension")}
                      fullWidth
                      margin="normal"
                      value={form.tess_dimension}
                      onChange={(e) => {
                        const dim = dims.find((d: Dimension) => d.name === e.target.value);
                        setForm({
                          ...form,
                          tess_dimension: e.target.value,
                          tess_column_id: dim?.source_column_id ?? "",
                        });
                      }}
                    >
                      <MenuItem value="">{t("namedSets.selectPlaceholder")}</MenuItem>
                      {dims.map((d: Dimension) => (
                        <MenuItem key={d.id} value={d.name}>
                          {d.display_name || d.name}
                        </MenuItem>
                      ))}
                    </TextField>

                    <Typography
                      variant="body2"
                      color={form.tess_members.length >= namedSetsApi._cachedMemberCap ? "error" : "text.secondary"}
                      sx={{ mt: 1, mb: 1 }}
                      data-testid="tess-member-count"
                    >
                      {t("namedSets.memberEditorCount", {
                        count: String(form.tess_members.length),
                        cap: String(namedSetsApi._cachedMemberCap),
                      })}
                    </Typography>

                    <Box display="flex" gap={1} alignItems="flex-start">
                      <TextField
                        label={t("namedSets.memberEditorManualLabel")}
                        placeholder={t("namedSets.memberEditorManualPlaceholder")}
                        size="small"
                        value={tessMemberInput}
                        onChange={(e) => { setTessMemberInput(e.target.value); setTessMemberError(null); }}
                        onKeyDown={handleTessMemberKeyDown}
                        error={Boolean(tessMemberError)}
                        helperText={tessMemberError ?? undefined}
                        sx={{ flex: 1 }}
                        inputProps={{ "data-testid": "tess-member-input" }}
                      />
                      <Button
                        variant="outlined"
                        size="small"
                        onClick={handleTessMemberAdd}
                        disabled={!tessMemberInput.trim()}
                        sx={{ mt: 0.5 }}
                      >
                        {t("namedSets.memberEditorAddButton")}
                      </Button>
                    </Box>

                    <TextField
                      label={t("namedSets.memberEditorPasteLabel")}
                      placeholder={t("namedSets.memberEditorPastePlaceholder")}
                      fullWidth
                      margin="normal"
                      multiline
                      rows={3}
                      value={tessPasteInput}
                      onChange={(e) => { setTessPasteInput(e.target.value); setTessPasteError(null); }}
                      inputProps={{ "data-testid": "tess-paste-input" }}
                    />
                    <Button
                      variant="outlined"
                      size="small"
                      onClick={handleTessPasteImport}
                      disabled={!tessPasteInput.trim()}
                    >
                      {t("namedSets.memberEditorPasteButton")}
                    </Button>
                    {tessPasteError && (
                      <Alert severity="warning" sx={{ mt: 1 }} onClose={() => setTessPasteError(null)} data-testid="tess-paste-error">
                        {tessPasteError}
                      </Alert>
                    )}

                    {form.tess_members.length > 0 && (
                      <Box display="flex" justifyContent="flex-end" sx={{ mt: 1 }}>
                        <Button
                          size="small"
                          color="error"
                          variant="text"
                          onClick={() => {
                            setForm((prev) => ({ ...prev, tess_members: [] }));
                            setTessMemberError(null);
                          }}
                          data-testid="tess-clear-all"
                        >
                          {t("namedSets.memberEditorClearAll")}
                        </Button>
                      </Box>
                    )}
                    {form.tess_members.length > 0 && (
                      <List dense sx={{ maxHeight: 200, overflow: "auto" }} data-testid="tess-member-list">
                        {form.tess_members.map((member, idx) => (
                          <ListItem
                            key={idx}
                            secondaryAction={
                              <IconButton
                                edge="end"
                                size="small"
                                onClick={() => removeTessMember(idx)}
                                aria-label={t("namedSets.remove")}
                              >
                                <DeleteIcon fontSize="small" />
                              </IconButton>
                            }
                          >
                            <ListItemText
                              primary={String(member)}
                              primaryTypographyProps={{ variant: "body2", fontFamily: "monospace" }}
                            />
                          </ListItem>
                        ))}
                      </List>
                    )}
                  </>
                )}

                {/* --- topN builder --- */}
                {form.tess_definition_type === "topN" && (
                  <>
                    <TextField
                      select
                      label={t("namedSets.selectDimension")}
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
                      label={t("namedSets.measure")}
                      fullWidth
                      margin="normal"
                      value={form.topn_measure}
                      onChange={(e) => setForm({ ...form, topn_measure: e.target.value })}
                      placeholder={t("namedSets.measurePlaceholder")}
                    />
                    <Box display="flex" gap={2} mt={1}>
                      <TextField
                        label={t("namedSets.count")}
                        type="number"
                        value={form.topn_count}
                        onChange={(e) => setForm({ ...form, topn_count: e.target.value })}
                        inputProps={{ min: 1 }}
                        sx={{ width: 120 }}
                      />
                      <TextField
                        select
                        label={t("namedSets.direction")}
                        value={form.topn_direction}
                        onChange={(e) => setForm({ ...form, topn_direction: e.target.value as "top" | "bottom" })}
                        sx={{ width: 140 }}
                      >
                        <MenuItem value="top">{t("namedSets.directionTop")}</MenuItem>
                        <MenuItem value="bottom">{t("namedSets.directionBottom")}</MenuItem>
                      </TextField>
                    </Box>
                  </>
                )}

                {/* --- filter builder --- */}
                {form.tess_definition_type === "filter" && (
                  <>
                    <TextField
                      select
                      label={t("namedSets.selectDimension")}
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
                    <TextField
                      select
                      label={t("namedSets.filterLogic")}
                      value={form.filter_logic}
                      onChange={(e) => setForm({ ...form, filter_logic: e.target.value as "AND" | "OR" })}
                      margin="normal"
                      sx={{ width: 120 }}
                    >
                      <MenuItem value="AND">AND</MenuItem>
                      <MenuItem value="OR">OR</MenuItem>
                    </TextField>
                    {form.filter_conditions.map((cond, ci) => (
                      <Box key={ci} display="flex" gap={1} alignItems="center" mt={1}>
                        <TextField
                          label={t("namedSets.conditionField")}
                          size="small"
                          value={cond.field}
                          onChange={(e) => {
                            const updated = [...form.filter_conditions];
                            updated[ci] = { ...cond, field: e.target.value };
                            setForm({ ...form, filter_conditions: updated });
                          }}
                          sx={{ flex: 1 }}
                        />
                        <TextField
                          select
                          size="small"
                          value={cond.operator}
                          onChange={(e) => {
                            const updated = [...form.filter_conditions];
                            updated[ci] = { ...cond, operator: e.target.value };
                            setForm({ ...form, filter_conditions: updated });
                          }}
                          sx={{ width: 80 }}
                        >
                          {FILTER_OPERATORS.map((op) => (
                            <MenuItem key={op.value} value={op.value}>{op.label}</MenuItem>
                          ))}
                        </TextField>
                        <TextField
                          label={t("namedSets.conditionValue")}
                          size="small"
                          value={cond.value}
                          onChange={(e) => {
                            const updated = [...form.filter_conditions];
                            updated[ci] = { ...cond, value: e.target.value };
                            setForm({ ...form, filter_conditions: updated });
                          }}
                          sx={{ flex: 1 }}
                        />
                        <IconButton
                          size="small"
                          onClick={() => {
                            const updated = form.filter_conditions.filter((_, i) => i !== ci);
                            setForm({ ...form, filter_conditions: updated.length ? updated : [{ field: "", operator: ">", value: "" }] });
                          }}
                        >
                          <DeleteIcon fontSize="small" />
                        </IconButton>
                      </Box>
                    ))}
                    <Button
                      size="small"
                      variant="text"
                      onClick={() => setForm({ ...form, filter_conditions: [...form.filter_conditions, { field: "", operator: ">", value: "" }] })}
                      sx={{ mt: 1 }}
                    >
                      {t("namedSets.addCondition")}
                    </Button>
                  </>
                )}

                {/* --- sql_query builder --- */}
                {form.tess_definition_type === "sql_query" && (
                  <>
                    <TextField
                      label={t("namedSets.sqlQueryLabel")}
                      fullWidth
                      margin="normal"
                      multiline
                      rows={5}
                      value={form.tess_sql_query}
                      onChange={(e) => setForm({ ...form, tess_sql_query: e.target.value })}
                      placeholder={t("namedSets.sqlQueryPlaceholder")}
                      helperText={t("namedSets.sqlQueryHelp")}
                      inputProps={{ style: { fontFamily: "monospace" }, "data-testid": "tess-sql-query" }}
                    />
                  </>
                )}

                {/* --- Refresh button + status (dynamic types only, edit mode) --- */}
                {isDynamicTess && (
                  <Box mt={2}>
                    {dialogMode === "edit" && editId && (
                      <Box display="flex" alignItems="center" gap={2}>
                        <Tooltip
                          title={formDirty ? t("namedSets.refreshDirtyTooltip") : ""}
                          placement="top"
                        >
                          <span>
                            <Button
                              variant="contained"
                              size="small"
                              startIcon={refreshLoading ? <CircularProgress size={16} /> : <RefreshIcon />}
                              onClick={handleRefresh}
                              disabled={refreshLoading || formDirty}
                              data-testid="tess-refresh-btn"
                            >
                              {t("namedSets.refreshButton")}
                            </Button>
                          </span>
                        </Tooltip>
                        <Typography
                          variant="caption"
                          color="text.secondary"
                          data-testid="tess-last-refreshed"
                        >
                          {form.tess_last_refreshed_at
                            ? t("namedSets.lastRefreshed", { time: new Date(form.tess_last_refreshed_at).toLocaleString() })
                            : t("namedSets.neverRefreshed")}
                        </Typography>
                      </Box>
                    )}
                    {dialogMode === "create" && (
                      <Alert severity="info" sx={{ mt: 1 }}>
                        {t("namedSets.saveToRefresh")}
                      </Alert>
                    )}
                    {refreshError && (
                      <Alert severity="error" sx={{ mt: 1 }} onClose={() => setRefreshError(null)}>
                        {refreshError}
                      </Alert>
                    )}

                    {/* Empty-members warning for dynamic types */}
                    {form.tess_members.length === 0 && dialogMode === "edit" && !refreshLoading && (
                      <Alert severity="warning" sx={{ mt: 1 }} data-testid="tess-empty-dynamic">
                        {form.tess_last_refreshed_at
                          ? t("namedSets.zeroRowRefreshWarning")
                          : t("namedSets.emptyDynamicWarning")}
                      </Alert>
                    )}

                    {/* Computed members display (read-only) */}
                    {form.tess_members.length > 0 && (
                      <>
                        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                          {t("namedSets.computedMembersCount", { count: String(form.tess_members.length) })}
                        </Typography>
                        <List dense sx={{ maxHeight: 200, overflow: "auto", bgcolor: "action.hover", borderRadius: 1, mt: 0.5 }} data-testid="tess-computed-members">
                          {form.tess_members.map((member, idx) => (
                            <ListItem key={idx} dense>
                              <ListItemText
                                primary={String(member)}
                                primaryTypographyProps={{ variant: "body2", fontFamily: "monospace" }}
                              />
                            </ListItem>
                          ))}
                        </List>
                      </>
                    )}
                  </Box>
                )}
              </Box>
            )}
          </Box>

          {/* Tab 3: Scope & Governance */}
          <Box sx={{ display: dialogTab === "scope" ? "block" : "none" }}>
            {/* Scope selector: Tessallite lists are always global; hide for that kind */}
            {!isTessallite && (
              <TextField
                select
                label={t("namedSets.scope")}
                fullWidth
                margin="normal"
                value={form.scope}
                onChange={(e) => setForm({ ...form, scope: Number(e.target.value) })}
                helperText={t("namedSets.scopeHelp")}
              >
                {SCOPE_OPTIONS.map((sv) => (
                  <MenuItem key={sv} value={sv}>
                    {t(`namedSets.scope.${sv}`)}
                  </MenuItem>
                ))}
              </TextField>
            )}
            {/* Dimensions field is an MDX concept; hide for Tessallite lists */}
            {!isTessallite && (
              <TextField
                label={t("namedSets.dimensions")}
                fullWidth
                margin="normal"
                value={form.dimensions}
                onChange={(e) => setForm({ ...form, dimensions: e.target.value })}
                placeholder={t("namedSets.dimensionsPlaceholder")}
                helperText={t("namedSets.dimensionsHelp")}
              />
            )}
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
            {/* Tessallite Named List preview: IN fragment + usage snippet */}
            {isTessallite ? (
              <Box data-testid="tess-preview">
                <Typography variant="subtitle2" mb={1}>
                  {t("namedSets.previewMemberCount", { count: String(form.tess_members.length) })}
                </Typography>

                {tessInFragment && (
                  <Box sx={{ mb: 2 }}>
                    <Typography variant="caption" fontWeight={600} display="block" mb={0.5}>
                      {t("namedSets.previewInFragment")}
                    </Typography>
                    <Box
                      sx={{
                        p: 1.5,
                        bgcolor: "grey.50",
                        borderRadius: 1,
                        border: "1px solid",
                        borderColor: "divider",
                        fontFamily: "monospace",
                        fontSize: 13,
                        overflowX: "auto",
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-all",
                      }}
                      data-testid="tess-in-fragment"
                    >
                      {tessInFragment}
                    </Box>
                  </Box>
                )}

                {tessUsageSnippet && (
                  <Box sx={{ mb: 2 }}>
                    <Typography variant="caption" fontWeight={600} display="block" mb={0.5}>
                      {t("namedSets.previewUsageSnippet")}
                    </Typography>
                    <Box display="flex" alignItems="center" gap={1}>
                      <Box
                        sx={{
                          p: 1.5,
                          bgcolor: "grey.50",
                          borderRadius: 1,
                          border: "1px solid",
                          borderColor: "divider",
                          fontFamily: "monospace",
                          fontSize: 13,
                          flex: 1,
                        }}
                        data-testid="tess-usage-snippet"
                      >
                        {tessUsageSnippet}
                      </Box>
                      <Tooltip title={copyFeedback ? t("namedSets.previewCopied") : t("namedSets.previewCopy")}>
                        <IconButton
                          size="small"
                          onClick={() => handleCopyToClipboard(tessUsageSnippet)}
                        >
                          <ContentCopyIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    </Box>
                  </Box>
                )}

                {form.tess_members.length === 0 && (
                  <Typography variant="body2" color="text.secondary">
                    {t("namedSets.noPreviewData")}
                  </Typography>
                )}
              </Box>
            ) : (
              /* MDX preview: existing server-side preview */
              <>
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
              </>
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

      <NamedQueryEditor
        open={nqEditorOpen}
        mode={nqEditorMode}
        initial={nqEditorInitial}
        projectId={projectId}
        modelId={modelId}
        canEdit={canEdit}
        needsSaveOrDeploy={needsSaveOrDeploy}
        onClose={closeNqEditor}
      />

      <TemplateGalleryDialog
        open={templateGalleryOpen}
        onClose={() => setTemplateGalleryOpen(false)}
        entityType="named_set"
        onApplyNamedSet={applyNamedSetTemplate}
      />
    </Box>
  );
}
