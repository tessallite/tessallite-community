import { useState, useCallback, useEffect, useRef, useMemo, createContext, useContext } from 'react';
import {
  ThemeProvider, CssBaseline, Box, Typography,
  IconButton, CircularProgress,
  Menu, MenuItem, ListItemText, Select,
} from '@mui/material';
import {
  MenuBookOutlined, Settings as SettingsIcon,
  FilterAltOutlined,
  AnalyticsOutlined,
  AutoAwesomeOutlined,
  SpeedOutlined,
} from '@mui/icons-material';
import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query';
import { theme, tokens } from './theme';
import { useAuth } from './hooks/useAuth';
import type { LoginFormData } from './hooks/useAuth';
import { useExcel } from './hooks/useExcel';
import { usePersonaFiltered } from './hooks/usePersona';
import { useToast } from './components/Toast/ToastProvider';
import { healthCheck } from './api/gateway';
import { setLastMode, getLastMode, setModelContext, clearModelContext, setActivePersonaId as persistActivePersonaId } from './utils/storage';
import { getAgentConfig } from './api/agentService';
import { clearFunctionCaches, applyContextTransition } from './functions';
import { getProjects, getModels, getMeasures } from './api/modelService';
import { useGlossary } from './hooks/useModel';
import { useConversationStore, type TurnResponse, type ConversationState, type AgentConfig } from '@tessallite/shared-ui';
import { createExcelAdapter } from './api/agentChatAdapter';
import { chatT } from './i18n/chatStrings';
import LoginScreen from './components/LoginScreen/LoginScreen';
import ExcelChatShell from './components/AskTessallite/ExcelChatShell';
import { ToastProvider } from './components/Toast/ToastProvider';
import ReportBuilder from './components/ReportBuilder/ReportBuilder';
import { KpiPanel } from './components/KpiPanel';
import GlossaryModal from './components/Glossary/GlossaryModal';
import PersonaDropdown from './components/PersonaSwitcher/PersonaDropdown';
import ProfileSwitcher from './components/ProfileSwitcher/ProfileSwitcher';
import DrillPanel from './components/DrillThrough/DrillPanel';
import DiagnosticsPanel from './components/Settings/DiagnosticsPanel';
import { strings, templates } from './i18n/strings';
import StatusBadge from './components/common/StatusBadge';
import { buildDrillRequestContext, resolveCellContext, type MeasureLookup } from './utils/cellContext';
import { TESSALLITE_CONNECTION_NAME } from './utils/excelFormulas';
import type { Persona } from './types/tessallite';
import { mapAgentChartType, type ChartTypeRecommendation } from './utils/excelCharts';
import { describeChartInsertResult, describeTableInsertResult } from './utils/measureFormulaInsert';
import { executeQuery } from './api/queryRouter';
import { normalizeAgentSemanticQuery } from './utils/semanticQueryNormalizer';

export type AppMode = 'ask' | 'report-builder' | 'kpi';

function createAppQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: 2, staleTime: 30000 } },
  });
}

/**
 * Bug-5787: context to let AppInner request a fresh QueryClient when profiles
 * switch, so no stale cache entries from a previous profile can leak.
 */
const ResetQueryClientContext = createContext<() => void>(() => {});

interface AgentConfigState {
  configured: boolean;
  provider?: string;
  model?: string;
  displayName?: string;
  // Bug-6360: keep the full server AgentConfig so downstream chat features that
  // read arbitrary config fields (feedback_enabled, output format, chart
  // settings, disclosure text, ...) actually receive them. Previously only four
  // fields were copied into state, so `config.feedback_enabled` was always
  // undefined and chat feedback was permanently disabled in Excel.
  config: AgentConfig | null;
}

