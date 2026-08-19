import { useState, useCallback, useMemo, useEffect, useRef } from 'react';
import {
  Box, Typography, CircularProgress, Skeleton, Chip, Button,
  Select, MenuItem,
  Dialog, DialogTitle, DialogContent, DialogActions,
  List, ListItem, ListItemText, ListItemIcon,
} from '@mui/material';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import {
  AccountTreeOutlined,
  FunctionsOutlined,
  ManageSearchOutlined,
  RefreshOutlined,
  TableChartOutlined,
} from '@mui/icons-material';
import { tokens } from '../../theme';
import SearchBar from '../common/SearchBar';
import { useMeasures, useDimensions, useHierarchies, useKpis, useNamedSets, useGlossary, useAliasMap, useFieldCompatibility } from '../../hooks/useModel';
import { executeQuery, discoverMembers, type PluginExecuteParams } from '../../api/queryRouter';
import { rowSecurityDeniedAll } from '../../utils/rowSecurity';
import { useToast } from '../Toast/ToastProvider';
import { useExcel } from '../../hooks/useExcel';
import type { ReportTemplate } from '../../utils/reportTemplates';
import { ApiError, formatApiError } from '../../api/client';
import { isTextualType } from '../../utils/dataTypes';
import { buildXmlaCatalogName, TESSALLITE_CONNECTION_NAME } from '../../utils/excelFormulas';
import { enrichAnnotationTimeDimensions } from '../../utils/excelCharts';
import { refreshCustomFunctionValues } from '../../functions';
import { refreshTables, buildRefreshDetailRows, type RefreshDetailRow } from '../../utils/tableRefresh';
import RefreshDetailsPanel from './RefreshDetailsPanel';
import { getInsertMode } from '../../utils/storage';
import { buildZoneQuery, buildLocalPivotFieldMapping, buildLocalPivotQuery, evaluateNamedSetZoneGate, resolveNamedSetDimension, resolveZoneAxes, pivotZoneResult, resolveZoneItemName, planKpiZoneAdd, planKpiZoneRemove, unsafeLocalPivotMeasures } from '../../utils/zoneQuery';
import { describeMeasureFormulaInsertResult, describeKpiFormulaInsertResult, describeChartInsertResult, describeTableInsertResult, planKpiInsertAction, planKpiCubeEligibility, type KpiFormulaRouteReason } from '../../utils/measureFormulaInsert';
import { buildTableDrillMetadata } from '../../utils/drillMetadata';
import { buildScorecardPayload } from '../../utils/kpiScorecard';
import { evaluateZoneFieldCompatibility, formatZoneCompatibilityMessages } from '../../utils/fieldCompatibility';
import type { Measure, Dimension, Kpi, NamedSet, Hierarchy, HierarchyLevel, SemanticQuery, ExecuteResponse, DiscoverMembersResponse, PluginRouteTrace } from '../../types/tessallite';
import { checkStaleEntities, updateManifestStatuses, trackEntityUsage, type StaleEntity } from '../../utils/workbookMetadata';
import { reportNamedSetUsage, reportKpiUsage, previewNamedSet, getHierarchyDetail, evaluateKpiBatch } from '../../api/modelService';
import ZoneMappingGrid from './ZoneMappingGrid';
import type { ZoneItem } from './ZoneMappingGrid';
import type { Zone } from '../../types/tessallite';
import MeasureLibrary from './MeasureLibrary';
import DimensionLibrary from './DimensionLibrary';
import HierarchyLibrary from './HierarchyLibrary';
import KpiLibrary from './KpiLibrary';
import NamedSetLibrary from './NamedSetLibrary';
import TemplatePicker from './TemplatePicker';
import CubeFormulaWizard from '../CubeFunctions/CubeFormulaWizard';
import LiveConnectionWizard from '../Connection/LiveConnectionWizard';
import TraceModal from '../QueryTrace/TraceModal';
import { logError } from '../../utils/diagnostics';
import { strings, templates } from '../../i18n/strings';

// Bug-6359 / Bug-6365: the default cap on report rows. Surfaced to the user as a
// truncation notice when a result reaches it, and lowered per-template (Top N).
const REPORT_ROW_LIMIT = 1000;

interface ExecuteZoneQueryOptions {
  pivotColumns?: boolean;
  includeFilterDimensions?: boolean;
}

interface ReportBuilderProps {
  projectId: string;
  modelId: string;
  serverUrl: string;
  personaId?: string | null;
  personaSlug?: string | null;
  modelsList?: { id: string; name: string; slug?: string }[];
  onModelChange?: (modelId: string) => void;
}

