import { Fragment, useEffect, useMemo, useState, type ReactNode } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
import { extractApiError } from "../../utils/extractApiError";
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
  FormControlLabel,
  FormHelperText,
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
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ChevronRightIcon from "@mui/icons-material/ChevronRight";
import ErrorOutlineIcon from "@mui/icons-material/ErrorOutline";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import { useCanAuthorModel } from "../../auth/useCanAuthorModel";
import { measuresApi } from "../../api/client";
import { recordCreate, recordUpdate, recordDelete } from "../Builder/emitDrawerHistory";
import DrillThroughSetEditor from "./DrillThroughSetEditor";
import { useAllModelTables, useDimensions, useFieldCompatibility, useHierarchies, useMeasures, useModelSourceStatistics, useModels, useSources, useTableAttributes } from "../../api/hooks";
import type {
  Measure,
  MeasureCreate,
  MeasureFormatToken,
  SemiAdditiveBehavior,
  ValidateMeasureExpressionResponse,
} from "../../api/types";
import { MEASURE_FORMAT_LABELS, MEASURE_FORMAT_TOKENS } from "../../api/measureFormat";
import { ui } from "../../theme/tokens";
import {
  CANONICAL_TIME_VARIANT_NAMES,
  RATIO_VARIANT_DEFAULT_FORMAT,
  TIME_VARIANT_DEFAULT_N,
  TIME_VARIANT_LABELS,
  TIME_VARIANTS_NEEDING_CALENDAR,
  type TimeVariantKind,
  isParametricVariant,
  isRatioVariant,
} from "../../constants/timeVariants";
import {
  type VariantCreatePlan,
} from "../../lib/variantDiff";
import { useConfirm } from "../Confirm";
import CalendarBindingHint from "../CalendarBindingHint";
import PersonaPicker from "../Persona/PersonaPicker";
import {
  summarizeMeasureCompatibility,
  type MeasureCompatibilitySummary,
} from "./measureCompatibility";
import MeasureRenameImpactDialog from "./MeasureRenameImpactDialog";

const AGG_OPTIONS = [
  "sum",
  "avg",
  "count",
  "count_distinct",
  "min",
  "max",
] as const;

const MEASURE_TYPES = [
  { value: "standard", label: "measureType.standard" },
  { value: "calculated", label: "measureType.calculated" },
] as const;

// #10: "by_account" is not a supported behaviour and is no longer offered.
const SEMI_ADDITIVE_OPTIONS: {
  value: SemiAdditiveBehavior;
  label: string;
}[] = [
  { value: "last_non_empty", label: "semiAdditive.lastNonEmpty" },
  { value: "first_non_empty", label: "semiAdditive.firstNonEmpty" },
  { value: "avg_of_children", label: "semiAdditive.avgOfChildren" },
  { value: "min", label: "semiAdditive.min" },
  { value: "max", label: "semiAdditive.max" },
];

const CALC_AGG_MODES = [
  {
    value: "expression_as_written" as const,
    label: "calcAggMode.expressionAsWritten",
    description: "calcAggMode.expressionAsWrittenDesc",
  },
  {
    value: "per_row_then_aggregate" as const,
    label: "calcAggMode.perRowThenAggregate",
    description: "calcAggMode.perRowThenAggregateDesc",
  },
] as const;

function MeasureCompatibilityTooltip({
  summary,
  t,
}: {
  summary: MeasureCompatibilitySummary;
  t: (key: string, vars?: Record<string, string>) => string;
}) {
  let body: ReactNode;
  if (summary.state === "loading") {
    body = (
      <Typography variant="caption">
        {t("measures.compatibility.loading")}
      </Typography>
    );
  } else if (summary.state === "unavailable") {
    body = (
      <Typography variant="caption">
        {t("measures.compatibility.unavailable")}
      </Typography>
    );
  } else if (summary.compatibleDimensionNames.length > 0) {
    body = (
      <Box component="ul" sx={{ m: 0, pl: 2 }}>
        {summary.compatibleDimensionNames.map((name) => (
          <Box component="li" key={name}>
            <Typography variant="caption">{name}</Typography>
          </Box>
        ))}
      </Box>
    );
  } else {
    body = (
      <Typography variant="caption">
        {summary.limitation === "accessPolicyAggregateOnly"
          ? t("measures.compatibility.accessPolicyAggregateOnly")
          : t("measures.compatibility.noCompatibleDimensions")}
      </Typography>
    );
  }

  return (
    <Box sx={{ maxWidth: 280 }}>
      <Typography variant="caption" sx={{ display: "block", fontWeight: 700, mb: 0.5 }}>
        {t("measures.compatibility.title")}
      </Typography>
      {body}
    </Box>
  );
}

function variantPayload(
  base: Measure,
  plan: VariantCreatePlan,
  kindLabel?: string,
): MeasureCreate {
  // F-015-15: default the display name from the translated variant label
  // (e.g. "Revenue (YTD Prior Year)") rather than the raw kind token
  // ("Revenue (ytd_prior_year)").  The dialog seeds tvDisplay with the same
  // translated label on kind selection; this keeps the fallback correct even
  // when tvDisplay is empty at submit or the payload is built via an API path.
  return {
    name: `${base.name}_${plan.kind}`,
    display_name: `${base.display_name || base.name} (${kindLabel ?? plan.kind})`,
    description: base.description ?? null,
    display_folder: base.display_folder ?? null,
    source_table_id: base.source_table_id ?? undefined,
    source_column_name: base.source_column_name ?? undefined,
    user_defined_attribute_id: base.user_defined_attribute_id ?? undefined,
    measure_type: base.measure_type,
    expression: base.expression ?? undefined,
    calc_agg_mode: base.calc_agg_mode ?? undefined,
    default_agg: (base.default_agg as MeasureCreate["default_agg"]) || "sum",
    data_type: base.data_type,
    format: base.format,
    is_additive: base.is_additive,
    variant_kind: plan.kind,
    variant_of_measure_id: base.id,
    variant_n: plan.n,
    calendar_model_table_id: base.calendar_model_table_id ?? null,
    hierarchy_id: base.hierarchy_id ?? null,
    date_dimension_column_id: base.date_dimension_column_id ?? null,
  };
}

/**
 * Map a persisted measure to a create-shaped payload so undo/redo can restore
 * it (Bug-8227). Used to build the inverse op for a delete (re-create) and the
 * prior-values op for an update.
 */
function measureToPayload(m: Measure): Record<string, unknown> {
  // Use ?? null (not ?? undefined) so undo actively resets a newly-set field
  // (null->value) back to null via the PATCH, instead of omitting the key and
  // silently leaving the new value (Fable review finding 3).
  return {
    name: m.name,
    display_name: m.display_name || m.name,
    description: m.description ?? null,
    display_folder: m.display_folder ?? null,
    source_table_id: m.source_table_id ?? null,
    source_column_name: m.source_column_name ?? null,
    user_defined_attribute_id: m.user_defined_attribute_id ?? null,
    measure_type: m.measure_type,
    expression: m.expression ?? null,
    calc_agg_mode: m.calc_agg_mode ?? null,
    default_agg: (m.default_agg as MeasureCreate["default_agg"]) || "sum",
    data_type: m.data_type,
    format: m.format,
    is_additive: m.is_additive,
    semi_additive_behavior: m.semi_additive_behavior ?? null,
    semi_additive_account_column_id: m.semi_additive_account_column_id ?? null,
    variant_kind: m.variant_kind ?? null,
    variant_of_measure_id: m.variant_of_measure_id ?? null,
    variant_n: m.variant_n ?? null,
    calendar_model_table_id: m.calendar_model_table_id ?? null,
    hierarchy_id: m.hierarchy_id ?? null,
    date_dimension_column_id: m.date_dimension_column_id ?? null,
    cross_model_source_model_id: m.cross_model_source_model_id ?? null,
    cross_model_source_measure_id: m.cross_model_source_measure_id ?? null,
  };
}