function AppInner() {
  const queryCache = useQueryClient();
  const resetQueryClient = useContext(ResetQueryClientContext);
  const { authState, profiles, activeProfile, login, logout, switchProfile, deleteProfile } = useAuth();
  const { showToast } = useToast();
  const handleBusy = useCallback(() => showToast(strings.toasts.excelBusy, 'info'), [showToast]);

  const startNewConversation = useConversationStore((s: ConversationState) => s.startNewConversation);
  const resetSession = useConversationStore((s: ConversationState) => s.resetSession);
  const setPendingPersonaId = useConversationStore((s: ConversationState) => s.setPendingPersonaId);
  const modelIdRef = useRef<string | null>(null);
  // Bug-5965: refs to the tab elements so keyboard navigation can move focus
  // (roving tabindex) as the user arrows across the tab strip.
  const tabRefs = useRef<(HTMLDivElement | null)[]>([]);

  const [mode, setMode] = useState<AppMode>('report-builder');
  const [loginError, setLoginError] = useState<string | null>(null);
  const [loginLoading, setLoginLoading] = useState(false);
  const [connected, setConnected] = useState(false);
  const [offlineBanner, setOfflineBanner] = useState(false);
  const failCountRef = useRef(0);

  const [projectId, setProjectId] = useState<string | null>(null);
  const [modelId, setModelId] = useState<string | null>(null);
  modelIdRef.current = modelId;
  // F-025-16 / Bug-6363: pass the active modelId so scorecard/entity inserts
  // scope their manifest entries to this model (no cross-model "Deleted from
  // source" false positives). Declared here because useExcel needs modelId.
  const { insertTable: excelInsertTable, insertChart: excelInsertChart, insertLocalPivot: excelInsertLocalPivot, insertKpiScorecard: excelInsertKpiScorecard, readCellValue } = useExcel(undefined, handleBusy, modelId || undefined);
  const excelAdapter = useMemo(
    () => createExcelAdapter(() => modelIdRef.current),
    [],
  );
  const [projectsLoading, setProjectsLoading] = useState(false);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [projectsList, setProjectsList] = useState<{ id: string; name: string }[]>([]);
  const [modelsList, setModelsList] = useState<{ id: string; name: string; slug?: string }[]>([]);

  const [agentConfig, setAgentConfig] = useState<AgentConfigState>({ configured: false, config: null });

  const [glossaryOpen, setGlossaryOpen] = useState(false);

  const [activePersonaId, setActivePersonaId] = useState<string | null>(null);
  const personaData = usePersonaFiltered(projectId, modelId, activePersonaId);
  const { data: glossaryEntries } = useGlossary(projectId, modelId, activePersonaId);

  const [settingsAnchorEl, setSettingsAnchorEl] = useState<HTMLElement | null>(null);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);

  const [drillOpen, setDrillOpen] = useState(false);
  const [drillMeasureId, setDrillMeasureId] = useState('');
  const [drillMeasureName, setDrillMeasureName] = useState('');
  const [drillContext, setDrillContext] = useState<Record<string, unknown>>({});

  const handleProjectChange = useCallback(async (newProjectId: string) => {
    setProjectId(newProjectId);
    setModelId(null);
    setModelsList([]);
    setProjectsError(null);
    startNewConversation();
    setActivePersonaId(null);
    await clearModelContext().catch(() => {});
    // F-025-03: a project switch changes the governed catalogue (and resets the
    // persona) exactly like a model switch, so it carries the same stale-value
    // risk. Persist the resolved model context and cleared persona into storage
    // FIRST, then run the single awaited transition so the separate functions
    // runtime invalidates and the workbook rebuilds under the new project.
    await persistActivePersonaId(null).catch(() => {});
    try {
      const models = await getModels(newProjectId);
      setModelsList(models);
      if (models.length > 0) {
        const firstModel = models[0];
        setModelId(firstModel.id);
        await setModelContext(newProjectId, firstModel.id, firstModel.slug, firstModel.name).catch(() => {});
      } else {
        // Bug-6370: an empty model list is a real, user-visible state — record it
        // rather than leaving the panes blank with no explanation.
        setProjectsError(strings.projects.noModels);
      }
    } catch {
      // Bug-6370: a failed model fetch on project change was silently swallowed,
      // leaving the panes empty. Surface it so the user knows to retry.
      setProjectsError(strings.projects.loadFailed);
      showToast(strings.projects.loadFailed, 'error');
    }
    // Invalidate + rebuild after the new governed scope is persisted (or after
    // a failed load, where the cleared context must still invalidate stale cells).
    await applyContextTransition();
  }, [startNewConversation, showToast]);

  const handleModelChange = useCallback((newModelId: string) => {
    setModelId(newModelId);
    startNewConversation();
    setActivePersonaId(null);
    // F-025-03: a model switch changes the governed catalogue (and resets the
    // persona to the model default). Persist the new model context AND the
    // cleared persona into OfficeRuntime.storage FIRST, then invalidate the
    // functions runtime and rebuild the workbook via the single awaited
    // transition. Without this, cells authored against the previous model
    // could keep resolving old measure/KPI IDs under a stale generation.
    (async () => {
      if (projectId) {
        const selectedModel = modelsList.find(m => m.id === newModelId);
        await setModelContext(projectId, newModelId, selectedModel?.slug, selectedModel?.name).catch(() => {});
      }
      await persistActivePersonaId(null).catch(() => {});
      await applyContextTransition();
    })();
  }, [startNewConversation, projectId, modelsList]);

  const handleLogin = useCallback(async (data: LoginFormData, remember: boolean) => {
    setLoginLoading(true);
    setLoginError(null);
    try {
      await login(data, remember);
    } catch (e) {
      setLoginError((e as Error).message || strings.auth.loginFailed);
    } finally {
      setLoginLoading(false);
    }
  }, [login]);

  const handleModeChange = useCallback((newMode: AppMode) => {
    setMode(newMode);
    setLastMode(newMode);
  }, []);

  // Bug-5965: the tab strip order, driving keyboard navigation. Keep in sync
  // with the rendered tab order below.
  const tabOrder: AppMode[] = ['report-builder', 'kpi', 'ask'];
  const handleTabKeyDown = useCallback((e: React.KeyboardEvent, index: number) => {
    const count = tabOrder.length;
    let next: number | null = null;
    switch (e.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        next = (index + 1) % count;
        break;
      case 'ArrowLeft':
      case 'ArrowUp':
        next = (index - 1 + count) % count;
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = count - 1;
        break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        handleModeChange(tabOrder[index]);
        return;
      default:
        return;
    }
    e.preventDefault();
    handleModeChange(tabOrder[next]);
    tabRefs.current[next]?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [handleModeChange]);

  const clearSessionState = useCallback(() => {
    // Bug-7735: use resetSession (not startNewConversation) for session
    // teardown so pendingModelId/pendingPersonaId are cleared immediately,
    // preventing stale scope from leaking across sessions.
    resetSession();
    setProjectId(null);
    setModelId(null);
    setActivePersonaId(null);
    setProjectsLoading(false);
    setProjectsError(null);
    setAgentConfig({ configured: false, config: null });
  }, [resetSession]);

  const handleLogout = useCallback(async () => {
    clearSessionState();
    queryCache.cancelQueries();
    queryCache.clear();
    clearFunctionCaches();
    resetQueryClient();
    await logout();
  }, [logout, queryCache, clearSessionState, resetQueryClient]);

  const handleSwitchProfile = useCallback(async (profileId: string) => {
    clearSessionState();
    queryCache.cancelQueries();
    queryCache.clear();
    resetQueryClient();
    // F-025-03: switch the persisted profile (removes JWT, sets the new active
    // profile in OfficeRuntime.storage) BEFORE invalidating and rebuilding the
    // functions runtime. The previous order bumped the generation while storage
    // still held the old profile, so the separate functions runtime could fetch
    // under the old profile after seeing the new generation. Persisting first
    // then running the single awaited transition removes that window and forces
    // a full workbook rebuild so no cell keeps the prior profile's values.
    await switchProfile(profileId);
    await applyContextTransition();
  }, [switchProfile, queryCache, clearSessionState, resetQueryClient]);

  const handleRemoveProfile = useCallback(async (profileId: string) => {
    await deleteProfile(profileId);
    showToast(strings.app.profileRemoved, 'info');
  }, [deleteProfile, showToast]);

  // F-025-24: a single health-poll routine, shared by the 30 s interval and the
  // offline-banner Retry button. `wasConnected` lives in a ref so the Retry
  // path observes the same state-transition logic (toast on recovery) as the
  // interval, and clicking Retry triggers an immediate poll instead of merely
  // resetting the failure counter and waiting up to 30 s.
  const wasConnectedRef = useRef(true);
  const runHealthPoll = useCallback(async () => {
    try {
      await healthCheck();
      failCountRef.current = 0;
      if (!wasConnectedRef.current) {
        showToast(strings.connection.restored, 'success');
      }
      setConnected(true);
      setOfflineBanner(false);
      wasConnectedRef.current = true;
    } catch {
      failCountRef.current++;
      if (wasConnectedRef.current) {
        showToast(strings.connection.lost, 'error');
      }
      setConnected(false);
      wasConnectedRef.current = false;
      if (failCountRef.current >= 3) {
        setOfflineBanner(true);
      }
    }
  }, [showToast]);

  useEffect(() => {
    if (authState !== 'authenticated') return;
    failCountRef.current = 0;
    wasConnectedRef.current = true;
    runHealthPoll();
    const interval = setInterval(runHealthPoll, 30000);
    return () => { clearInterval(interval); };
  }, [authState, runHealthPoll]);

  // F-025-24: Retry now resets the failure counter AND polls immediately so the
  // banner clears the moment connectivity is back, not on the next 30 s tick.
  const handleRetryConnection = useCallback(() => {
    failCountRef.current = 0;
    runHealthPoll();
  }, [runHealthPoll]);

  // F-025-24: the stored last-mode is the seed value; the `?mode=` ribbon
  // deep-link, if present, must win. Resolve the async stored mode first and
  // only apply it when no URL mode param is supplied, so a deep-link can never
  // be overwritten by a late-resolving storage read. `kpi` is now an accepted
  // URL mode value too (it is a valid stored/selectable mode).
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const modeParam = params.get('mode');
    if (modeParam === 'ask' || modeParam === 'report-builder' || modeParam === 'kpi') {
      setMode(modeParam);
      return;
    }
    getLastMode().then((m: string | null) => {
      if (m === 'ask' || m === 'report-builder' || m === 'kpi') setMode(m);
    });
  }, []);

  // F-025-17: mirror the active persona into OfficeRuntime.storage so the
  // separate custom-functions runtime evaluates TESS.* KPIs under the same
  // persona the pane is viewing as.
  useEffect(() => {
    persistActivePersonaId(activePersonaId).catch(() => {});
  }, [activePersonaId]);

  useEffect(() => {
    if (authState !== 'authenticated' || projectId) return;

    let cancelled = false;
    setProjectsLoading(true);
    setProjectsError(null);

    (async () => {
      try {
        const projects = await getProjects();
        if (cancelled) return;
        setProjectsList(projects);
        if (projects.length === 0) {
          setProjectsLoading(false);
          setProjectsError(strings.projects.none);
          return;
        }
        const pid = projects[0].id;
        let models: { id: string; name: string; slug?: string }[] = [];
        try {
          models = await getModels(pid);
        } catch { /* models fetch failed, still show project */ }
        if (cancelled) return;
        setModelsList(models);
        setProjectId(pid);
        if (models.length > 0) {
          const firstModel = models[0];
          setModelId(firstModel.id);
          setModelContext(pid, firstModel.id, firstModel.slug, firstModel.name).catch(() => {});
        } else {
          setProjectsError(strings.projects.noModels);
        }
        setProjectsLoading(false);
      } catch (e) {
        if (cancelled) return;
        setProjectsLoading(false);
        setProjectsError((e as Error).message || strings.projects.loadFailed);
      }
    })();

    return () => { cancelled = true; };
  }, [authState, projectId]);

  useEffect(() => {
    if (!projectId) {
      setAgentConfig({ configured: false, config: null });
      return;
    }
    let cancelled = false;

    getAgentConfig(projectId)
      .then(config => {
        if (cancelled) return;
        setAgentConfig({
          configured: config.configured ?? config.enabled ?? false,
          provider: config.provider,
          model: config.model,
          displayName: config.display_name,
          // Bug-6360: retain the entire server config object. getAgentConfig's
          // generic return type is a thin view, but the runtime payload carries
          // every AgentConfig field (feedback_enabled et al.); keep it intact
          // for the chat surface.
          config: config as unknown as AgentConfig,
        });
      })
      .catch(() => {
        if (cancelled) return;
        setAgentConfig({ configured: false, config: null });
      });

    return () => { cancelled = true; };
  }, [projectId]);

  // Bug-6357: resolve the full result set for an agent chat turn.
  // The agent-service caps `query_result_sample` at 50 rows, so inserts
  // must re-fetch the full data via `/api/v1/plugin/execute` when the
  // turn's `query_result_rows` exceeds the sample length.
  // R1 review fix: when the data IS truncated but re-fetch is impossible
  // (missing semantic_query / projectId / modelId) or fails (network/auth),
  // warn the user via toast and return a blocked status to BLOCK the insert
  // rather than silently inserting the partial 50-row sample.
  // Bug-6357-A: returns a discriminated result so insert handlers can
  // distinguish "blocked (toast already shown)" from "genuinely empty
  // (no data at all)" and show exactly ONE correct toast per case.
  // Bug-6357-B: when re-fetch succeeds but returns empty rows for a
  // truncated result, block the insert instead of falling back to the
  // partial 50-row sample.
  const resolveFullResult = useCallback(async (
    turn: TurnResponse,
  ): Promise<
    | { status: 'ok'; headers: string[]; rows: (string | number)[][] }
    | { status: 'blocked' }
    | { status: 'empty' }
  > => {
    const sample = turn.query_result_sample;
    if (!sample || sample.length === 0) return { status: 'empty' };

    const totalRows = turn.query_result_rows ?? sample.length;
    const isTruncated = totalRows > sample.length;

    if (!isTruncated) {
      // Sample IS the full result -- safe to use directly
      const headers = Object.keys(sample[0]);
      const rows = sample.map(r => headers.map(h => r[h] as string | number));
      return { status: 'ok', headers, rows };
    }

    // Data is truncated -- must re-fetch full result
    if (!turn.semantic_query || !projectId || !modelId) {
      // Cannot re-fetch: warn and block rather than silently inserting partial data
      showToast(
        templates.toasts.insertTruncatedWarning(sample.length, totalRows),
        'warning',
      );
      return { status: 'blocked' };
    }

    try {
      // Bug-7392: the agent-service stores semantic_query with agent-schema
      // fields (where/having/sort), not the plugin's SemanticQuery shape
      // (filters/order). The unsafe `as SemanticQuery` cast silently dropped
      // filters and ordering, causing re-fetches to insert UNFILTERED numbers.
      const sq = normalizeAgentSemanticQuery(turn.semantic_query);
      const response = await executeQuery(sq, {
        projectId,
        modelId,
        personaId: activePersonaId || undefined,
      });
      if (response.data && response.data.length > 0) {
        const headers = Object.keys(response.data[0]);
        const rows = response.data.map(
          r => headers.map(h => r[h] as string | number),
        );
        return { status: 'ok', headers, rows };
      }
    } catch {
      // Re-fetch failed (network error, auth expiry, etc.)
      showToast(
        templates.toasts.insertRefetchFailed(sample.length, totalRows),
        'warning',
      );
      return { status: 'blocked' };
    }

    // Bug-6357-B: re-fetch returned empty for a truncated result -- block
    // the insert and warn; never silently fall back to the partial sample.
    showToast(
      templates.toasts.insertRefetchEmpty(sample.length, totalRows),
      'warning',
    );
    return { status: 'blocked' };
  }, [projectId, modelId, activePersonaId, showToast]);

  const handleInsertTable = useCallback(async (turn: TurnResponse) => {
    const resolved = await resolveFullResult(turn);
    if (resolved.status === 'blocked') return;
    if (resolved.status === 'empty') {
      showToast(strings.toasts.noDataToInsert, 'info');
      return;
    }
    const { headers, rows } = resolved;
    try {
      const tableResult = await excelInsertTable(
        headers,
        rows,
        // Bug-6915: anchor at the user's active cell (with overwrite confirm),
        // not A1. insertTable owns the OVERWRITE_WARNING confirm-and-retry.
        { useActiveCell: true },
        {
          projectId: projectId || undefined,
          modelId: modelId || undefined,
          personaId: activePersonaId || undefined,
          // Bug-6366: pass human-readable model and persona labels so the
          // inserted table's provenance footer reads "Model: Sales | Viewing as:
          // Analyst" instead of raw UUIDs (the footer falls back to ids only
          // when no label is supplied).
          modelLabel: modelsList.find(m => m.id === modelId)?.name,
          personaLabel: personaData.activePersona?.name,
          conversationId: turn.conversation_id || undefined,
          turnId: turn.id || undefined,
          semanticQuery: turn.id
            ? JSON.stringify({ source: 'agent', conversation_id: turn.conversation_id, message_id: turn.id })
            : undefined,
          columnHeaders: headers,
        },
      );
      // Bug-6737: outcome-derived toast -- same pattern as chart inserts.
      const toastCall = describeTableInsertResult(tableResult.address !== null, rows.length, tableResult.postStepWarning, tableResult.blocked);
      if (toastCall) showToast(toastCall.message, toastCall.severity);
    } catch {
      showToast(strings.toasts.insertTableFailed, 'error');
    }
  }, [resolveFullResult, excelInsertTable, showToast, projectId, modelId, activePersonaId, modelsList, personaData.activePersona]);

  const handleFeedback = useCallback(async (turnId: string, vote: 'up' | 'down') => {
    if (!projectId) return;
    const convId = useConversationStore.getState().activeConversationId;
    if (!convId) return;
    try {
      await excelAdapter.submitFeedback(projectId, convId, turnId, vote);
      showToast(strings.toasts.feedbackRecorded, 'success');
    } catch {
      showToast(strings.toasts.feedbackFailed, 'error');
    }
  }, [projectId, excelAdapter, showToast]);

  // Bug-6733: chart insert toast is decided from the actual insert outcome.
  const handleInsertChart = useCallback(async (turn: TurnResponse, chartType?: ChartTypeRecommendation) => {
    // Bug-6357: resolve the full result set instead of the 50-row sample.
    // Bug-6357-A: only show "no data" for genuinely empty results; when
    // blocked (truncation warning already shown), do NOT add a second toast.
    const resolved = await resolveFullResult(turn);
    if (resolved.status === 'blocked') return;
    if (resolved.status === 'empty') {
      showToast(strings.toasts.noDataToChart, 'info');
      return;
    }
    const { headers, rows } = resolved;
    const agentMapped = mapAgentChartType(turn.chart_type);
    if (agentMapped === null && turn.chart_type === 'kpi') {
      showToast(strings.toasts.kpiChartNotSuitable, 'info');
      return;
    }
    // Bug-5800: use the agent-mapped chart type first, then fall back to the
    // heuristic recommendation passed from ExcelChatShell.
    const effectiveType = agentMapped ?? chartType ?? undefined;
    // R1 Finding 4: use the centralized describeChartInsertResult helper
    // (same pattern as ReportBuilder) instead of hand-rolling the branch.
    try {
      const chartResult = await excelInsertChart(headers, rows, effectiveType);
      const toastCall = describeChartInsertResult(chartResult.address !== null, chartResult.postStepWarning);
      if (toastCall) showToast(toastCall.message, toastCall.severity);
    } catch {
      showToast(strings.toasts.chartInsertFailed, 'error');
    }
  }, [resolveFullResult, excelInsertChart, showToast]);

  const handleInsertLocalPivot = useCallback(async (turn: TurnResponse) => {
    // Bug-6357: resolve the full result set instead of the 50-row sample.
    // Bug-6357-A: only show "no data" for genuinely empty results; when
    // blocked (truncation warning already shown), do NOT add a second toast.
    const resolved = await resolveFullResult(turn);
    if (resolved.status === 'blocked') return;
    if (resolved.status === 'empty') {
      showToast(strings.toasts.noDataForPivot, 'info');
      return;
    }
    const { headers, rows } = resolved;
    try {
      await excelInsertLocalPivot(headers, rows);
      showToast(strings.toasts.pivotCreated, 'success');
    } catch {
      showToast(strings.toasts.pivotInsertFailed, 'error');
    }
  }, [resolveFullResult, excelInsertLocalPivot, showToast]);

  const handleOpenDrill = useCallback(async () => {
    try {
      const cell = await readCellValue();
      // Build measure lookup if project/model are available
      let measureLookup: MeasureLookup | undefined;
      if (projectId && modelId) {
        try {
          const measures = await getMeasures(projectId, modelId, activePersonaId || undefined);
          const byName = new Map<string, string>();
          const byDisplayName = new Map<string, string>();
          for (const m of measures) {
            byName.set(m.name, m.id);
            byDisplayName.set(m.display_name, m.id);
          }
          measureLookup = { byName, byDisplayName };
        } catch {
          // proceed without measure lookup
        }
      }
      const ctx = await resolveCellContext(cell.address, cell.value, cell.formula, measureLookup);
      if (ctx.type === 'unknown') {
        showToast(strings.toasts.selectCellForDrill, 'info');
        return;
      }
      if (ctx.unavailableReason) {
        showToast(
          ctx.unavailableReason === 'cube_filter_context_unrecoverable'
            ? strings.toasts.cubeDrillContextUnavailable
            : ctx.unavailableReason,
          'error',
        );
        return;
      }
      if (!ctx.measureId) {
        showToast(strings.toasts.drillMeasureUnavailable, 'error');
        return;
      }
      setDrillMeasureId(ctx.measureId || '');
      setDrillMeasureName(ctx.measureName || strings.app.selectedValue);
      // F-025-06/F-025-04: build the request in the backend's
      // DrillThroughRequest shape. Inserted-table row coordinates go in
      // grouping_levels; CUBEVALUE slicer/member coordinates go in filters so
      // they constrain the leaf result without triggering hierarchy step-down.
      setDrillContext(buildDrillRequestContext(ctx, activePersonaId));
      setDrillOpen(true);
    } catch {
      showToast(strings.toasts.cellContextFailed, 'error');
    }
  }, [readCellValue, projectId, modelId, activePersonaId]);

  const handleDrillInsertSheet = useCallback(async (headers: string[], rows: (string | number)[][]) => {
    try {
      // Bug-6915: anchor at the active cell instead of A1 (silent overwrite).
      const tableResult = await excelInsertTable(headers, rows, { useActiveCell: true });
      // Bug-6737 / R1 Finding 5: use the outcome-derived toast helper so a
      // post-step failure is reported as a warning, not silently swallowed.
      // Drill rows use their own row-count label for clean success.
      if (tableResult.address) {
        if (tableResult.postStepWarning) {
          const toastCall = describeTableInsertResult(true, rows.length, true);
          if (toastCall) showToast(toastCall.message, toastCall.severity);
        } else {
          showToast(templates.toasts.insertedDrillRows(rows.length), 'success');
        }
      }
    } catch (e) {
      showToast(strings.toasts.insertDrillRowsFailed, 'error');
    }
  }, [excelInsertTable, showToast]);

  const handlePersonaSelect = useCallback((persona: Persona | null) => {
    const newPersonaId = persona?.id || null;
    setActivePersonaId(newPersonaId);
    // Bug-7735: synchronously update the zustand store so handleSend sees
    // the correct persona even before React's useEffect sync fires.
    setPendingPersonaId(newPersonaId);
    // F-025-03: a persona switch changes the governed scope. Persist the new
    // persona into OfficeRuntime.storage FIRST, then invalidate + recalc via
    // the single awaited transition. Doing the generation bump before the
    // persist (the old order) let the separate functions runtime observe the
    // new generation while storage still held the previous persona, so it
    // could fetch and cache values authorised for the OLD persona. Persist-
    // then-bump makes that race impossible, and the transition also requests a
    // full workbook rebuild so already-inserted cells recompute under the new
    // persona instead of retaining the previous persona's values for up to 60s.
    (async () => {
      await persistActivePersonaId(newPersonaId).catch(() => {});
      await applyContextTransition();
    })();
    startNewConversation();
    if (persona) {
      showToast(templates.toasts.switchedPersona(persona.name), 'info');
    }
  }, [showToast, startNewConversation, setPendingPersonaId]);

  const providerModel = agentConfig.configured && agentConfig.provider && agentConfig.model
    ? `${agentConfig.provider} ${agentConfig.model}`
    : (projectId ? strings.app.loadingProviderInfo : undefined);
  const activeProjectName = projectsList.find(p => p.id === projectId)?.name;
  const activeModelName = modelsList.find(m => m.id === modelId)?.name;
  const askTabLabel = agentConfig.displayName?.trim()
    ? templates.app.askAgent(agentConfig.displayName.trim())
    : strings.app.askTessallite;

  if (authState === 'loading') {
    return (
      <ThemeProvider theme={theme}>
        <CssBaseline />
        <Box sx={{ width: '100vw', minWidth: 320, maxWidth: 420, height: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <CircularProgress size={24} sx={{ color: tokens.colorPrimary }} />
        </Box>
      </ThemeProvider>
    );
  }

  if (authState === 'unauthenticated') {
    return (
      <ThemeProvider theme={theme}>
        <CssBaseline />
        <Box sx={{ width: '100vw', minWidth: 320, maxWidth: 420, height: '100vh', display: 'flex', flexDirection: 'column' }}>
          <LoginScreen
            onLogin={handleLogin}
            loading={loginLoading}
            error={loginError}
          />
        </Box>
      </ThemeProvider>
    );
  }

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <Box sx={{ width: '100vw', minWidth: 320, maxWidth: 420, height: '100vh', display: 'flex', flexDirection: 'column', overflow: 'hidden', bgcolor: tokens.colorWhite }}>
        <Box
          component="header"
          role="banner"
          sx={{
            minHeight: 48, display: 'flex', alignItems: 'center', px: 1.5, pr: 5.5,
            borderBottom: `1px solid ${tokens.colorBorderLight}`, bgcolor: tokens.colorWhite,
            gap: 1,
          }}
        >
          <svg width="18" height="16" viewBox="0 0 32 28" fill="none" aria-hidden="true" style={{ flexShrink: 0 }}>
            <polygon points="0,7 8,0 16,7 8,14" fill="#185a33" />
            <polygon points="16,7 24,0 32,7 24,14" fill="#c9a520" />
            <polygon points="0,21 8,14 16,21 8,28" fill="#217346" />
            <polygon points="16,21 24,14 32,21 24,28" fill="#185a33" />
          </svg>
          <Box sx={{ minWidth: 0, flexShrink: 0 }}>
            <Typography sx={{ fontSize: 14, fontWeight: 700, color: tokens.colorCharcoal, lineHeight: 1.15 }}>
              {strings.app.title}
            </Typography>
            <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, lineHeight: 1.15 }}>
              {strings.app.subtitle}
            </Typography>
          </Box>
          <Box sx={{ mr: 'auto' }} />
          <IconButton size="small" title={strings.app.drillThrough} aria-label={strings.app.drillThroughCell} onClick={handleOpenDrill}>
            <FilterAltOutlined sx={{ fontSize: 20, color: tokens.colorTextSecondary }} />
          </IconButton>
          <IconButton size="small" title={strings.app.glossary} aria-label={strings.app.glossaryAria} onClick={() => setGlossaryOpen(true)}>
            <MenuBookOutlined sx={{ fontSize: 20, color: tokens.colorTextSecondary }} />
          </IconButton>
          {profiles.length > 0 && (
            <ProfileSwitcher
              profiles={profiles}
              activeProfile={activeProfile}
              onSwitch={handleSwitchProfile}
              onRemove={handleRemoveProfile}
              onLogout={handleLogout}
            />
          )}
          <IconButton
            size="small"
            title={strings.app.settings}
            aria-label={strings.app.settingsAria}
            onClick={e => setSettingsAnchorEl(e.currentTarget)}
          >
            <SettingsIcon sx={{ fontSize: 20, color: tokens.colorTextSecondary }} />
          </IconButton>
        </Box>

        <Box
          sx={{
            px: 1.5,
            py: 0.75,
            borderBottom: `1px solid ${tokens.colorBorderLight}`,
            bgcolor: tokens.colorWhite,
          }}
        >
          <Box sx={{ minWidth: 0 }}>
            <Typography sx={{ fontSize: 9, fontWeight: 700, color: tokens.colorTextSecondary, textTransform: 'uppercase', mb: 0.25 }}>
              {strings.app.projectLabel}
            </Typography>
            {projectsList.length > 1 && projectId ? (
              <Select
                aria-label={strings.app.projectSelectorAria}
                size="small"
                value={projectId}
                onChange={e => handleProjectChange(e.target.value as string)}
                sx={{ fontSize: 11, minWidth: 0, width: '100%', '& .MuiSelect-select': { py: 0.5, px: 1 } }}
              >
                {projectsList.map(p => (
                  <MenuItem key={p.id} value={p.id} sx={{ fontSize: 11 }}>{p.name}</MenuItem>
                ))}
              </Select>
            ) : (
              <Typography sx={{ fontSize: 11, color: activeProjectName ? tokens.colorCharcoal : tokens.colorTextSecondary, px: 1, py: 0.75, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', bgcolor: tokens.colorSubtleFill, borderRadius: 1 }}>
                {activeProjectName || strings.status.loading}
              </Typography>
            )}
          </Box>
        </Box>

        <Menu
          anchorEl={settingsAnchorEl}
          open={Boolean(settingsAnchorEl)}
          onClose={() => setSettingsAnchorEl(null)}
        >
          <MenuItem onClick={() => { setSettingsAnchorEl(null); setDiagnosticsOpen(true); }}>
            <ListItemText
              primary={strings.app.diagnosticsMenuItem}
              primaryTypographyProps={{ fontSize: 13 }}
            />
          </MenuItem>
          <MenuItem onClick={() => { setSettingsAnchorEl(null); handleLogout(); }}>
            <ListItemText
              primary={strings.app.signOut}
              primaryTypographyProps={{ fontSize: 13, color: tokens.colorRed }}
            />
          </MenuItem>
        </Menu>

        <Box
          component="nav"
          role="tablist"
          aria-label={strings.app.sectionsAria}
          sx={{
            height: 44, display: 'grid', gridTemplateColumns: '1fr 1fr 1fr',
            borderBottom: `1px solid ${tokens.colorBorderLight}`,
            bgcolor: tokens.colorSubtleFill,
          }}
        >
          {([
            { id: 'report-builder' as const, label: strings.app.tabAnalyse, icon: AnalyticsOutlined },
            { id: 'kpi' as const, label: strings.app.tabKpis, icon: SpeedOutlined },
            { id: 'ask' as const, label: askTabLabel, icon: AutoAwesomeOutlined },
          ]).map((tab, index) => {
            const active = mode === tab.id;
            const Icon = tab.icon;
            return (
              <Box
                key={tab.id}
                ref={(el: HTMLDivElement | null) => { tabRefs.current[index] = el; }}
                role="tab"
                id={`tab-${tab.id}`}
                aria-controls={`tabpanel-${tab.id}`}
                aria-selected={active}
                aria-current={active ? 'page' : undefined}
                // Bug-5965: roving tabindex — only the active tab is in the tab
                // order; arrow keys move between tabs, Enter/Space activate.
                tabIndex={active ? 0 : -1}
                onKeyDown={(e) => handleTabKeyDown(e, index)}
                onClick={() => handleModeChange(tab.id)}
                sx={{
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  gap: 0.5,
                  minWidth: 0,
                  px: 0.5,
                  cursor: 'pointer',
                  fontSize: 12, fontWeight: 700,
                  color: active ? tokens.colorPrimary : tokens.colorTextSecondary,
                  bgcolor: active ? tokens.colorWhite : 'transparent',
                  borderBottom: active ? `2px solid ${tokens.colorPrimary}` : '2px solid transparent',
                  '&:hover': {
                    bgcolor: tokens.colorWhite,
                    color: active ? tokens.colorPrimary : tokens.colorCharcoal,
                  },
                  '&:focus-visible': {
                    outline: `2px solid ${tokens.colorPrimary}`,
                    outlineOffset: '-2px',
                  },
                }}
              >
                <Icon sx={{ fontSize: 17, flexShrink: 0 }} />
                <Typography sx={{ fontSize: 12, fontWeight: 700, color: 'inherit', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {tab.label}
                </Typography>
              </Box>
            );
          })}
        </Box>

        {offlineBanner && (
          <Box sx={{
            px: 1.5, py: 0.75, bgcolor: tokens.colorRedBg,
            borderLeft: `3px solid ${tokens.colorRed}`,
            display: 'flex', alignItems: 'center', gap: 1,
          }}>
            <Typography sx={{ fontSize: 11, color: tokens.colorRed, flex: 1 }}>
              {strings.connection.lost}
            </Typography>
            <Box
              component="button"
              onClick={handleRetryConnection}
              sx={{
                fontSize: 10, px: 1, py: 0.25, borderRadius: 1, cursor: 'pointer',
                border: `1px solid ${tokens.colorRed}`, color: tokens.colorRed,
                bgcolor: 'transparent', textTransform: 'none',
              }}
            >
              {strings.app.retry}
            </Box>
          </Box>
        )}

        {activePersonaId && personaData.activePersona && (
          <Box sx={{
            px: 2, py: 0.5, bgcolor: tokens.colorSubtleFill,
            borderBottom: `1px solid ${tokens.colorBorderLight}`,
          }}>
            <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary }}>
              {templates.app.viewingAsPersona(personaData.activePersona.name)}{' '}
              <Box component="span" sx={{ color: tokens.colorPrimary, cursor: 'pointer' }} onClick={() => handlePersonaSelect(null)}>
                {strings.app.resetToDefault}
              </Box>
            </Typography>
          </Box>
        )}

        <Box
          component="main"
          sx={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}
        >
          {mode === 'ask' && (
            <Box
              role="tabpanel"
              id="tabpanel-ask"
              aria-labelledby="tab-ask"
              sx={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}
            >
              {projectId ? (
                <Box
                  role="log"
                  aria-live="polite"
                  aria-label={strings.app.chatMessagesAria}
                  sx={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}
                >
                  <ExcelChatShell
                    adapter={excelAdapter}
                    t={chatT}
                    projectId={projectId}
                    config={agentConfig.configured ? agentConfig.config : null}
                    activeModelId={modelId}
                    activePersonaId={activePersonaId}
                    agentConfigured={agentConfig.configured}
                    providerModel={providerModel}
                    loading={projectsLoading}
                    error={projectsError}
                    onInsertTable={handleInsertTable}
                    onInsertChart={handleInsertChart}
                    onInsertLocalPivot={handleInsertLocalPivot}
                    onFeedback={handleFeedback}
                  />
                </Box>
              ) : projectsLoading ? (
                <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                  <CircularProgress size={24} sx={{ color: tokens.colorPrimary }} />
                </Box>
              ) : (
                // Bug-6370: without a loaded project the Ask tab used to render a
                // blank pane. Show the same empty-state the other tabs use.
                <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', p: 3 }}>
                  <Typography sx={{ fontSize: 13, color: tokens.colorTextSecondary, textAlign: 'center' }}>
                    {projectsError || strings.projects.noneSelected}
                  </Typography>
                </Box>
              )}
            </Box>
          )}
          {mode === 'report-builder' && (
            <Box
              role="tabpanel"
              id="tabpanel-report-builder"
              aria-labelledby="tab-report-builder"
              aria-label={strings.app.reportBuilderAria}
              sx={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}
            >
              {projectId && modelId ? (
                <ReportBuilder
                  projectId={projectId}
                  modelId={modelId}
                  serverUrl={activeProfile?.serverUrl || ''}
                  personaId={activePersonaId}
                  personaSlug={personaData.activePersona?.slug || null}
                  modelsList={modelsList}
                  onModelChange={handleModelChange}
                />
              ) : projectsLoading ? (
                <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                  <CircularProgress size={24} sx={{ color: tokens.colorPrimary }} />
                </Box>
              ) : (
                <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', p: 3 }}>
                  <Typography sx={{ fontSize: 13, color: tokens.colorTextSecondary, textAlign: 'center' }}>
                    {projectsError || strings.projects.noneSelected}
                  </Typography>
                </Box>
              )}
            </Box>
          )}
          {mode === 'kpi' && (
            <Box
              role="tabpanel"
              id="tabpanel-kpi"
              aria-labelledby="tab-kpi"
              aria-label={strings.app.kpiScorecardAria}
              sx={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}
            >
              {projectId && modelId ? (
                <KpiPanel
                  projectId={projectId}
                  modelId={modelId}
                  personaId={activePersonaId}
                  onInsertTable={async (headers, rows) => {
                    // Bug-6737 / R1 Finding 1: wrap the raw insert to show
                    // an outcome-derived toast (same pattern as the chart
                    // wrapper below). The KpiPanel discards the result, so
                    // without this wrapper a post-step failure or even a
                    // success goes un-toasted.
                    try {
                      // Bug-6915: anchor at the active cell instead of A1.
                      const tableResult = await excelInsertTable(headers, rows, { useActiveCell: true });
                      const toastCall = describeTableInsertResult(tableResult.address !== null, rows.length, tableResult.postStepWarning, tableResult.blocked);
                      if (toastCall) showToast(toastCall.message, toastCall.severity);
                      return tableResult;
                    } catch {
                      showToast(strings.toasts.insertTableFailed, 'error');
                      return { address: null, postStepWarning: false };
                    }
                  }}
                  onInsertChart={async (headers, rows) => {
                    // R1 Finding 5: handle the chart outcome properly --
                    // show toast on success/warning and catch failures
                    // instead of discarding postStepWarning and leaving
                    // errors as unhandled promise rejections.
                    try {
                      const chartResult = await excelInsertChart(headers, rows, 'columnClustered');
                      const toastCall = describeChartInsertResult(chartResult.address !== null, chartResult.postStepWarning);
                      if (toastCall) showToast(toastCall.message, toastCall.severity);
                      return chartResult.address;
                    } catch {
                      showToast(strings.toasts.chartInsertFailed, 'error');
                      return null;
                    }
                  }}
                  onInsertScorecard={excelInsertKpiScorecard}
                  connectionName={TESSALLITE_CONNECTION_NAME}
                  modelSlug={modelsList.find(m => m.id === modelId)?.slug || null}
                />
              ) : projectsLoading ? (
                <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                  <CircularProgress size={24} sx={{ color: tokens.colorPrimary }} />
                </Box>
              ) : (
                <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', p: 3 }}>
                  <Typography sx={{ fontSize: 13, color: tokens.colorTextSecondary, textAlign: 'center' }}>
                    {projectsError || strings.projects.noneSelected}
                  </Typography>
                </Box>
              )}
            </Box>
          )}
        </Box>

        <Box
          component="footer"
          sx={{
            height: 28, display: 'flex', alignItems: 'center', px: 2,
            bgcolor: tokens.colorSubtleFill, borderTop: `1px solid ${tokens.colorBorderLight}`,
            gap: 1,
          }}
        >
          <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, mr: 'auto' }}>
            {activeProfile ? `${activeProfile.email}` : ''}
          </Typography>
          {personaData.personas.length > 0 && (
            <PersonaDropdown
              personas={personaData.personas}
              activePersonaId={activePersonaId}
              onSelect={handlePersonaSelect}
            />
          )}
          <StatusBadge
            status={offlineBanner ? 'reconnecting' : connected ? 'connected' : 'disconnected'}
            label={offlineBanner ? strings.status.reconnecting : connected ? strings.status.connected : strings.status.disconnected}
          />
        </Box>
      </Box>

      <GlossaryModal
        open={glossaryOpen}
        onClose={() => setGlossaryOpen(false)}
        entries={glossaryEntries || []}
      />

      <DrillPanel
        open={drillOpen}
        onClose={() => setDrillOpen(false)}
        measureId={drillMeasureId}
        measureName={drillMeasureName}
        context={drillContext}
        projectId={projectId || undefined}
        modelId={modelId || undefined}
        onInsertSheet={handleDrillInsertSheet}
      />

      <DiagnosticsPanel
        open={diagnosticsOpen}
        onClose={() => setDiagnosticsOpen(false)}
      />
    </ThemeProvider>
  );
}

export default function App() {
  const [queryClient, setQueryClient] = useState(createAppQueryClient);

  const handleResetQueryClient = useCallback(() => {
    setQueryClient(createAppQueryClient());
  }, []);

  return (
    <QueryClientProvider client={queryClient}>
      <ResetQueryClientContext.Provider value={handleResetQueryClient}>
        <ToastProvider>
          <AppInner />
        </ToastProvider>
      </ResetQueryClientContext.Provider>
    </QueryClientProvider>
  );
}
