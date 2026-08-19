/**
 * Bug-7117 — SchedulerPanel's "Run AI now" lacked the unsaved-changes guard
 * that SmartBuilderSection already has (F-011-16b): clicking Run with a
 * pending, unsaved form edit hit the backend with stale config and surfaced a
 * raw error like "AI optimiser is not enabled for model <uuid>". This proves
 * the guard is wired into SchedulerPanel specifically, not merely present in
 * its sibling panel.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const useAISchedulerConfigMock = vi.fn();
const useAggregatesMock = vi.fn();
const updateMock = vi.fn();
const triggerRunMock = vi.fn();
const getDependenciesMock = vi.fn();

vi.mock("../../api/hooks", () => ({
  useAISchedulerConfig: (...a: unknown[]) => useAISchedulerConfigMock(...a),
  useAggregates: (...a: unknown[]) => useAggregatesMock(...a),
}));

vi.mock("../../api/client", () => ({
  aiSchedulerApi: { update: (...a: unknown[]) => updateMock(...a) },
  aiOptimizerApi: { triggerRun: (...a: unknown[]) => triggerRunMock(...a) },
  optimizerApiClient: { runModelSweep: vi.fn() },
  schedulerApiClient: {
    getDependencies: (...a: unknown[]) => getDependenciesMock(...a),
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
  useBuilderStore: (selector: (s: { readOnly: boolean }) => unknown) =>
    selector({ readOnly: false }),
}));

vi.mock("../Settings/SLAConfigPanel", () => ({
  SLAConfigPanel: () => null,
}));

vi.mock("../../i18n", () => ({ useT: () => (key: string) => key }));

import SchedulerPanel from "./SchedulerPanel";

function makeConfig(overrides: Record<string, unknown> = {}) {
  return {
    id: "cfg-1",
    model_id: "m-1",
    ai_enabled: true,
    cron_expression: "0 5 * * *",
    lookback_hours: 168,
    max_creates_per_run: 3,
    dry_run: false,
    created_at: "2026-06-13T00:00:00Z",
    updated_at: "2026-06-13T00:00:00Z",
    ...overrides,
  };
}

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SchedulerPanel projectId="p-1" modelId="m-1" tenantId="acme" />
    </QueryClientProvider>,
  );
}

function runAiButton() {
  return screen.getByText("scheduler.runAiButton").closest("button") as HTMLButtonElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  useAggregatesMock.mockReturnValue({ data: [] });
  getDependenciesMock.mockResolvedValue({ dependencies: [], execution_order: [] });
  updateMock.mockImplementation((_p, _m, body) =>
    Promise.resolve(makeConfig(body as Record<string, unknown>)),
  );
});

describe("SchedulerPanel — save-before-run guard (Bug-7117)", () => {
  it("refuses Run AI now and shows a translated prompt when there is an unsaved change", async () => {
    useAISchedulerConfigMock.mockReturnValue({
      data: makeConfig({ ai_enabled: true }),
      isLoading: false,
    });
    renderPanel();
    const user = userEvent.setup();

    // Flip "Preview only" without saving — this dirties the form.
    await user.click(screen.getByLabelText("scheduler.previewOnly"));
    await user.click(runAiButton());

    expect(triggerRunMock).not.toHaveBeenCalled();
    expect(screen.getByText("scheduler.saveBeforeRun")).toBeInTheDocument();
  });

  it("allows Run AI now when the form matches the saved config", async () => {
    triggerRunMock.mockResolvedValue({});
    useAISchedulerConfigMock.mockReturnValue({
      data: makeConfig({ ai_enabled: true }),
      isLoading: false,
    });
    renderPanel();
    const user = userEvent.setup();

    await user.click(runAiButton());
    await waitFor(() => expect(triggerRunMock).toHaveBeenCalled());
    expect(
      screen.queryByText("scheduler.saveBeforeRun"),
    ).not.toBeInTheDocument();
  });

  it("clears the guard once the dirty change is saved", async () => {
    triggerRunMock.mockResolvedValue({});
    useAISchedulerConfigMock.mockReturnValue({
      data: makeConfig({ ai_enabled: true }),
      isLoading: false,
    });
    renderPanel();
    const user = userEvent.setup();

    await user.click(screen.getByLabelText("scheduler.previewOnly"));
    await user.click(screen.getByText("scheduler.saveSettings").closest("button")!);
    await waitFor(() => expect(updateMock).toHaveBeenCalled());

    await user.click(runAiButton());
    await waitFor(() => expect(triggerRunMock).toHaveBeenCalled());
  });
});
