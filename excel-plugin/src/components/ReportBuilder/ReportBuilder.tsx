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
} from '@mui/icons-material';
import { tokens } from '../../theme';
import SearchBar from '../common/SearchBar';
import { useMeasures, useDimensions, useHierarchies, useKpis, useNamedSets, useGlossary, useAliasMap, useFieldCompatibility } from '../../hooks/useModel';
import { executeQuery, discoverMembers, type PluginExecuteParams } from '../../api/queryRouter';
import { useToast } from '../Toast/ToastProvider';
import { useExcel } from '../../hooks/useExcel';
import type { ReportTemplate } from '../../utils/reportTemplates';
import { ApiError, formatApiError } from '../../api/client';
import { isTextualType } from '../../utils/dataTypes';
import { buildXmlaCatalogName, TESSALLITE_CONNECTION_NAME } from '../../utils/excelFormulas';
import { buildZoneQuery, evaluateNamedSetZoneGate, resolveZoneAxes, pivotZoneResult, resolveZoneItemName } from '../../utils/zoneQuery';
import { buildScorecardPayload } from '../../utils/kpiScorecard';
import { evaluateZoneFieldCompatibility, formatZoneCompatibilityMessages } from '../../utils/fieldCompatibility';
import type { Measure, Dimension, Kpi, NamedSet, Hierarchy, HierarchyLevel, SemanticQuery, ExecuteResponse, DiscoverMembersResponse, PluginRouteTrace } from '../../types/tessallite';
import { checkStaleEntities, updateManifestStatuses, trackEntityUsage, type StaleEntity } from '../../utils/workbookMetadata';
import { reportNamedSetUsage, reportKpiUsage, previewNamedSet, getHierarchyDetail } from '../../api/modelService';
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
  const handleBusy = useCallback(() => showToast('An Excel operation is already in progress', 'info'), [showToast]);
  const {
    insertTable: excelInsertTable, insertFormula, insertChart: excelInsertChart,
    insertLocalPivot: excelInsertLocalPivot, insertNamedSetAsFormulas, insertKpiFormulas,
    insertKpiFullRow, insertKpiValueOnly, insertKpiStatusOnly, insertKpiTrendOnly,
    insertKpiValueFormula, insertMeasureAsFormula, insertKpiScorecard,
  } = useExcel(undefined, handleBusy);

  const { data: measures, isLoading: measuresLoading } = useMeasures(projectId, modelId, personaId);
  const { data: dimensions, isLoading: dimsLoading } = useDimensions(projectId, modelId, personaId);
  const { data: hierarchies, isLoading: hierLoading } = useHierarchies(projectId, modelId, personaId);
  const { data: kpis, isLoading: kpisLoading } = useKpis(projectId, modelId);
  const { data: namedSets, isLoading: namedSetsLoading } = useNamedSets(projectId, modelId);
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

  useEffect(() => {
    setZoneItems([]);
    setSortField('');
  }, [personaId]);

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
      setMemberPreview(result);
    } catch {
      setMemberPreview(null);
      showToast('Could not load members', 'error');
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
    ? 'Remove incompatible fields before inserting this report.'
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
    setZoneItems(prev => prev.map(i =>
      i.id === id ? { ...i, operator, values } : i,
    ));
  }, []);

  const removeFromZone = useCallback((id: string) => {
    setZoneItems(prev => prev.filter(i => i.id !== id));
  }, []);

  const clearZones = useCallback(() => {
    setZoneItems([]);
  }, []);

  // F-025-07: the live XMLA connection's Initial Catalog must be the gateway's
  // published catalog name — `<model slug>` (+ `_<persona slug>` when a persona
  // is active) — NOT the model UUID, which the connect dialog never lists.
  const xmlaCatalog = useMemo(() => {
    const slug = modelsList?.find(m => m.id === modelId)?.slug || '';
    return buildXmlaCatalogName(slug, personaSlug);
  }, [modelsList, modelId, personaSlug]);

  const executeZoneQuery = useCallback(async (): Promise<{ headers: string[]; rows: (string | number)[][]; annotation?: ExecuteResponse['annotation']; query: SemanticQuery; pivoted: boolean } | null> => {
    // F-025-11: named-set and hierarchy-level items bind via their resolved
    // dimension (+ member-key `in` filter for named sets), never their raw UUID
    // token. buildZoneQuery is the single, unit-tested translation.
    // F-025-27: pass the optional sort selection through to the query-router's
    // order_by. buildZoneQuery drops the field if it is not in the query.
    const order = sortField ? { [sortField]: sortDir } : undefined;
    const query = buildZoneQuery(zoneItems, { measures, dimensions }, 1000, order);
    if (!query) return null;

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
      if (!result.data || result.data.length === 0) {
        showToast('Query returned no results', 'info');
        return null;
      }

      // F-025-15: the query-router always returns a flat grouped result, so a
      // dimension placed on the Columns axis must be pivoted client-side to
      // become a real cross-tab (column-dim members as side-by-side headers).
      // resolveZoneAxes gives the row/column split; pivotZoneResult reshapes the
      // flat rows. With no column dims this is the unchanged flat table.
      const { rowDimNames, colDimNames } = resolveZoneAxes(zoneItems, { measures, dimensions });
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
        rowDimNames,
        colDimNames,
        measureKeys,
        titles,
      );

      return { headers, rows, annotation: result.annotation, query, pivoted: colDimNames.length > 0 };
    } catch (e) {
      if (e instanceof ApiError) {
        // Surface the contract's readable reason (e.g. the 422 filter-operator
        // message from the canonical filter contract, or a persona 403) rather
        // than a generic "Query execution failed". formatApiError maps 403 to a
        // permission notice and 422 to "Validation error: <detail>".
        showToast(formatApiError(e), 'error');
      } else {
        showToast('Query execution failed', 'error');
      }
      console.error('Report Builder query failed:', e);
      return null;
    }
  }, [zoneItems, measures, dimensions, showToast, projectId, modelId, personaId, sortField, sortDir]);

  const handleInsertTable = useCallback(async () => {
    if (zoneCompatibility.blocking) {
      showToast(compatibilityBlockedReason || 'Remove incompatible fields before inserting this report.', 'warning');
      return;
    }
    const result = await executeZoneQuery();
    if (!result) return;
    const formatTokens: Record<string, string> = {};
    // F-025-06: measureColumns maps a column TITLE to the measure's UUID — not
    // its semantic name. The annotation key is the measure NAME (plugin.py
    // `_build_annotation` keys on `m.name`), and the drill-through routes take
    // a measure UUID path param, so storing the name produced a 422. Resolve
    // name -> id via the loaded measure list.
    //
    // F-025-15: drill-through provenance maps a single measure/dimension column
    // TITLE to its id/name. A pivoted cross-tab has composite headers
    // ("Q1 — Revenue") with no 1:1 title mapping, so the per-cell drill
    // metadata is omitted for pivoted inserts (the flat-table path keeps it).
    const measureColumns: Record<string, string> = {};
    const dimensionColumns: Record<string, string> = {};
    if (result.annotation?.measures) {
      for (const [key, m] of Object.entries(result.annotation.measures)) {
        if (m.format) formatTokens[key] = m.format;
        if (m.title && m.format) formatTokens[m.title] = m.format;
        if (!result.pivoted) {
          const measureId = measures?.find(x => x.name === key)?.id;
          if (measureId) measureColumns[m.title] = measureId;
        }
      }
    }
    // F-025-06: dimensionColumns maps a dimension column TITLE to its semantic
    // name so a drill from a row cell can read that row's dimension values and
    // send them as filters (the cell coordinates). Without this the drill was
    // globally unfiltered (silent-wrong-data).
    if (!result.pivoted && result.annotation?.dimensions) {
      for (const [name, d] of Object.entries(result.annotation.dimensions)) {
        if (d.title) dimensionColumns[d.title] = name;
      }
    }
    try {
      const rangeAddress = await excelInsertTable(result.headers, result.rows, undefined, {
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
      if (rangeAddress) {
        showToast(`Inserted ${result.rows.length} rows`, 'success');
      }
    } catch (e) {
      showToast('Insert failed', 'error');
    }
  }, [executeZoneQuery, excelInsertTable, showToast, projectId, modelId, personaId, measures, zoneCompatibility.blocking, compatibilityBlockedReason]);

  const handleInsertChart = useCallback(async () => {
    if (zoneCompatibility.blocking) {
      showToast(compatibilityBlockedReason || 'Remove incompatible fields before inserting this chart.', 'warning');
      return;
    }
    const result = await executeZoneQuery();
    if (!result) return;
    try {
      await excelInsertChart(result.headers, result.rows, undefined, result.annotation);
      showToast('Chart created', 'success');
    } catch (e) {
      showToast('Chart creation failed', 'error');
    }
  }, [executeZoneQuery, excelInsertChart, showToast, zoneCompatibility.blocking, compatibilityBlockedReason]);

  const handleInsertLocalPivot = useCallback(async () => {
    if (zoneCompatibility.blocking) {
      showToast(compatibilityBlockedReason || 'Remove incompatible fields before inserting a local PivotTable.', 'warning');
      return;
    }
    const result = await executeZoneQuery();
    if (!result) return;
    try {
      await excelInsertLocalPivot(result.headers, result.rows, undefined, result.annotation);
      showToast('Local PivotTable created', 'success');
    } catch (e) {
      showToast('Local PivotTable failed. Requires Excel 2019+ or Web.', 'error');
    }
  }, [executeZoneQuery, excelInsertLocalPivot, showToast, zoneCompatibility.blocking, compatibilityBlockedReason]);

  const handleTemplateSelect = useCallback((template: ReportTemplate) => {
    clearZones();

    if (template.id === 'time-series') {
      const timeDim = dimensions?.find(d => d.is_time_dimension);
      if (timeDim) addToZone(timeDim.id, timeDim.display_name, 'rows');
      filteredMeasures.forEach((m, i) => { if (i < 2) addToZone(m.id, m.display_name, 'values'); });
    } else if (template.id === 'top-n' || template.id === 'geographic') {
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
    insertFormula(formula, targetCell).then(() => {
      showToast('Formula inserted', 'success');
    }).catch(() => {
      showToast('Formula insertion failed', 'error');
    });
  }, [insertFormula, showToast]);

  const handleInsertNamedSetAsFormulas = useCallback(async (ns: NamedSet) => {
    try {
      const result = await insertNamedSetAsFormulas(
        { id: ns.id, name: ns.name, display_name: ns.display_name, expression: ns.expression, updated_at: ns.updated_at },
        TESSALLITE_CONNECTION_NAME,
      );
      if (result) {
        showToast('CUBESET formulas inserted', 'success');
        reportNamedSetUsage(projectId, modelId, ns.id, {
          cell_reference: result, usage_type: 'excel_insert',
        }).catch(() => {});
      }
    } catch {
      showToast('Formula insertion failed', 'error');
    }
  }, [insertNamedSetAsFormulas, showToast, projectId, modelId]);

  const handleInsertKpiAsFormulas = useCallback(async (kpi: Kpi) => {
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
        showToast('KPI formulas inserted', 'success');
        reportKpiUsage(projectId, modelId, kpi.id, {
          cell_reference: result, usage_type: 'excel_insert',
        }).catch(() => {});
      }
    } catch {
      showToast('Formula insertion failed', 'error');
    }
  }, [insertKpiFormulas, measures, showToast, projectId, modelId]);

  const handleInsertMeasureAsFormula = useCallback(async (measureId: string) => {
    const m = measures?.find(fm => fm.id === measureId);
    if (!m) return;
    try {
      const result = await insertMeasureAsFormula(m.name, TESSALLITE_CONNECTION_NAME);
      if (result) showToast('CUBEVALUE formula inserted', 'success');
    } catch {
      showToast('Formula insertion failed', 'error');
    }
  }, [measures, insertMeasureAsFormula, showToast]);

  const handleInsertKpi = useCallback(async (kpi: Kpi, mode: string) => {
    const valueMeasure = measures?.find(m => m.id === kpi.value_measure_id);
    const goalMeasure = measures?.find(m => m.id === kpi.goal_measure_id);
    const kpiDisplayName = kpi.display_name || kpi.name;
    // Bug-5294: compute static goal literal for individual KPI insert paths
    const goalLiteral = (kpi.goal_measure_id == null && kpi.target_type === 'static')
      ? kpi.target_value : null;

    try {
      let result: string | null = null;
      switch (mode) {
        case 'full_row':
          result = await insertKpiFullRow(
            { id: kpi.id, name: kpi.name, display_name: kpi.display_name, updated_at: kpi.updated_at },
            valueMeasure?.name || null,
            goalMeasure?.name || null,
            TESSALLITE_CONNECTION_NAME,
            goalLiteral,
          );
          break;
        case 'value_only':
          if (valueMeasure) {
            result = await insertKpiValueOnly(valueMeasure.name, TESSALLITE_CONNECTION_NAME);
          }
          break;
        case 'value_goal':
          result = await insertKpiFormulas(
            { id: kpi.id, name: kpi.name, display_name: kpi.display_name, updated_at: kpi.updated_at },
            valueMeasure?.name || null,
            goalMeasure?.name || null,
            TESSALLITE_CONNECTION_NAME,
            goalLiteral,
          );
          break;
        case 'status_only':
          result = await insertKpiStatusOnly(kpi.name, TESSALLITE_CONNECTION_NAME);
          break;
        case 'trend_only':
          result = await insertKpiTrendOnly(kpi.name, TESSALLITE_CONNECTION_NAME);
          break;
        case 'kpi_card':
          result = await insertKpiFullRow(
            { id: kpi.id, name: kpi.name, display_name: kpi.display_name, updated_at: kpi.updated_at },
            valueMeasure?.name || null,
            goalMeasure?.name || null,
            TESSALLITE_CONNECTION_NAME,
            goalLiteral,
          );
          break;
        case 'formula_ref':
          result = await insertKpiValueFormula(kpi.name, TESSALLITE_CONNECTION_NAME);
          break;
      }
      if (result) {
        if (mode === 'value_only' || mode === 'status_only' || mode === 'trend_only' || mode === 'formula_ref') {
          await trackEntityUsage('kpi', kpi.id, kpiDisplayName, result, undefined, kpi.updated_at, modelId);
        }
        showToast(`KPI ${mode.replace('_', ' ')} inserted`, 'success');
        reportKpiUsage(projectId, modelId, kpi.id, {
          cell_reference: result, usage_type: 'excel_insert',
        }).catch(() => {});
      }
    } catch {
      showToast('KPI insertion failed', 'error');
    }
  }, [measures, insertKpiFullRow, insertKpiValueOnly, insertKpiFormulas, insertKpiStatusOnly, insertKpiTrendOnly, insertKpiValueFormula, showToast, projectId, modelId]);

  const handleInsertKpiScorecard = useCallback(async () => {
    if (!kpis || kpis.length === 0) {
      showToast('No KPIs available', 'info');
      return;
    }
    const scorecardKpis = buildScorecardPayload(kpis, measures || []);
    try {
      const result = await insertKpiScorecard(scorecardKpis, TESSALLITE_CONNECTION_NAME);
      if (result) {
        showToast(`KPI scorecard inserted (${scorecardKpis.length} KPIs)`, 'success');
        for (const k of scorecardKpis) {
          reportKpiUsage(projectId, modelId, k.id, {
            cell_reference: result, usage_type: 'excel_insert',
          }).catch(() => {});
        }
      }
    } catch {
      showToast('Scorecard insertion failed', 'error');
    }
  }, [kpis, measures, insertKpiScorecard, showToast, projectId, modelId]);

  // F-025-11: derive a named set's bound dimension from its builder definition
  // (`entity`) or its stored `dimensions` field. A raw/MDX named set has no
  // single bindable dimension and cannot become a zone constraint.
  const namedSetDimension = useCallback((ns: NamedSet): string | null => {
    const bd = ns.builder_definition as { entity?: unknown } | null;
    const entity = bd && typeof bd.entity === 'string' ? bd.entity.trim() : '';
    if (entity) return entity;
    const dims = (ns.dimensions || '').split(',').map(s => s.trim()).filter(Boolean);
    return dims.length === 1 ? dims[0] : null;
  }, []);

  // F-025-11: add a named set to a zone as a bindable item. We resolve the
  // set's dimension and its member keys (via the preview endpoint) so the
  // query builder can place the dimension on the axis and constrain it with an
  // `in` filter — instead of sending the set's UUID, which the binder rejects.
  const handleAddNamedSetToZone = useCallback(async (ns: NamedSet, zone: Zone) => {
    const dim = namedSetDimension(ns);
    if (!dim) {
      showToast('This named set uses an advanced expression and cannot be dropped into a zone. Use "Insert as formulas" instead.', 'info');
      return;
    }
    try {
      const preview = await previewNamedSet(projectId, modelId, ns.id);
      const memberKeys = (preview.items || []).map(i => i.key).filter(Boolean);
      if (memberKeys.length === 0) {
        showToast('This named set has no resolvable members to filter by.', 'info');
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
        const message =
          gate.reason === 'truncated_dynamic'
            ? `"${ns.display_name || ns.name}" is a dynamic set with more members than can be captured in a zone (showing ${memberKeys.length} of ${preview.total_count}). A zone drop would freeze a partial, point-in-time list. Use "Insert as formulas" for the full, live set.`
            : gate.reason === 'truncated'
              ? `"${ns.display_name || ns.name}" has more members (${preview.total_count}) than can be captured in a zone (only ${memberKeys.length} would bind), so the result would under-count. Use "Insert as formulas" for the full set.`
              : `"${ns.display_name || ns.name}" is a dynamic set; a zone drop freezes its membership at this point in time. Use "Insert as formulas" to keep it live.`;
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
    } catch {
      showToast('Could not resolve this named set. Try again.', 'error');
    }
  }, [namedSetDimension, previewNamedSet, projectId, modelId, addResolvedToZone, showToast]);

  // F-025-11: add a hierarchy (whole or a specific level) to a zone. The
  // hierarchy list endpoint omits level key-attributes, so we fetch detail and
  // resolve the level to its bindable dimension. A whole-hierarchy drop binds
  // its leaf level.
  const handleAddHierarchyToZone = useCallback(async (h: Hierarchy, level: HierarchyLevel | undefined, zone: Zone) => {
    try {
      let dimName = level?.dimensionName;
      let label = level ? `${h.display_name || h.name}: ${level.name}` : (h.display_name || h.name);
      if (!dimName) {
        const detail = await getHierarchyDetail(projectId, modelId, h.id, personaId || undefined);
        const target = level
          ? detail.find(l => l.level_number === level.level_number)
          : detail[detail.length - 1]; // whole hierarchy -> leaf level
        dimName = target?.dimensionName;
        if (target && !level) label = `${h.display_name || h.name}: ${target.name}`;
      }
      if (!dimName) {
        showToast('Could not resolve this hierarchy level to a field.', 'error');
        return;
      }
      const d = dimensions?.find(x => x.name === dimName);
      addResolvedToZone({
        id: level ? `${h.id}:${level.level_number}` : h.id,
        name: label,
        zone,
        kind: 'hierarchy_level',
        bindDimension: dimName,
        data_type: d?.data_type,
      });
    } catch {
      showToast('Could not load hierarchy detail. Try again.', 'error');
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
            Model
          </Typography>
          <Select
            aria-label="Model selector"
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
          title: 'Selected fields are not compatible',
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
            Sort by
          </Typography>
          <Select
            size="small"
            displayEmpty
            value={sortField}
            onChange={(e) => setSortField(e.target.value as string)}
            sx={{ fontSize: 11, minWidth: 140, flex: 1 }}
          >
            <MenuItem value="" sx={{ fontSize: 11 }}>None</MenuItem>
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
            <MenuItem value="asc" sx={{ fontSize: 11 }}>Ascending</MenuItem>
            <MenuItem value="desc" sx={{ fontSize: 11 }}>Descending</MenuItem>
          </Select>
        </Box>
      )}

      <Box sx={{ px: 1.25, py: 1, borderBottom: `1px solid ${tokens.colorBorderLight}`, bgcolor: tokens.colorWhite }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mb: 0.75 }}>
          <ManageSearchOutlined sx={{ fontSize: 17, color: tokens.colorTextSecondary }} />
          <Box sx={{ minWidth: 0, flex: 1 }}>
            <Typography sx={{ fontSize: 12, fontWeight: 700, color: tokens.colorCharcoal, lineHeight: 1.2 }}>
              Available fields
            </Typography>
            <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, lineHeight: 1.2 }}>
              Click a field's zone icon to place it above.
            </Typography>
          </Box>
        </Box>
        <SearchBar
          value={search}
          onChange={setSearch}
          placeholder="Search measures, KPIs, lists, dimensions..."
        />
        <Box sx={{ display: 'flex', gap: 0.5, mt: 0.75 }}>
          <Chip
            label="Certified only"
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
              if (zoneItems.some(i => i.id === measureId)) {
                removeFromZone(measureId);
              }
            }}
            onAddToValues={(measureId) => {
              const m = filteredMeasures.find(fm => fm.id === measureId);
              if (m) addToZone(m.id, m.display_name, 'values');
            }}
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
            searchQuery={debouncedSearch}
            selectedKpiValueMeasureIds={selectedMeasureIds}
            onToggleKpi={(kpi: Kpi) => {
              if (kpi.value_measure_id && zoneItems.some(i => i.id === kpi.value_measure_id)) {
                removeFromZone(kpi.value_measure_id);
              }
            }}
            onAddKpiToValues={(kpi: Kpi) => {
              if (kpi.value_measure_id) {
                const m = measures?.find(fm => fm.id === kpi.value_measure_id);
                const label = kpi.display_name || kpi.name;
                addToZone(kpi.value_measure_id, m?.display_name || label, 'values');
                if (kpi.goal_measure_id && kpi.goal_measure_id !== kpi.value_measure_id) {
                  const gm = measures?.find(fm => fm.id === kpi.goal_measure_id);
                  if (gm) addToZone(kpi.goal_measure_id, gm.display_name, 'values');
                }
              }
            }}
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

          <Box sx={{ p: 1.25, display: 'grid', gridTemplateColumns: lastQuery ? '1fr 1fr 1fr' : '1fr 1fr', gap: 0.5 }}>
            <Button
              size="small"
              variant="outlined"
              startIcon={<FunctionsOutlined sx={{ fontSize: 16 }} />}
              onClick={() => setCubeWizardOpen(true)}
              sx={{ fontSize: 11, minWidth: 0, px: 0.75, '& .MuiButton-startIcon': { mr: 0.5 } }}
            >
              CUBE
            </Button>
            <Button
              size="small"
              variant="outlined"
              startIcon={<AccountTreeOutlined sx={{ fontSize: 16 }} />}
              onClick={() => setConnWizardOpen(true)}
              sx={{ fontSize: 11, minWidth: 0, px: 0.75, '& .MuiButton-startIcon': { mr: 0.5 } }}
            >
              Connect
            </Button>
            {lastQuery && (
              <Button
                size="small"
                variant="outlined"
                startIcon={<ManageSearchOutlined sx={{ fontSize: 16 }} />}
                onClick={() => setTraceOpen(true)}
                sx={{ fontSize: 11, minWidth: 0, px: 0.75, '& .MuiButton-startIcon': { mr: 0.5 } }}
              >
                Trace
              </Button>
            )}
          </Box>
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
          Stale Entities Detected
        </DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" mb={2}>
            The following entities used in this workbook have changed on the server.
            Formulas referencing these entities may return outdated results.
          </Typography>
          <List dense>
            {staleDialogEntries.map(s => (
              <ListItem key={`${s.entry.type}:${s.entry.id}`}>
                <ListItemIcon sx={{ minWidth: 32 }}>
                  <WarningAmberIcon fontSize="small" color={s.reason === 'deprecated' || s.reason === 'deleted' ? 'error' : 'warning'} />
                </ListItemIcon>
                <ListItemText
                  primary={s.entry.displayName}
                  secondary={`${s.entry.type === 'kpi' ? 'KPI' : 'Named Set'} — ${
                    s.reason === 'deleted' ? 'Deleted from server' :
                    s.reason === 'deprecated' ? 'Deprecated' :
                    s.reason === 'status_changed' ? `Status changed to ${s.currentStatus}` :
                    'Definition updated'
                  }${s.entry.cellLocations.length > 0 ? ` — cells: ${s.entry.cellLocations.join(', ')}` : ''}`}
                  primaryTypographyProps={{ fontSize: 13, fontWeight: 600 }}
                  secondaryTypographyProps={{ fontSize: 11 }}
                />
              </ListItem>
            ))}
          </List>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setStaleDialogEntries([])} size="small">
            Later
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
            Accept Current
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