export default function MeasuresPanel() {
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const qc = useQueryClient();
  const t = useT();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingMeasureId, setEditingMeasureId] = useState<string | null>(null);
  const [measName, setMeasName] = useState("");
  const [measDisplay, setMeasDisplay] = useState("");
  const [measDescription, setMeasDescription] = useState("");
  const [measFolder, setMeasFolder] = useState("");
  const [measTableId, setMeasTableId] = useState("");
  const [measAttrId, setMeasAttrId] = useState("");
  const [pendingMeasureAttrName, setPendingMeasureAttrName] = useState<string | null>(null);
  const [measType, setMeasType] = useState("standard");
  const [measExpression, setMeasExpression] = useState("");
  const [measCalcMode, setMeasCalcMode] =
    useState<"expression_as_written" | "per_row_then_aggregate">(
      "expression_as_written",
    );
  const [validationResult, setValidationResult] =
    useState<ValidateMeasureExpressionResponse | null>(null);
  const [validationLoading, setValidationLoading] = useState(false);
  const [measAgg, setMeasAgg] =
    useState<MeasureCreate["default_agg"]>("sum");
  const [measDataType, setMeasDataType] = useState("numeric");
  const [measFormat, setMeasFormat] = useState<MeasureFormatToken | "">("");
  const [measAdditive, setMeasAdditive] = useState(true);
  const [measSemiAdditive, setMeasSemiAdditive] = useState<SemiAdditiveBehavior | "">("");
  const [measCalendarModelTableId, setMeasCalendarModelTableId] = useState("");
  const [measHierarchyId, setMeasHierarchyId] = useState("");
  const [measDateDimColId, setMeasDateDimColId] = useState("");
  // Bug-6227 (DEC-DATEDIM): the date-dimension column setting stays hidden
  // until it is actually used (the edited measure already carries a value).
  // A modeller may manually reveal it, in which case we warn but allow it.
  const [showDateDimSetting, setShowDateDimSetting] = useState(false);
  const [crossModelSourceModelId, setCrossModelSourceModelId] = useState("");
  const [crossModelSourceMeasureId, setCrossModelSourceMeasureId] = useState("");
  const [expandedDrillId, setExpandedDrillId] = useState<string | null>(null);
  const [compatibilityPersonaId, setCompatibilityPersonaId] = useState<string | null>(null);
  const [tvDialogOpen, setTvDialogOpen] = useState(false);
  const [tvBaseMeasureId, setTvBaseMeasureId] = useState("");
  const [tvKind, setTvKind] = useState("");
  const [tvN, setTvN] = useState<number | "">("");
  const [tvName, setTvName] = useState("");
  const [tvDisplay, setTvDisplay] = useState("");
  const [tvDescription, setTvDescription] = useState("");
  const [tvFolder, setTvFolder] = useState("");
  const [tvFormat, setTvFormat] = useState<MeasureFormatToken | "">("");
  const [tvHierarchyId, setTvHierarchyId] = useState("");
  const [tvCalendarModelTableId, setTvCalendarModelTableId] = useState("");
  const [renameImpact, setRenameImpact] = useState<import("../../api/types").MeasureRenameImpactResponse | null>(null);
  const [renameImpactLoading, setRenameImpactLoading] = useState(false);
  const [renameImpactError, setRenameImpactError] = useState<string | null>(null);

  const canEdit = useCanAuthorModel();
  const measures = useMeasures(projectId!, modelId!);
  const dimensions = useDimensions(projectId!, modelId!);
  const sources = useSources(projectId!, modelId!);
  const sourceIds = (sources.data ?? []).map((s) => s.id);
  const allTables = useAllModelTables(projectId!, modelId!, sourceIds);
  const { columnStatsMap } = useModelSourceStatistics(projectId!, modelId!);
  const hierarchies = useHierarchies(projectId!, modelId!);
  const tableAttributes = useTableAttributes(projectId!, modelId!, measTableId);
  const selectedAttribute = (tableAttributes.data ?? []).find((a) => a.id === measAttrId);
  const projectModels = useModels(projectId!);
  const otherModels = useMemo(
    () => (projectModels.data ?? []).filter((m) => m.id !== modelId),
    [projectModels.data, modelId],
  );
  const [crossModelMeasures, setCrossModelMeasures] = useState<
    { id: string; name: string; display_name: string }[]
  >([]);
  useEffect(() => {
    if (!crossModelSourceModelId || !projectId) {
      setCrossModelMeasures([]);
      return;
    }
    let cancelled = false;
    measuresApi
      .list(projectId, crossModelSourceModelId)
      .then((list) => {
        if (!cancelled)
          setCrossModelMeasures(
            list.map((m) => ({ id: m.id, name: m.name, display_name: m.display_name ?? m.name })),
          );
      })
      .catch(() => {
        if (!cancelled) setCrossModelMeasures([]);
      });
    return () => {
      cancelled = true;
    };
  }, [crossModelSourceModelId, projectId]);

  const editingMeasure = useMemo(
    () => measures.data?.find((m) => m.id === editingMeasureId) ?? null,
    [measures.data, editingMeasureId],
  );
  const isEditingVariant = !!editingMeasure?.variant_kind;
  const hasCalendarAliases = useMemo(
    () => (allTables.data ?? []).some((t) => !!t.calendar_table_id),
    [allTables.data],
  );
  const compatibilityMeasureIds = useMemo(
    () => (measures.data ?? []).map((measure) => measure.id),
    [measures.data],
  );
  const compatibilityDimensions = useMemo(
    () => (dimensions.data ?? []).filter((dimension) => !dimension.is_hidden),
    [dimensions.data],
  );
  const compatibilityDimensionIds = useMemo(
    () => compatibilityDimensions.map((dimension) => dimension.id),
    [compatibilityDimensions],
  );
  const compatibilityQueryMeasureIds =
    dimensions.isLoading || dimensions.isError ? [] : compatibilityMeasureIds;
  const fieldCompatibility = useFieldCompatibility(
    projectId ?? "",
    modelId ?? "",
    compatibilityPersonaId,
    compatibilityQueryMeasureIds,
    compatibilityDimensionIds,
  );
  const compatibilityLoading =
    compatibilityMeasureIds.length > 0 &&
    (dimensions.isLoading || fieldCompatibility.isLoading);
  const compatibilityUnavailable =
    compatibilityMeasureIds.length > 0 &&
    !compatibilityLoading &&
    (dimensions.isError || fieldCompatibility.isError || !fieldCompatibility.data);
  const compatibilityByMeasureId = useMemo(() => {
    const byMeasure = new Map<string, MeasureCompatibilitySummary>();
    for (const measureId of compatibilityMeasureIds) {
      byMeasure.set(
        measureId,
        summarizeMeasureCompatibility({
          measureId,
          matrix: fieldCompatibility.data,
          dimensions: compatibilityDimensions,
          loading: compatibilityLoading,
          unavailable: compatibilityUnavailable,
        }),
      );
    }
    return byMeasure;
  }, [
    compatibilityMeasureIds,
    fieldCompatibility.data,
    compatibilityDimensions,
    compatibilityLoading,
    compatibilityUnavailable,
  ]);

  // F-015-23: surface per-kind eligibility inside the time-variant dialog so
  // ineligible kinds are disabled (with their reason) before the modeler fills
  // the form, rather than failing with a 422 on submit.
  const tvAvailableVariants = useQuery({
    queryKey: ["available-variants", projectId, modelId, tvBaseMeasureId],
    queryFn: () =>
      measuresApi.listAvailableVariants(projectId!, modelId!, tvBaseMeasureId),
    enabled: tvDialogOpen && Boolean(tvBaseMeasureId),
    staleTime: 30_000,
  });
  // kind -> { eligible, reason, existing } for O(1) lookup in the picker.
  const tvVariantInfo = useMemo(() => {
    const map = new Map<
      string,
      { eligible: boolean; reason: string | null; exists: boolean }
    >();
    for (const v of tvAvailableVariants.data?.variants ?? []) {
      map.set(v.kind, {
        eligible: v.eligible,
        reason: v.reason,
        exists: Boolean(v.existing_measure_id),
      });
    }
    return map;
  }, [tvAvailableVariants.data]);

  useEffect(() => {
    if (!pendingMeasureAttrName || !tableAttributes.data) return;
    const match = tableAttributes.data.find(
      (a) => !a.is_user_defined && a.name === pendingMeasureAttrName,
    );
    if (match) {
      setMeasAttrId(match.id);
    }
    setPendingMeasureAttrName(null);
  }, [pendingMeasureAttrName, tableAttributes.data]);

  useEffect(() => {
    if (measType !== "calculated") {
      setValidationResult(null);
      setValidationLoading(false);
      return;
    }
    const expr = measExpression.trim();
    if (!expr) {
      setValidationResult(null);
      setValidationLoading(false);
      return;
    }
    setValidationLoading(true);
    let cancelled = false;
    const handle = setTimeout(async () => {
      try {
        const res = await measuresApi.validateExpression(projectId!, modelId!, {
          expression: expr,
          self_measure_id: editingMeasureId,
        });
        if (!cancelled) setValidationResult(res);
      } catch (err) {
        if (!cancelled) {
          setValidationResult({
            valid: false,
            referenced_measure_ids: [],
            referenced_measure_names: [],
            error: err instanceof Error ? err.message : t("measures.validationFailed"),
          });
        }
      } finally {
        if (!cancelled) setValidationLoading(false);
      }
    }, 400);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [measType, measExpression, projectId, modelId, editingMeasureId]);

  function buildMeasurePayload(): MeasureCreate {
    return {
      name: measName,
      display_name: measDisplay || measName,
      description: measDescription.trim() ? measDescription.trim() : null,
      display_folder: measFolder.trim() ? measFolder.trim() : null,
      source_table_id:
        selectedAttribute && !selectedAttribute.is_user_defined
          ? measTableId || undefined
          : undefined,
      source_column_name:
        selectedAttribute && !selectedAttribute.is_user_defined
          ? selectedAttribute.name
          : undefined,
      user_defined_attribute_id:
        selectedAttribute && selectedAttribute.is_user_defined
          ? measAttrId
          : undefined,
      measure_type: measType,
      expression: measType === "calculated" ? measExpression : undefined,
      calc_agg_mode: measType === "calculated" ? measCalcMode : undefined,
      default_agg: measAgg,
      data_type: measDataType,
      format: measFormat || null,
      is_additive: measAdditive,
      semi_additive_behavior: measSemiAdditive || null,
      // #10: by_account (the only consumer of the account column) is no longer
      // authorable, so this is never set from the editor.
      semi_additive_account_column_id: null,
      calendar_model_table_id: measCalendarModelTableId || null,
      hierarchy_id: measHierarchyId || null,
      date_dimension_column_id: measDateDimColId || null,
      cross_model_source_model_id: crossModelSourceModelId || null,
      cross_model_source_measure_id: crossModelSourceMeasureId || null,
    };
  }

  const createMeas = useMutation({
    mutationFn: async () => {
      const payload = buildMeasurePayload();
      const created = await measuresApi.create(projectId!, modelId!, payload);
      return { created, payload };
    },
    onSuccess: ({ created, payload }) => {
      // Bug-8227: record the create so undo removes it / redo re-creates it.
      recordCreate("measure", created.id, payload as unknown as Record<string, unknown>);
      qc.invalidateQueries({ queryKey: ["measures", projectId, modelId] });
      setDialogOpen(false);
    },
  });

  const createTv = useMutation({
    mutationFn: async () => {
      const base = (measures.data ?? []).find((m) => m.id === tvBaseMeasureId);
      if (!base) throw new Error(t("measures.baseMeasureNotFound"));
      const n =
        isParametricVariant(tvKind) && typeof tvN === "number" && tvN > 0
          ? tvN
          : TIME_VARIANT_DEFAULT_N[tvKind] ?? null;
      const payload = variantPayload(base, { kind: tvKind, n }, t(TIME_VARIANT_LABELS[tvKind] ?? tvKind));
      if (tvName) payload.name = tvName;
      if (tvDisplay) payload.display_name = tvDisplay;
      if (tvDescription.trim()) payload.description = tvDescription.trim();
      if (tvFolder.trim()) payload.display_folder = tvFolder.trim();
      if (tvFormat) payload.format = tvFormat;
      if (tvHierarchyId) payload.hierarchy_id = tvHierarchyId;
      if (tvCalendarModelTableId) payload.calendar_model_table_id = tvCalendarModelTableId;
      const created = await measuresApi.create(projectId!, modelId!, payload);
      return { created, payload };
    },
    onSuccess: ({ created, payload }) => {
      // Bug-8227: time-variant create is a drawer-authored measure create that
      // must be undoable (Fable review finding 2 — missing history entry).
      recordCreate("measure", created.id, payload as unknown as Record<string, unknown>);
      qc.invalidateQueries({ queryKey: ["measures", projectId, modelId] });
      setTvDialogOpen(false);
    },
  });

  const updateMeas = useMutation({
    mutationFn: async () => {
      const payload = isEditingVariant
        ? {
            name: measName,
            display_name: measDisplay || measName,
            description: measDescription.trim() || null,
            display_folder: measFolder.trim() || null,
            format: measFormat || null,
          }
        : buildMeasurePayload();
      // Bug-8227: capture the prior definition BEFORE the write so undo can
      // PATCH the measure back to its previous field values (S5: undoing a
      // calculated-measure formula change is the most consequential edit).
      const prior = (measures.data ?? []).find((m) => m.id === editingMeasureId);
      const priorPayload = prior ? measureToPayload(prior) : null;
      await measuresApi.update(
        projectId!,
        modelId!,
        editingMeasureId!,
        payload,
      );
      return { id: editingMeasureId!, payload, priorPayload };
    },
    onSuccess: ({ id, payload, priorPayload }) => {
      if (priorPayload) {
        recordUpdate(
          "measure",
          id,
          priorPayload,
          payload as unknown as Record<string, unknown>,
        );
      }
      qc.invalidateQueries({ queryKey: ["measures", projectId, modelId] });
      setDialogOpen(false);
      setEditingMeasureId(null);
    },
  });

  async function handleMeasureSave() {
    const originalName = editingMeasure?.name;
    const candidate = measName.trim();
    if (!editingMeasureId || !originalName || candidate === originalName) {
      updateMeas.mutate();
      return;
    }
    setRenameImpactError(null);
    setRenameImpactLoading(true);
    try {
      const impact = await measuresApi.renameImpact(
        projectId!,
        modelId!,
        editingMeasureId,
        candidate,
      );
      setRenameImpact(impact);
    } catch (err) {
      setRenameImpactError(
        err instanceof Error ? err.message : t("measures.renameImpact.loadFailed"),
      );
    } finally {
      setRenameImpactLoading(false);
    }
  }

  const deleteMeas = useMutation({
    // Carry the full measure through so the undo entry can re-create it from
    // its prior definition after the delete confirms (Bug-8227).
    mutationFn: async (measure: Measure) => {
      await measuresApi.delete(projectId!, modelId!, measure.id);
      return measure;
    },
    onSuccess: (measure) => {
      recordDelete(
        "measure",
        measure.id,
        measureToPayload(measure),
      );
      qc.invalidateQueries({ queryKey: ["measures", projectId, modelId] });
    },
  });

  const confirm = useConfirm();
  async function handleDeleteMeas(measure: Measure) {
    const dependents = (measures.data ?? []).filter(
      (m) => m.variant_of_measure_id === measure.id,
    );
    const ok = await confirm({
      title: t("measures.deleteMeasureTitle"),
      message:
        dependents.length > 0 ? (
          <span>
            {dependents.length === 1
              ? t("measures.deleteVariantsSingular", { name: measure.name })
              : t("measures.deleteVariantsPlural", { name: measure.name, count: String(dependents.length) })}
            <br />
            <Typography variant="caption" color="text.secondary">
              {dependents.map((d) => d.name).join(", ")}
            </Typography>
            <br />
            {t("measures.aggregatesNeedRevalidation")}
          </span>
        ) : (
          <span>
            {t("measures.deleteConfirm", { name: measure.name })}
          </span>
        ),
      confirmLabel: t("measures.deleteMeasureConfirmLabel"),
    });
    if (ok) deleteMeas.mutate(measure);
  }

  function tableLabel(tableId: string | null) {
    if (!tableId) return null;
    const t = allTables.data?.find((t) => t.id === tableId);
    return t?.alias ?? t?.display_name ?? null;
  }

  function openDialog() {
    setEditingMeasureId(null);
    setMeasName("");
    setMeasDisplay("");
    setMeasDescription("");
    setMeasFolder("");
    setMeasTableId("");
    setMeasAttrId("");
    setPendingMeasureAttrName(null);
    setMeasType("standard");
    setMeasExpression("");
    setMeasCalcMode("expression_as_written");
    setValidationResult(null);
    setMeasAgg("sum");
    setMeasDataType("numeric");
    setMeasFormat("");
    setMeasAdditive(true);
    setMeasSemiAdditive("");
    setMeasCalendarModelTableId("");
    setMeasHierarchyId("");
    setMeasDateDimColId("");
    setShowDateDimSetting(false);
    setCrossModelSourceModelId("");
    setCrossModelSourceMeasureId("");
    setTvBaseMeasureId("");
    setTvKind("");
    setTvN("");
    createMeas.reset();
    updateMeas.reset();
    setDialogOpen(true);
  }

  function openTvDialog() {
    setTvBaseMeasureId("");
    setTvKind("");
    setTvN("");
    setTvName("");
    setTvDisplay("");
    setTvDescription("");
    setTvFolder("");
    setTvFormat("");
    setTvHierarchyId("");
    setTvCalendarModelTableId("");
    createTv.reset();
    setTvDialogOpen(true);
  }

  function openEditDialog(measureId: string) {
    const measure = measures.data?.find((m) => m.id === measureId);
    if (!measure) return;
    setEditingMeasureId(measure.id);
    setMeasName(measure.name);
    setMeasDisplay(
      measure.display_name && measure.display_name !== measure.name ? measure.display_name : "",
    );
    setMeasDescription(measure.description ?? "");
    setMeasFolder(measure.display_folder ?? "");
    setMeasTableId(measure.source_table_id ?? "");
    if (measure.user_defined_attribute_id) {
      setMeasAttrId(measure.user_defined_attribute_id);
      setPendingMeasureAttrName(null);
    } else {
      setMeasAttrId("");
      setPendingMeasureAttrName(measure.source_column_name ?? null);
    }
    setMeasType(measure.measure_type || "standard");
    setMeasExpression(measure.expression ?? "");
    setMeasCalcMode(measure.calc_agg_mode ?? "expression_as_written");
    setValidationResult(null);
    setMeasAgg((measure.default_agg as MeasureCreate["default_agg"]) || "sum");
    setMeasDataType(measure.data_type || "numeric");
    setMeasFormat((measure.format as MeasureFormatToken) || "");
    setMeasAdditive(measure.is_additive);
    setMeasSemiAdditive((measure.semi_additive_behavior as SemiAdditiveBehavior) || "");
    setMeasCalendarModelTableId(measure.calendar_model_table_id ?? "");
    setMeasHierarchyId(measure.hierarchy_id ?? "");
    setMeasDateDimColId(measure.date_dimension_column_id ?? "");
    setShowDateDimSetting(false);
    setCrossModelSourceModelId(measure.cross_model_source_model_id ?? "");
    setCrossModelSourceMeasureId(measure.cross_model_source_measure_id ?? "");
    createMeas.reset();
    updateMeas.reset();
    setDialogOpen(true);
  }

  return (
    <Box>
      <CalendarBindingHint context="measures-list" />
      <Box display="flex" alignItems="center" mb={1.5}>
        <Typography variant="body2" color="text.secondary" sx={{ flex: 1 }}>
          {t("measures.description")}
        </Typography>
        <Box sx={{ ml: 1, flexShrink: 0 }}>
          <PersonaPicker
            projectId={projectId ?? ""}
            modelId={modelId ?? ""}
            value={compatibilityPersonaId}
            onChange={setCompatibilityPersonaId}
            label={t("measures.compatibility.personaLabel")}
          />
        </Box>
        {canEdit && (
          <Button
            size="small"
            variant="contained"
            startIcon={<AddIcon />}
            onClick={openDialog}
            sx={{ ml: 1, whiteSpace: "nowrap" }}
          >
            {t("measures.add")}
          </Button>
        )}
        {canEdit && (
          <Button
            size="small"
            variant="outlined"
            startIcon={<AddIcon />}
            onClick={openTvDialog}
            sx={{ ml: 1, whiteSpace: "nowrap" }}
          >
            {t("measures.addTimeVariant")}
          </Button>
        )}
      </Box>

      {measures.isLoading ? (
        <CircularProgress size={20} />
      ) : (
        <TableContainer component={Paper} variant="outlined">
          <Table size="small">
            <TableHead>
              <TableRow sx={{ bgcolor: ui.tableHeaderBg }}>
                <TableCell><strong>{t("measures.name")}</strong></TableCell>
                <TableCell><strong>{t("measures.source")}</strong></TableCell>
                <TableCell><strong>{t("measures.type")}</strong></TableCell>
                <TableCell><strong>{t("measures.agg")}</strong></TableCell>
                <TableCell><strong>{t("measures.format")}</strong></TableCell>
                <TableCell><strong>{t("measures.additive")}</strong></TableCell>
                <TableCell><strong>{t("measures.variant")}</strong></TableCell>
                {canEdit && <TableCell />}
              </TableRow>
            </TableHead>
            <TableBody>
              {measures.data?.map((m) => (
                <Fragment key={m.id}>
                <TableRow>
                  <TableCell>
                    <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
                      {canEdit && m.measure_type !== "calculated" && (
                        <Tooltip title={t("measures.drillThroughTooltip")}>
                          <IconButton
                            size="small"
                            onClick={() =>
                              setExpandedDrillId(
                                expandedDrillId === m.id ? null : m.id,
                              )
                            }
                          >
                            {expandedDrillId === m.id ? (
                              <ExpandMoreIcon fontSize="small" />
                            ) : (
                              <ChevronRightIcon fontSize="small" />
                            )}
                          </IconButton>
                        </Tooltip>
                      )}
                      <Typography variant="body2" fontWeight={500}>
                        {m.name}
                      </Typography>
                      {m.measure_type === "calculated" && (
                        <Tooltip
                          title={
                            m.calc_agg_mode === "per_row_then_aggregate"
                              ? t("measures.formulaEvalPerRow")
                              : t("measures.formulaEvalAtAggTime")
                          }
                        >
                          <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.purpleBg, color: ui.purple, fontWeight: 600, fontSize: 10 }}>{t("measures.typeFormula")}</Typography>
                        </Tooltip>
                      )}
                      {m.is_invalid && (
                        <Tooltip title={m.invalid_reason || t("measures.invalidMeasure")}>
                          <ErrorOutlineIcon sx={{ fontSize: 16, color: ui.red }} />
                        </Tooltip>
                      )}
                      <Tooltip
                        title={
                          <MeasureCompatibilityTooltip
                            summary={
                              compatibilityByMeasureId.get(m.id) ??
                              summarizeMeasureCompatibility({
                                measureId: m.id,
                                matrix: fieldCompatibility.data,
                                dimensions: compatibilityDimensions,
                                loading: compatibilityLoading,
                                unavailable: compatibilityUnavailable,
                              })
                            }
                            t={t}
                          />
                        }
                        arrow
                      >
                        <IconButton
                          size="small"
                          aria-label={t("measures.compatibility.ariaLabel", {
                            name: m.display_name || m.name,
                          })}
                          sx={{ p: 0.25, color: "text.secondary" }}
                        >
                          <InfoOutlinedIcon sx={{ fontSize: 15 }} />
                        </IconButton>
                      </Tooltip>
                    </Box>
                    {m.display_name !== m.name && (
                      <Typography variant="caption" color="text.secondary">
                        {m.display_name}
                      </Typography>
                    )}
                  </TableCell>
                  <TableCell>
                    {m.source_column_name || m.user_defined_attribute_name ? (
                      <Typography variant="caption" sx={{ fontFamily: "monospace", fontSize: 11, color: m.user_defined_attribute_name ? ui.purple : ui.muted }}>
                        {`${tableLabel(m.source_table_id) ?? ""}.${m.user_defined_attribute_name ?? m.source_column_name}`}
                      </Typography>
                    ) : m.expression ? (
                      <Typography variant="caption" sx={{ fontStyle: "italic", color: ui.purple, fontSize: 11 }}>{t("measures.typeFormula")}</Typography>
                    ) : (
                      <Typography variant="caption" color="text.secondary">{t("common.na")}</Typography>
                    )}
                  </TableCell>
                  <TableCell>
                    {(() => {
                      const cs = m.source_column_id ? columnStatsMap[m.source_column_id] : undefined;
                      return (
                        <Typography variant="caption" color="text.secondary" sx={{ fontSize: 11 }}>
                          {cs?.data_type ?? m.data_type ?? t("common.na")}
                          {cs?.distinct_count != null && cs?.row_count != null && cs.row_count > 0
                            ? ` (${((cs.distinct_count / cs.row_count) * 100).toFixed(1)}%)`
                            : ""}
                        </Typography>
                      );
                    })()}
                  </TableCell>
                  <TableCell>
                    <Typography component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.greenBg, color: ui.green, fontWeight: 600, fontSize: 11 }}>{m.default_agg}</Typography>
                  </TableCell>
                  <TableCell>
                    {m.format ? (
                      <Typography variant="caption" sx={{ color: ui.muted, fontSize: 11 }}>{m.format}</Typography>
                    ) : (
                      <Typography variant="caption" color="text.secondary">{t("common.na")}</Typography>
                    )}
                  </TableCell>
                  <TableCell>
                    <Typography variant="caption" sx={{ fontWeight: 500, fontSize: 11, color: m.is_additive ? ui.green : ui.goldDark }}>{m.is_additive ? t("common.yes") : t("common.no")}</Typography>
                  </TableCell>
                  <TableCell>
                    {m.variant_kind ? (
                      <Typography variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, bgcolor: ui.goldBg, color: ui.goldDark, fontSize: 11 }}>
                        {m.variant_n != null ? `${m.variant_kind} (n=${m.variant_n})` : m.variant_kind}
                      </Typography>
                    ) : (
                      <Typography variant="caption" color="text.secondary">{t("common.na")}</Typography>
                    )}
                  </TableCell>
                  {canEdit && (
                  <TableCell align="right">
                    <Tooltip title={t("common.edit")}>
                      <IconButton
                        size="small"
                        onClick={() => openEditDialog(m.id)}
                      >
                        <EditIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title={t("common.delete")}>
                      <IconButton
                        size="small"
                        onClick={() => handleDeleteMeas(m)}
                      >
                        <DeleteIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  </TableCell>
                  )}
                </TableRow>
                {expandedDrillId === m.id && (
                  <TableRow>
                    <TableCell colSpan={canEdit ? 8 : 7} sx={{ p: 0, borderBottom: 0 }}>
                      <DrillThroughSetEditor
                        projectId={projectId!}
                        modelId={modelId!}
                        measure={m}
                        tables={allTables.data ?? []}
                      />
                    </TableCell>
                  </TableRow>
                )}
                </Fragment>
              ))}
              {measures.data?.length === 0 && (
                <TableRow>
                  <TableCell colSpan={canEdit ? 8 : 7}>
                    <Typography variant="body2" color="text.secondary">
                      {t("measures.none")}
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
        <DialogTitle>{editingMeasureId ? t("measures.editMeasure") : t("measures.addMeasure")}</DialogTitle>
        <DialogContent>
          {isEditingVariant && (() => {
            const baseMeasure = measures.data?.find(
              (m) => m.id === editingMeasure?.variant_of_measure_id,
            );
            const calAlias = baseMeasure?.calendar_model_table_id
              ? (allTables.data ?? []).find(
                  (t) => t.id === baseMeasure.calendar_model_table_id,
                )
              : null;
            const linkedHierarchy = baseMeasure?.hierarchy_id
              ? (hierarchies.data ?? []).find(
                  (h) => h.id === baseMeasure.hierarchy_id,
                )
              : null;
            const isPeriodAware = [
              "ytd", "qtd", "mtd", "wtd", "prior_year", "prior_quarter",
              "prior_month", "prior_week", "ytd_prior_year", "yoy_growth",
              "yoy_growth_pct", "period_to_date", "same_period_last_year",
            ].includes(editingMeasure?.variant_kind ?? "");
            const needsHierarchy = isPeriodAware && !linkedHierarchy;
            const vKind = editingMeasure?.variant_kind ?? "";
            const vKindLabel = t(TIME_VARIANT_LABELS[vKind] ?? vKind);
            const vN = editingMeasure?.variant_n;
            return (
              <>
                <Alert severity={needsHierarchy ? "warning" : "info"} sx={{ mt: 1 }}>
                  {t("measures.variantEditInfo", { name: baseMeasure?.name ?? "" })}
                  {needsHierarchy && (
                    <Box sx={{ mt: 0.5 }}>
                      {t("measures.variantEditInfoNoHierarchy", { name: baseMeasure?.name ?? t("measures.baseMeasureNotFound") })}
                    </Box>
                  )}
                </Alert>
                <Box sx={{ display: "flex", gap: 2, mt: 1 }}>
                  <TextField
                    label={t("measures.variantTypeLabel")}
                    fullWidth
                    margin="dense"
                    value={vKindLabel}
                    InputProps={{ readOnly: true }}
                  />
                  {vN != null && (
                    <TextField
                      label={t("measures.nLabel")}
                      margin="dense"
                      value={vN}
                      InputProps={{ readOnly: true }}
                      sx={{ width: 120, flexShrink: 0 }}
                    />
                  )}
                </Box>
                {(linkedHierarchy || calAlias) && (
                  <Box sx={{ display: "flex", gap: 2 }}>
                    {linkedHierarchy && (
                      <TextField
                        label={t("measures.hierarchyLabel")}
                        fullWidth
                        margin="dense"
                        value={`${linkedHierarchy.name}${linkedHierarchy.calendar_type ? ` (${linkedHierarchy.calendar_type})` : ""}`}
                        InputProps={{ readOnly: true }}
                      />
                    )}
                    {calAlias && (
                      <TextField
                        label={t("measures.calendarAliasLabel")}
                        fullWidth
                        margin="dense"
                        value={calAlias.alias ?? calAlias.display_name}
                        InputProps={{ readOnly: true }}
                      />
                    )}
                  </Box>
                )}
              </>
            );
          })()}
          <TextField
            label={t("measures.nameLabel")}
            fullWidth
            margin="normal"
            value={measName}
            onChange={(e) => setMeasName(e.target.value)}
            autoFocus
            error={!measName && dialogOpen}
            helperText={!measName ? t("measures.nameHelper") : ""}
          />
          <TextField
            label={t("common.displayName")}
            fullWidth
            margin="normal"
            value={measDisplay}
            onChange={(e) => setMeasDisplay(e.target.value)}
          />
          <TextField
            label={t("measures.businessDescription")}
            fullWidth
            margin="normal"
            multiline
            minRows={2}
            maxRows={6}
            value={measDescription}
            onChange={(e) => setMeasDescription(e.target.value)}
            placeholder={t("measures.businessDescriptionPlaceholder")}
          />
          <TextField
            label={t("measures.displayFolder")}
            fullWidth
            margin="normal"
            value={measFolder}
            onChange={(e) => setMeasFolder(e.target.value)}
            placeholder={t("measures.displayFolderPlaceholder")}
          />

          <Typography variant="subtitle2" sx={{ mt: 2, mb: 0.5 }}>
            {t("measures.sourceColumn")}
          </Typography>
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("measures.tableLabel")}</InputLabel>
            <Select
              value={measTableId}
              label={t("measures.tableLabel")}
              onChange={(e) => {
                setMeasTableId(e.target.value);
                setMeasAttrId("");
                setPendingMeasureAttrName(null);
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
          <FormControl fullWidth margin="dense" disabled={!measTableId}>
            <InputLabel>{t("measures.attributeLabel")}</InputLabel>
            <Select
              value={measAttrId}
              label={t("measures.attributeLabel")}
              onChange={(e) => setMeasAttrId(e.target.value)}
            >
              <MenuItem value="">{t("common.none")}</MenuItem>
              {tableAttributes.data?.map((attr) => (
                <MenuItem key={attr.id} value={attr.id}>
                  {attr.is_user_defined ? `fx ${attr.name}` : attr.name}
                </MenuItem>
              ))}
              {tableAttributes.isLoading && (
                <MenuItem value="" disabled>
                  <CircularProgress size={14} sx={{ mr: 1 }} /> {t("measures.loadingAttributes")}
                </MenuItem>
              )}
            </Select>
          </FormControl>

          <Typography variant="subtitle2" sx={{ mt: 2, mb: 0.5 }}>
            {t("measures.definitionTitle")}
          </Typography>
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("measures.typeLabel")}</InputLabel>
            <Select
              value={measType}
              label={t("measures.typeLabel")}
              onChange={(e) => setMeasType(e.target.value)}
              disabled={!!editingMeasureId}
            >
              {MEASURE_TYPES.map((mt) => (
                <MenuItem key={mt.value} value={mt.value}>
                  {t(mt.label)}
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          {measType === "calculated" && (
            <>
              <TextField
                label={t("measures.expressionLabel")}
                fullWidth
                margin="dense"
                multiline
                minRows={2}
                maxRows={6}
                value={measExpression}
                onChange={(e) => setMeasExpression(e.target.value)}
                placeholder={t("measures.expressionPlaceholder")}
                helperText={
                  t("measures.expressionHelper")
                }
                error={
                  !!validationResult && !validationResult.valid && !validationLoading
                }
              />
              {validationLoading && (
                <Typography variant="caption" color="text.secondary">
                  <CircularProgress size={10} sx={{ mr: 0.5 }} /> {t("measures.validating")}
                </Typography>
              )}
              {validationResult && !validationLoading && (
                <Box sx={{ mt: 0.5, mb: 1 }}>
                  {validationResult.valid ? (
                    <Alert severity="success" sx={{ py: 0 }}>
                      {t("measures.expressionValid")}
                      {validationResult.referenced_measure_names.length > 0 && (
                        <Box
                          sx={{ mt: 0.5, display: "flex", flexWrap: "wrap", gap: 0.5 }}
                        >
                          <Typography variant="caption" sx={{ mr: 0.5 }}>
                            {t("measures.references")}
                          </Typography>
                          {validationResult.referenced_measure_names.map((n) => (
                            <Typography key={n} component="span" variant="caption" sx={{ px: 0.5, py: 0.125, borderRadius: 0.5, border: "1px solid", borderColor: ui.green, color: ui.green, fontSize: 11 }}>{n}</Typography>
                          ))}
                        </Box>
                      )}
                    </Alert>
                  ) : (
                    <Alert severity="error" sx={{ py: 0 }}>
                      {validationResult.error ?? t("measures.invalidExpression")}
                    </Alert>
                  )}
                </Box>
              )}
              <FormControl fullWidth margin="dense">
                <InputLabel>{t("measures.aggregationMode")}</InputLabel>
                <Select
                  value={measCalcMode}
                  label={t("measures.aggregationMode")}
                  onChange={(e) =>
                    setMeasCalcMode(
                      e.target.value as
                        | "expression_as_written"
                        | "per_row_then_aggregate",
                    )
                  }
                >
                  {CALC_AGG_MODES.map((m) => (
                    <MenuItem key={m.value} value={m.value}>
                      {t(m.label)}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
              <Typography
                variant="caption"
                color="text.secondary"
                sx={{ display: "block", mb: 1 }}
              >
                {(() => {
                  const desc = CALC_AGG_MODES.find(
                    (m) => m.value === measCalcMode,
                  )?.description;
                  return desc ? t(desc) : "";
                })()}
              </Typography>
            </>
          )}

          <FormControl fullWidth margin="dense">
            <InputLabel>{t("measures.aggregation")}</InputLabel>
            <Select
              value={measAgg}
              label={t("measures.aggregation")}
              onChange={(e) =>
                setMeasAgg(e.target.value as MeasureCreate["default_agg"])
              }
            >
              {AGG_OPTIONS.map((a) => (
                <MenuItem key={a} value={a}>
                  {a}
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          <TextField
            label={t("measures.dataType")}
            fullWidth
            margin="dense"
            value={measDataType}
            onChange={(e) => setMeasDataType(e.target.value)}
          />

          <FormControl fullWidth margin="dense">
            <InputLabel>{t("measures.formatLabel")}</InputLabel>
            <Select
              value={measFormat}
              label={t("measures.formatLabel")}
              onChange={(e) => setMeasFormat(e.target.value as MeasureFormatToken | "")}
            >
              <MenuItem value="">{t("measures.formatNone")}</MenuItem>
              {MEASURE_FORMAT_TOKENS.map((token) => (
                <MenuItem key={token} value={token}>
                  {t(MEASURE_FORMAT_LABELS[token])}
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          <Typography variant="subtitle2" sx={{ mt: 2, mb: 0.5 }}>
            {t("measures.additivity")}
          </Typography>
          <Typography variant="caption" color="text.secondary" display="block" mb={0.5}>
            {t("measures.additivityInfo")}
          </Typography>
          <FormControlLabel
            control={
              <Switch
                checked={measAdditive}
                onChange={(e) => setMeasAdditive(e.target.checked)}
              />
            }
            label={t("measures.additiveMeasure")}
            sx={{ mt: 0.5 }}
          />

          <FormControl fullWidth margin="dense">
            <InputLabel>{t("measures.semiAdditiveBehavior")}</InputLabel>
            <Select
              value={measSemiAdditive}
              label={t("measures.semiAdditiveBehavior")}
              onChange={(e) =>
                setMeasSemiAdditive(e.target.value as SemiAdditiveBehavior | "")
              }
            >
              <MenuItem value="">{t("semiAdditive.none")}</MenuItem>
              {SEMI_ADDITIVE_OPTIONS.map((opt) => (
                <MenuItem key={opt.value} value={opt.value}>
                  {t(opt.label)}
                </MenuItem>
              ))}
            </Select>
            <FormHelperText>
              {t("measures.semiAdditiveHelp")}
            </FormHelperText>
          </FormControl>

          {!isEditingVariant && (
            <>
              <Typography variant="subtitle2" sx={{ mt: 2, mb: 0.5 }}>
                {t("measures.timeHierarchy")}
              </Typography>
              {(() => {
                const calendarAliases = (allTables.data ?? []).filter(
                  (t) => !!t.calendar_table_id,
                );
                const dateHierarchies = (hierarchies.data ?? []).filter((h) =>
                  h.dimension_kind === "time" ||
                  h.type === "date_embedded" ||
                  !!h.calendar_type,
                );
                return (
                  <>
                    <FormControl fullWidth margin="dense">
                      <InputLabel>{t("measures.hierarchyLabel")}</InputLabel>
                      <Select
                        value={measHierarchyId}
                        label={t("measures.hierarchyLabel")}
                        onChange={(e) => setMeasHierarchyId(e.target.value)}
                      >
                        <MenuItem value="">{t("common.none")}</MenuItem>
                        {dateHierarchies.map((h) => (
                          <MenuItem key={h.id} value={h.id}>
                            {h.name}
                            {h.calendar_type ? ` (${h.calendar_type})` : ""}
                          </MenuItem>
                        ))}
                      </Select>
                      <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
                        {t("measures.hierarchyHelp")}
                      </Typography>
                    </FormControl>
                    {calendarAliases.length > 0 && (
                      <FormControl fullWidth margin="dense">
                        <InputLabel>{t("measures.calendarAliasLabel")}</InputLabel>
                        <Select
                          value={measCalendarModelTableId}
                          label={t("measures.calendarAliasLabel")}
                          onChange={(e) => setMeasCalendarModelTableId(e.target.value)}
                        >
                          <MenuItem value="">{t("common.none")}</MenuItem>
                          {calendarAliases.map((t) => (
                            <MenuItem key={t.id} value={t.id}>
                              {t.alias ?? t.display_name} ({t.physical_name})
                            </MenuItem>
                          ))}
                        </Select>
                        <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
                          {t("measures.calendarAliasHelp")}
                        </Typography>
                      </FormControl>
                    )}
                    {/* Bug-6227 (DEC-DATEDIM): the date-dimension column is an
                        advanced ORDER-BY hint for window variants. It stays
                        hidden until a measure already uses it; a modeller can
                        reveal it manually, but we warn first. */}
                    {(() => {
                      const dateDimInUse =
                        !!editingMeasure?.date_dimension_column_id;
                      if (!dateDimInUse && !showDateDimSetting) {
                        return (
                          <Button
                            size="small"
                            variant="text"
                            onClick={() => setShowDateDimSetting(true)}
                            sx={{ mt: 0.5, pl: 0, textTransform: "none" }}
                          >
                            {t("measures.dateDimShowLink")}
                          </Button>
                        );
                      }
                      // Candidate date columns span BOTH the measure's own
                      // (fact) table and every date-typed dimension column in
                      // the model. The stored value can point at either shape:
                      // a fact-embedded date, or a calendar-alias / date-
                      // dimension column (which is what the orphan-cascade
                      // guard matches against). Sourcing from the already-
                      // loaded dimensions list keeps the picker and the label
                      // map correct across tables without extra fetches.
                      const isDateType = (dt?: string | null) =>
                        /date|timestamp/i.test(dt || "");
                      const dateColumns: {
                        id: string;
                        label: string;
                      }[] = [];
                      const seenColIds = new Set<string>();
                      const pushCol = (id?: string | null, label?: string) => {
                        if (!id || seenColIds.has(id)) return;
                        seenColIds.add(id);
                        dateColumns.push({ id, label: label || id });
                      };
                      // Only PHYSICAL columns are valid: date_dimension_column_id
                      // is FK'd to model_columns.id. User-defined attribute ids
                      // live in a different namespace and would fail the FK on
                      // save, so they are excluded here. Hidden physical columns
                      // are intentionally kept — a deliberately hidden technical
                      // ordering column is still a valid pick for this setting.
                      for (const a of tableAttributes.data ?? []) {
                        if (a.is_user_defined) continue;
                        if (isDateType(a.data_type)) pushCol(a.id, a.name);
                      }
                      for (const d of dimensions.data ?? []) {
                        if (!d.source_column_id || d.is_hidden) continue;
                        if (!isDateType(d.data_type) && !d.is_time_dim) continue;
                        const col = d.source_column_name ?? d.name;
                        pushCol(
                          d.source_column_id,
                          d.source_table_display_name
                            ? `${d.source_table_display_name}.${col}`
                            : col,
                        );
                      }
                      const currentEntry = dateColumns.find(
                        (c) => c.id === measDateDimColId,
                      );
                      const currentInList = !measDateDimColId || !!currentEntry;
                      // Fallback label for a stored value that is neither a
                      // fact-table date column nor a modelled date dimension
                      // (rare); the raw id is the last resort.
                      const currentLabel = currentEntry
                        ? currentEntry.label
                        : measDateDimColId;
                      return (
                        <>
                          {showDateDimSetting && !dateDimInUse && (
                            <Alert severity="warning" sx={{ mt: 1 }}>
                              {t("measures.dateDimManualWarning")}
                            </Alert>
                          )}
                          <FormControl fullWidth margin="dense">
                            <InputLabel>
                              {t("measures.dateDimColumnLabel")}
                            </InputLabel>
                            <Select
                              value={measDateDimColId}
                              label={t("measures.dateDimColumnLabel")}
                              onChange={(e) =>
                                setMeasDateDimColId(e.target.value)
                              }
                            >
                              <MenuItem value="">
                                {t("common.none")}
                              </MenuItem>
                              {measDateDimColId && !currentInList && (
                                <MenuItem value={measDateDimColId}>
                                  {currentLabel}
                                </MenuItem>
                              )}
                              {dateColumns.map((c) => (
                                <MenuItem key={c.id} value={c.id}>
                                  {c.label}
                                </MenuItem>
                              ))}
                            </Select>
                            <Typography
                              variant="caption"
                              color="text.secondary"
                              sx={{ mt: 0.5 }}
                            >
                              {t("measures.dateDimColumnHelp")}
                            </Typography>
                          </FormControl>
                        </>
                      );
                    })()}
                  </>
                );
              })()}
            </>
          )}

          {(createMeas.isError || updateMeas.isError) && (
            <Alert severity="error" sx={{ mt: 1 }}>
              {extractApiError(
                createMeas.error || updateMeas.error,
                t(editingMeasureId ? "measures.updateFailed" : "measures.createFailed"),
              )}
            </Alert>
          )}

          {/* Cross-model reference (same project only) */}
          <Box mt={2}>
            <Typography variant="subtitle2" gutterBottom>{t("measures.crossModelRef")}</Typography>
            <Typography variant="caption" color="text.secondary" display="block" mb={1}>
              {t("measures.crossModelRefInfo")}
            </Typography>
            <FormControl fullWidth margin="dense" size="small">
              <InputLabel>{t("measures.sourceModelLabel")}</InputLabel>
              <Select
                value={crossModelSourceModelId}
                label={t("measures.sourceModelLabel")}
                onChange={(e) => {
                  setCrossModelSourceModelId(e.target.value);
                  setCrossModelSourceMeasureId("");
                }}
              >
                <MenuItem value="">{t("common.none")}</MenuItem>
                {otherModels.map((m) => (
                  <MenuItem key={m.id} value={m.id}>
                    {m.display_name || m.slug}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            <FormControl
              fullWidth
              margin="dense"
              size="small"
              disabled={!crossModelSourceModelId}
            >
              <InputLabel>{t("measures.sourceMeasureLabel")}</InputLabel>
              <Select
                value={crossModelSourceMeasureId}
                label={t("measures.sourceMeasureLabel")}
                onChange={(e) => setCrossModelSourceMeasureId(e.target.value)}
              >
                <MenuItem value="">{t("common.none")}</MenuItem>
                {crossModelMeasures.map((m) => (
                  <MenuItem key={m.id} value={m.id}>
                    {m.display_name || m.name}
                  </MenuItem>
                ))}
                {crossModelSourceModelId && crossModelMeasures.length === 0 && (
                  <MenuItem value="" disabled>
                    {projectModels.isLoading ? t("measures.loading") : t("measures.noMeasuresFound")}
                  </MenuItem>
                )}
              </Select>
            </FormControl>
          </Box>
        </DialogContent>
        <DialogActions sx={{ flexDirection: "column", alignItems: "stretch", gap: 0.5, px: 3, pb: 2 }}>
          {(() => {
            const reasons: string[] = [];
            if (!measName) reasons.push(t("measures.nameRequired"));
            if (measType === "calculated" && !measExpression.trim())
              reasons.push(t("measures.expressionRequired"));
            if (
              measType === "calculated" &&
              validationResult != null &&
              !validationResult.valid
            )
              reasons.push(t("measures.expressionValidationError"));
            if (reasons.length === 0) {
              return null;
            }
            return (
              <Typography variant="caption" color="error" sx={{ textAlign: "right" }}>
                {reasons.join(" ")}
              </Typography>
            );
          })()}
          <Box display="flex" justifyContent="flex-end" gap={1}>
            <Button onClick={() => setDialogOpen(false)}>{t("common.cancel")}</Button>
            <Button
              variant="contained"
              onClick={() => (editingMeasureId ? void handleMeasureSave() : createMeas.mutate())}
              disabled={
                !measName ||
                createMeas.isPending ||
                updateMeas.isPending ||
                renameImpactLoading ||
                (measType === "calculated" &&
                  (!measExpression.trim() ||
                    validationLoading ||
                    (validationResult != null && !validationResult.valid)))
              }
            >
              {createMeas.isPending || updateMeas.isPending ? (
                <CircularProgress size={18} />
              ) : editingMeasureId ? t("common.save") : t("common.add")}
            </Button>
            {renameImpactError && (
              <Typography variant="caption" color="error">
                {renameImpactError}
              </Typography>
            )}
          </Box>
        </DialogActions>
      </Dialog>

      <MeasureRenameImpactDialog
        impact={renameImpact}
        open={Boolean(renameImpact)}
        onCancel={() => setRenameImpact(null)}
        onConfirm={() => {
          setRenameImpact(null);
          updateMeas.mutate();
        }}
      />

      <Dialog open={tvDialogOpen} onClose={() => setTvDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{t("measures.timeVariant.addTitle")}</DialogTitle>
        <DialogContent>
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("measures.baseMeasureLabel")}</InputLabel>
            <Select
              value={tvBaseMeasureId}
              label={t("measures.baseMeasureLabel")}
              onChange={(e) => {
                setTvBaseMeasureId(e.target.value);
                // F-015-23: eligibility is computed per base measure, so a kind
                // valid for the previous base may be ineligible for the new one.
                // Reset the kind selection (and its dependent fields) so the
                // picker re-evaluates against the newly fetched eligibility.
                setTvKind("");
                setTvN("");
                setTvName("");
                setTvDisplay("");
                setTvFormat("");
              }}
            >
              <MenuItem value="">{t("measures.selectBaseMeasure")}</MenuItem>
              {(measures.data ?? [])
                .filter((m) => !m.variant_kind && m.measure_type !== "calculated")
                .map((m) => (
                  <MenuItem key={m.id} value={m.id}>
                    {m.name}
                    {m.display_name && m.display_name !== m.name ? ` (${m.display_name})` : ""}
                  </MenuItem>
                ))}
            </Select>
          </FormControl>
          <FormControl fullWidth margin="dense">
            <InputLabel>{t("measures.variantTypeLabel")}</InputLabel>
            <Select
              value={tvKind}
              label={t("measures.variantTypeLabel")}
              disabled={!tvBaseMeasureId || tvAvailableVariants.isLoading}
              onChange={(e) => {
                const kind = e.target.value;
                setTvKind(kind);
                const base = (measures.data ?? []).find((m) => m.id === tvBaseMeasureId);
                if (base) {
                  setTvName(`${base.name}_${kind}`);
                  setTvDisplay(`${base.display_name || base.name} (${t(TIME_VARIANT_LABELS[kind] ?? kind)})`);
                }
                if (isParametricVariant(kind)) {
                  setTvN(TIME_VARIANT_DEFAULT_N[kind] ?? "");
                } else {
                  setTvN("");
                }
                // F-015-24: a ratio/percentage variant (yoy_growth_pct, …)
                // emits a decimal ratio, so default its format to percent
                // rather than inheriting the base measure's (e.g. currency).
                // Only set the default when the modeler has not chosen a
                // format yet; never override an explicit choice.
                setTvFormat((prev) =>
                  isRatioVariant(kind) && !prev
                    ? RATIO_VARIANT_DEFAULT_FORMAT
                    : prev,
                );
              }}
            >
              <MenuItem value="">{t("measures.selectVariant")}</MenuItem>
              {CANONICAL_TIME_VARIANT_NAMES.map((kind) => {
                // F-015-23: once eligibility has loaded, disable kinds the base
                // measure cannot support and append the reason inline. Before
                // the response arrives (or if it errors) all kinds stay
                // selectable — the submit-time 422 remains the backstop.
                const info = tvVariantInfo.get(kind);
                const loaded = tvAvailableVariants.data != null;
                const ineligible = loaded && info != null && !info.eligible;
                const exists = loaded && info != null && info.exists;
                const disabled = ineligible || exists;
                const suffix = exists
                  ? ` — ${t("variant.alreadyAdded")}`
                  : ineligible && info?.reason
                    ? ` — ${info.reason}`
                    : "";
                return (
                  <MenuItem key={kind} value={kind} disabled={disabled}>
                    {t(TIME_VARIANT_LABELS[kind] ?? kind)}
                    {TIME_VARIANTS_NEEDING_CALENDAR.has(kind) ? t("measures.requiresHierarchy") : ""}
                    {suffix}
                  </MenuItem>
                );
              })}
            </Select>
          </FormControl>
          {isParametricVariant(tvKind) && (
            <TextField
              label={t("measures.nLabel")}
              fullWidth
              margin="dense"
              type="number"
              value={tvN}
              onChange={(e) => setTvN(e.target.value ? Number(e.target.value) : "")}
              inputProps={{ min: 1 }}
              placeholder={String(TIME_VARIANT_DEFAULT_N[tvKind] ?? "")}
            />
          )}
          {tvBaseMeasureId && tvKind && (() => {
            const needsCal = TIME_VARIANTS_NEEDING_CALENDAR.has(tvKind as TimeVariantKind);
            const calendarAliases = (allTables.data ?? []).filter(
              (tbl) => !!tbl.calendar_table_id,
            );
            const dateHierarchies = (hierarchies.data ?? []).filter((h) =>
              h.dimension_kind === "time" ||
              h.type === "date_embedded" ||
              !!h.calendar_type,
            );
            const base = (measures.data ?? []).find((m) => m.id === tvBaseMeasureId);
            const baseHier = base?.hierarchy_id
              ? dateHierarchies.find((h) => h.id === base.hierarchy_id)
              : null;
            const baseCal = base?.calendar_model_table_id
              ? calendarAliases.find((tbl) => tbl.id === base.calendar_model_table_id)
              : null;
            return (
              <>
                <FormControl fullWidth margin="dense">
                  <InputLabel>{t("measures.hierarchyLabel")}</InputLabel>
                  <Select
                    value={tvHierarchyId}
                    label={t("measures.hierarchyLabel")}
                    onChange={(e) => setTvHierarchyId(e.target.value)}
                  >
                    <MenuItem value="">{baseHier ? t("measures.timeVariant.inheritHierarchy", { name: baseHier.name }) : t("common.none")}</MenuItem>
                    {dateHierarchies.map((h) => (
                      <MenuItem key={h.id} value={h.id}>
                        {h.name}
                        {h.calendar_type ? ` (${h.calendar_type})` : ""}
                      </MenuItem>
                    ))}
                  </Select>
                  <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
                    {t("measures.hierarchyHelp")}
                  </Typography>
                </FormControl>
                {calendarAliases.length > 0 && (
                  <FormControl fullWidth margin="dense">
                    <InputLabel>{t("measures.calendarAliasLabel")}</InputLabel>
                    <Select
                      value={tvCalendarModelTableId}
                      label={t("measures.calendarAliasLabel")}
                      onChange={(e) => setTvCalendarModelTableId(e.target.value)}
                    >
                      <MenuItem value="">{baseCal ? t("measures.timeVariant.inheritCalendar", { name: baseCal.alias ?? baseCal.display_name }) : t("common.none")}</MenuItem>
                      {calendarAliases.map((tbl) => (
                        <MenuItem key={tbl.id} value={tbl.id}>
                          {tbl.alias ?? tbl.display_name} ({tbl.physical_name})
                        </MenuItem>
                      ))}
                    </Select>
                    <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
                      {t("measures.calendarAliasHelp")}
                    </Typography>
                  </FormControl>
                )}
                {needsCal && !baseHier && !tvHierarchyId && (
                  <Alert severity="warning" sx={{ mt: 1 }}>
                    {t("measures.noHierarchyWarning", { name: base?.name ?? "" })}
                  </Alert>
                )}
              </>
            );
          })()}
          {tvBaseMeasureId && tvKind && (
            <>
              <TextField
                label={t("measures.nameLabel")}
                fullWidth
                margin="normal"
                value={tvName}
                onChange={(e) => setTvName(e.target.value)}
                helperText={t("measures.nameAutoHelp")}
              />
              <TextField
                label={t("common.displayName")}
                fullWidth
                margin="normal"
                value={tvDisplay}
                onChange={(e) => setTvDisplay(e.target.value)}
              />
              <TextField
                label={t("measures.descriptionLabel")}
                fullWidth
                margin="normal"
                multiline
                minRows={2}
                maxRows={4}
                value={tvDescription}
                onChange={(e) => setTvDescription(e.target.value)}
              />
              <TextField
                label={t("measures.displayFolder")}
                fullWidth
                margin="normal"
                value={tvFolder}
                onChange={(e) => setTvFolder(e.target.value)}
              />
              <FormControl fullWidth margin="dense">
                <InputLabel>{t("measures.formatLabel")}</InputLabel>
                <Select
                  value={tvFormat}
                  label={t("measures.formatLabel")}
                  onChange={(e) => setTvFormat(e.target.value as MeasureFormatToken | "")}
                >
                  <MenuItem value="">{t("measures.inheritFormat")}</MenuItem>
                  {MEASURE_FORMAT_TOKENS.map((token) => (
                    <MenuItem key={token} value={token}>
                      {t(MEASURE_FORMAT_LABELS[token])}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
            </>
          )}
          {createTv.isError && (
            <Alert severity="error" sx={{ mt: 1 }}>
              {extractApiError(createTv.error, t("measures.createVariantFailed"))}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setTvDialogOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => createTv.mutate()}
            disabled={!tvBaseMeasureId || !tvKind || !tvName || createTv.isPending}
          >
            {createTv.isPending ? <CircularProgress size={18} /> : t("common.add")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
