/**
 * Bug-9881: every KPI EVALUATION the add-in issues carries deployed_only=true.
 *
 * The pane's list/preview reads already went through `consumptionQuery()`; the
 * three evaluation call sites hand-rolled their query string and threaded only
 * `persona_id`, so a KPI card or a `TESSALLITE.KPIVALUE` cell showed the number
 * a modeller's unsaved draft produces rather than the deployed one.
 *
 * The functions-runtime call site (`functions.ts` `evalKpiCached`) is asserted
 * in `customFunctions.test.ts`, which drives the real registered function; this
 * file covers the task-pane API client. Both transports are separate clients of
 * the same routes — the pane's `apiClient` is not reachable from the WWAHost
 * functions runtime — so each needs its own guard.
 */
import { afterEach, describe, it, expect, vi } from 'vitest';
import { apiClient } from '../api/client';
import { evaluateKpi, evaluateKpiBatch } from '../api/modelService';

afterEach(() => vi.restoreAllMocks());

describe('Bug-9881 -- KPI evaluation is pinned to the deployed snapshot', () => {
  it('evaluateKpi sends deployed_only=true', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({});
    await evaluateKpi('project-1', 'model-1', 'kpi-1');
    expect(post).toHaveBeenCalledWith(
      '/api/v1/projects/project-1/models/model-1/kpis/kpi-1/evaluate?deployed_only=true',
    );
  });

  it('evaluateKpi keeps deployed_only alongside the persona', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({});
    await evaluateKpi('project-1', 'model-1', 'kpi-1', 'persona-1');
    expect(post).toHaveBeenCalledWith(
      '/api/v1/projects/project-1/models/model-1/kpis/kpi-1/evaluate'
      + '?deployed_only=true&persona_id=persona-1',
    );
  });

  it('evaluateKpiBatch sends deployed_only=true', async () => {
    const post = vi
      .spyOn(apiClient, 'post')
      .mockResolvedValue({ results: [], evaluation_ms: 0 });
    await evaluateKpiBatch('project-1', 'model-1', ['kpi-1']);
    expect(post).toHaveBeenCalledWith(
      '/api/v1/projects/project-1/models/model-1/kpis/evaluate-batch?deployed_only=true',
      { kpi_ids: ['kpi-1'] },
    );
  });
});