export default function ReportBuilder({ projectId, modelId, serverUrl, personaId, personaSlug, modelsList, onModelChange }: ReportBuilderProps) {
  const { showToast } = useToast();
  const handleBusy = useCallback(() => showToast(strings.toasts.excelBusy, 'info'), [showToast]);
  const {
    insertTable: excelInsertTable, insertFormula, insertLiteral, insertChart: excelInsertChart,
    insertLocalPivot: excelInsertLocalPivot, insertNamedSetAsFormulas, insertKpiFormulas,
    insertKpiFullRow, insertKpiValueOnly, insertKpiStatusOnly,
    insertKpiValueFormula, insertMeasureAsFormula, insertKpiScorecard,
  } = useExcel(undefined, handleBusy, modelId);

  const { data: measures, isLoading: measuresLoading } = useMeasures(projectId, modelId, personaId);
  const { data: dimensions, isLoading: dimsLoading } = useDimensions(projectId, modelId, personaId);
  const { data: hierarchies, isLoading: hierLoading } = useHierarchies(projectId, modelId, personaId);
  const { data: kpis, isLoading: kpisLoading } = useKpis(projectId, modelId, personaId);
  const { data: namedSets, isLoading: namedSetsLoading } = useNamedSets(projectId, modelId, personaId);
  const { data: glossary } = useGlossary(projectId, modelId, personaId);
  const { data: aliasMap } = useAliasMap(projectId, modelId, personaId);

  const [search, setSearch] = useState('');
  const [certifiedOnly, setCertifiedOnly] = useState(false);

  // F-025-24: a real debounce. The previous code called setDebouncedSearch(v)
  // synchronously with setSearch(v) in the SearchBar onChange, so every
  // keystroke re-filtered immediately — the "debounced" name was a lie. Now the
  // input updates `search` on each keystroke and `debouncedSearch` (which all
  // the filtering memos depend on) only catches up 200 ms after typing stops.
  const [debouncedSearch, setDebouncedSearch] = useState('');

  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 200);
    return () => clearTimeout(t);
  }, [search]);

  const [zoneItems, setZoneItems] = useState<ZoneItem[]>([]);
  const selectedMeasureIds = useMemo(
    () => zoneItems.filter(i => i.zone === 'values').map(i => i.id),
    [zoneItems],
  );

  // F-025-27: optional result sort. Empty field = no explicit ordering.
  const [sortField, setSortField] = useState<string>('');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');

  // Bug-6365: the active row cap. Defaults to REPORT_ROW_LIMIT; the Top N
  // Breakdown template lowers it (with a descending sort) so it actually ranks
  // and returns only the top-N rows.
  const [rowLimit, setRowLimit] = useState<number>(REPORT_ROW_LIMIT);

  const [measuresExpanded, setMeasuresExpanded] = useState(false);
  const [dimsExpanded, setDimsExpanded] = useState(false);
  const [hierExpanded, setHierExpanded] = useState(false);
  const [kpisExpanded, setKpisExpanded] = useState(false);
  const [namedSetsExpanded, setNamedSetsExpanded] = useState(false);

  const [templatesOpen, setTemplatesOpen] = useState(false);
  const [cubeWizardOpen, setCubeWizardOpen] = useState(false);
  const [connWizardOpen, setConnWizardOpen] = useState(false);
  const [traceOpen, setTraceOpen] = useState(false);
  const [lastQuery, setLastQuery] = useState<SemanticQuery | null>(null);
  // F-025-20: the route decision (aggregate/pocket/source + rewritten SQL) of
  // the most recent execution, shown in the Query Trace modal.
  const [lastRoute, setLastRoute] = useState<PluginRouteTrace | null>(null);
  const [staleDialogEntries, setStaleDialogEntries] = useState<StaleEntity[]>([]);
  const pendingStaleUpdate = useRef<{ entities: { id: string; type: 'named_set' | 'kpi'; certification_status: string; updated_at?: string }[]; skipKeys: Set<string>; modelId: string } | null>(null);

  const [memberPreviewDimId, setMemberPreviewDimId] = useState<string | null>(null);
  const [memberPreview, setMemberPreview] = useState<DiscoverMembersResponse | null>(null);
  const [membersPreviewLoading, setMembersPreviewLoading] = useState(false);

  // F-025-05: reset all model-owned report state whenever the governed context
  // identity changes — not only on persona change. The previous dependency list
  // ([personaId]) missed the two catalogue-identity changes (project/model), and
  // a model switch commonly leaves persona null, so the reset never ran. Stale
  // zone IDs then resolved against the new model's catalogue, producing a report
  // that looks configured but silently drops fields or fails on execution.
  // Member-preview, sort direction, and the last-query/route previews are all
  // model-scoped and must clear with the rest. Placed after every referenced
  // state declaration to avoid a temporal-dead-zone reference.
  useEffect(() => {
    setZoneItems([]);
    setSortField('');
    setSortDir('desc');
    setRowLimit(REPORT_ROW_LIMIT);
    setMemberPreviewDimId(null);
    setMemberPreview(null);
    setLastQuery(null);
    setLastRoute(null);
  }, [projectId, modelId, personaId]);

  const handlePreviewMembers = useCallback(async (dimId: string) => {
    if (memberPreviewDimId === dimId) {
      setMemberPreviewDimId(null);
      setMemberPreview(null);
      return;
    }
    setMemberPreviewDimId(dimId);
    setMembersPreviewLoading(true);
    try {
      const dim = dimensions?.find(d => d.id === dimId);
      const result = await discoverMembers(modelId, dim?.name ?? dimId, personaId || undefined);
      // Bug-8453 / R4 finding 3: member discovery is RLS-filtered. A deny-all
      // returns zero members, which the picker renders as "this dimension has
      // no members" -- a false statement about the model, not about access.
      if (rowSecurityDeniedAll(result)) {
        setMemberPreview(null);
        showToast(strings.toasts.queryRowSecurityDenied, 'warning');
        return;
      }
      setMemberPreview(result);
    } catch {
      setMemberPreview(null);
      showToast(strings.toasts.couldNotLoadMembers, 'error');
    } finally {
      setMembersPreviewLoading(false);
    }
  }, [memberPreviewDimId, showToast, dimensions, modelId, personaId]);

  const glossarySynonyms = useMemo(() => {
    const map = new Map<string, string[]>();
    if (glossary) {
      for (const entry of glossary) {
        for (const syn of entry.synonyms) {
          const arr = map.get(entry.term) || [];
          arr.push(syn.toLowerCase());
          map.set(entry.term, arr);
        }
      }
    }
    return map;
  }, [glossary]);

  const aliasLookup = useMemo(() => {
    const byCanonical = new Map<string, string[]>();
    if (aliasMap) {
      for (const entry of aliasMap) {
        const arr = byCanonical.get(entry.canonical.toLowerCase()) || [];
        arr.push(entry.alias.toLowerCase());
        byCanonical.set(entry.canonical.toLowerCase(), arr);
      }
    }
    return byCanonical;
  }, [aliasMap]);

  const filterBySearch = useCallback(<T extends { display_name: string; name?: string; effective_description?: string; description?: string; display_folder?: string }>(items: T[] | undefined): T[] => {
    if (!items) return [];
    if (!debouncedSearch.trim()) return items;
    const q = debouncedSearch.toLowerCase();
    return items.filter(item => {
      if (
        (item.display_name?.toLowerCase().includes(q)) ||
        (item.name?.toLowerCase().includes(q)) ||
        (item.effective_description?.toLowerCase().includes(q)) ||
        (item.description?.toLowerCase().includes(q)) ||
        (item.display_folder?.toLowerCase().includes(q))
      ) return true;
      const syns = glossarySynonyms.get(item.display_name);
      if (syns?.some(s => s.includes(q))) return true;
      const aliases = aliasLookup.get(item.name?.toLowerCase() ?? '') ?? aliasLookup.get(item.display_name.toLowerCase());
      if (aliases?.some(a => a.includes(q))) return true;
      return false;
    });
  }, [debouncedSearch, glossarySynonyms, aliasLookup]);

  const filteredMeasures = useMemo(() => filterBySearch(measures), [measures, filterBySearch]);
  const filteredDimensions = useMemo(() => filterBySearch(dimensions), [dimensions, filterBySearch]);
  // Bug-7416: the technical names of the model's time dimensions, used to
  // reclassify the plugin-execute annotation (which never marks time dimensions)
  // so the chart recommender can detect a time series and pick a line chart.
  const timeDimensionNames = useMemo(
    () => (dimensions || []).filter(d => d.is_time_dimension).map(d => d.name),
    [dimensions],
  );
  const compatibilityDimensionIds = useMemo(
    () => (dimensions || []).map(d => d.id),
    [dimensions],
  );
  const fieldCompatibility = useFieldCompatibility(
    projectId,
    modelId,
    personaId,
    selectedMeasureIds,
    compatibilityDimensionIds,
  );
  const zoneCompatibility = useMemo(
    () => evaluateZoneFieldCompatibility({
      items: zoneItems,
      dimensions: dimensions || [],
      matrix: fieldCompatibility.data,
    }),
    [zoneItems, dimensions, fieldCompatibility.data],
  );
  const compatibilityMessages = useMemo(
    () => formatZoneCompatibilityMessages(zoneCompatibility),
    [zoneCompatibility],
  );
  const compatibilityBlockedReason = zoneCompatibility.blocking
    ? strings.reportBuilder.removeIncompatibleTable
    : null;

  const filteredKpis = useMemo(() => {
    if (!kpis) return [];
    let result = kpis;
    if (certifiedOnly) {
      result = result.filter((k: Kpi) => k.certification_status === 'certified');
    }
    if (!debouncedSearch.trim()) return result;
    const q = debouncedSearch.toLowerCase();
    return result.filter((k: Kpi) =>
      (k.display_name?.toLowerCase().includes(q)) ||
      (k.name?.toLowerCase().includes(q)) ||
      (k.description?.toLowerCase().includes(q)) ||
      (k.display_folder?.toLowerCase().includes(q))
    );
  }, [kpis, debouncedSearch, certifiedOnly]);

  const filteredNamedSets = useMemo(() => {
    if (!namedSets) return [];
    let result = namedSets;
    if (certifiedOnly) {
      result = result.filter((ns: NamedSet) => ns.certification_status === 'certified');
    }
    if (!debouncedSearch.trim()) return result;
    const q = debouncedSearch.toLowerCase();
    return result.filter((ns: NamedSet) =>
      (ns.display_name?.toLowerCase().includes(q)) ||
      (ns.name?.toLowerCase().includes(q)) ||
      (ns.description?.toLowerCase().includes(q)) ||
      (ns.display_folder?.toLowerCase().includes(q))
    );
  }, [namedSets, debouncedSearch, certifiedOnly]);

  const addToZone = useCallback((id: string, name: string, zone: Zone, dataType?: string) => {
    setZoneItems(prev => {
      const exists = prev.find(i => i.id === id && i.zone === zone);
      if (exists) return prev;
      // Bug-1062: carry the dimension's physical data_type so the filter dialog
      // can gate gt/lt by semantic category (date/text gt is server-valid).
      return [...prev, { id, name, zone, data_type: dataType }];
    });
  }, []);

  // F-025-11: add a named-set or hierarchy-level item that resolves to a
  // bindable dimension (and, for named sets, a member-key list) instead of an
  // unbindable UUID token.
  const addResolvedToZone = useCallback((item: ZoneItem) => {
    setZoneItems(prev => {
      const exists = prev.find(i => i.id === item.id && i.zone === item.zone);
      if (exists) return prev;
      return [...prev, item];
    });
  }, []);

  const updateFilterItem = useCallback((id: string, operator: string, values: string[]) => {
    // Bug-6358: only the filter-zone item is edited. Filter chips are the sole
    // editable zone, so a field placed on an axis AND as a filter must not have
    // its axis chip mutated when its filter is edited.
    setZoneItems(prev => prev.map(i =>
      i.id === id && i.zone === 'filters' ? { ...i, operator, values } : i,
    ));
  }, []);

  const removeFromZone = useCallback((id: string, zone: Zone) => {
    // Bug-6358: zone-qualified removal — the same id can exist in multiple
    // zones; only the targeted zone's item is removed.
    setZoneItems(prev => prev.filter(i => !(i.id === id && i.zone === zone)));
  }, []);

  const clearZones = useCallback(() => {
    setZoneItems([]);
    setRowLimit(REPORT_ROW_LIMIT);
  }, []);

  // F-025-07: the live XMLA connection's Initial Catalog must be the gateway's
  // published catalog name — `<model slug>` (+ `_<persona slug>` when a persona
  // is active) — NOT the model UUID, which the connect dialog never lists.
  const xmlaCatalog = useMemo(() => {
    const slug = modelsList?.find(m => m.id === modelId)?.slug || '';
    return buildXmlaCatalogName(slug, personaSlug);
  }, [modelsList, modelId, personaSlug]);

  const executeZoneQuery = useCallback(async (options: ExecuteZoneQueryOptions = {}): Promise<{ headers: string[]; rows: (string | number)[][]; annotation?: ExecuteResponse['annotation']; query: SemanticQuery; pivoted: boolean } | null> => {
    // F-025-11: named-set and hierarchy-level items bind via their resolved
    // dimension (+ member-key `in` filter for named sets), never their raw UUID
    // token. buildZoneQuery is the single, unit-tested translation.
    // F-025-27: pass the optional sort selection through to the query-router's
    // order_by. buildZoneQuery drops the field if it is not in the query.
    const order = sortField ? { [sortField]: sortDir } : undefined;
    const baseQuery = buildZoneQuery(zoneItems, { measures, dimensions }, rowLimit, order);
    if (!baseQuery) return null;
    const query = options.includeFilterDimensions
      ? buildLocalPivotQuery(baseQuery, zoneItems, { measures, dimensions })
      : baseQuery;

    setLastQuery(query);

    const pluginParams: PluginExecuteParams = {
      projectId,
      modelId,
      personaId: personaId || undefined,
    };

    try {
      const result = await executeQuery(query, pluginParams);
      // F-025-20: capture the route trace even when the result is empty.
      setLastRoute(result.route ?? null);
      // Bug-8453 / R3 finding S-1: a row-security deny-all returns HTTP 200
      // with no rows (or, for a COUNT-shaped measure, a row containing 0).
      // Inserting that into a workbook -- or reporting it as "no results" --
      // puts a permissions artefact in front of a business user as if it were
      // a measurement. Checked BEFORE the empty-result branch, and independent
      // of row count, because a denial can return a row.
      if (rowSecurityDeniedAll(result)) {
        showToast(strings.toasts.queryRowSecurityDenied, 'warning');
        return null;
      }
      if (!result.data || result.data.length === 0) {
        showToast(strings.toasts.queryNoResults, 'info');
        return null;
      }

      // Bug-6359: the query is capped at the default safety limit. When a result
      // reaches it, it was almost certainly truncated, so tell the analyst rather
      // than silently inserting a partial result they might read as complete.
      // A template that deliberately lowers the cap (Top N) is an intentional
      // limit, not a surprise truncation, so it is excluded.
      if (rowLimit >= REPORT_ROW_LIMIT && result.data.length >= rowLimit) {
        showToast(templates.toasts.resultTruncated(rowLimit), 'warning');
      }

      // F-025-15: the query-router always returns a flat grouped result. Table
      // and chart inserts pivot the Columns zone client-side. Local PivotTable
      // insertion deliberately leaves the grouped result flat so Excel's native
      // PivotTable receives row, column, filter and value fields as columns.
      const { rowDimNames, colDimNames } = resolveZoneAxes(zoneItems, { measures, dimensions });
      const shouldPivotColumns = options.pivotColumns !== false;
      const flatDimNames = query.dimensions || [];
      const measureKeys = result.annotation
        ? Object.keys(result.annotation.measures || {})
        : query.measures || [];
      // Build the title map (technical key -> friendly header) from the
      // annotation; fall back to the key itself.
      const titles: Record<string, string> = {};
      if (result.annotation) {
        for (const [k, m] of Object.entries(result.annotation.measures || {})) titles[k] = m.title;
        for (const [k, d] of Object.entries(result.annotation.dimensions || {})) titles[k] = d.title;
      }
      const { headers, rows } = pivotZoneResult(
        result.data as Record<string, unknown>[],
        shouldPivotColumns ? rowDimNames : flatDimNames,
        shouldPivotColumns ? colDimNames : [],
        measureKeys,
        titles,
      );

      return { headers, rows, annotation: result.annotation, query, pivoted: shouldPivotColumns && colDimNames.length > 0 };
    } catch (e) {
      if (e instanceof ApiError) {
        // Surface the contract's readable reason (e.g. the 422 filter-operator
        // message from the canonical filter contract, or a persona 403) rather
        // than a generic "Query execution failed". formatApiError maps 403 to a
        // permission notice and 422 to "Validation error: <detail>".
        showToast(formatApiError(e), 'error');
      } else {
        showToast(strings.toasts.queryExecutionFailed, 'error');
      }
      console.error('Report Builder query failed:', e);
      return null;
    }
  }, [zoneItems, measures, dimensions, showToast, projectId, modelId, personaId, sortField, sortDir, rowLimit]);

  const handleInsertTable = useCallback(async () => {
    if (zoneCompatibility.blocking) {
      showToast(compatibilityBlockedReason || strings.reportBuilder.removeIncompatibleTable, 'warning');
      return;
    }
    const result = await executeZoneQuery();
    if (!result) return;
    // Bug-6362: build the drill provenance via the single source of truth
    // (buildTableDrillMetadata) rather than an inline near-duplicate. It resolves
    // the annotation measure NAME -> measure UUID (drill routes take a UUID path
    // param; F-025-06) and covers time dimensions too. formatTokens always apply;
    // per-cell measure/dimension column maps are omitted for a pivoted cross-tab
    // because its composite headers ("Q1 — Revenue") have no 1:1 title mapping
    // (F-025-15).
    const drill = buildTableDrillMetadata(result.annotation, measures || []);
    const formatTokens = drill.formatTokens;
    const measureColumns: Record<string, string> = result.pivoted ? {} : drill.measureColumns;
    const dimensionColumns: Record<string, string> = result.pivoted ? {} : drill.dimensionColumns;
    try {
      // Bug-6735: pass useActiveCell so the table starts at the user's
      // selected cell instead of A1. All other insert paths (KPI formulas,
      // measure formulas, named-set formulas) already honour the active cell
      // via `context.workbook.getSelectedRange()`. The table path was the
      // inconsistent outlier, placing tables at (0,0) regardless of where
      // the user's cursor sat.
      const tableResult = await excelInsertTable(result.headers, result.rows, { useActiveCell: true }, {
        projectId, modelId, personaId: personaId || undefined,
        // F-025-23: friendly labels for the provenance footer.
        modelLabel: modelsList?.find(m => m.id === modelId)?.name,
        personaLabel: personaSlug || undefined,
        formatTokens,
        semanticQuery: JSON.stringify(result.query),
        columnHeaders: result.headers,
        measureColumns: Object.keys(measureColumns).length > 0 ? measureColumns : undefined,
        dimensionColumns: Object.keys(dimensionColumns).length > 0 ? dimensionColumns : undefined,
      });
      // Bug-6737: the toast is decided from the ACTUAL insert outcome, not
      // a blanket try/catch. A post-step failure (metadata/footer) after a
      // successful write shows a warning stating the table was inserted,
      // never "Insert failed".
      const toastCall = describeTableInsertResult(tableResult.address !== null, result.rows.length, tableResult.postStepWarning, tableResult.blocked);
      if (toastCall) showToast(toastCall.message, toastCall.severity);
    } catch (e) {
      showToast(strings.toasts.insertFailed, 'error');
    }
  }, [executeZoneQuery, excelInsertTable, showToast, projectId, modelId, personaId, measures, zoneCompatibility.blocking, compatibilityBlockedReason]);

  // Bug-6733: chart insert toast is decided from the actual insert outcome,
  // not a blanket try/catch that conflates a post-step axis-formatting error
  // with a genuine chart creation failure. describeChartInsertResult is the
  // pure helper (same pattern as Bug-6709's describeMeasureFormulaInsertResult).
  const handleInsertChart = useCallback(async () => {
    if (zoneCompatibility.blocking) {
      showToast(compatibilityBlockedReason || strings.reportBuilder.removeIncompatibleChart, 'warning');
      return;
    }
    const result = await executeZoneQuery();
    if (!result) return;
    try {
      // Bug-7416: the backend annotation never populates timeDimensions, so a
      // time-series result would be charted as a categorical column chart.
      // Reclassify the model's known time dimensions client-side so the chart
      // recommender (which reads annotation.timeDimensions) picks a line chart.
      const chartAnnotation = enrichAnnotationTimeDimensions(
        result.annotation,
        timeDimensionNames,
      );
      const chartResult = await excelInsertChart(result.headers, result.rows, undefined, chartAnnotation);
      const toastCall = describeChartInsertResult(chartResult.address !== null, chartResult.postStepWarning);
      if (toastCall) showToast(toastCall.message, toastCall.severity);
    } catch (e) {
      showToast(strings.toasts.chartCreationFailed, 'error');
    }
  }, [executeZoneQuery, excelInsertChart, showToast, zoneCompatibility.blocking, compatibilityBlockedReason, timeDimensionNames]);

  const handleInsertLocalPivot = useCallback(async () => {
    if (zoneCompatibility.blocking) {
      showToast(compatibilityBlockedReason || strings.reportBuilder.removeIncompatiblePivot, 'warning');
      return;
    }
    const unsafeMeasures = unsafeLocalPivotMeasures(zoneItems, measures);
    if (unsafeMeasures.length > 0) {
      showToast(
        templates.toasts.localPivotUnsafeMeasures(
          unsafeMeasures.map(m => m.display_name || m.name),
        ),
        'warning',
      );
      return;
    }
    const result = await executeZoneQuery({ pivotColumns: false, includeFilterDimensions: true });
    if (!result) return;
    try {
      const mapping = buildLocalPivotFieldMapping(zoneItems, { measures, dimensions }, result.annotation);
      await excelInsertLocalPivot(result.headers, result.rows, mapping, result.annotation);
      showToast(strings.toasts.pivotCreated, 'success');
    } catch (e) {
      // Bug-6909: classify PivotTable insertion failures instead of collapsing
      // every error into the single "requires Excel 2019+" message.
      const errMsg = e instanceof Error ? e.message : String(e);
      logError(`PivotTable insertion failed: ${errMsg}`);
      if (/not.*support|not.*implement|not.*function|GeneralException/i.test(errMsg)) {
        showToast(strings.toasts.pivotInsertFailedCompat, 'error');
      } else if (/mapping|field|range|source/i.test(errMsg)) {
        showToast(strings.toasts.pivotInsertFailedMapping, 'error');
      } else {
        showToast(strings.toasts.pivotInsertFailedGeneric, 'error');
      }
    }
  }, [executeZoneQuery, excelInsertLocalPivot, showToast, zoneCompatibility.blocking, compatibilityBlockedReason, zoneItems, measures, dimensions]);

  const handleTemplateSelect = useCallback((template: ReportTemplate) => {
    clearZones();

    if (template.id === 'time-series') {
      const timeDim = dimensions?.find(d => d.is_time_dimension);
      if (timeDim) addToZone(timeDim.id, timeDim.display_name, 'rows');
      filteredMeasures.forEach((m, i) => { if (i < 2) addToZone(m.id, m.display_name, 'values'); });
    } else if (template.id === 'top-n') {
      const catDim = dimensions?.find(d => !d.is_time_dimension && isTextualType(d.data_type));
      if (catDim) addToZone(catDim.id, catDim.display_name, 'rows');
      const topMeasure = filteredMeasures[0];
      if (topMeasure) {
        addToZone(topMeasure.id, topMeasure.display_name, 'values');
        // Bug-6365: the Top N Breakdown template must actually rank and cap. Sort
        // by the primary measure descending and lower the row limit to the
        // template's topN, so the result is a real top-N leaderboard rather than
        // an unranked, unbounded list. `topMeasure.name` is the technical field
        // name the query-router's order_by expects.
        setSortField(topMeasure.name);
        setSortDir('desc');
        if (template.topN) setRowLimit(template.topN);
      }
    } else if (template.id === 'geographic') {
      const catDim = dimensions?.find(d => !d.is_time_dimension && isTextualType(d.data_type));
      if (catDim) addToZone(catDim.id, catDim.display_name, 'rows');
      if (filteredMeasures[0]) addToZone(filteredMeasures[0].id, filteredMeasures[0].display_name, 'values');
    } else if (template.id === 'period-comparison') {
      const timeDim = dimensions?.find(d => d.is_time_dimension);
      if (timeDim) addToZone(timeDim.id, timeDim.display_name, 'columns');
      filteredMeasures.forEach((m, i) => { if (i < 2) addToZone(m.id, m.display_name, 'values'); });
    } else if (template.id === 'variance') {
      const catDim = dimensions?.find(d => !d.is_time_dimension);
      if (catDim) addToZone(catDim.id, catDim.display_name, 'rows');
      filteredMeasures.forEach((m, i) => { if (i < 2) addToZone(m.id, m.display_name, 'values'); });
    } else {
      filteredMeasures.forEach((m, i) => { if (i < 3) addToZone(m.id, m.display_name, 'values'); });
    }
    setTemplatesOpen(false);
  }, [dimensions, filteredMeasures, addToZone, clearZones]);

  const handleInsertFormula = useCallback((formula: string, targetCell: string) => {
    // Bug-7397 R12-1: insertFormula now reports whether the write ACTUALLY
    // happened. A declined overwrite confirm, or blocks held by another Excel
    // operation, must not produce a "Formula inserted" success toast (a busy
    // outcome already raises its own notice; a decline is deliberately silent
    // per the Bug-6709 no-toast-on-decline invariant).
    insertFormula(formula, targetCell).then((written) => {
      if (written) showToast(strings.toasts.formulaInserted, 'success');
    }).catch(() => {
      showToast(strings.toasts.formulaInsertionFailed, 'error');
    });
  }, [insertFormula, showToast]);

  const handleInsertNamedSetAsFormulas = useCallback(async (ns: NamedSet) => {
    try {
      const result = await insertNamedSetAsFormulas(
        { id: ns.id, name: ns.name, display_name: ns.display_name, expression: ns.expression, updated_at: ns.updated_at },
        TESSALLITE_CONNECTION_NAME,
      );
      if (result) {
        // Bug-6709: CUBESET formulas need the workbook connection exactly
        // like CUBEVALUE -- warn with the requirement, never bare success.
        showToast(templates.toasts.cubeSetFormulasInsertedNeedsConnection(TESSALLITE_CONNECTION_NAME), 'warning');
        reportNamedSetUsage(projectId, modelId, ns.id, {
          cell_reference: result, usage_type: 'excel_insert',
        }).catch(() => {});
      }
    } catch {
      showToast(strings.toasts.formulaInsertionFailed, 'error');
    }
  }, [insertNamedSetAsFormulas, showToast, projectId, modelId]);

  const handleInsertKpiAsFormulas = useCallback(async (kpi: Kpi) => {
    // Bug-6728: composite-expression KPIs cannot resolve CUBE formulas.
    // Evaluate via the plugin protocol and insert a literal value+goal block.
    const eligibility = planKpiCubeEligibility(kpi, measures?.map(m => m.id), measures?.map(m => m.name));
    if (eligibility === 'composite_expression') {
      try {
        const batchResults = await evaluateKpiBatch(projectId, modelId, [kpi.id], personaId || undefined);
        const ev = batchResults[0];
        const goalLiteral = (kpi.goal_measure_id == null && kpi.target_type === 'static')
          ? kpi.target_value : null;
        // Reuse the value+goal insert path with evaluated literal values.
        const result = await insertKpiFormulas(
          { id: kpi.id, name: kpi.name, display_name: kpi.display_name, updated_at: kpi.updated_at },
          null, // no value measure
          null, // no goal measure
          TESSALLITE_CONNECTION_NAME,
          ev?.goal ?? goalLiteral,
          ev?.value ?? null,  // Bug-6728: literal value (may be null)
          true,  // forceLiteral: never fall through to a permanently-#N/A formula
        );
        if (result) {
          showToast(templates.toasts.compositeKpiFormulaInserted(kpi.display_name || kpi.name), 'info');
        }
      } catch {
        showToast(strings.toasts.formulaInsertionFailed, 'error');
      }
      return;
    }
    const valueMeasure = measures?.find(m => m.id === kpi.value_measure_id);
    const goalMeasure = measures?.find(m => m.id === kpi.goal_measure_id);
    // Bug-5294: pass the static goal literal so the Goal cell is populated
    // for KPIs with target_type=static and no goal measure.
    const goalLiteral = (kpi.goal_measure_id == null && kpi.target_type === 'static')
      ? kpi.target_value : null;
    try {
      const result = await insertKpiFormulas(
        { id: kpi.id, name: kpi.name, display_name: kpi.display_name, updated_at: kpi.updated_at },
        valueMeasure?.name || null,
        goalMeasure?.name || null,
        TESSALLITE_CONNECTION_NAME,
        goalLiteral,
      );
      if (result) {
        // Bug-6709: never bare success on a CUBE-formula insert.
        showToast(templates.toasts.kpiFormulasInsertedNeedsConnection(TESSALLITE_CONNECTION_NAME), 'warning');
        // Bug-6728: post-insert undeployed warning (never pre-insert, per
        // the Bug-6709 invariant: no toast for a declined/cancelled insert).
        if (eligibility === 'undeployed') {
          showToast(templates.toasts.undeployedKpiFormulaWarning(kpi.display_name || kpi.name, TESSALLITE_CONNECTION_NAME), 'warning');
        }
        reportKpiUsage(projectId, modelId, kpi.id, {
          cell_reference: result, usage_type: 'excel_insert',
        }).catch(() => {});
      }
    } catch {
      showToast(strings.toasts.formulaInsertionFailed, 'error');
    }
  }, [insertKpiFormulas, measures, showToast, projectId, modelId, personaId]);

  // Phase A: insert a measure as a TESSALLITE.VALUE() custom function formula
  // (connectionless -- the default action on the sigma icon).
  // Task 3: respects the global insert-mode setting. In 'static' mode, fetches
  // the current value via plugin-execute and writes it as a literal number.
  const handleInsertMeasureAsFunction = useCallback(async (measureId: string) => {
    const m = measures?.find(fm => fm.id === measureId);
    if (!m) return;
    const modelSlug = modelsList?.find(ml => ml.id === modelId)?.slug || modelsList?.find(ml => ml.id === modelId)?.name || '';
    if (!modelSlug) {
      showToast(strings.toasts.insertFailed, 'error');
      return;
    }

    const mode = await getInsertMode();
    if (mode === 'static') {
      // Bug-7393: fetch the value and write via values channel (insertLiteral),
      // NOT the formula channel. Source-derived strings starting with '='
      // would otherwise execute as formulas (CWE-1236).
      try {
        const params: PluginExecuteParams = { projectId, modelId, personaId: personaId || undefined };
        const result = await executeQuery({ measures: [m.name], limit: 1 }, params);
        const rawValue = result.data?.[0]?.[m.name];
        const cellValue = (rawValue === null || rawValue === undefined)
          ? '#N/A'
          : String(rawValue);
        await insertLiteral(cellValue);
      } catch {
        showToast(strings.toasts.insertFailed, 'error');
      }
      return;
    }

    // Default 'live' mode: insert TESSALLITE.VALUE formula.
    const escapedSlug = modelSlug.replace(/"/g, '""');
    const escapedName = m.name.replace(/"/g, '""');
    const formula = `=TESSALLITE.VALUE("${escapedSlug}","${escapedName}")`;
    try {
      await insertFormula(formula);
    } catch {
      showToast(strings.toasts.formulaInsertionFailed, 'error');
    }
  }, [measures, insertFormula, showToast, modelsList, modelId, projectId, personaId]);

  // Advanced: insert a measure as a CUBEVALUE formula (requires workbook connection).
  const handleInsertMeasureAsFormula = useCallback(async (measureId: string) => {
    const m = measures?.find(fm => fm.id === measureId);
    if (!m) return;
    try {
      const result = await insertMeasureAsFormula(m.name, TESSALLITE_CONNECTION_NAME);
      const toastCall = describeMeasureFormulaInsertResult(result, TESSALLITE_CONNECTION_NAME);
      if (toastCall) showToast(toastCall.message, toastCall.severity);
    } catch {
      showToast(strings.toasts.formulaInsertionFailed, 'error');
    }
  }, [measures, insertMeasureAsFormula, showToast]);

  // Bug-6709: `routedFromAddKpi` marks the Bug-6701 "Add KPI" reroute. The
  // post-insert toast then explains the reroute as well; it is decided from
  // the insert RESULT (pure `describeKpiFormulaInsertResult`), never
  // announced up front -- the insert can be declined by the busy-guard or
  // cancelled at the overwrite confirm. Bug-6721: the value is the reroute
  // REASON from planKpiZoneAdd (no_measure vs unresolvable_measure), so the
  // toast states the true cause.
  const handleInsertKpi = useCallback(async (
    kpi: Kpi,
    mode: string,
    routedFromAddKpi: KpiFormulaRouteReason | false = false,
  ) => {
    // Phase A: single-cell KPI value/status inserts default to TESSALLITE.KPI
    // formulas (connectionless). Multi-cell layouts (full_row, kpi_card,
    // value_goal, formula_ref) keep the existing CUBE formula routing.
    // Task 3: respects the global insert-mode setting. In 'static' mode,
    // fetches the current KPI value/status and writes the literal number.
    if (mode === 'value_only' || mode === 'status_only') {
      const modelSlug = modelsList?.find(ml => ml.id === modelId)?.slug || modelsList?.find(ml => ml.id === modelId)?.name || '';
      if (modelSlug) {
        const insertMode = await getInsertMode();
        if (insertMode === 'static') {
          // Bug-7393: write static KPI values through the values channel
          // (insertLiteral) to prevent formula injection from source data.
          try {
            const batchResults = await evaluateKpiBatch(projectId, modelId, [kpi.id], personaId || undefined);
            const ev = batchResults[0];
            const property = mode === 'value_only' ? 'value' : 'status';
            const rawValue = ev?.[property];
            const cellValue = (rawValue === null || rawValue === undefined)
              ? '#N/A'
              : String(rawValue);
            await insertLiteral(cellValue);
          } catch {
            showToast(strings.toasts.kpiInsertionFailed, 'error');
          }
          return;
        }
        // Default 'live' mode: insert TESSALLITE.KPI formula.
        const kpiNameEscaped = (kpi.name || '').replace(/"/g, '""');
        const slugEscaped = modelSlug.replace(/"/g, '""');
        const property = mode === 'value_only' ? 'value' : 'status';
        const formula = `=TESSALLITE.KPI("${slugEscaped}","${kpiNameEscaped}","${property}")`;
        try {
          await insertFormula(formula);
        } catch {
          showToast(strings.toasts.kpiInsertionFailed, 'error');
        }
        return;
      }
      // Fall through to CUBE path if model slug unavailable.
    }

    const valueMeasure = measures?.find(m => m.id === kpi.value_measure_id);
    const goalMeasure = measures?.find(m => m.id === kpi.goal_measure_id);
    const kpiDisplayName = kpi.display_name || kpi.name;
    // Bug-5294: compute static goal literal for individual KPI insert paths
    const goalLiteral = (kpi.goal_measure_id == null && kpi.target_type === 'static')
      ? kpi.target_value : null;

    try {
      // Bug-6728: check KPI CUBE-formula eligibility. Composite-expression
      // KPIs cannot be served through CUBE formulas at all; for individual
      // inserts, show an explanatory toast and evaluate the literal value.
      const eligibility = planKpiCubeEligibility(kpi, measures?.map(m => m.id), measures?.map(m => m.name));
      if (eligibility === 'composite_expression') {
        // Bug-6728: composite KPIs cannot resolve CUBE formulas on the XMLA
        // surface. Evaluate via the plugin protocol and insert a literal.
        // Respect the user's chosen mode (R3 Finding 5). Gate toast/telemetry
        // on a truthy result (R4 Finding 1: Bug-6709 invariant).
        try {
          const batchResults = await evaluateKpiBatch(projectId, modelId, [kpi.id], personaId || undefined);
          const ev = batchResults[0];
          let compositeResult: string | null = null;

          if (mode === 'status_only') {
            // Bug-7397 R12 review finding 1 (BLOCKING): this used to insert the
            // CUBE formula and then overwrite the same cell from a raw,
            // UNLOCKED `Excel.run` here in the component -- outside the
            // block-lock contract entirely, and as a second acquisition after
            // the first released. The evaluated status is now handed to
            // insertKpiStatusOnly, which writes it as the cell's only write
            // inside the ONE critical section it already holds.
            compositeResult = await insertKpiStatusOnly(
              kpi.name, TESSALLITE_CONNECTION_NAME, { statusLiteral: ev?.status ?? '' },
            );
          } else {
            const staticGoal = (kpi.goal_measure_id == null && kpi.target_type === 'static')
              ? kpi.target_value : null;
            compositeResult = await insertKpiFormulas(
              { id: kpi.id, name: kpi.name, display_name: kpi.display_name, updated_at: kpi.updated_at },
              null,
              null,
              TESSALLITE_CONNECTION_NAME,
              ev?.goal ?? staticGoal,
              ev?.value ?? null,
              true,  // forceLiteral: never fall through to a permanently-#N/A formula
            );
          }
          if (compositeResult) {
            showToast(templates.toasts.compositeKpiFormulaInserted(kpiDisplayName), 'info');
            reportKpiUsage(projectId, modelId, kpi.id, {
              cell_reference: compositeResult, usage_type: 'excel_insert',
            }).catch(() => {});
          }
        } catch {
          showToast(strings.toasts.formulaInsertionFailed, 'error');
        }
        return;
      }
      // Bug-6714: the pure planner is TOTAL over the published modes for
      // every KPI shape, so no mode can silently fall through -- the old
      // dispatch skipped 'value_only' entirely (`if (valueMeasure)`) for
      // custom/expression KPIs, the exact Bug-6701 silent-no-op class. A
      // measure-less KPI's value comes from its KPI-native CUBEKPIMEMBER
      // Value property (the construct 'formula_ref' already uses).
      const action = planKpiInsertAction(mode, Boolean(valueMeasure));
      if (action === null) {
        // Unknown mode string: a programming error, surfaced -- never silent.
        showToast(strings.toasts.kpiInsertionFailed, 'error');
        return;
      }
      let result: string | null = null;
      switch (action) {
        case 'full_row':
          result = await insertKpiFullRow(
            { id: kpi.id, name: kpi.name, display_name: kpi.display_name, updated_at: kpi.updated_at },
            valueMeasure?.name || null,
            goalMeasure?.name || null,
            TESSALLITE_CONNECTION_NAME,
            goalLiteral,
          );
          break;
        case 'measure_value_cell':
          result = await insertKpiValueOnly(valueMeasure!.name, TESSALLITE_CONNECTION_NAME);
          break;
        case 'kpi_value_formula':
          result = await insertKpiValueFormula(kpi.name, TESSALLITE_CONNECTION_NAME);
          break;
        case 'value_goal_rows':
          result = await insertKpiFormulas(
            { id: kpi.id, name: kpi.name, display_name: kpi.display_name, updated_at: kpi.updated_at },
            valueMeasure?.name || null,
            goalMeasure?.name || null,
            TESSALLITE_CONNECTION_NAME,
            goalLiteral,
          );
          break;
        case 'status_cell':
          result = await insertKpiStatusOnly(kpi.name, TESSALLITE_CONNECTION_NAME);
          break;
      }
      if (result) {
        if (mode === 'value_only' || mode === 'status_only' || mode === 'formula_ref') {
          await trackEntityUsage('kpi', kpi.id, kpiDisplayName, result, undefined, kpi.updated_at, modelId);
        }
        const toastCall = describeKpiFormulaInsertResult(
          result, mode, TESSALLITE_CONNECTION_NAME,
          routedFromAddKpi ? { name: kpiDisplayName, reason: routedFromAddKpi } : null,
        );
        if (toastCall) showToast(toastCall.message, toastCall.severity);
        // Bug-6728: post-insert undeployed warning (never pre-insert).
        if (eligibility === 'undeployed') {
          showToast(templates.toasts.undeployedKpiFormulaWarning(kpiDisplayName, TESSALLITE_CONNECTION_NAME), 'warning');
        }
        reportKpiUsage(projectId, modelId, kpi.id, {
          cell_reference: result, usage_type: 'excel_insert',
        }).catch(() => {});
      }
    } catch {
      showToast(strings.toasts.kpiInsertionFailed, 'error');
    }
  }, [measures, insertKpiFullRow, insertKpiValueOnly, insertKpiFormulas, insertKpiStatusOnly, insertKpiValueFormula, showToast, projectId, modelId, personaId]);

  // Bug-6701: "Add KPI" (the KpiCard "+" icon / clicking an unchecked row)
  // stages the KPI's value (and goal) measure into the pivot "Values" zone so
  // it can be inserted later as a table/chart/local pivot. That only works
  // for a KPI backed by real measures (`value_measure_id` set) -- every
  // custom/expression KPI (kpi_type "custom" in the model, value_measure_id
  // null) has nothing to stage: buildZoneQuery/SemanticQuery resolve zone
  // items to measure or dimension names only, and the query-router has no
  // field for a KPI expression (sensitive-component guard: fixing that is a
  // query-router change, out of scope here). The previous code silently did
  // nothing for this shape. Route it to the KPI-native insert instead -- the
  // same CUBEKPIMEMBER formula the "Insert options -> Formula Reference" menu
  // already uses successfully for custom KPIs; the post-insert toast (Bug-6709)
  // explains why the action differed from a normal "Add to Values".
  const handleAddKpiToValues = useCallback((kpi: Kpi) => {
    // Bug-6719: pass the loaded (persona-scoped) measure ids so a KPI whose
    // value measure is unresolvable here (deleted, or hidden from this
    // persona) routes to the formula insert instead of staging a dangling
    // UUID that the zone query would reject with an uninterpretable 422.
    const plan = planKpiZoneAdd(kpi, measures?.map(m => m.id));
    if (plan.kind === 'zone_stage') {
      const m = measures?.find(fm => fm.id === plan.valueMeasureId);
      addToZone(plan.valueMeasureId!, m?.display_name || (kpi.display_name || kpi.name), 'values');
      if (plan.goalMeasureId) {
        const gm = measures?.find(fm => fm.id === plan.goalMeasureId);
        if (gm) addToZone(plan.goalMeasureId, gm.display_name, 'values');
      }
      return;
    }
    // Bug-6709: no pre-insert toast -- the reroute explanation is part of the
    // post-insert result toast inside handleInsertKpi, so nothing is
    // announced when the insert is declined or cancelled. Bug-6721: pass the
    // plan's reason so the toast states the true cause.
    handleInsertKpi(kpi, 'formula_ref', plan.routeReason ?? 'no_measure');
  }, [measures, addToZone, handleInsertKpi]);

  const handleInsertKpiScorecard = useCallback(async () => {
    if (!kpis || kpis.length === 0) {
      showToast(strings.toasts.noKpisAvailable, 'info');
      return;
    }
    const scorecardKpis = buildScorecardPayload(kpis, measures || []);

    // Connectionless default: evaluate every KPI through the plugin protocol
    // and insert literal values. CUBE formulas remain available through the
    // explicit advanced formula actions, but the scorecard must work without a
    // workbook connection named "Tessallite".
    let evaluatedMap = new Map<string, { value: number | null; goal: number | null; status: number | null }>();
    try {
      const batchResults = await evaluateKpiBatch(
        projectId, modelId,
        scorecardKpis.map(k => k.id),
        personaId || undefined,
      );
      for (const r of batchResults) {
        evaluatedMap.set(r.kpi_id, { value: r.value, goal: r.goal, status: r.status });
      }
    } catch {
      showToast(strings.toasts.scorecardInsertionFailed, 'error');
      return;
    }

    // Enrich the scorecard payload with evaluated values for every KPI.
    const enrichedKpis = scorecardKpis.map(k => {
      const ev = evaluatedMap.get(k.id);
      return {
        ...k,
        evaluatedValue: ev?.value ?? null,
        evaluatedGoal: ev?.goal ?? null,
        evaluatedStatus: ev?.status ?? null,
      };
    });

    try {
      // Bug-6903: pass model slug for TESSALLITE.KPI formula scorecard.
      const rbModelSlug = modelsList?.find(ml => ml.id === modelId)?.slug;
      const result = await insertKpiScorecard(enrichedKpis, TESSALLITE_CONNECTION_NAME, rbModelSlug);
      if (result) {
        showToast(templates.toasts.scorecardInserted(scorecardKpis.length), 'success');
        for (const k of scorecardKpis) {
          reportKpiUsage(projectId, modelId, k.id, {
            cell_reference: result, usage_type: 'excel_insert',
          }).catch(() => {});
        }
      }
    } catch {
      showToast(strings.toasts.scorecardInsertionFailed, 'error');
    }
  }, [kpis, measures, insertKpiScorecard, showToast, projectId, modelId, personaId]);

  // Task 2: Refresh all tracked Tessallite tables on the active sheet.
  const [tableRefreshLoading, setTableRefreshLoading] = useState(false);
  // Bug-7397 R12-5: the per-table skip/warning REASONS the refresh computes.
  // Every honest skip message the R6-R11 redesign produced was unreachable from
  // the UI: the toast said "1 skipped (see details)" and there was no details
  // view behind it, so a user could not learn WHICH table was skipped, WHY, or
  // that simply refreshing again would fix a transient (concurrency / resize)
  // skip. These rows back the details surface rendered next to the button.
  const [refreshDetails, setRefreshDetails] = useState<RefreshDetailRow[]>([]);
  const [refreshGeneration, setRefreshGeneration] = useState(0);
  const handleRefreshSheetData = useCallback(async () => {
    setTableRefreshLoading(true);
    setRefreshDetails([]);
    setRefreshGeneration(g => g + 1);
    showToast(strings.tableRefresh.refreshing, 'info');
    try {
      const result = await refreshTables('activeSheet', personaId);
      setRefreshDetails(buildRefreshDetailRows(result));
      // Bug-7397 R12-R4-2: a table can now end up ONLY in `warnings` (a rewrite
      // that failed part-way through is neither a clean refresh nor an
      // untouched skip), so every branch below must consider all three counts.
      if (result.refreshed.length === 0 && result.skipped.length === 0 && result.warnings.length === 0) {
        showToast(strings.tableRefresh.noTables, 'info');
      } else if (result.skipped.length === 0 && result.warnings.length === 0) {
        showToast(templates.tableRefresh.refreshed(result.refreshed.length), 'success');
      } else if (result.refreshed.length === 0 && result.warnings.length === 0) {
        showToast(templates.tableRefresh.allSkipped(result.skipped.length), 'warning');
      } else if (result.refreshed.length === 0 && result.skipped.length === 0) {
        // Warnings only: nothing refreshed cleanly, nothing skipped cleanly --
        // "Refreshed 0 tables" would read as a non-event.
        showToast(templates.tableRefresh.warningsOnly(result.warnings.length), 'warning');
      } else if (result.skipped.length > 0 && result.warnings.length > 0) {
        // Bug-7397 R7-2: report refreshed / skipped / warned as THREE distinct
        // counts. A warned table was refreshed (not skipped); folding warnings
        // into the skipped count would overstate failures and mislabel a
        // success as a failure.
        showToast(
          templates.tableRefresh.refreshedWithSkippedAndWarnings(
            result.refreshed.length, result.skipped.length, result.warnings.length,
          ),
          'warning',
        );
      } else if (result.skipped.length > 0) {
        showToast(
          templates.tableRefresh.refreshedWithSkipped(result.refreshed.length, result.skipped.length),
          'warning',
        );
      } else {
        // Refreshed with warnings only (data written, some provenance failed).
        showToast(
          templates.tableRefresh.refreshedWithWarnings(result.refreshed.length, result.warnings.length),
          'warning',
        );
      }
    } catch {
      showToast(strings.toasts.queryExecutionFailed, 'error');
    } finally {
      setTableRefreshLoading(false);
    }
  }, [personaId, showToast]);

  // Task 3: Insert-mode toggle (live vs static).
  const [insertModeState, setInsertModeState] = useState<'live' | 'static'>('live');
  useEffect(() => {
    getInsertMode().then(setInsertModeState).catch(() => {});
  }, []);
  const handleInsertModeChange = useCallback(async (newMode: 'live' | 'static') => {
    const { setInsertMode } = await import('../../utils/storage');
    await setInsertMode(newMode);
    setInsertModeState(newMode);
  }, []);

  // F-025-11 / Bug-6904: add a named set to a zone as a bindable item. We resolve
  // the set's dimension (via the pure resolveNamedSetDimension helper) and its
  // member keys (via the preview endpoint) so the query builder can place the
  // dimension on the axis and constrain it with an `in` filter — instead of
  // sending the set's UUID, which the binder rejects.
  const handleAddNamedSetToZone = useCallback(async (ns: NamedSet, zone: Zone) => {
    const dim = resolveNamedSetDimension(ns);
    if (!dim) {
      showToast(strings.toasts.namedSetAdvancedExpression, 'info');
      return;
    }
    try {
      const preview = await previewNamedSet(projectId, modelId, ns.id, personaId || undefined);
      const memberKeys = (preview.items || []).map(i => i.key).filter(Boolean);
      if (memberKeys.length === 0) {
        showToast(strings.toasts.namedSetNoMembers, 'info');
        return;
      }
      // Bug-1112: a zone drop binds the named set as a static `in` filter over
      // the previewed keys. That is only correct when the preview returned the
      // full membership AND the set is not dynamic. If the preview truncated
      // (>server limit) the aggregate would silently under-count; if the set is
      // top-N / filtered its membership is frozen at this point in time. In
      // either case, do not silently bind a partial / stale key list — warn the
      // analyst and steer them to "Insert as formulas" (CUBESET), which keeps
      // full and dynamic membership live.
      const gate = evaluateNamedSetZoneGate(ns, preview);
      if (!gate.safe) {
        const nsLabel = ns.display_name || ns.name;
        const message =
          gate.reason === 'truncated_dynamic'
            ? templates.namedSet.dynamicTruncated(nsLabel, memberKeys.length, preview.total_count)
            : gate.reason === 'truncated'
              ? templates.namedSet.truncated(nsLabel, preview.total_count, memberKeys.length)
              : templates.namedSet.dynamic(nsLabel);
        showToast(message, 'warning');
        return;
      }
      addResolvedToZone({
        id: ns.id,
        name: ns.display_name || ns.name,
        zone,
        kind: 'named_set',
        bindDimension: dim,
        memberKeys,
      });
    } catch (e) {
      // Bug-8712: distinguish "not published" (404 withheld / 409 unreadable
      // deployed version) from a generic resolve failure. The generic message
      // tells the analyst to retry, which can never succeed here — the fix is a
      // Deploy, by someone else.
      showToast(
        e instanceof ApiError && (e.status === 404 || e.status === 409)
          ? strings.namedSetLibrary.previewNotPublished
          : strings.toasts.namedSetResolveFailed,
        'error',
      );
    }
    // Bug-6364: personaId must be a dependency — the named-set preview resolves
    // membership under the active persona, so a stale closure would bind the set
    // using the PREVIOUS persona's row-security view after a persona switch.
  }, [previewNamedSet, projectId, modelId, personaId, addResolvedToZone, showToast]);

  // F-025-11 / Bug-6738: add a hierarchy (whole or a specific level) to a
  // zone. The hierarchy list endpoint omits level key-attributes, so we fetch
  // detail and resolve the level(s) to bindable dimensions.
  //
  // Bug-6738: when adding a WHOLE hierarchy (level === undefined), add ALL
  // levels as ordered columns (coarse to fine) -- not just the leaf. If some
  // level cannot be resolved, warn explicitly (never-silent contract).
  const handleAddHierarchyToZone = useCallback(async (h: Hierarchy, level: HierarchyLevel | undefined, zone: Zone) => {
    try {
      const rawDetail = await getHierarchyDetail(projectId, modelId, h.id, personaId || undefined);
      // Bug-6738 / R1 Finding 3: sort defensively by level_number ascending
      // so the coarse-to-fine order is guaranteed locally regardless of the
      // backend's response ordering.
      const detail = [...rawDetail].sort((a, b) => a.level_number - b.level_number);

      if (level) {
        // Single level add: resolve the specific level.
        let dimName = level.dimensionName;
        if (!dimName) {
          const target = detail.find(l => l.name === level.name) ?? detail.find(l => l.level_number === level.level_number);
          dimName = target?.dimensionName;
        }
        if (!dimName) {
          showToast(strings.toasts.hierarchyResolveFailed, 'error');
          return;
        }
        const d = dimensions?.find(x => x.name === dimName);
        addResolvedToZone({
          id: `${h.id}:${level.level_number}`,
          name: `${h.display_name || h.name}: ${level.name}`,
          zone,
          kind: 'hierarchy_level',
          bindDimension: dimName,
          data_type: d?.data_type,
        });
      } else {
        // Bug-6738: whole hierarchy add -- add ALL levels coarse to fine.
        // The detail array is ordered by level_number ascending (coarsest
        // first), which is exactly the column order the user expects.
        const unresolvedLevels: string[] = [];
        let addedCount = 0;
        for (const lv of detail) {
          if (!lv.dimensionName) {
            unresolvedLevels.push(lv.name);
            continue;
          }
          const d = dimensions?.find(x => x.name === lv.dimensionName);
          addResolvedToZone({
            id: `${h.id}:${lv.level_number}`,
            name: `${h.display_name || h.name}: ${lv.name}`,
            zone,
            kind: 'hierarchy_level',
            bindDimension: lv.dimensionName,
            data_type: d?.data_type,
          });
          addedCount++;
        }
        if (unresolvedLevels.length > 0 && addedCount > 0) {
          // Some levels resolved, some did not -- warn explicitly.
          showToast(
            templates.toasts.hierarchyPartialLevels(h.display_name || h.name, unresolvedLevels),
            'warning',
          );
        } else if (addedCount === 0) {
          showToast(strings.toasts.hierarchyResolveFailed, 'error');
        }
      }
    } catch {
      showToast(strings.toasts.hierarchyDetailFailed, 'error');
    }
  }, [getHierarchyDetail, projectId, modelId, personaId, dimensions, addResolvedToZone, showToast]);

  useEffect(() => {
    if (kpisLoading || namedSetsLoading) return;
    if (!kpis && !namedSets) return;
    const allEntities: { id: string; type: 'named_set' | 'kpi'; certification_status: string; updated_at?: string }[] = [];
    if (namedSets) {
      for (const ns of namedSets) {
        allEntities.push({ id: ns.id, type: 'named_set', certification_status: ns.certification_status, updated_at: ns.updated_at });
      }
    }
    if (kpis) {
      for (const k of kpis) {
        allEntities.push({ id: k.id, type: 'kpi', certification_status: k.certification_status, updated_at: k.updated_at });
      }
    }
    const entityLookup = new Map(allEntities.map(e => [`${e.type}:${e.id}`, e]));
    // F-025-16: scope the staleness comparison to the loaded model so switching
    // models / opening another workbook does not flag healthy entities as
    // "Deleted from server".
    checkStaleEntities(allEntities, modelId).then(stale => {
      const skipKeys = new Set<string>();
      for (const s of stale) {
        const key = `${s.entry.type}:${s.entry.id}`;
        const current = entityLookup.get(key);
        if (s.entry.updatedAt && current?.updated_at && s.entry.updatedAt !== current.updated_at) {
          skipKeys.add(key);
        }
      }
      if (stale.length > 0) {
        setStaleDialogEntries(stale);
        pendingStaleUpdate.current = { entities: allEntities, skipKeys, modelId };
      } else {
        updateManifestStatuses(allEntities, undefined, modelId);
      }
    }).catch(() => {});
  }, [kpis, kpisLoading, namedSets, namedSetsLoading, modelId]);

  const loading = measuresLoading || dimsLoading || hierLoading;

  // F-025-27: sortable fields are the measures + row/column dimensions placed in
  // the zones, keyed by their resolved technical name (what order_by expects)
  // with the friendly label for display.
  const sortOptions = useMemo(() => {
    const seen = new Set<string>();
    const opts: { field: string; label: string }[] = [];
    for (const i of zoneItems) {
      if (i.zone !== 'values' && i.zone !== 'rows' && i.zone !== 'columns') continue;
      const field = resolveZoneItemName(i, { measures, dimensions });
      if (seen.has(field)) continue;
      seen.add(field);
      opts.push({ field, label: i.name });
    }
    return opts;
  }, [zoneItems, measures, dimensions]);

  // Clear a stale sort selection when its field leaves the zones.
  useEffect(() => {
    if (sortField && !sortOptions.some(o => o.field === sortField)) {
      setSortField('');
    }
  }, [sortOptions, sortField]);

  return (
    <Box sx={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      {modelsList && modelsList.length > 1 && onModelChange && (
        <Box sx={{ px: 1.5, py: 0.75, borderBottom: `1px solid ${tokens.colorBorderLight}`, bgcolor: tokens.colorWhite }}>
          <Typography sx={{ fontSize: 9, fontWeight: 700, color: tokens.colorTextSecondary, textTransform: 'uppercase', mb: 0.25 }}>
            {strings.reportBuilder.modelLabel}
          </Typography>
          <Select
            aria-label={strings.reportBuilder.modelSelectorAria}
            size="small"
            value={modelId}
            onChange={e => onModelChange(e.target.value as string)}
            sx={{ fontSize: 11, minWidth: 0, width: '100%', '& .MuiSelect-select': { py: 0.5, px: 1 } }}
          >
            {modelsList.map(m => (
              <MenuItem key={m.id} value={m.id} sx={{ fontSize: 11 }}>{m.name}</MenuItem>
            ))}
          </Select>
        </Box>
      )}

      <ZoneMappingGrid
        items={zoneItems}
        onRemove={removeFromZone}
        onClear={clearZones}
        onInsertTable={handleInsertTable}
        onInsertChart={handleInsertChart}
        onInsertLocalPivot={handleInsertLocalPivot}
        onOpenTemplates={() => setTemplatesOpen(true)}
        onUpdateFilter={updateFilterItem}
        compatibilityWarning={zoneCompatibility.blocking ? {
          title: strings.reportBuilder.incompatibleFields,
          messages: compatibilityMessages,
          compatibleDimensionNames: zoneCompatibility.compatibleDimensionNames,
        } : null}
        insertDisabledReason={compatibilityBlockedReason}
      />

      {sortOptions.length > 0 && (
        <Box
          sx={{
            display: 'flex', alignItems: 'center', gap: 0.75, px: 1.25, py: 0.75,
            borderBottom: `1px solid ${tokens.colorBorderLight}`, bgcolor: tokens.colorWhite,
          }}
        >
          <Typography sx={{ fontSize: 11, fontWeight: 700, color: tokens.colorTextSecondary }}>
            {strings.reportBuilder.sortBy}
          </Typography>
          <Select
            size="small"
            displayEmpty
            value={sortField}
            onChange={(e) => setSortField(e.target.value as string)}
            sx={{ fontSize: 11, minWidth: 140, flex: 1 }}
          >
            <MenuItem value="" sx={{ fontSize: 11 }}>{strings.reportBuilder.sortNone}</MenuItem>
            {sortOptions.map((o) => (
              <MenuItem key={o.field} value={o.field} sx={{ fontSize: 11 }}>{o.label}</MenuItem>
            ))}
          </Select>
          <Select
            size="small"
            value={sortDir}
            disabled={!sortField}
            onChange={(e) => setSortDir(e.target.value as 'asc' | 'desc')}
            sx={{ fontSize: 11, minWidth: 96 }}
          >
            <MenuItem value="asc" sx={{ fontSize: 11 }}>{strings.reportBuilder.sortAscending}</MenuItem>
            <MenuItem value="desc" sx={{ fontSize: 11 }}>{strings.reportBuilder.sortDescending}</MenuItem>
          </Select>
        </Box>
      )}

      <Box sx={{ px: 1.25, py: 1, borderBottom: `1px solid ${tokens.colorBorderLight}`, bgcolor: tokens.colorWhite }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mb: 0.75 }}>
          <ManageSearchOutlined sx={{ fontSize: 17, color: tokens.colorTextSecondary }} />
          <Box sx={{ minWidth: 0, flex: 1 }}>
            <Typography sx={{ fontSize: 12, fontWeight: 700, color: tokens.colorCharcoal, lineHeight: 1.2 }}>
              {strings.reportBuilder.availableFields}
            </Typography>
            <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, lineHeight: 1.2 }}>
              {strings.reportBuilder.availableFieldsHint}
            </Typography>
          </Box>
        </Box>
        <SearchBar
          value={search}
          onChange={setSearch}
          placeholder={strings.reportBuilder.searchPlaceholder}
        />
        <Box sx={{ display: 'flex', gap: 0.5, mt: 0.75 }}>
          <Chip
            label={strings.reportBuilder.certifiedOnly}
            size="small"
            clickable
            variant={certifiedOnly ? 'filled' : 'outlined'}
            color={certifiedOnly ? 'success' : 'default'}
            onClick={() => setCertifiedOnly(!certifiedOnly)}
            sx={{ fontSize: 10, height: 20 }}
          />
        </Box>
      </Box>

      {loading ? (
        <Box sx={{ flex: 1, p: 1.5 }}>
          <Skeleton variant="text" width="60%" height={20} sx={{ mb: 1 }} />
          <Skeleton variant="rectangular" height={48} sx={{ mb: 0.5, borderRadius: 1 }} />
          <Skeleton variant="rectangular" height={48} sx={{ mb: 0.5, borderRadius: 1 }} />
          <Skeleton variant="rectangular" height={48} sx={{ mb: 2, borderRadius: 1 }} />
          <Skeleton variant="text" width="50%" height={20} sx={{ mb: 1 }} />
          <Skeleton variant="rectangular" height={64} sx={{ mb: 0.5, borderRadius: 1 }} />
          <Skeleton variant="rectangular" height={64} sx={{ mb: 0.5, borderRadius: 1 }} />
        </Box>
      ) : (
        <Box sx={{ flex: 1, overflowY: 'auto' }}>
          <MeasureLibrary
            measures={filteredMeasures}
            searchQuery={debouncedSearch}
            selectedMeasureIds={selectedMeasureIds}
            onToggleMeasure={(measureId) => {
              if (zoneItems.some(i => i.id === measureId && i.zone === 'values')) {
                removeFromZone(measureId, 'values');
              }
            }}
            onAddToValues={(measureId) => {
              const m = filteredMeasures.find(fm => fm.id === measureId);
              if (m) addToZone(m.id, m.display_name, 'values');
            }}
            onInsertMeasureAsFunction={handleInsertMeasureAsFunction}
            onInsertMeasureAsFormula={handleInsertMeasureAsFormula}
            expanded={measuresExpanded}
            onToggleExpanded={() => setMeasuresExpanded(!measuresExpanded)}
            loading={measuresLoading}
            glossaryEntries={glossary}
          />

          <KpiLibrary
            kpis={filteredKpis}
            measures={measures || []}
            projectId={projectId}
            modelId={modelId}
            personaId={personaId}
            searchQuery={debouncedSearch}
            selectedKpiValueMeasureIds={selectedMeasureIds}
            onToggleKpi={(kpi: Kpi) => {
              // Bug-6715: untoggle is the exact inverse of Add -- it removes
              // BOTH measures Add staged (value and distinct goal), not just
              // the value, so no orphan goal chip stays behind.
              const stagedIds = zoneItems.filter(i => i.zone === 'values').map(i => i.id);
              for (const id of planKpiZoneRemove(kpi, stagedIds)) {
                removeFromZone(id, 'values');
              }
            }}
            onAddKpiToValues={handleAddKpiToValues}
            onInsertKpiAsFormulas={handleInsertKpiAsFormulas}
            onInsertKpi={handleInsertKpi}
            onInsertScorecard={handleInsertKpiScorecard}
            expanded={kpisExpanded}
            onToggleExpanded={() => setKpisExpanded(!kpisExpanded)}
            loading={kpisLoading}
          />

          <NamedSetLibrary
            namedSets={filteredNamedSets}
            searchQuery={debouncedSearch}
            projectId={projectId}
            modelId={modelId}
            personaId={personaId || undefined}
            onAddToRows={(ns: NamedSet) => { handleAddNamedSetToZone(ns, 'rows'); }}
            onAddToColumns={(ns: NamedSet) => { handleAddNamedSetToZone(ns, 'columns'); }}
            onAddToFilter={(ns: NamedSet) => { handleAddNamedSetToZone(ns, 'filters'); }}
            onInsertAsFormulas={handleInsertNamedSetAsFormulas}
            expanded={namedSetsExpanded}
            onToggleExpanded={() => setNamedSetsExpanded(!namedSetsExpanded)}
            loading={namedSetsLoading}
          />

          <DimensionLibrary
            dimensions={filteredDimensions}
            searchQuery={debouncedSearch}
            onAddToRows={(dimId) => {
              const d = filteredDimensions.find(fd => fd.id === dimId);
              if (d) addToZone(d.id, d.display_name, 'rows');
            }}
            onAddToColumns={(dimId) => {
              const d = filteredDimensions.find(fd => fd.id === dimId);
              if (d) addToZone(d.id, d.display_name, 'columns');
            }}
            onAddToFilter={(dimId) => {
              const d = filteredDimensions.find(fd => fd.id === dimId);
              if (d) addToZone(d.id, d.display_name, 'filters', d.data_type);
            }}
            onPreviewMembers={handlePreviewMembers}
            expanded={dimsExpanded}
            onToggleExpanded={() => setDimsExpanded(!dimsExpanded)}
            loading={dimsLoading}
            memberPreviewDimId={memberPreviewDimId}
            memberPreview={memberPreview}
            membersPreviewLoading={membersPreviewLoading}
            onCloseMemberPreview={() => setMemberPreviewDimId(null)}
            compatibilityByDimensionId={zoneCompatibility.unavailableByDimensionId}
          />

          <HierarchyLibrary
            hierarchies={hierarchies || []}
            expanded={hierExpanded}
            onToggle={() => setHierExpanded(!hierExpanded)}
            onAssignToRows={(h, level) => { handleAddHierarchyToZone(h, level, 'rows'); }}
          />

          <Box sx={{ px: 1.25, py: 0.5, display: 'flex', alignItems: 'center', gap: 0.75, borderBottom: `1px solid ${tokens.colorBorderLight}` }}>
            <Typography sx={{ fontSize: 10, fontWeight: 700, color: tokens.colorTextSecondary }}>
              {strings.insertMode.label}
            </Typography>
            <Chip
              label={strings.insertMode.live}
              size="small"
              clickable
              variant={insertModeState === 'live' ? 'filled' : 'outlined'}
              color={insertModeState === 'live' ? 'primary' : 'default'}
              onClick={() => handleInsertModeChange('live')}
              sx={{ fontSize: 10, height: 20 }}
            />
            <Chip
              label={strings.insertMode.static}
              size="small"
              clickable
              variant={insertModeState === 'static' ? 'filled' : 'outlined'}
              color={insertModeState === 'static' ? 'primary' : 'default'}
              onClick={() => handleInsertModeChange('static')}
              sx={{ fontSize: 10, height: 20 }}
            />
          </Box>

          <Box sx={{ p: 1.25, display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(72px, 1fr))', gap: 0.5 }}>
            <Button
              size="small"
              variant="outlined"
              startIcon={<RefreshOutlined sx={{ fontSize: 16 }} />}
              onClick={() => { refreshCustomFunctionValues(); showToast(strings.toasts.refreshingValues, 'info'); }}
              sx={{ fontSize: 11, minWidth: 0, px: 0.75, '& .MuiButton-startIcon': { mr: 0.5 } }}
            >
              {strings.reportBuilder.refreshValues}
            </Button>
            <Button
              size="small"
              variant="outlined"
              aria-label={strings.tableRefresh.refreshSheetDataAria}
              startIcon={<TableChartOutlined sx={{ fontSize: 16 }} />}
              onClick={handleRefreshSheetData}
              disabled={tableRefreshLoading}
              sx={{ fontSize: 11, minWidth: 0, px: 0.75, '& .MuiButton-startIcon': { mr: 0.5 } }}
            >
              {strings.tableRefresh.refreshSheetData}
            </Button>
            <Button
              size="small"
              variant="outlined"
              startIcon={<FunctionsOutlined sx={{ fontSize: 16 }} />}
              onClick={() => setCubeWizardOpen(true)}
              sx={{ fontSize: 11, minWidth: 0, px: 0.75, '& .MuiButton-startIcon': { mr: 0.5 } }}
            >
              {strings.reportBuilder.cubeButton}
            </Button>
            <Button
              size="small"
              variant="outlined"
              startIcon={<AccountTreeOutlined sx={{ fontSize: 16 }} />}
              onClick={() => setConnWizardOpen(true)}
              sx={{ fontSize: 11, minWidth: 0, px: 0.75, '& .MuiButton-startIcon': { mr: 0.5 } }}
            >
              {strings.reportBuilder.connectButton}
            </Button>
            {lastQuery && (
              <Button
                size="small"
                variant="outlined"
                startIcon={<ManageSearchOutlined sx={{ fontSize: 16 }} />}
                onClick={() => setTraceOpen(true)}
                sx={{ fontSize: 11, minWidth: 0, px: 0.75, '& .MuiButton-startIcon': { mr: 0.5 } }}
              >
                {strings.reportBuilder.traceButton}
              </Button>
            )}
          </Box>

          {/*
            Bug-7397 R12-5: the details surface behind the refresh toast's
            "(see details)". Without it every honest skip reason the refresh
            computes -- concurrent modification, table resized, corrupted
            provenance, blocked cell blocks -- was dead text the user could
            never read, and a transient skip looked identical to a permanent
            failure. Keyed by refresh generation so a new refresh collapses the
            previous run's expanded list instead of showing stale rows open.
          */}
          <RefreshDetailsPanel key={refreshGeneration} rows={refreshDetails} />
        </Box>
      )}

      <TemplatePicker
        open={templatesOpen}
        onClose={() => setTemplatesOpen(false)}
        onSelect={handleTemplateSelect}
        measureCount={filteredMeasures.length}
        hasTimeDimension={dimensions?.some(d => d.is_time_dimension) || false}
        hasCategoricalDimension={dimensions?.some(d => !d.is_time_dimension && isTextualType(d.data_type)) || false}
      />

      <CubeFormulaWizard
        open={cubeWizardOpen}
        onClose={() => setCubeWizardOpen(false)}
        measures={measures || []}
        dimensions={dimensions || []}
        projectId={projectId}
        modelId={modelId}
        personaId={personaId || undefined}
        connectionName={TESSALLITE_CONNECTION_NAME}
        onInsertFormula={handleInsertFormula}
      />

      <LiveConnectionWizard
        open={connWizardOpen}
        onClose={() => setConnWizardOpen(false)}
        serverUrl={serverUrl}
        catalog={xmlaCatalog}
      />

      <TraceModal
        open={traceOpen}
        onClose={() => setTraceOpen(false)}
        query={lastQuery}
        route={lastRoute}
        modelId={modelId}
        personaId={personaId}
      />

      <Dialog
        open={staleDialogEntries.length > 0}
        onClose={() => setStaleDialogEntries([])}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <WarningAmberIcon color="warning" />
          {strings.reportBuilder.staleDialogTitle}
        </DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" mb={2}>
            {strings.reportBuilder.staleDialogDescription}
          </Typography>
          <List dense>
            {staleDialogEntries.map(s => (
              <ListItem key={`${s.entry.type}:${s.entry.id}`}>
                <ListItemIcon sx={{ minWidth: 32 }}>
                  <WarningAmberIcon fontSize="small" color={s.reason === 'deprecated' || s.reason === 'deleted' ? 'error' : 'warning'} />
                </ListItemIcon>
                <ListItemText
                  primary={s.entry.displayName}
                  secondary={`${s.entry.type === 'kpi' ? strings.reportBuilder.staleKpi : strings.reportBuilder.staleNamedSet} — ${
                    s.reason === 'deleted' ? strings.reportBuilder.staleDeleted :
                    s.reason === 'deprecated' ? strings.reportBuilder.staleDeprecated :
                    s.reason === 'status_changed' ? templates.reportBuilder.staleStatusChanged(s.currentStatus) :
                    strings.reportBuilder.staleDefinitionUpdated
                  }${s.entry.cellLocations.length > 0 ? ` — ${strings.reportBuilder.staleCells} ${s.entry.cellLocations.join(', ')}` : ''}`}
                  primaryTypographyProps={{ fontSize: 13, fontWeight: 600 }}
                  secondaryTypographyProps={{ fontSize: 11 }}
                />
              </ListItem>
            ))}
          </List>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setStaleDialogEntries([])} size="small">
            {strings.reportBuilder.staleLater}
          </Button>
          <Button
            onClick={() => {
              if (pendingStaleUpdate.current) {
                updateManifestStatuses(
                  pendingStaleUpdate.current.entities,
                  pendingStaleUpdate.current.skipKeys.size > 0 ? pendingStaleUpdate.current.skipKeys : undefined,
                  pendingStaleUpdate.current.modelId,
                );
                pendingStaleUpdate.current = null;
              }
              setStaleDialogEntries([]);
            }}
            size="small"
            variant="contained"
            color="primary"
          >
            {strings.reportBuilder.staleAcceptCurrent}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
