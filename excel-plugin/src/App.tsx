import { useState, useCallback, useEffect, useRef, useMemo } from 'react';
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
import { clearFunctionCaches } from './functions';
import { getProjects, getModels, getMeasures } from './api/modelService';
import { useGlossary } from './hooks/useModel';
import { useConversationStore, type TurnResponse, type ConversationState } from '@tessallite/shared-ui';
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
import { strings } from './i18n/strings';
import StatusBadge from './components/common/StatusBadge';
import { buildDrillRequestContext, resolveCellContext, type MeasureLookup } from './utils/cellContext';
import { TESSALLITE_CONNECTION_NAME } from './utils/excelFormulas';
import type { Persona } from './types/tessallite';
import { mapAgentChartType } from './utils/excelCharts';

export type AppMode = 'ask' | 'report-builder' | 'kpi';

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 2, staleTime: 30000 } },
});

interface AgentConfigState {
  configured: boolean;
  provider?: string;
  model?: string;
  displayName?: string;
}

function AppInner() {
  const queryCache = useQueryClient();
  const { authState, profiles, activeProfile, login, logout, switchProfile, deleteProfile } = useAuth();
  const { showToast } = useToast();
  const handleBusy = useCallback(() => showToast(strings.toasts.excelBusy, 'info'), [showToast]);
  const { insertTable: excelInsertTable, insertChart: excelInsertChart, insertLocalPivot: excelInsertLocalPivot, insertKpiScorecard: excelInsertKpiScorecard, readCellValue } = useExcel(undefined, handleBusy);

  const startNewConversation = useConversationStore((s: ConversationState) => s.startNewConversation);
  const modelIdRef = useRef<string | null>(null);

  const [mode, setMode] = useState<AppMode>('report-builder');
  const [loginError, setLoginError] = useState<string | null>(null);
  const [loginLoading, setLoginLoading] = useState(false);
  const [connected, setConnected] = useState(false);
  const [offlineBanner, setOfflineBanner] = useState(false);
  const failCountRef = useRef(0);

  const [projectId, setProjectId] = useState<string | null>(null);
  const [modelId, setModelId] = useState<string | null>(null);
  modelIdRef.current = modelId;
  const excelAdapter = useMemo(
    () => createExcelAdapter(() => modelIdRef.current),
    [],
  );
  const [projectsLoading, setProjectsLoading] = useState(false);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [projectsList, setProjectsList] = useState<{ id: string; name: string }[]>([]);
  const [modelsList, setModelsList] = useState<{ id: string; name: string; slug?: string }[]>([]);

  const [agentConfig, setAgentConfig] = useState<AgentConfigState>({ configured: false });

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
    startNewConversation();
    setActivePersonaId(null);
    clearModelContext().catch(() => {});
    try {
      const models = await getModels(newProjectId);
      setModelsList(models);
      if (models.length > 0) {
        setModelId(models[0].id);
        setModelContext(newProjectId, models[0].id).catch(() => {});
      }
    } catch { /* ignore */ }
  }, [startNewConversation]);

  const handleModelChange = useCallback((newModelId: string) => {
    setModelId(newModelId);
    startNewConversation();
    setActivePersonaId(null);
    if (projectId) setModelContext(projectId, newModelId).catch(() => {});
  }, [startNewConversation, projectId]);

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

  const clearSessionState = useCallback(() => {
    startNewConversation();
    setProjectId(null);
    setModelId(null);
    setActivePersonaId(null);
    setProjectsLoading(false);
    setProjectsError(null);
    setAgentConfig({ configured: false });
  }, [startNewConversation]);

  const handleLogout = useCallback(async () => {
    clearSessionState();
    queryCache.clear();
    clearFunctionCaches();
    await logout();
  }, [logout, queryCache, clearSessionState]);

  const handleSwitchProfile = useCallback(async (profileId: string) => {
    clearSessionState();
    queryCache.clear();
    clearFunctionCaches();
    await switchProfile(profileId);
  }, [switchProfile, queryCache, clearSessionState]);

  const handleRemoveProfile = useCallback(async (profileId: string) => {
    await deleteProfile(profileId);
    showToast('Profile removed', 'info');
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
        let models: { id: string; name: string }[] = [];
        try {
          models = await getModels(pid);
        } catch { /* models fetch failed, still show project */ }
        if (cancelled) return;
        setModelsList(models);
        setProjectId(pid);
        if (models.length > 0) {
          setModelId(models[0].id);
          setModelContext(pid, models[0].id).catch(() => {});
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
      setAgentConfig({ configured: false });
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
        });
      })
      .catch(() => {
        if (cancelled) return;
        setAgentConfig({ configured: false });
      });

    return () => { cancelled = true; };
  }, [projectId]);


  const handleInsertTable = useCallback(async (turn: TurnResponse) => {
    const sample = turn.query_result_sample;
    if (!sample || sample.length === 0) return;
    const headers = Object.keys(sample[0]);
    const rows = sample.map(r => headers.map(h => r[h] as string | number));
    try {
      const rangeAddress = await excelInsertTable(
        headers,
        rows,
        undefined,
        {
          projectId: projectId || undefined,
          modelId: modelId || undefined,
          personaId: activePersonaId || undefined,
          conversationId: turn.conversation_id || undefined,
          turnId: turn.id || undefined,
          semanticQuery: turn.id
            ? JSON.stringify({ source: 'agent', conversation_id: turn.conversation_id, message_id: turn.id })
            : undefined,
          columnHeaders: headers,
        },
      );
      if (rangeAddress) {
        showToast(`Inserted ${rows.length} rows`, 'success');
      }
    } catch {
      showToast(strings.toasts.insertTableFailed, 'error');
    }
  }, [excelInsertTable, showToast, projectId, modelId, activePersonaId]);

  const handleFeedback = useCallback(async (turnId: string, vote: 'up' | 'down') => {
    if (!projectId) return;
    const convId = useConversationStore.getState().activeConversationId;
    if (!convId) return;
    try {
      await excelAdapter.submitFeedback(projectId, convId, turnId, vote);
      showToast('Feedback recorded', 'success');
    } catch {
      showToast(strings.toasts.feedbackFailed, 'error');
    }
  }, [projectId, excelAdapter, showToast]);

  const handleInsertChart = useCallback(async (turn: TurnResponse) => {
    const sample = turn.query_result_sample;
    if (!sample || sample.length === 0) {
      showToast(strings.toasts.noDataToChart, 'info');
      return;
    }
    const headers = Object.keys(sample[0]);
    const rows = sample.map(r => headers.map(h => r[h] as string | number));
    const agentMapped = mapAgentChartType(turn.chart_type);
    if (agentMapped === null && turn.chart_type === 'kpi') {
      showToast('KPI results are best viewed as values, not charts.', 'info');
      return;
    }
    const effectiveType = agentMapped ?? undefined;
    try {
      await excelInsertChart(headers, rows, effectiveType);
      showToast(strings.toasts.chartCreated, 'success');
    } catch {
      showToast(strings.toasts.chartInsertFailed, 'error');
    }
  }, [excelInsertChart, showToast]);

  const handleInsertLocalPivot = useCallback(async (turn: TurnResponse) => {
    const sample = turn.query_result_sample;
    if (!sample || sample.length === 0) {
      showToast(strings.toasts.noDataForPivot, 'info');
      return;
    }
    const headers = Object.keys(sample[0]);
    const rows = sample.map(r => headers.map(h => r[h] as string | number));
    try {
      await excelInsertLocalPivot(headers, rows);
      showToast('Local PivotTable created', 'success');
    } catch {
      showToast(strings.toasts.pivotInsertFailed, 'error');
    }
  }, [excelInsertLocalPivot, showToast]);

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
      await excelInsertTable(headers, rows);
      showToast(`Inserted ${rows.length} drill-through rows`, 'success');
    } catch (e) {
      showToast(strings.toasts.insertDrillRowsFailed, 'error');
    }
  }, [excelInsertTable, showToast]);

  const handlePersonaSelect = useCallback((persona: Persona | null) => {
    setActivePersonaId(persona?.id || null);
    startNewConversation();
    if (persona) {
      showToast(`Switched to ${persona.name} view`, 'info');
    }
  }, [showToast, startNewConversation]);

  const providerModel = agentConfig.configured && agentConfig.provider && agentConfig.model
    ? `${agentConfig.provider} ${agentConfig.model}`
    : (projectId ? strings.app.loadingProviderInfo : undefined);
  const activeProjectName = projectsList.find(p => p.id === projectId)?.name;
  const activeModelName = modelsList.find(m => m.id === modelId)?.name;
  const askTabLabel = agentConfig.displayName?.trim()
    ? `Ask ${agentConfig.displayName.trim()}`
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
              Tessallite
            </Typography>
            <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, lineHeight: 1.15 }}>
              Excel plugin
            </Typography>
          </Box>
          <Box sx={{ mr: 'auto' }} />
          <IconButton size="small" title={strings.app.drillThrough} aria-label={strings.app.drillThroughCell} onClick={handleOpenDrill}>
            <FilterAltOutlined sx={{ fontSize: 20, color: tokens.colorTextSecondary }} />
          </IconButton>
          <IconButton size="small" title="Glossary" aria-label="Open glossary" onClick={() => setGlossaryOpen(true)}>
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
            title="Settings"
            aria-label="Open settings"
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
              Project
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
              primary="Diagnostics"
              primaryTypographyProps={{ fontSize: 13 }}
            />
          </MenuItem>
          <MenuItem onClick={() => { setSettingsAnchorEl(null); handleLogout(); }}>
            <ListItemText
              primary="Sign Out"
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
            { id: 'report-builder' as const, label: 'Analyse', icon: AnalyticsOutlined },
            { id: 'kpi' as const, label: 'KPIs', icon: SpeedOutlined },
            { id: 'ask' as const, label: askTabLabel, icon: AutoAwesomeOutlined },
          ]).map(tab => {
            const active = mode === tab.id;
            const Icon = tab.icon;
            return (
              <Box
                key={tab.id}
                role="tab"
                aria-selected={active}
                aria-current={active ? 'page' : undefined}
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
              Connection lost. Retrying...
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
              Retry
            </Box>
          </Box>
        )}

        {activePersonaId && personaData.activePersona && (
          <Box sx={{
            px: 2, py: 0.5, bgcolor: tokens.colorSubtleFill,
            borderBottom: `1px solid ${tokens.colorBorderLight}`,
          }}>
            <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary }}>
              Viewing as &quot;{personaData.activePersona.name}&quot;.{' '}
              <Box component="span" sx={{ color: tokens.colorPrimary, cursor: 'pointer' }} onClick={() => handlePersonaSelect(null)}>
                Reset to default
              </Box>
            </Typography>
          </Box>
        )}

        <Box
          component="main"
          sx={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}
        >
          {mode === 'ask' && projectId && (
            <Box
              role="log"
              aria-live="polite"
              aria-label="Chat messages"
              sx={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}
            >
              <ExcelChatShell
                adapter={excelAdapter}
                t={chatT}
                projectId={projectId}
                config={agentConfig.configured ? (agentConfig as unknown as import('@tessallite/shared-ui').AgentConfig) : null}
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
          )}
          {mode === 'report-builder' && (
            <Box
              role="region"
              aria-label="Report Builder"
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
              role="region"
              aria-label="KPI Scorecard"
              sx={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}
            >
              {projectId && modelId ? (
                <KpiPanel
                  projectId={projectId}
                  modelId={modelId}
                  personaId={activePersonaId}
                  onInsertTable={excelInsertTable}
                  onInsertChart={(headers, rows) => excelInsertChart(headers, rows, 'columnClustered')}
                  onInsertScorecard={excelInsertKpiScorecard}
                  connectionName={TESSALLITE_CONNECTION_NAME}
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
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <AppInner />
      </ToastProvider>
    </QueryClientProvider>
  );
}
