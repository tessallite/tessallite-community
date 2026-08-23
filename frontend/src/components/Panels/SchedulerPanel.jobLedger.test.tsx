import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const jobsMock = vi.fn();
const getDependenciesMock = vi.fn();

vi.mock("../../api/hooks", () => ({
  useAISchedulerConfig: () => ({
    data: {
      id: "cfg-1",
      model_id: "m-1",
      ai_enabled: false,
      cron_expression: "0 5 * * *",
      lookback_hours: 168,
      max_creates_per_run: 3,
      dry_run: false,
    },
    isLoading: false,
  }),
  useAggregates: () => ({ data: [] }),
}));

vi.mock("../../api/client", () => ({
  aiSchedulerApi: { update: vi.fn() },
  aiOptimizerApi: { triggerRun: vi.fn() },
  optimizerApiClient: { runModelSweep: vi.fn() },
  schedulerApiClient: {
    jobs: (...args: unknown[]) => jobsMock(...args),
    getDependencies: (...args: unknown[]) => getDependenciesMock(...args),
    createDependency: vi.fn(),
    deleteDependency: vi.fn(),
    triggerRefresh: vi.fn(),
    triggerKpiSnapshotSweep: vi.fn(),
    triggerKpiSnapshotPurge: vi.fn(),
    triggerPocketSweep: vi.fn(),
    triggerPocketEviction: vi.fn(),
  },
}));

vi.mock("../../store/builderStore", () => ({
  useBuilderStore: (selector: (state: { readOnly: boolean }) => unknown) =>
    selector({ readOnly: false }),
}));

vi.mock("../Settings/SLAConfigPanel", () => ({
  SLAConfigPanel: () => null,
}));

vi.mock("../../i18n", () => ({ useT: () => (key: string) => key }));

import SchedulerPanel from "./SchedulerPanel";

beforeEach(() => {
  vi.clearAllMocks();
  getDependenciesMock.mockResolvedValue({ dependencies: [], execution_order: [] });
});

describe("SchedulerPanel scheduler job ledger", () => {
  it("renders Bug-9419 durable job outcomes from GET /scheduler/jobs", async () => {
    const lastFinished = "2026-08-22T14:05:00Z";
    const nextRun = "2026-08-23T05:00:00Z";
    jobsMock.mockResolvedValue({
      jobs: [{
        job_id: "daily_query_log_purge_sweep",
        name: "Query log purge",
        next_run_time: nextRun,
        last_status: "error",
        last_run_at: "2026-08-22T14:00:00Z",
        last_finished_at: lastFinished,
        last_outcome: "0 tenants completed",
        last_error: "source unavailable",
        last_trigger_source: "scheduled",
      }],
    });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <SchedulerPanel projectId="p-1" modelId="m-1" tenantId="acme" />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Query log purge")).toBeInTheDocument();
    expect(screen.getByText("scheduler.jobLedgerStatusError")).toBeInTheDocument();
    expect(screen.getByText(new Date(lastFinished).toLocaleString())).toBeInTheDocument();
    expect(screen.getByText("source unavailable")).toBeInTheDocument();
    expect(screen.getByText(new Date(nextRun).toLocaleString())).toBeInTheDocument();
    expect(screen.getByText("scheduler.jobLedgerTriggerScheduled")).toBeInTheDocument();
    expect(jobsMock).toHaveBeenCalledTimes(1);
  });
});
