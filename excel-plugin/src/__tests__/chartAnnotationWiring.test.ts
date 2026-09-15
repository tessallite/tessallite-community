import { createElement, type ReactNode } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { TurnResponse } from '@tessallite/shared-ui';
import App from '../App';
import * as charts from '../utils/excelCharts';
import { strings } from '../i18n/strings';

const mocks = vi.hoisted(() => ({
  insertChart: vi.fn(),
  insertLocalPivot: vi.fn(),
  executeQuery: vi.fn(),
  showToast: vi.fn(),
  store: {
    startNewConversation: vi.fn(), resetSession: vi.fn(),
    setPendingPersonaId: vi.fn(), setActiveConversation: vi.fn(),
    activeConversationId: null,
  },
  turn: {
    id: 'turn-9737', conversation_id: 'conv-1', turn_index: 0,
    user_message: 'total base amount by account type this year',
    answer_text: 'Base amount by account type', status: 'complete',
    latency_ms: null, thought_summary: null,
    semantic_query: { measures: ['base_amount'], dimensions: ['account_type'] },
    routed_sql: null, route: null,
    citations: [
      { kind: 'measure', id: 'm1', name: 'base_amount', display_name: 'Base amount', value: null },
      { kind: 'dimension', id: 'd1', name: 'account_type', display_name: 'Account type', value: null },
    ],
    user_feedback: null, judge_verdict: null, judge_reasoning: null,
    judge_metrics: null, guardrail_actions: null,
    usage_input_tokens: null, usage_output_tokens: null,
    rendered_output: null, llm_plan: null, query_result_rows: 2,
    query_result_sample: [
      { base_amount: '23332917.80', account_type: 'CREDIT' },
      { base_amount: '23055047.22', account_type: 'CURRENT' },
    ],
    calculation_steps: null, chart_type: null, provider: null,
  } satisfies TurnResponse,
}));

// Keep the real App, ExcelChatShell, InsertActions and chart utilities. Isolate
// unrelated panels, remote services and the native Office boundary only.
vi.mock('@tessallite/shared-ui', () => ({
  parseVisualArtifact: () => null, // This fixture is a result sample, not a visual artifact.
  useConversationStore: (select: (state: unknown) => unknown) => select(mocks.store),
  ChatProvider: ({ children }: { children: ReactNode }) => children,
  ChatCanvas: ({ renderTurnActions }: {
    renderTurnActions: (turn: TurnResponse, rows: Record<string, unknown>[]) => ReactNode;
  }) => renderTurnActions(mocks.turn, mocks.turn.query_result_sample),
}));
vi.mock('../hooks/useAuth', () => ({
  useAuth: () => ({ authState: 'authenticated', profiles: [], activeProfile: null }),
}));
vi.mock('../hooks/useExcel', () => ({
  useExcel: () => ({ insertChart: mocks.insertChart, insertLocalPivot: mocks.insertLocalPivot }),
}));
vi.mock('../hooks/usePersona', () => ({
  usePersonaFiltered: () => ({ personas: [], activePersona: null }),
}));
vi.mock('../hooks/useModel', () => ({ useGlossary: () => ({ data: [] }) }));
vi.mock('../api/gateway', () => ({ healthCheck: async () => undefined }));
vi.mock('../api/modelService', () => ({
  getProjects: async () => [{ id: 'p1', name: 'Project' }],
  getModels: async () => [{ id: 'm1', name: 'Model', slug: 'model' }],
}));
vi.mock('../api/agentService', () => ({ getAgentConfig: async () => ({ configured: true }) }));
vi.mock('../api/agentChatAdapter', () => ({
  createExcelAdapter: () => ({ getConversations: async () => [] }),
}));
vi.mock('../api/queryRouter', () => ({ executeQuery: (...args: unknown[]) => mocks.executeQuery(...args) }));
vi.mock('../utils/storage', () => ({
  getLastMode: async () => 'ask', setLastMode: async () => undefined,
  setModelContext: async () => undefined, clearModelContext: async () => undefined,
  setActivePersonaId: async () => undefined,
}));
vi.mock('../functions', () => ({
  clearFunctionCaches: vi.fn(),
  applyContextTransition: async ({ persist }: { persist?: () => Promise<void> }) => persist?.(),
}));
vi.mock('../components/Toast/ToastProvider', () => ({
  ToastProvider: ({ children }: { children: ReactNode }) => children,
  useToast: () => ({ showToast: mocks.showToast }),
}));
vi.mock('../components/Confirm/ConfirmProvider', () => ({
  ConfirmProvider: ({ children }: { children: ReactNode }) => children,
  useConfirm: () => vi.fn(),
}));
vi.mock('../components/LoginScreen/LoginScreen', () => ({ default: () => null }));
vi.mock('../components/ReportBuilder/ReportBuilder', () => ({ default: () => null }));
vi.mock('../components/KpiPanel', () => ({ KpiPanel: () => null }));
vi.mock('../components/Glossary/GlossaryModal', () => ({ default: () => null }));
vi.mock('../components/DrillThrough/DrillPanel', () => ({ default: () => null }));
vi.mock('../components/Settings/DiagnosticsPanel', () => ({ default: () => null }));
vi.mock('../components/Shell', () => ({
  AppHeader: () => null, ModeTabs: () => null, OfflineBanner: () => null, AppFooter: () => null,
}));

