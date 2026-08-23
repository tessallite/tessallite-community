/**
 * MeasureQueryPanel — Phase 6.B pivot surface.
 *
 * Supports ordered multi-dimension nesting on rows and columns (up to
 * PIVOT_MAX_ROW_DIMS × PIVOT_MAX_COL_DIMS). SQL is auto-synthesised and
 * routed through the query-router; cells are client-pivoted, capped by
 * PIVOT_MAX_CELLS. Cell clicks open the drill-through drawer.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  AlertTitle,
  Box,
  Button,
  Chip,
  Collapse,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControl,
  FormControlLabel,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Stack,
  Switch,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import BookmarkIcon from "@mui/icons-material/Bookmark";
import BookmarkBorderIcon from "@mui/icons-material/BookmarkBorder";
import LinkIcon from "@mui/icons-material/Link";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import SaveIcon from "@mui/icons-material/Save";
import TableViewIcon from "@mui/icons-material/TableView";
import { ui } from "../../../theme/tokens";
import { rowSecurityDeniedAll } from "../../../utils/rowSecurity";
import { buildPivotViewConfig, parsePivotViewSortConfig, queryRouterApiClient, savedQueriesApi, pivotViewsApi, scratchpadApi, glossaryApi } from "../../../api/client";
import type { MeasureGlossaryInfo } from "./controls/MeasureInfoPopover";
import type { PivotSort, PivotView } from "../../../api/client";
import { useDimensions, useFieldCompatibility, useHierarchiesWithLevels, useMeasures, useModel } from "../../../api/hooks";
import { useBuilderStore } from "../../../store/builderStore";
import { useModelTranslations, translatedName } from "../../../hooks/useModelTranslations";
import type {
  Dimension,
  DrillableHierarchy,
  DrillThroughFilter,
  DrillThroughResponse,
  ExecuteResponse,
  HierarchyPathEntry,
  Measure,
} from "../../../api/types";
import PersonaPicker from "../../Persona/PersonaPicker";
import PickerBar from "./controls/PickerBar";
import FreshnessIndicator from "./controls/FreshnessIndicator";
import SlicerBar from "./controls/SlicerBar";
import PivotGrid, { type ConditionalFormat, type EmptyCellMode } from "./grid/PivotGrid";
import { resolvePivotSort } from "./grid/sortState";
import DrillThroughPanel, {
  type DrillPageSize,
} from "./drawer/DrillThroughPanel";
import CalcDrillThroughDrawer from "./drawer/CalcDrillThroughDrawer";
import ExportMenu from "./export/ExportMenu";
import { buildPivotSql } from "./sql";
import { computePivot, evaluatePivotCompatibility } from "./pivot";
import { routeBadgeLabel } from "./routeLabels";
import { computeTotals } from "./totals";
import type { TotalsModel } from "./totals";
import {
  needsServerSubtotals,
  grainSpecsFor,
  buildGrainSql,
  assembleServerTotals,
  type GrainSpec,
} from "./subtotalRequery";
import {
  buildColumnMeasures,
  recordCountMeasure,
  RECORD_COUNT_ID,
  type MeasureSel,
  type PivotColumnMeasure,
} from "./measureColumns";
import type { CellCoord, DrillContext, Slicer } from "./types";
import {
  buildDrillInvocation,
  buildInitialGroupingLevels as createInitialGroupingLevels,
  buildSlicerFilters as createSlicerFilters,
} from "./drillRequest";
import { PIVOT_MAX_CELLS } from "./types";
import CalendarBindingHint from "../../CalendarBindingHint";
import UnsavedDeployWarning from "../../Builder/UnsavedDeployWarning";
import { useT } from "../../../i18n";
import PivotErrorAlert from "./PivotErrorAlert";
import { toPivotError, type PivotPanelError } from "./pivotErrors";
import {
  decodeSlicers,
  decodeConditionalFormat,
  decodeMeasureSelections,
  decodeEmptyCellMode,
  decodeBool,
  decodePersonaId,
} from "./pivotConfigDecode";

// Derive the ordered measure selections a saved view represents. New views
// store the authoritative ``config.measureSelections``; legacy views only have
// ``measure_id`` + ``extraMeasureIds`` + ``measureAggOverrides``. Both load
// (handleLoadView) and publish (handleToggleShare) go through this so their
// scratchpad/unresolved-measure detection uses identical selection derivation
// (Bug-6424).
function selectionsFromView(view: PivotView): MeasureSel[] {
  const cfg = view.config ?? {};
  // Bug-8161 (review B2): a malformed persisted config must not crash the loader
  // — buildColumnMeasures dereferences ``sel.measureId``, so a null/scalar entry
  // would throw. Sanitize every derivation path here rather than casting blindly.
  if ("measureSelections" in cfg && Array.isArray(cfg.measureSelections)) {
    return decodeMeasureSelections(cfg.measureSelections).value;
  }
  const extraIds =
    "extraMeasureIds" in cfg && Array.isArray(cfg.extraMeasureIds)
      ? (cfg.extraMeasureIds as unknown[]).filter((x): x is string => typeof x === "string")
      : [];
  const allIds = view.measure_id ? [view.measure_id, ...extraIds] : extraIds;
  const ao =
    "measureAggOverrides" in cfg &&
    cfg.measureAggOverrides &&
    typeof cfg.measureAggOverrides === "object" &&
    !Array.isArray(cfg.measureAggOverrides)
      ? (cfg.measureAggOverrides as Record<string, unknown>)
      : {};
  return allIds.map((id) => ({
    measureId: id,
    agg: typeof ao[id] === "string" ? (ao[id] as string) : "",
  }));
}

// Bug-5894 / Bug-6971: show a translated summary for known route types,
// but also surface the router's detailed reason string when available so
// the tooltip carries the specific routing decision (e.g., which aggregate
// was matched). Falls back to the raw reason for unrecognised route_type.
function routeReasonLabel(
  routeType: string,
  reason: string | undefined,
  t: (key: string, vars?: Record<string, string | number>) => string,
): string {
  const LOCALIZED: Record<string, string> = {
    source: "pivot.routeReasonSource",
    aggregate: "pivot.routeReasonAggregate",
    pocket: "pivot.routeReasonPocket",
  };
  const i18nKey = LOCALIZED[routeType];
  if (i18nKey) {
    const summary = t(i18nKey);
    return reason ? `${summary}\n${reason}` : summary;
  }
  return reason || t("pivot.routeTypeTooltip", { route: routeType });
}


export default function MeasureQueryPanel() {
  const t = useT();
  const { projectId, modelId } = useParams<{ projectId: string; modelId: string }>();
  const model = useModel(projectId ?? "", modelId ?? "");
  const measures = useMeasures(projectId ?? "", modelId ?? "");
  const dimensions = useDimensions(projectId ?? "", modelId ?? "");

  const translations = useModelTranslations(projectId ?? "", modelId ?? "");

  const scratchpadQuery = useQuery({
    queryKey: ["scratchpad-measures", projectId, modelId],
    queryFn: () => scratchpadApi.list(projectId!, modelId!),
    enabled: Boolean(projectId && modelId),
  });

  // Bug-8102 / F-104-02: approved glossary definitions + synonyms so an analyst
  // can see what a measure means at selection time. Only approved entries are
  // surfaced to consumers; attachments link an entry to its measure(s).
  const glossaryQuery = useQuery({
    queryKey: ["glossary", projectId, modelId, "approved"],
    queryFn: () => glossaryApi.list(projectId!, modelId!, "approved"),
    enabled: Boolean(projectId && modelId),
  });

  const glossaryByMeasureId = useMemo(() => {
    const map = new Map<string, MeasureGlossaryInfo>();
    for (const entry of glossaryQuery.data ?? []) {
      for (const att of entry.attachments ?? []) {
        if (att.target_type !== "measure" || !att.target_id) continue;
        // First approved entry wins; measures rarely have more than one.
        if (!map.has(att.target_id)) {
          map.set(att.target_id, {
            definition: entry.definition,
            synonyms: entry.synonyms ?? [],
          });
        }
      }
    }
    return map;
  }, [glossaryQuery.data]);

  const localizedMeasures = useMemo(() => {
    const raw = measures.data ?? [];
    const translated = translations.data?.length
      ? raw.map((m) => ({
          ...m,
          display_name: translatedName(translations.data, "measure", m.id, "display_name", m.display_name),
        }))
      : raw;
    const scratchpad = (scratchpadQuery.data ?? []).map((s) => ({
      id: s.id,
      name: s.name,
      display_name: t("pivot.scratchpadSuffix", { name: s.display_name || s.name }),
      expression: s.expression,
      default_agg: "SUM",
      data_type: s.data_type,
      format: null,
      measure_type: "calculated",
      source_column_id: null,
      source_column_name: null,
      source_table_id: null,
      user_defined_attribute_id: null,
      user_defined_attribute_name: null,
      is_additive: false,
      _scratchpad: true,
    })) as (Measure & { _scratchpad: boolean })[];
    return [...translated, ...scratchpad];
  }, [measures.data, translations.data, scratchpadQuery.data, t]);

  // Ids of the model's real, persistable measures (excludes the synthetic
  // Record Count and per-user scratchpad measures). Used to derive the legacy
  // primary ``measure_id`` pointer for a saved view (Bug-6412) and to detect
  // unresolvable measures on load (Bug-6424).
  const realMeasureIds = useMemo(
    () => new Set((measures.data ?? []).map((m) => m.id)),
    [measures.data],
  );
  // Ids of the current user's personal scratchpad measures. A shared view must
  // not embed these — other users cannot resolve them (Bug-6424).
  const scratchpadIds = useMemo(
    () => new Set((scratchpadQuery.data ?? []).map((s) => s.id)),
    [scratchpadQuery.data],
  );
  const selectionsIncludeScratchpad = (sels: MeasureSel[]): boolean =>
    sels.some((s) => scratchpadIds.has(s.measureId));
  // The first selection that maps to a real model measure, used as the saved
  // view's legacy ``measure_id`` pointer. Record Count / scratchpad / empty
  // selections yield "" — the authoritative list lives in config.
  const primaryModelMeasureId = (sels: MeasureSel[]): string =>
    sels.find((s) => realMeasureIds.has(s.measureId))?.measureId ?? "";

  const localizedDimensions = useMemo(() => {
    const raw = dimensions.data ?? [];
    if (!translations.data?.length) return raw;
    return raw.map((d) => ({
      ...d,
      display_name: translatedName(translations.data, "dimension", d.id, "display_name", d.display_name ?? d.name),
    }));
  }, [dimensions.data, translations.data]);

  // Bug-935: hidden dimensions cannot bind in SELECT (Rows/Columns) — the binder
  // runs the business view (include_hidden=False) and rejects them with HTTP 422.
  // They remain valid as slicers/filters (WHERE), so exclude hidden dims from the
  // Rows/Columns picker only; the slicer picker keeps the full list.
  const visibleDimensions = useMemo(
    () => localizedDimensions.filter((d) => !d.is_hidden),
    [localizedDimensions],
  );

  const pivotState = useBuilderStore((s) => s.pivotState);
  const setPivotState = useBuilderStore((s) => s.setPivotState);

  const [measureSelections, setMeasureSelections] = useState<MeasureSel[]>(
    pivotState.measureSelections?.length
      ? pivotState.measureSelections
      : pivotState.measureId
        ? [{ measureId: pivotState.measureId, agg: "" }]
        : [],
  );
  const [rowDimIds, setRowDimIds] = useState<string[]>(pivotState.rowDimIds);
  const [colDimIds, setColDimIds] = useState<string[]>(pivotState.colDimIds);
  const [slicers, setSlicers] = useState<Slicer[]>([]);

  const [executing, setExecuting] = useState(false);
  const [setupExpanded, setSetupExpanded] = useState(!pivotState.executeResult);
  const [executeResult, setExecuteResult] = useState<ExecuteResponse | null>(
    pivotState.executeResult as ExecuteResponse | null,
  );
  // Structured so a load/save/run failure can lead with a friendly, mapped
  // message and keep raw backend/transport text behind an accordion (Bug-8182).
  // Only the friendly message persists to pivotState (a string); the raw detail
  // is transient and re-derived on the next failure.
  const [error, setError] = useState<PivotPanelError | null>(
    pivotState.error ? { message: pivotState.error } : null,
  );
  // Bug-8453 / R5 finding F3: DERIVED, never stored. `executeResult` is
  // persisted in pivotState so results survive panel navigation; a separate
  // useState for the denial verdict did not, so returning to the panel restored
  // the result with rowSecurityDenied back to false -- the warning vanished AND
  // the suppressed pivot came back, rendering the `WHERE 0 = 1` zero as an
  // authoritative grand total. Deriving it makes that desync impossible, and
  // matches how the sibling QueryPanel already does it.
  const rowSecurityDenied = rowSecurityDeniedAll(executeResult);
  const abortRef = useRef<AbortController | null>(null);
  // F-019-02 (Bug-8046): server-computed non-additive subtotals, keyed by
  // measure name. Populated by the supplementary-grain effect below; merged into
  // allTotals so AVG/MIN/MAX/COUNT DISTINCT/calculated measures show real
  // subtotals instead of an em-dash. A separate abort controller cancels stale
  // supplementary fetches when the pivot changes.
  const [serverTotals, setServerTotals] = useState<Map<string, TotalsModel>>(
    () => new Map(),
  );
  const subtotalAbortRef = useRef<AbortController | null>(null);

  const [searchParams, setSearchParams] = useSearchParams();
  const hydratedRef = useRef(false);
  useEffect(() => {
    if (hydratedRef.current) return;
    hydratedRef.current = true;
    const qms = searchParams.get("ms");
    const qm = searchParams.get("m");
    const qr = searchParams.get("r");
    const qc = searchParams.get("c");
    const qem = searchParams.get("em");
    const qao = searchParams.get("ao");
    const qsl = searchParams.get("sl");
    const qsub = searchParams.get("sub");
    const qgt = searchParams.get("gt");
    const qecm = searchParams.get("ecm");
    const qcf = searchParams.get("cf");
    const qfl = searchParams.get("fl");
    const qpid = searchParams.get("pid");
    if (qms) {
      try {
        const parsed = JSON.parse(qms) as MeasureSel[];
        if (Array.isArray(parsed)) setMeasureSelections(parsed);
      } catch { /* ignore malformed */ }
    } else if (qm || qem) {
      // Back-compat: build selections from legacy m/em/ao params.
      const ids = qm ? [qm] : [];
      if (qem) ids.push(...qem.split(",").filter(Boolean));
      let ao: Record<string, string> = {};
      if (qao) { try { ao = JSON.parse(qao); } catch { /* ignore */ } }
      setMeasureSelections(ids.map((id) => ({ measureId: id, agg: ao[id] ?? "" })));
    }
    if (qr) setRowDimIds(qr.split(",").filter(Boolean));
    if (qc) setColDimIds(qc.split(",").filter(Boolean));
    if (qsl) { try { setSlicers(JSON.parse(qsl)); } catch { /* ignore malformed */ } }
    if (qsub === "1") setShowSubtotals(true);
    if (qgt === "0") setShowGrandTotals(false);
    if (qecm === "zero" || qecm === "dash") setEmptyCellMode(qecm);
    if (qcf) { try { setConditionalFormat(JSON.parse(qcf)); } catch { /* ignore malformed */ } }
    // Bug-5712: restore forceLive and personaId from shareable link params
    if (qfl === "1") setForceLive(true);
    if (qpid) setPersonaId(qpid);
    if (qms || qm || qr || qc || qem) {
      const next = new URLSearchParams(searchParams);
      ["ms", "m", "r", "c", "em", "ao", "sl", "sub", "gt", "ecm", "cf", "fl", "pid", "tab"].forEach((k) => next.delete(k));
      setSearchParams(next, { replace: true });
    }
  }, [searchParams, setSearchParams]);

  useEffect(() => {
    setPivotState({
      measureId: measureSelections[0]?.measureId ?? "",
      measureSelections,
      rowDimIds,
      colDimIds,
      executeResult,
      // Persist only the friendly message; the raw detail is transient.
      error: error?.message ?? null,
    });
  }, [measureSelections, rowDimIds, colDimIds, executeResult, error, setPivotState]);

  useEffect(() => {
    return () => { abortRef.current?.abort(); };
  }, []);
  const [lastSql, setLastSql] = useState<string | null>(null);
  const [forceLive, setForceLive] = useState(false);
  const [personaId, setPersonaId] = useState<string | null>(null);
  const [showSubtotals, setShowSubtotals] = useState(false);
  const [showGrandTotals, setShowGrandTotals] = useState(true);
  const [displayOptionsOpen, setDisplayOptionsOpen] = useState(false);
  const [emptyCellMode, setEmptyCellMode] = useState<EmptyCellMode>("blank");
  const [conditionalFormat, setConditionalFormat] = useState<ConditionalFormat>({ kind: "none" });
  const [pivotSort, setPivotSort] = useState<PivotSort | null>(null);
  const [sortNotice, setSortNotice] = useState<string | null>(null);
  // F-019-08: the grid's current sorted row order, so the export honours the
  // user's header-click sort instead of the pivot's default order.
  const [exportRowOrder, setExportRowOrder] = useState<string[][] | null>(null);

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [calcDrawerOpen, setCalcDrawerOpen] = useState(false);
  const [drillLoading, setDrillLoading] = useState(false);
  const [drillResult, setDrillResult] = useState<DrillThroughResponse | null>(null);
  // Bug-8182 (review B4): structured so the primary drill surface leads with a
  // friendly message and keeps raw backend/transport text collapsed.
  const [drillError, setDrillError] = useState<PivotPanelError | null>(null);
  const [drillContext, setDrillContext] = useState<DrillContext | null>(null);
  const [drillPageSize, setDrillPageSize] = useState<DrillPageSize>(50);
  // Stack of cursors for every page we've loaded except the current one.
  // push on Next, pop on Prev; `null` marks "page 1" (no cursor).
  const [drillCursorStack, setDrillCursorStack] = useState<(string | null)[]>([]);
  // Accumulated grouping levels for hierarchy drill-down (deeper levels
  // add entries as the user clicks rows).
  const [drillGroupingLevels, setDrillGroupingLevels] = useState<DrillThroughFilter[]>([]);
  const [drillHierarchyId, setDrillHierarchyId] = useState<string | null>(null);
  const [drillHierarchyOptions, setDrillHierarchyOptions] = useState<DrillableHierarchy[]>([]);
  const [drillOverrideAgg, setDrillOverrideAgg] = useState<string | null>(null);
  const hierarchiesWithLevels = useHierarchiesWithLevels(projectId ?? "", modelId ?? "");

  type DrillPath = { dimName: string; value: string };
  const [hierarchyDrillPath, setHierarchyDrillPath] = useState<DrillPath[]>([]);
  // Bumped by in-grid hierarchy drill/drill-up to trigger an automatic re-run once
  // rowDimIds/slicers have committed; without it the grid recomputes against the
  // stale result and renders "(null)" row labels.
  const [drillRunToken, setDrillRunToken] = useState(0);

  const qc = useQueryClient();
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [saveDesc, setSaveDesc] = useState("");
  const saveMutation = useMutation({
    mutationFn: (data: { name: string; description?: string; query_text: string }) =>
      savedQueriesApi.create(projectId!, modelId!, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["savedQueries", projectId, modelId] });
      setSaveOpen(false);
      setSaveName("");
      setSaveDesc("");
    },
  });

  // Saved pivot views
  const [viewMenuOpen, setViewMenuOpen] = useState(false);
  const [viewSaveName, setViewSaveName] = useState("");
  const [viewSaveShared, setViewSaveShared] = useState(false);
  const [savedViews, setSavedViews] = useState<PivotView[]>([]);

  useEffect(() => {
    if (projectId && modelId) {
      pivotViewsApi
        .list(projectId, modelId)
        .then(setSavedViews)
        .catch((err) => setError(toPivotError(err, t, t("errors.requestFailed"))));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, modelId]);

  const handleSaveView = async () => {
    if (!projectId || !modelId || !viewSaveName.trim()) return;
    // Bug-6424: a shared view must not embed personal scratchpad measures —
    // other users cannot resolve them, so the view would silently degrade.
    // Surface it and block the shared save rather than persisting a broken view.
    if (viewSaveShared && selectionsIncludeScratchpad(measureSelections)) {
      setError({ message: t("pivot.shareScratchpadBlocked") });
      return;
    }
    const sortIdentityColumns = pivotSort?.target.kind === "column"
      ? [pivotSort.target.columnKey]
      : [];
    const sortIdentityValid = !pivotSort || Boolean(
      resolvePivotSort(pivotSort, columnMeasures, sortIdentityColumns),
    );
    const sortTargetValid = !pivotSort || !pivot || Boolean(
      resolvePivotSort(pivotSort, columnMeasures, pivot.colKeys),
    );
    const sortForSave = sortIdentityValid && sortTargetValid ? pivotSort : null;
    if (pivotSort && !sortForSave) {
      setPivotSort(null);
      setSortNotice(t("pivot.savedSortUnavailable"));
    }
    try {
      await pivotViewsApi.create(projectId, modelId, {
        name: viewSaveName.trim(),
        // Bug-6412: the legacy primary pointer must be a real model measure or
        // empty; Record Count / scratchpad / no first measure would otherwise
        // fail server-side validation. The full selection travels in config.
        measure_id: primaryModelMeasureId(measureSelections),
        row_dim_ids: pivotState.rowDimIds,
        col_dim_ids: pivotState.colDimIds,
        config: buildPivotViewConfig({
          emptyCellMode,
          showSubtotals,
          showGrandTotals,
          measureSelections,
          slicers,
          conditionalFormat,
          forceLive,
          personaId,
        }, sortForSave),
        is_shared: viewSaveShared,
      });
      const views = await pivotViewsApi.list(projectId, modelId);
      setSavedViews(views);
      setViewSaveName("");
      setViewSaveShared(false);
      setViewMenuOpen(false);
    } catch (err) {
      // Pivot view save failures used to be unhandled rejections logged only to
      // the console; surface them in the existing error Alert (F-029-17).
      setError(toPivotError(err, t, t("errors.requestFailed")));
    }
  };

  const handleLoadView = (view: PivotView) => {
    const cfg = view.config ?? {};
    // Includes the legacy-format back-compat synthesis (Bug-6424 helper).
    const selections = selectionsFromView(view);
    setMeasureSelections(selections);
    setRowDimIds(view.row_dim_ids);
    setColDimIds(view.col_dim_ids);
    setPivotState({
      measureId: view.measure_id,
      measureSelections: selections,
      rowDimIds: view.row_dim_ids,
      colDimIds: view.col_dim_ids,
    });
    // Bug-8161 (review B2): decode every loader-consumed field so a historical
    // malformed config (e.g. a shared view saved before the typed contract) is
    // coerced to a safe shape instead of crashing SlicerBar/PivotGrid on read.
    const slicersDecoded = decodeSlicers("slicers" in cfg ? cfg.slicers : undefined);
    const conditionalFormatDecoded = decodeConditionalFormat(
      "conditionalFormat" in cfg ? cfg.conditionalFormat : undefined,
    );
    const configSanitized = !slicersDecoded.ok || !conditionalFormatDecoded.ok;
    setEmptyCellMode(decodeEmptyCellMode("emptyCellMode" in cfg ? cfg.emptyCellMode : undefined));
    setShowSubtotals(decodeBool("showSubtotals" in cfg ? cfg.showSubtotals : undefined, false));
    setShowGrandTotals(decodeBool("showGrandTotals" in cfg ? cfg.showGrandTotals : undefined, true));
    setSlicers(slicersDecoded.value);
    setConditionalFormat(conditionalFormatDecoded.value);
    const parsedSort = parsePivotViewSortConfig(cfg);
    const loadedColumns = buildColumnMeasures(selections, availableMeasures, t);
    const identityColumns = parsedSort.sort?.target.kind === "column"
      ? [parsedSort.sort.target.columnKey]
      : [];
    const loadedSort = parsedSort.sort && resolvePivotSort(parsedSort.sort, loadedColumns, identityColumns)
      ? parsedSort.sort
      : null;
    setPivotSort(loadedSort);
    setSortNotice(
      parsedSort.issue === "unsupported-version"
        ? t("pivot.savedSortUnsupportedVersion")
        : parsedSort.issue === "invalid-sort" || (parsedSort.sort && !loadedSort)
          ? t("pivot.savedSortUnavailable")
          : null,
    );
    // Bug-5712: restore forceLive and personaId from saved view config
    setForceLive(decodeBool("forceLive" in cfg ? cfg.forceLive : undefined, false));
    setPersonaId(decodePersonaId("personaId" in cfg ? cfg.personaId : undefined));
    setHierarchyDrillPath([]);
    setViewMenuOpen(false);
    // Review B2: if a historical config carried malformed loader fields we had to
    // drop, tell the user the view was partially recovered (the specific
    // missing-measures notice below takes precedence when it also applies).
    if (configSanitized) {
      setError({ message: t("pivot.loadViewInvalidConfig") });
    }
    // Bug-6424: a shared view authored by another user may reference measures
    // this user cannot see (their scratchpad measures, or measures removed
    // since). Detect the unresolvable selections and surface them rather than
    // letting the corresponding columns vanish silently.
    const unresolved = selections.filter(
      (s) =>
        s.measureId &&
        s.measureId !== RECORD_COUNT_ID &&
        !realMeasureIds.has(s.measureId) &&
        !scratchpadIds.has(s.measureId),
    );
    if (unresolved.length > 0) {
      setError({ message: t("pivot.loadViewMissingMeasures", { count: String(unresolved.length) }) });
    }
  };

  const handleInvalidPivotSort = useCallback(() => {
    setPivotSort(null);
    setSortNotice(t("pivot.savedSortUnavailable"));
  }, [t]);

  const handlePivotSortChange = useCallback((next: PivotSort | null) => {
    setPivotSort(next);
    setSortNotice(null);
  }, []);

  const handleDeleteView = async (viewId: string) => {
    if (!projectId || !modelId) return;
    try {
      await pivotViewsApi.delete(projectId, modelId, viewId);
      setSavedViews((prev) => prev.filter((v) => v.id !== viewId));
    } catch (err) {
      setError(toPivotError(err, t, t("errors.requestFailed")));
    }
  };

  // Owner-only: publish a personal view to the tenant or pull it back (F-029-22).
  const handleToggleShare = async (view: PivotView) => {
    if (!projectId || !modelId) return;
    // Bug-6424: publishing (personal -> shared) must not expose a view that
    // embeds this user's scratchpad measures; other users cannot resolve them.
    // Derive selections the same way load does (incl. legacy-format views) so
    // publish-time and load-time detection stay consistent.
    if (!view.is_shared && selectionsIncludeScratchpad(selectionsFromView(view))) {
      setError({ message: t("pivot.shareScratchpadBlocked") });
      return;
    }
    try {
      const updated = await pivotViewsApi.update(projectId, modelId, view.id, {
        is_shared: !view.is_shared,
      });
      setSavedViews((prev) => prev.map((v) => (v.id === view.id ? updated : v)));
    } catch (err) {
      setError(toPivotError(err, t, t("errors.requestFailed")));
    }
  };

  const handleCopyLink = () => {
    const params = new URLSearchParams();
    if (measureSelections.length) params.set("ms", JSON.stringify(measureSelections));
    if (pivotState.rowDimIds.length) params.set("r", pivotState.rowDimIds.join(","));
    if (pivotState.colDimIds.length) params.set("c", pivotState.colDimIds.join(","));
    if (slicers.length) params.set("sl", JSON.stringify(slicers));
    if (showSubtotals) params.set("sub", "1");
    if (!showGrandTotals) params.set("gt", "0");
    if (emptyCellMode !== "blank") params.set("ecm", emptyCellMode);
    if (conditionalFormat.kind !== "none") params.set("cf", JSON.stringify(conditionalFormat));
    // Bug-5712: include execution mode and persona in shareable links
    if (forceLive) params.set("fl", "1");
    if (personaId) params.set("pid", personaId);
    params.set("tab", "pivot");
    const url = `${window.location.origin}${window.location.pathname}?${params}`;
    navigator.clipboard.writeText(url).catch(() => {});
  };

  const dimsById = useMemo(() => {
    const m = new Map<string, Dimension>();
    for (const d of dimensions.data ?? []) m.set(d.id, d);
    return m;
  }, [dimensions.data]);

  const dimsByName = useMemo(() => {
    const m = new Map<string, Dimension>();
    for (const d of dimensions.data ?? []) m.set(d.name, d);
    return m;
  }, [dimensions.data]);

  const hierarchyChildMap = useMemo(() => {
    const map = new Map<string, { childDimName: string; hierarchyName: string }>();
    for (const h of hierarchiesWithLevels.data ?? []) {
      const levels = h.levels ?? [];
      for (let i = 0; i < levels.length - 1; i++) {
        const parentAttr = levels[i].key_attribute;
        const childAttr = levels[i + 1].key_attribute;
        if (parentAttr?.name && childAttr?.name) {
          map.set(parentAttr.name, {
            childDimName: childAttr.name,
            hierarchyName: h.name,
          });
        }
      }
    }
    return map;
  }, [hierarchiesWithLevels.data]);

  // Available base measures (real + synthetic Record Count) for the picker.
  const availableMeasures = useMemo<Measure[]>(
    () => [recordCountMeasure(t), ...localizedMeasures],
    [localizedMeasures, t],
  );

  // Expand ordered (measure, agg) selections into synthetic column measures
  // whose `.name` is a unique alias so pivot/totals/grid keying works unchanged.
  const columnMeasures = useMemo<PivotColumnMeasure[]>(
    () => buildColumnMeasures(measureSelections, availableMeasures, t),
    [measureSelections, availableMeasures, t],
  );
  const compatibilityMeasureIds = useMemo(
    () => measureSelections
      .map((selection) => selection.measureId)
      .filter((id) => id && id !== RECORD_COUNT_ID),
    [measureSelections],
  );
  const compatibilityDimensionIds = useMemo(
    () => [
      ...new Set([
        ...visibleDimensions.map((dimension) => dimension.id),
        ...localizedDimensions.map((dimension) => dimension.id),
        ...rowDimIds,
        ...colDimIds,
        ...slicers.map((slicer) => slicer.dimensionId),
      ].filter(Boolean)),
    ],
    [visibleDimensions, localizedDimensions, rowDimIds, colDimIds, slicers],
  );
  const fieldCompatibility = useFieldCompatibility(
    projectId ?? "",
    modelId ?? "",
    personaId,
    compatibilityMeasureIds,
    compatibilityDimensionIds,
  );
  const pivotCompatibility = useMemo(
    () => evaluatePivotCompatibility({
      measureIds: compatibilityMeasureIds,
      rowDimIds,
      colDimIds,
      slicers,
      dimensions: localizedDimensions,
      matrix: fieldCompatibility.data,
    }),
    [compatibilityMeasureIds, rowDimIds, colDimIds, slicers, localizedDimensions, fieldCompatibility.data],
  );
  const selectedMeasure = columnMeasures[0] ?? null;
  const extraMeasures = useMemo<PivotColumnMeasure[]>(
    () => columnMeasures.slice(1),
    [columnMeasures],
  );
  const rowDims = useMemo<Dimension[]>(
    () => rowDimIds.map((id) => dimsById.get(id)).filter((d): d is Dimension => !!d),
    [rowDimIds, dimsById],
  );
  const colDims = useMemo<Dimension[]>(
    () => colDimIds.map((id) => dimsById.get(id)).filter((d): d is Dimension => !!d),
    [colDimIds, dimsById],
  );

  const drillableRowDims = useMemo(() => {
    const set = new Set<string>();
    for (const d of rowDims) {
      if (hierarchyChildMap.has(d.name)) {
        const child = hierarchyChildMap.get(d.name)!;
        if (dimsByName.has(child.childDimName)) {
          set.add(d.name);
        }
      }
    }
    return set;
  }, [rowDims, hierarchyChildMap, dimsByName]);

  // dimName -> hierarchy name, so the grid can hint which hierarchy a row drills through.
  const drillHierarchyNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const d of rowDims) {
      const child = hierarchyChildMap.get(d.name);
      if (child && dimsByName.has(child.childDimName)) {
        map.set(d.name, child.hierarchyName);
      }
    }
    return map;
  }, [rowDims, hierarchyChildMap, dimsByName]);

  function handleRowDimsChange(ids: string[]) {
    setRowDimIds(ids);
    setColDimIds((prev) => prev.filter((id) => !ids.includes(id)));
    setHierarchyDrillPath([]);
  }

  function keepOnlyDimensionIds(ids: string[]) {
    const keep = new Set(ids);
    setRowDimIds((prev) => prev.filter((id) => keep.has(id)));
    setColDimIds((prev) => prev.filter((id) => keep.has(id)));
    setSlicers((prev) => prev.filter((s) => keep.has(s.dimensionId)));
  }

  function removeDimensionIds(ids: string[]) {
    const remove = new Set(ids);
    setRowDimIds((prev) => prev.filter((id) => !remove.has(id)));
    setColDimIds((prev) => prev.filter((id) => !remove.has(id)));
    setSlicers((prev) => prev.filter((s) => !remove.has(s.dimensionId)));
  }

  function handleKeepCommonDimensions() {
    keepOnlyDimensionIds(pivotCompatibility.commonDimensionIds);
  }

  function handleRemoveIncompatibleDimensions() {
    removeDimensionIds(pivotCompatibility.incompatibleDimensionIds);
  }

  function handleSplitIntoSeparatePivots() {
    const firstSelection = measureSelections[0];
    if (!firstSelection) return;
    setMeasureSelections([firstSelection]);
    const firstCompatibleIds =
      fieldCompatibility.data?.measures[firstSelection.measureId]?.compatible_dimension_ids;
    if (firstCompatibleIds) keepOnlyDimensionIds(firstCompatibleIds);
  }

  function handleHierarchyDrill(dimName: string, value: string, rawValue?: unknown) {
    const child = hierarchyChildMap.get(dimName);
    if (!child) return;
    const childDim = dimsByName.get(child.childDimName);
    if (!childDim) return;

    setHierarchyDrillPath((prev) => [...prev, { dimName, value }]);

    setRowDimIds((prev) => {
      const parentDim = [...(dimensions.data ?? [])].find((d) => d.name === dimName);
      if (!parentDim) return prev;
      const idx = prev.indexOf(parentDim.id);
      if (idx === -1) return [...prev, childDim.id];
      const next = [...prev];
      next[idx] = childDim.id;
      return next;
    });

    // F-019-11: pin the raw cell value, not the rendered label. A null row
    // becomes an ``is_null`` slicer (was WHERE col = '(null)' → zero rows);
    // a numeric row pins its number (was the engine-dependent string '2024').
    const dimId = [...(dimensions.data ?? [])].find((d) => d.name === dimName)?.id ?? "";
    const isNullValue = rawValue === null || rawValue === undefined;
    const slicer: Slicer = isNullValue
      ? { dimensionId: dimId, op: "is_null", values: [] }
      : { dimensionId: dimId, op: "eq", values: [String(rawValue)] };

    setSlicers((prev) => [
      ...prev.filter((s) => {
        const d = dimsById.get(s.dimensionId);
        return d?.name !== dimName;
      }),
      slicer,
    ]);

    setDrillRunToken((n) => n + 1);
  }

  function handleHierarchyDrillUp(targetIndex: number) {
    const path = hierarchyDrillPath.slice(0, targetIndex);
    setHierarchyDrillPath(path);

    if (path.length === 0) {
      const firstDrillDimName = hierarchyDrillPath[0]?.dimName;
      if (firstDrillDimName) {
        const origDim = dimsByName.get(firstDrillDimName);
        if (origDim) {
          setRowDimIds((prev) => {
            const currentChildName = hierarchyDrillPath[hierarchyDrillPath.length - 1]?.dimName;
            const childInfo = currentChildName ? hierarchyChildMap.get(currentChildName) : null;
            const currentChildDim = childInfo ? dimsByName.get(childInfo.childDimName) : null;
            if (currentChildDim) {
              return prev.map((id) => id === currentChildDim.id ? origDim.id : id);
            }
            return prev;
          });
        }
      }
      setSlicers((prev) =>
        prev.filter((s) => !hierarchyDrillPath.some((p) => {
          const d = dimsById.get(s.dimensionId);
          return d?.name === p.dimName;
        }))
      );
    } else {
      const lastStep = path[path.length - 1];
      const childInfo = hierarchyChildMap.get(lastStep.dimName);
      if (childInfo) {
        const childDim = dimsByName.get(childInfo.childDimName);
        if (childDim) {
          setRowDimIds((prev) => {
            const currentLeafName = hierarchyDrillPath[hierarchyDrillPath.length - 1]?.dimName;
            const currentLeafChild = currentLeafName ? hierarchyChildMap.get(currentLeafName) : null;
            const currentLeafDim = currentLeafChild ? dimsByName.get(currentLeafChild.childDimName) : null;
            if (currentLeafDim) {
              return prev.map((id) => id === currentLeafDim.id ? childDim.id : id);
            }
            return prev;
          });
        }
      }
      setSlicers((prev) => {
        const drillDimNames = new Set(hierarchyDrillPath.map((p) => p.dimName));
        const keepDimNames = new Set(path.map((p) => p.dimName));
        return prev.filter((s) => {
          const d = dimsById.get(s.dimensionId);
          if (!d) return true;
          return !drillDimNames.has(d.name) || keepDimNames.has(d.name);
        });
      });
    }

    setDrillRunToken((n) => n + 1);
  }

  async function handleRun() {
    if (!modelId || !selectedMeasure || !model.data) return;
    if (
      compatibilityLoading ||
      compatibilityUnavailable ||
      pivotCompatibility.hasVerifiedIncompatibilities
    ) return;
    const sql = buildPivotSql(
      model.data,
      columnMeasures,
      rowDims,
      colDims,
      slicers,
      dimensions.data ?? [],
    );
    setLastSql(sql);
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setError(null);
    setExecuteResult(null);
    setExecuting(true);
    try {
      const result = await queryRouterApiClient.execute(
        {
          model_id: modelId,
          raw_query: sql,
          dialect: "postgresql",
          ...(forceLive ? { force_route: "source" as const } : {}),
        },
        personaId,
        controller.signal,
      );
      if (!controller.signal.aborted) {
        // Bug-8453: a row-security deny-all returns HTTP 200 with zero rows,
        // which this pivot rendered as an ordinary empty result. The denial is
        // derived from the stored result (see rowSecurityDenied above), so it
        // survives panel navigation with it.
        setExecuteResult(result);
        setSetupExpanded(false);
      }
    } catch (err) {
      if (!controller.signal.aborted) setError(toPivotError(err, t, t("errors.requestFailed")));
    } finally {
      if (!controller.signal.aborted) setExecuting(false);
    }
  }

  // Re-run the query after an in-grid hierarchy drill/drill-up. The token is bumped
  // in the same state batch as rowDimIds/slicers, so by the time this effect fires
  // those values are committed and handleRun reads the fresh child dimensions.
  useEffect(() => {
    if (drillRunToken === 0) return;
    void handleRun();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drillRunToken]);

  const pivot = useMemo(() => {
    if (!executeResult || !selectedMeasure) return null;
    // F-019-16: localized "(null)" label for null dimension members.
    return computePivot(executeResult, selectedMeasure, rowDims, colDims, extraMeasures, t("pivot.nullValue"));
  }, [executeResult, selectedMeasure, rowDims, colDims, extraMeasures, t]);

  const allTotals = useMemo<Map<string, import("./totals").TotalsModel | null>>(() => {
    const result = new Map<string, import("./totals").TotalsModel | null>();
    if (!pivot || (!showSubtotals && !showGrandTotals)) return result;
    const measures = selectedMeasure ? [selectedMeasure, ...extraMeasures] : [];
    for (const m of measures) {
      // F-019-02: a non-additive/non-composable measure uses the server-computed
      // grain totals when they have arrived; until then computeTotals renders the
      // NOT_ADDITIVE marker (em-dash), which the effect below replaces with real
      // numbers. Additive measures always use the fast client-side path.
      const server = needsServerSubtotals(m) ? serverTotals.get(m.name) : undefined;
      result.set(m.name, server ?? computeTotals(pivot, m));
    }
    return result;
  }, [pivot, selectedMeasure, extraMeasures, showSubtotals, showGrandTotals, serverTotals]);

  // F-019-02 (Bug-8046): fetch supplementary subtotal grains for non-additive
  // measures through the same execute route XMLA uses, so web and Excel agree on
  // AVG/MIN/MAX/COUNT DISTINCT/calculated subtotals. One bounded GROUP BY query
  // per grain shape; results assembled into a TotalsModel per measure.
  useEffect(() => {
    // Reset when the pivot or toggles change; recompute only when needed.
    if (!pivot || !model.data || (!showSubtotals && !showGrandTotals)) {
      subtotalAbortRef.current?.abort();
      setServerTotals((prev) => (prev.size ? new Map() : prev));
      return;
    }
    const measures = selectedMeasure ? [selectedMeasure, ...extraMeasures] : [];
    const targets = measures.filter((m) => needsServerSubtotals(m));
    if (targets.length === 0) {
      setServerTotals((prev) => (prev.size ? new Map() : prev));
      return;
    }
    subtotalAbortRef.current?.abort();
    // Fable R1 F5: clear stale server totals SYNCHRONOUSLY before the async
    // fetch starts, so the pivot never transiently shows old-pivot totals
    // against new-pivot detail cells. computeTotals then shows the
    // NOT_ADDITIVE marker until fresh grains arrive.
    setServerTotals(new Map());
    const controller = new AbortController();
    subtotalAbortRef.current = controller;
    const specs = grainSpecsFor(rowDims, colDims);
    const slicerDims = dimensions.data ?? [];

    (async () => {
      const next = new Map<string, TotalsModel>();
      for (const m of targets) {
        const results = new Map<GrainSpec["id"], import("../../../api/types").ExecuteResponse>();
        try {
          for (const spec of specs) {
            const sql = buildGrainSql(model.data!, m, spec, slicers, slicerDims);
            const res = await queryRouterApiClient.execute(
              {
                model_id: modelId!,
                raw_query: sql,
                dialect: "postgresql",
                ...(forceLive ? { force_route: "source" as const } : {}),
              },
              personaId,
              controller.signal,
            );
            if (controller.signal.aborted) return;
            // Bug-8453 / R3 finding B-2 [wrong numbers]: a denied grain is not
            // a measurement. COUNT/COALESCE-shaped subtotal grains over
            // ``WHERE 0 = 1`` return a literal 0, and assembleServerTotals
            // would render that as an authoritative grand total. Drop the
            // measure onto the same safe path an execution failure takes (the
            // NOT_ADDITIVE marker) rather than publishing a fabricated zero.
            if (rowSecurityDeniedAll(res)) {
              throw new Error("row_security_denied");
            }
            results.set(spec.id, res);
          }
        } catch {
          // A supplementary-grain failure leaves this measure without server
          // totals; computeTotals then shows the NOT_ADDITIVE marker (safe: no
          // wrong number). Do not fault the whole pivot.
          continue;
        }
        next.set(m.name, assembleServerTotals(pivot, m, results, t("pivot.nullValue")));
      }
      if (!controller.signal.aborted) setServerTotals(next);
    })();

    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pivot, model.data, selectedMeasure, extraMeasures, rowDims, colDims,
      slicers, showSubtotals, showGrandTotals, forceLive, personaId, modelId]);

  const cellCount = pivot
    ? Math.max(1, pivot.rowKeys.length) * Math.max(1, pivot.colKeys.length)
    : 0;
  const overCellCap = cellCount > PIVOT_MAX_CELLS;

  // The clicked measure may be a synthetic column measure; drill-through must
  // target the underlying real measure id, never the per-column alias.
  function realMeasureId(m: Measure): string {
    return (m as PivotColumnMeasure)._measureId ?? m.id;
  }

  // F-019-03: active slicers must travel with every drill so the drilled
  // rows reconcile with the clicked cell. Each slicer is keyed by
  // dimensionId; the drill contract wants the dimension *name* as `column`.
  // The slicer op set maps 1:1 to drill ops except `ne` → `neq`.
  function buildSlicerFilters(): DrillThroughFilter[] {
    return createSlicerFilters(slicers, dimsById);
  }

  async function fireDrill(
    drillMeasure: Measure,
    groupingLevels: DrillThroughFilter[],
    cursor: string | null,
    limit: number,
    hierarchyId: string | null,
    overrideAgg?: string | null,
  ) {
    if (drillMeasure.measure_type === "calculated") return;
    const invocation = buildDrillInvocation({
      measure: drillMeasure as PivotColumnMeasure,
      groupingLevels,
      filters: buildSlicerFilters(),
      limit,
      cursor,
      hierarchyId,
      forceLive,
      overrideAggregation: overrideAgg,
    });
    return queryRouterApiClient.drillThrough(
      invocation.measureId,
      invocation.request,
      personaId,
    );
  }

  async function loadDrillPage(
    drillMeasure: Measure,
    groupingLevels: DrillThroughFilter[],
    cursor: string | null,
    limit: number,
    hierarchyId: string | null,
    overrideAgg?: string | null,
  ) {
    setDrillError(null);
    setDrillLoading(true);
    try {
      const result = await fireDrill(drillMeasure, groupingLevels, cursor, limit, hierarchyId, overrideAgg);
      // Bug-8453 / R6 finding 1: this is the PRIMARY drill surface -- every
      // pivot-cell click lands here and feeds DrillThroughPanel. An RLS
      // deny-all returns zero detail rows, which rendered as "0 rows" and is
      // indistinguishable from "this cell has no underlying data": a claim
      // about the business, not about the analyst's access. R5 wired the
      // calculated-measure sub-drill (DrillMiniPanel) and the Excel drill
      // panel but missed this one; mirrors DrillMiniPanel's pattern exactly.
      // Safe to clear: loadDrillPage REPLACES the result on every page
      // (cursor pagination, no accumulation), so no already-shown rows are
      // discarded here.
      if (result && rowSecurityDeniedAll(result)) {
        setDrillResult(null);
        setDrillError({ message: t("query.rowSecurityDeniedBody") });
        return;
      }
      if (result) setDrillResult(result);
    } catch (err) {
      setDrillError(toPivotError(err, t, t("drill.loadFailed")));
    } finally {
      setDrillLoading(false);
    }
  }

  function buildInitialGroupingLevels(coord: CellCoord): DrillThroughFilter[] {
    return createInitialGroupingLevels(coord, rowDims, colDims);
  }

  async function handleCellClick(coord: CellCoord, clickedMeasure: Measure) {
    // Record-count columns have no underlying measure to drill into.
    if ((clickedMeasure as PivotColumnMeasure)._recordCount) return;
    setDrillContext({ measure: clickedMeasure, coord });
    if (clickedMeasure.measure_type === "calculated") {
      setCalcDrawerOpen(true);
      return;
    }
    const levels = buildInitialGroupingLevels(coord);
    setDrillGroupingLevels(levels);
    setDrillHierarchyId(null);
    setDrillHierarchyOptions([]);
    // Bug-7265: derive override_agg from the clicked column measure's _agg
    // when it differs from the measure's default_agg. Scratchpad and Record
    // Count columns never carry a meaningful override.
    const colMeasure = clickedMeasure as PivotColumnMeasure;
    const effectiveAgg = colMeasure._agg ?? null;
    const defaultAgg = (clickedMeasure.default_agg ?? "SUM").toUpperCase();
    const aggOverride =
      effectiveAgg && effectiveAgg !== defaultAgg && !colMeasure._scratchpad
        ? effectiveAgg
        : null;
    setDrillOverrideAgg(aggOverride);
    setDrawerOpen(true);
    setDrillResult(null);
    setDrillCursorStack([]);
    setDrillLoading(true);
    try {
      const opts = await queryRouterApiClient.drillOptions(
        realMeasureId(clickedMeasure),
        { grouping_levels: levels },
        personaId,
      );
      setDrillHierarchyOptions(opts.hierarchies);
      if (opts.hierarchies.length === 1) {
        const hid = opts.hierarchies[0].hierarchy_id;
        setDrillHierarchyId(hid);
        setDrillLoading(false);
        await loadDrillPage(clickedMeasure, levels, null, drillPageSize, hid, aggOverride);
      } else if (opts.hierarchies.length === 0) {
        setDrillLoading(false);
        await loadDrillPage(clickedMeasure, levels, null, drillPageSize, null, aggOverride);
      } else {
        // multiple hierarchies -> picker shown in drawer
        setDrillLoading(false);
      }
    } catch {
      setDrillLoading(false);
      await loadDrillPage(clickedMeasure, levels, null, drillPageSize, null, aggOverride);
    }
  }

  async function handleSelectHierarchy(hid: string) {
    if (!drillContext) return;
    setDrillHierarchyId(hid);
    await loadDrillPage(drillContext.measure, drillGroupingLevels, null, drillPageSize, hid, drillOverrideAgg);
  }

  async function handleDrillRow(
    row: Record<string, unknown>,
    hierarchyId: string,
    pathEntry: HierarchyPathEntry,
  ) {
    if (!drillContext) return;
    const newLevel: DrillThroughFilter = {
      column: pathEntry.dimension_name,
      op: "eq",
      value: pathEntry.value,
    };
    const nextLevels = [...drillGroupingLevels, newLevel];
    setDrillGroupingLevels(nextLevels);
    setDrillHierarchyId(hierarchyId);
    setDrillCursorStack([]);
    await loadDrillPage(drillContext.measure, nextLevels, null, drillPageSize, hierarchyId, drillOverrideAgg);
  }

  async function handleDrillNextPage() {
    if (!drillContext || !drillResult?.page.next_cursor) return;
    const currentCursor = drillResult.page.cursor || null;
    setDrillCursorStack((s) => [...s, currentCursor]);
    await loadDrillPage(
      drillContext.measure,
      drillGroupingLevels,
      drillResult.page.next_cursor,
      drillPageSize,
      drillHierarchyId,
      drillOverrideAgg,
    );
  }

  async function handleDrillPrevPage() {
    if (!drillContext || drillCursorStack.length === 0) return;
    const stack = [...drillCursorStack];
    const prevCursor = stack.pop() ?? null;
    setDrillCursorStack(stack);
    await loadDrillPage(drillContext.measure, drillGroupingLevels, prevCursor, drillPageSize, drillHierarchyId, drillOverrideAgg);
  }

  async function handleDrillPageSizeChange(size: DrillPageSize) {
    setDrillPageSize(size);
    if (!drillContext) return;
    setDrillCursorStack([]);
    await loadDrillPage(drillContext.measure, drillGroupingLevels, null, size, drillHierarchyId, drillOverrideAgg);
  }

  const isVariantMeasure = Boolean(selectedMeasure?.variant_kind);
  const grainDims = useMemo<Dimension[]>(
    () => [...rowDims, ...colDims],
    [rowDims, colDims],
  );
  const hasTimeDimInGrain = grainDims.some((d) => d.is_time_dim);
  const variantNeedsTimeDim = isVariantMeasure && !hasTimeDimInGrain;
  const compatibilityLoading =
    compatibilityMeasureIds.length > 0 && fieldCompatibility.isLoading;
  const compatibilityUnavailable =
    compatibilityMeasureIds.length > 0 &&
    (fieldCompatibility.isError || (!compatibilityLoading && !fieldCompatibility.data));

  const runDisabled =
    !selectedMeasure ||
    executing ||
    !model.data ||
    measures.isLoading ||
    dimensions.isLoading ||
    compatibilityLoading ||
    compatibilityUnavailable ||
    variantNeedsTimeDim ||
    pivotCompatibility.hasVerifiedIncompatibilities;

  const canRenderResults = Boolean(pivot && !overCellCap && selectedMeasure);
  const setupOpen = setupExpanded || !executeResult;
  const measureSummary = columnMeasures.length
    ? columnMeasures.map((m) => m.display_name || m.name).join(", ")
    : t("pickerBar.none");
  const compatibilityMessages = useMemo(
    () => [...new Set(pivotCompatibility.selectedIssues.map((issue) => issue.message))],
    [pivotCompatibility.selectedIssues],
  );
  const showMultiMeasureCompatibility =
    compatibilityMeasureIds.length > 1 &&
    (pivotCompatibility.hasVerifiedIncompatibilities || pivotCompatibility.noCommonDimensions);

  return (
    <Box sx={{ display: "flex", flexDirection: "column", height: "100%", p: 1, gap: 1 }}>
      <UnsavedDeployWarning />
      <CalendarBindingHint context="pivot" />

      <Paper
        variant="outlined"
        sx={{
          px: 1,
          py: 0.75,
          borderRadius: 1,
          bgcolor: "background.paper",
          display: "flex",
          flexDirection: "column",
          gap: setupOpen ? 0.75 : 0,
          flexShrink: 0,
        }}
      >
        <Stack direction="row" alignItems="center" justifyContent="space-between" gap={1} flexWrap="wrap">
          <Stack direction="row" gap={0.75} alignItems="center" flexWrap="wrap" minWidth={0}>
            <Typography variant="subtitle2" fontWeight={700}>
              {t("pivot.panelTitle")}
            </Typography>
            <Typography variant="caption" color="text.secondary" noWrap sx={{ maxWidth: 360 }}>
              {t("pivot.summaryMeasures", { measures: measureSummary })}
              {" · "}
              {t("pivot.summaryRows", { count: String(rowDimIds.length) })}
              {" · "}
              {t("pivot.summaryCols", { count: String(colDimIds.length) })}
              {slicers.length > 0 && (
                <>
                  {" · "}
                  {t("pivot.summaryFilters", { count: String(slicers.length) })}
                </>
              )}
            </Typography>
          </Stack>
          <Stack direction="row" gap={0.75} alignItems="center">
            {executeResult && (
              <Button size="small" variant="text" onClick={() => setSetupExpanded((v) => !v)}>
                {setupOpen ? t("pivot.hide") : t("pivot.edit")}
              </Button>
            )}
            <Button
              variant="contained"
              size="small"
              startIcon={<PlayArrowIcon fontSize="small" />}
              disabled={runDisabled}
              onClick={handleRun}
            >
              {executing ? t("pickerBar.running") : t("pickerBar.run")}
            </Button>
          </Stack>
        </Stack>

        <Collapse in={setupOpen} timeout="auto" unmountOnExit>
          <Stack spacing={0.75}>
            <Divider />
            {(pivotCompatibility.hasVerifiedIncompatibilities || showMultiMeasureCompatibility) && (
              <Alert severity={pivotCompatibility.hasVerifiedIncompatibilities ? "warning" : "info"} sx={{ py: 0.25 }}>
                <Stack spacing={0.75}>
                  {showMultiMeasureCompatibility ? (
                    <Typography variant="body2">
                      {t("pivotCompatibility.multiSummary")}
                    </Typography>
                  ) : (
                    compatibilityMessages.map((message) => (
                      <Typography key={message} variant="body2">
                        {message}
                      </Typography>
                    ))
                  )}

                  {showMultiMeasureCompatibility && (
                    <Stack spacing={0.75}>
                      {pivotCompatibility.commonDimensionNames.length > 0 ? (
                        <Typography variant="caption">
                          {t("pivotCompatibility.commonDimensions", {
                            names: pivotCompatibility.commonDimensionNames.join(", "),
                          })}
                        </Typography>
                      ) : (
                        <Typography variant="caption">
                          {t("pivotCompatibility.noCommonDimensions")}
                        </Typography>
                      )}

                      {pivotCompatibility.conflictsByMeasure.length > 0 && (
                        <Box
                          component="table"
                          sx={{
                            borderCollapse: "collapse",
                            width: "100%",
                            "& th, & td": {
                              borderBottom: "1px solid",
                              borderColor: "divider",
                              py: 0.25,
                              pr: 1,
                              textAlign: "left",
                              verticalAlign: "top",
                              fontSize: 12,
                            },
                          }}
                        >
                          <Box component="thead">
                            <Box component="tr">
                              <Box component="th">{t("pivotCompatibility.measure")}</Box>
                              <Box component="th">{t("pivotCompatibility.cannotUseWith")}</Box>
                              <Box component="th">{t("pivotCompatibility.canUseWith")}</Box>
                            </Box>
                          </Box>
                          <Box component="tbody">
                            {pivotCompatibility.conflictsByMeasure.map((conflict) => (
                              <Box component="tr" key={conflict.measureId}>
                                <Box component="td">{conflict.measureName}</Box>
                                <Box component="td">{conflict.incompatibleDimensionNames.join(", ")}</Box>
                                <Box component="td">
                                  {conflict.compatibleDimensionNames.join(", ") || t("pickerBar.none")}
                                </Box>
                              </Box>
                            ))}
                          </Box>
                        </Box>
                      )}

                      <Stack direction="row" gap={0.75} flexWrap="wrap">
                        <Button
                          size="small"
                          variant="outlined"
                          disabled={!pivotCompatibility.actions.keep_common_dimensions}
                          onClick={handleKeepCommonDimensions}
                        >
                          {t("pivotCompatibility.keepCommon")}
                        </Button>
                        <Button
                          size="small"
                          variant="outlined"
                          disabled={!pivotCompatibility.actions.split_pivot}
                          onClick={handleSplitIntoSeparatePivots}
                        >
                          {t("pivotCompatibility.splitPivot")}
                        </Button>
                        <Button
                          size="small"
                          variant="outlined"
                          disabled={!pivotCompatibility.actions.remove_incompatible_dimensions}
                          onClick={handleRemoveIncompatibleDimensions}
                        >
                          {t("pivotCompatibility.removeIncompatible")}
                        </Button>
                      </Stack>
                    </Stack>
                  )}
                </Stack>
              </Alert>
            )}
            <PickerBar
              projectId={projectId ?? ""}
              modelId={modelId ?? ""}
              measures={availableMeasures}
              glossaryByMeasureId={glossaryByMeasureId}
              dimensions={visibleDimensions}
              selections={measureSelections}
              rowDimIds={rowDimIds}
              colDimIds={colDimIds}
              executing={executing}
              forceLive={forceLive}
              runDisabled={runDisabled}
              disabledDimensionReasons={pivotCompatibility.disabledDimensionReasons}
              onSelectionsChange={setMeasureSelections}
              onRowDimsChange={handleRowDimsChange}
              onColDimsChange={setColDimIds}
              onForceLiveChange={setForceLive}
              onRun={handleRun}
              hideRunButton
            />

            <Divider />

            <Stack direction="row" gap={1} alignItems="center" flexWrap="wrap">
              <Typography variant="caption" sx={{ fontWeight: 700, color: "text.secondary" }}>
                {t("pivot.filtersLabel")}
              </Typography>
              <PersonaPicker
                projectId={projectId ?? ""}
                modelId={modelId ?? ""}
                value={personaId}
                onChange={setPersonaId}
              />
              <SlicerBar
                projectId={projectId ?? ""}
                modelId={modelId ?? ""}
                modelSlug={model.data?.slug ?? ""}
                dimensions={localizedDimensions}
                slicers={slicers}
                personaId={personaId}
                disabledReasons={pivotCompatibility.disabledDimensionReasons}
                onChange={setSlicers}
              />
            </Stack>
          </Stack>
        </Collapse>
      </Paper>

      {variantNeedsTimeDim && (
        <Alert severity="info">
          {t("pivot.variantNeedsTimeDim", { name: selectedMeasure?.display_name || selectedMeasure?.name || "", kind: selectedMeasure?.variant_kind || "" })}
        </Alert>
      )}

      {compatibilityUnavailable && (
        <Alert severity="warning">
          {t("pivotCompatibility.unavailable")}
        </Alert>
      )}

      {error && <PivotErrorAlert error={error} />}

      {/* Bug-8453 / R3 finding B-2: shown regardless of row count, because a
          denied query still returns a row for COUNT-shaped SQL. */}
      {rowSecurityDenied && (
        <Alert severity="warning">
          <AlertTitle>{t("query.rowSecurityDeniedTitle")}</AlertTitle>
          {t("query.rowSecurityDeniedBody")}
        </Alert>
      )}

      {sortNotice && (
        <Alert severity="warning" onClose={() => setSortNotice(null)}>
          {sortNotice}
        </Alert>
      )}

      {/* R4 finding 4: a denied result still carries a COUNT-shaped 0 from
          `WHERE 0 = 1`. The warning above tells the truth, but a screenshot
          of the pivot cell does not travel with it, so suppress the value
          entirely -- the same doctrine the subtotals loop already applies
          by refusing to publish a denied grain. */}
      {executeResult && !rowSecurityDenied && (
        <Paper
          variant="outlined"
          sx={{
            p: 1,
            borderRadius: 1,
            bgcolor: "background.paper",
            display: "flex",
            flexDirection: "column",
            gap: 1,
            flexShrink: 0,
          }}
        >
          <Stack direction="row" alignItems="center" justifyContent="space-between" gap={1} flexWrap="wrap">
            <Stack direction="row" gap={1.5} alignItems="center" flexWrap="wrap">
              <TableViewIcon fontSize="small" color="action" />
              <Typography variant="subtitle2" fontWeight={700}>
                {t("pivot.resultsLabel")}
              </Typography>
              <Tooltip
                title={routeReasonLabel(executeResult.route_type, executeResult.reason, t)}
                placement="top"
                arrow
              >
                <Typography
                  variant="caption"
                  sx={{
                    fontWeight: 700,
                    color: executeResult.route_type === "aggregate" ? ui.green : executeResult.route_type === "pocket" ? ui.purple : ui.goldDark,
                  }}
                >
                  {t("pivot.routeBadge", { route: routeBadgeLabel(executeResult.route_type, t) })}
                </Typography>
              </Tooltip>
              <FreshnessIndicator
                routeType={executeResult.route_type}
                freshness={executeResult.freshness}
              />
              <Typography variant="caption" color="text.secondary">
                {t("pivot.rowsReturned", { count: String(executeResult.rows_returned) })}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {t("pivot.executionMs", { ms: String(executeResult.execution_ms) })}
              </Typography>
              {executeResult.bytes_processed > 0 && (
                <Typography variant="caption" color="text.secondary">
                  {t("pivot.bytesProcessed", { bytes: executeResult.bytes_processed.toLocaleString() })}
                </Typography>
              )}
            </Stack>

            <Stack direction="row" gap={0.75} alignItems="center" flexWrap="wrap">
              <Button
                size="small"
                variant="outlined"
                onClick={() => setDisplayOptionsOpen((v) => !v)}
              >
                {t("pivot.optionsButton")}
              </Button>
              <Tooltip title={t("pivot.savedViews")}>
                <Button
                  size="small"
                  variant="outlined"
                  startIcon={savedViews.length > 0 ? <BookmarkIcon fontSize="small" /> : <BookmarkBorderIcon fontSize="small" />}
                  onClick={() => setViewMenuOpen(true)}
                >
                  {savedViews.length > 0 ? t("pivot.viewsCount", { count: String(savedViews.length) }) : t("pivot.viewsButton")}
                </Button>
              </Tooltip>
              {measureSelections.length > 0 && (
                <Tooltip title={t("pivot.copyShareableLinkTooltip")}>
                  <Button
                    size="small"
                    variant="outlined"
                    startIcon={<LinkIcon fontSize="small" />}
                    onClick={handleCopyLink}
                  >
                    {t("pivot.linkButton")}
                  </Button>
                </Tooltip>
              )}
              {pivot && selectedMeasure && !overCellCap && (
                <>
                  <ExportMenu
                    pivot={pivot}
                    measure={selectedMeasure}
                    extraMeasures={extraMeasures}
                    executeResult={executeResult}
                    totals={allTotals.get(selectedMeasure.name) ?? null}
                    allTotals={allTotals}
                    showSubtotals={showSubtotals}
                    showGrandTotals={showGrandTotals}
                    emptyCellMode={emptyCellMode}
                    rowKeyOrder={exportRowOrder ?? undefined}
                  />
                  {lastSql && (
                    <Tooltip title={t("pivot.saveAsNamedQueryTooltip")}>
                      <Button
                        size="small"
                        variant="outlined"
                        startIcon={<SaveIcon fontSize="small" />}
                        onClick={() => setSaveOpen(true)}
                      >
                        {t("pivot.saveButton")}
                      </Button>
                    </Tooltip>
                  )}
                </>
              )}
            </Stack>
          </Stack>

          <Collapse in={displayOptionsOpen} timeout="auto" unmountOnExit>
            <Divider sx={{ my: 1 }} />
            <Stack direction="row" gap={1.5} alignItems="center" flexWrap="wrap">
              <FormControlLabel
                control={
                  <Switch
                    size="small"
                    checked={showSubtotals}
                    onChange={(_, v) => setShowSubtotals(v)}
                  />
                }
                label={<Typography variant="caption">{t("pivot.subtotals")}</Typography>}
                sx={{ mr: 0 }}
              />
              <FormControlLabel
                control={
                  <Switch
                    size="small"
                    checked={showGrandTotals}
                    onChange={(_, v) => setShowGrandTotals(v)}
                  />
                }
                label={<Typography variant="caption">{t("pivot.grandTotals")}</Typography>}
                sx={{ mr: 0 }}
              />
              <FormControl size="small" sx={{ minWidth: 130 }}>
                <InputLabel>{t("pivot.emptyCells")}</InputLabel>
                <Select
                  label={t("pivot.emptyCells")}
                  value={emptyCellMode}
                  onChange={(e) => setEmptyCellMode(e.target.value as EmptyCellMode)}
                >
                  <MenuItem value="blank">{t("pivot.blank")}</MenuItem>
                  <MenuItem value="zero">{t("pivot.zero")}</MenuItem>
                  <MenuItem value="dash">{t("pivot.dash")}</MenuItem>
                </Select>
              </FormControl>
              <FormControl size="small" sx={{ minWidth: 150 }}>
                <InputLabel>{t("pivot.cellShading")}</InputLabel>
                <Select
                  label={t("pivot.cellShading")}
                  value={conditionalFormat.kind}
                  onChange={(e) => {
                    const k = e.target.value;
                    if (k === "none") setConditionalFormat({ kind: "none" });
                    else if (k === "color-scale") setConditionalFormat({ kind: "color-scale", low: "#ffffff", high: "#4472C4" });
                    else if (k === "data-bars") setConditionalFormat({ kind: "data-bars", color: "#4472C4" });
                    else if (k === "threshold") setConditionalFormat({ kind: "threshold", below: "#FFCCCC", above: "#CCFFCC", threshold: 0 });
                  }}
                >
                  <MenuItem value="none">{t("pivot.none")}</MenuItem>
                  <MenuItem value="color-scale">{t("pivot.colorScale")}</MenuItem>
                  <MenuItem value="data-bars">{t("pivot.dataBars")}</MenuItem>
                  <MenuItem value="threshold">{t("pivot.threshold")}</MenuItem>
                </Select>
              </FormControl>
              {conditionalFormat.kind === "threshold" && (
                <TextField
                  size="small"
                  label={t("pivot.threshold")}
                  type="number"
                  value={conditionalFormat.threshold}
                  onChange={(e) => {
                    const v = parseFloat(e.target.value);
                    if (!isNaN(v)) {
                      setConditionalFormat({ ...conditionalFormat, threshold: v });
                    }
                  }}
                  sx={{ width: 110 }}
                  inputProps={{ "aria-label": t("pivot.thresholdValueAria") }}
                />
              )}
            </Stack>
          </Collapse>
        </Paper>
      )}

      {pivot && overCellCap && (
        <Alert severity="error">
          {t("pivot.cellsOverCap", { count: cellCount.toLocaleString(), limit: PIVOT_MAX_CELLS.toLocaleString() })}
        </Alert>
      )}

      {hierarchyDrillPath.length > 0 && (
        <Stack direction="row" gap={0.5} alignItems="center" flexWrap="wrap">
          <Typography
            variant="caption"
            sx={{ cursor: "pointer", color: "primary.main", fontWeight: 600, "&:hover": { textDecoration: "underline" } }}
            onClick={() => handleHierarchyDrillUp(0)}
          >
            {t("pivot.allBreadcrumb")}
          </Typography>
          {hierarchyDrillPath.map((step, i) => (
            <Stack key={i} direction="row" alignItems="center" gap={0.5}>
              <Typography variant="caption" color="text.disabled">/</Typography>
              <Typography
                variant="caption"
                sx={{
                  cursor: i < hierarchyDrillPath.length - 1 ? "pointer" : "default",
                  color: i < hierarchyDrillPath.length - 1 ? "primary.main" : "text.primary",
                  fontWeight: i < hierarchyDrillPath.length - 1 ? 600 : 400,
                  "&:hover": i < hierarchyDrillPath.length - 1 ? { textDecoration: "underline" } : {},
                }}
                onClick={() => i < hierarchyDrillPath.length - 1 && handleHierarchyDrillUp(i + 1)}
              >
                {/* F-019-16: show the dimension's display name, not its technical name. */}
                {(dimsByName.get(step.dimName)?.display_name || step.dimName)}: {step.value}
              </Typography>
            </Stack>
          ))}
        </Stack>
      )}

      {!executeResult && !executing && (
        <Paper
          variant="outlined"
          sx={{
            flex: 1,
            minHeight: 220,
            borderRadius: 1,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            bgcolor: ui.mutedBg,
            color: ui.muted,
            textAlign: "center",
            p: 3,
          }}
        >
          <Stack spacing={0.75} alignItems="center">
            <TableViewIcon color="action" />
            <Typography variant="subtitle2" fontWeight={700}>
              {t("pivot.noResultYet")}
            </Typography>
          </Stack>
        </Paper>
      )}

      {canRenderResults && pivot && selectedMeasure && (
        <Box sx={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
          <PivotGrid
            model={pivot}
            measure={selectedMeasure}
            extraMeasures={extraMeasures}
            allTotals={allTotals}
            showSubtotals={showSubtotals}
            showGrandTotals={showGrandTotals}
            emptyCellMode={emptyCellMode}
            conditionalFormat={conditionalFormat}
            sort={pivotSort}
            onSortChange={handlePivotSortChange}
            onSortInvalid={handleInvalidPivotSort}
            onCellClick={handleCellClick}
            drillableRowDims={drillableRowDims}
            drillHierarchyNames={drillHierarchyNames}
            onRowDrill={handleHierarchyDrill}
            onRowOrderChange={setExportRowOrder}
          />
        </Box>
      )}

      <DrillThroughPanel
        open={drawerOpen}
        loading={drillLoading}
        error={drillError}
        result={drillResult}
        context={drillContext}
        rowDims={rowDims}
        colDims={colDims}
        pageSize={drillPageSize}
        hasPrev={drillCursorStack.length > 0}
        modelId={modelId ?? ""}
        hierarchyId={drillHierarchyId}
        hierarchyOptions={drillHierarchyOptions}
        onClose={() => setDrawerOpen(false)}
        onLoadNextPage={handleDrillNextPage}
        onLoadPrevPage={handleDrillPrevPage}
        onPageSizeChange={handleDrillPageSizeChange}
        onSelectHierarchy={handleSelectHierarchy}
        onDrillRow={handleDrillRow}
      />

      <CalcDrillThroughDrawer
        open={calcDrawerOpen}
        context={drillContext}
        rowDims={rowDims}
        colDims={colDims}
        allMeasures={measures.data ?? []}
        personaId={personaId}
        filters={buildSlicerFilters()}
        forceLive={forceLive}
        onClose={() => setCalcDrawerOpen(false)}
      />

      <Dialog open={saveOpen} onClose={() => setSaveOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{t("pivot.savePivotTitle")}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label={t("pivot.nameLabel")}
              size="small"
              fullWidth
              value={saveName}
              onChange={(e) => setSaveName(e.target.value)}
            />
            <TextField
              label={t("pivot.descriptionOptional")}
              size="small"
              fullWidth
              value={saveDesc}
              onChange={(e) => setSaveDesc(e.target.value)}
            />
            <TextField
              label={t("pivot.sqlLabel")}
              size="small"
              fullWidth
              multiline
              minRows={3}
              maxRows={8}
              value={lastSql ?? ""}
              InputProps={{ readOnly: true, sx: { fontFamily: "monospace", fontSize: "0.85rem" } }}
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setSaveOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            disabled={!saveName.trim() || saveMutation.isPending}
            onClick={() =>
              saveMutation.mutate({
                name: saveName.trim(),
                description: saveDesc.trim() || undefined,
                query_text: (lastSql ?? "").trim(),
              })
            }
          >
            {saveMutation.isPending ? t("pivot.saving") : t("pivot.saveButton")}
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={viewMenuOpen} onClose={() => setViewMenuOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{t("pivot.savedViewsTitle")}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            {savedViews.length === 0 && (
              <Typography variant="body2" color="text.secondary">
                {t("pivot.noSavedViews")}
              </Typography>
            )}
            {savedViews.map((v) => (
              <Stack key={v.id} direction="row" alignItems="center" justifyContent="space-between"
                sx={{ p: 1, border: "1px solid", borderColor: "divider", borderRadius: 1, cursor: "pointer", "&:hover": { bgcolor: "action.hover" } }}
                onClick={() => handleLoadView(v)}
              >
                <Box>
                  <Stack direction="row" alignItems="center" gap={0.75}>
                    <Typography variant="body2" fontWeight={600}>{v.name}</Typography>
                    {v.is_shared && (
                      <Chip size="small" variant="outlined" color="primary"
                        label={t("pivot.sharedBadge")} sx={{ height: 18, fontSize: "0.65rem" }} />
                    )}
                  </Stack>
                  <Typography variant="caption" color="text.secondary">
                    {t("pivot.rowsColsInfo", { rows: String(v.row_dim_ids.length), cols: String(v.col_dim_ids.length) })}
                    {!v.is_owner && ` — ${t("pivot.sharedByOwner", { owner: v.created_by })}`}
                  </Typography>
                </Box>
                {(v.is_owner || v.can_edit) && (
                  <Stack direction="row" gap={0.5} alignItems="center" onClick={(e) => e.stopPropagation()}>
                    {/* Publish/unpublish stays owner-only (F-029-22). */}
                    {v.is_owner && (
                      <Tooltip title={v.is_shared ? t("pivot.unshareTooltip") : t("pivot.shareTooltip")}>
                        <Button size="small" onClick={() => handleToggleShare(v)}>
                          {v.is_shared ? t("pivot.unshareView") : t("pivot.shareView")}
                        </Button>
                      </Tooltip>
                    )}
                    {/* Bug-5839: a modeler may delete a shared view they do not own. */}
                    {v.can_edit && (
                      <Button
                        size="small"
                        color="error"
                        onClick={() => handleDeleteView(v.id)}
                      >
                        {t("pivot.deleteView")}
                      </Button>
                    )}
                  </Stack>
                )}
              </Stack>
            ))}
            <Stack gap={0.5}>
              <Stack direction="row" gap={1} alignItems="center">
                <TextField
                  label={t("pivot.newViewName")}
                  size="small"
                  fullWidth
                  value={viewSaveName}
                  onChange={(e) => setViewSaveName(e.target.value)}
                />
                <Button
                  variant="contained"
                  size="small"
                  disabled={!viewSaveName.trim() || measureSelections.length === 0}
                  onClick={handleSaveView}
                >
                  {t("pivot.saveButton")}
                </Button>
              </Stack>
              <FormControlLabel
                control={
                  <Switch
                    size="small"
                    checked={viewSaveShared}
                    onChange={(e) => setViewSaveShared(e.target.checked)}
                  />
                }
                label={t("pivot.shareWithTeam")}
              />
              <Typography variant="caption" color="text.secondary">
                {t("pivot.sharingHelp")}
              </Typography>
            </Stack>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setViewMenuOpen(false)}>{t("pivot.close")}</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
