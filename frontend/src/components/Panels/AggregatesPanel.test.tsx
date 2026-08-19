/**
 * F-010-01 — the predictive preview/approval UI must be reachable from the
 * Aggregates panel. Regression for the panel-consolidation refactor that
 * orphaned PredictiveAggregatesPanel: clicking the Predictive tab must show
 * the budget controls AND the scored candidate table, and an approval-gated
 * model must surface the pending-approval indicator.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

const getPredictivePreviewMock = vi.fn();

vi.mock("../../api/client", () => ({
  aggregatesApi: { delete: vi.fn() },
  dataQualityApi: { aggregateViolationSummary: vi.fn().mockResolvedValue({}) },
  modelsApi: { update: vi.fn() },
  optimizerApiClient: {
    getModelROI: vi.fn().mockResolvedValue([]),
    getPredictivePreview: (...args: unknown[]) =>
      getPredictivePreviewMock(...args),
    runPredictiveBuild: vi.fn(),
  },
  schedulerApiClient: { triggerRefresh: vi.fn() },
  hierarchiesApi: { grainSuggestions: vi.fn().mockResolvedValue([]) },
}));

vi.mock("../../api/hooks", () => ({
  useAggregates: () => ({ data: [], isLoading: false }),
  useModel: () => ({
    data: {
      id: "model-1",
      status: "active",
      aggregations_enabled: true,
      include_all_measures: true,
      predictive_requires_approval: true,
      predictive_eviction_policy: "predicted_first",
      predictive_storage_budget_bytes: null,
      predictive_storage_budget_count: null,
    },
  }),
  usePersonas: () => ({ data: [] }),
}));

vi.mock("../Confirm", () => ({
  useConfirm: () => async () => true,
}));

// The build half of the predictive panel is tenant_admin-gated (review F-1);
// this suite exercises the admin persona the approval flow targets.
vi.mock("../../auth/currentUser", () => ({
  isTenantAdmin: () => true,
  isSystemAdmin: () => false,
  canEditModelConfig: () => true,
  currentUserRole: () => "tenant_admin",
}));

vi.mock("./AggregateDrawer", () => ({ default: () => null }));
vi.mock("../Aggregates/AggregateCard", () => ({ default: () => null }));

import AggregatesPanel from "./AggregatesPanel";

function renderPanel() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/p/project-1/m/model-1"]}>
        <Routes>
          <Route path="/p/:projectId/m/:modelId" element={<AggregatesPanel />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AggregatesPanel — Predictive tab", () => {
  beforeEach(() => {
    getPredictivePreviewMock.mockReset();
    getPredictivePreviewMock.mockResolvedValue({
      model_id: "model-1",
      candidates: [
        {
          grain: ["region", "month"],
          measure_names: ["total_sales"],
          fact_table: "public.sales",
          score: 0.87,
          heuristic_reuse_score: 0.62,
          row_reduction: 50000,
          estimated_rows: 1200,
          rationale: "Low-cardinality grain (50000× reduction)",
        },
      ],
    });
  });

  it("reaches the candidate table and approval indicator from the Predictive tab", async () => {
    renderPanel();
    const user = userEvent.setup();

    await user.click(screen.getByTestId("agg-tab-predictive"));

    // budget/eviction controls are still there
    expect(
      await screen.findByTestId("agg-tab-content-predictive"),
    ).toBeInTheDocument();

    // the previously orphaned preview panel is mounted: candidates visible
    expect(await screen.findByText("public.sales")).toBeInTheDocument();
    expect(await screen.findByText("50,000×")).toBeInTheDocument();

    // approval-gated model surfaces the pending queue indicator
    expect(
      await screen.findByTestId("predictive-approval-pending"),
    ).toBeInTheDocument();
  });
});