const headers = ['base_amount', 'account_type'];
const numericRows = [[23332917.8, 'CREDIT'], [23055047.22, 'CURRENT']];
const annotation: charts.ChartAnnotation = {
  measures: { base_amount: { title: 'Base amount', type: 'measure' } },
  dimensions: { account_type: { title: 'Account type', type: 'dimension' } },
};

beforeEach(() => {
  vi.clearAllMocks();
  mocks.turn.query_result_rows = 2;
  mocks.insertChart.mockResolvedValue({ address: 'Chart Data!A1:B3' });
  mocks.insertLocalPivot.mockResolvedValue(undefined);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('Bug-9737 chart annotation wiring', () => {
  it('passes citation classification and numeric rows to chart recommendation', async () => {
    const recommend = vi.spyOn(charts, 'recommendChartType');
    render(createElement(App));
    await screen.findByRole('button', { name: strings.insertActions.chart });
    expect(recommend).toHaveBeenCalledWith(headers, numericRows, annotation);
    expect(recommend.mock.results.at(-1)?.value.chartType).toBe('pie');
  });

  it('inserts a chart from the fixture turn with one dimension and one numeric measure', async () => {
    render(createElement(App));
    fireEvent.click(await screen.findByRole('button', { name: strings.insertActions.chart }));
    await waitFor(() => expect(mocks.insertChart).toHaveBeenCalledOnce());
    expect(mocks.insertChart).toHaveBeenCalledWith(headers, numericRows, 'pie', annotation);
    const [actualHeaders, actualRows, , actualAnnotation] = mocks.insertChart.mock.calls[0];
    expect(charts.separateColumns(actualHeaders, actualRows, actualAnnotation)).toEqual({
      chartHeaders: ['Category', 'base_amount'],
      chartRows: [['CREDIT', 23332917.8], ['CURRENT', 23055047.22]],
    });
  });

  it('passes citation classification and numeric rows to local pivot insertion', async () => {
    render(createElement(App));
    fireEvent.click(await screen.findByRole('button', { name: strings.insertActions.localPivot }));
    await waitFor(() => expect(mocks.insertLocalPivot).toHaveBeenCalledOnce());
    expect(mocks.insertLocalPivot).toHaveBeenCalledWith(headers, numericRows, undefined, annotation);
  });

  it('uses all re-fetched rows for a truncated turn and retains its annotation', async () => {
    mocks.turn.query_result_rows = 3;
    mocks.executeQuery.mockResolvedValue({ data: [
      ...mocks.turn.query_result_sample,
      { base_amount: 12000, account_type: 'SAVINGS' },
    ] });
    render(createElement(App));
    fireEvent.click(await screen.findByRole('button', { name: strings.insertActions.chart }));
    await waitFor(() => expect(mocks.insertChart).toHaveBeenCalledOnce());
    expect(mocks.executeQuery).toHaveBeenCalledOnce();
    expect(mocks.insertChart).toHaveBeenCalledWith(
      headers, [...numericRows, [12000, 'SAVINGS']], 'pie', annotation,
    );
  });
});
