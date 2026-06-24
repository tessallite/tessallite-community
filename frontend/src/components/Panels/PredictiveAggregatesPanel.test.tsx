/**
 * F-010-01 — predictive preview / approval UI.
 *
 * Business outcomes under test:
 * - The candidate table renders scored candidates with rationale.
 * - row_reduction is a multiplier and renders as "N×", never as a percent
 *   (F-010-04: 50000 must show "50,000×", not "5000000.0%").
 * - With approval required, the pending-approval indicator is shown and a
 *   selected candidate can be approved (built) from the UI.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

const getPredictivePreviewMock = vi.fn();
const runPredictiveBuildMock = vi.fn();
const grainSuggestionsMock = vi.fn();

vi.mock("../../api/client", () => ({
  optimizerApiClient: {
    getPredictivePreview: (...args: unknown[]) =>
      getPredictivePreviewMock(...args),
    runPredictiveBuild: (...args: unknown[]) =>
      runPredictiveBuildMock(...args),
  },
  hierarchiesApi: {
    grainSuggestions: (...args: unknown[]) => grainSuggestionsMock(...args),
  },
}));

// Review F-1: the read-only preview is open to every tenant user; the build
// half is tenant_admin-only. Mutable flag lets tests cover both personas.
let mockIsTenantAdmin = true;
vi.mock("../../auth/currentUser", () => ({
  isTenantAdmin: () => mockIsTenantAdmin,
  isSystemAdmin: () => false,
  canEditModelConfig: () => true,
  currentUserRole: () => (mockIsTenantAdmin ? "tenant_admin" : "member"),
}));

import PredictiveAggregatesPanel from "./PredictiveAggregatesPanel";

const CANDIDATE = {
  grain: ["region", "month"],
  measure_names: ["total_sales"],
  fact_table: "public.sales",
  score: 0.87,
  score_pct: 100,
  expected_hit_rate: 0.62,
  row_reduction: 50000,
  estimated_rows: 1200,
  rationale: "Low-cardinality grain (50000× reduction)",
};

function renderPanel(props: { requiresApproval?: boolean } = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/p/project-1/m/model-1"]}>
        <Routes>
          <Route
            path="/p/:projectId/m/:modelId"
            element={<PredictiveAggregatesPanel {...props} />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PredictiveAggregatesPanel", () => {
  beforeEach(() => {
    getPredictivePreviewMock.mockReset();
    runPredictiveBuildMock.mockReset();
    grainSuggestionsMock.mockReset();
    grainSuggestionsMock.mockResolvedValue([]);
    mockIsTenantAdmin = true;
  });

  it("renders the candidate table with rationale", async () => {
    getPredictivePreviewMock.mockResolvedValue({
      model_id: "model-1",
      candidates: [CANDIDATE],
    });

    renderPanel();

    expect(await screen.findByText("public.sales")).toBeInTheDocument();
    expect(screen.getByText("region, month")).toBeInTheDocument();
    expect(
      screen.getByText(/Low-cardinality grain/),
    ).toBeInTheDocument();
  });

  it("renders row_reduction as a multiplier, not a percent (F-010-04)", async () => {
    getPredictivePreviewMock.mockResolvedValue({
      model_id: "model-1",
      candidates: [CANDIDATE],
    });

    renderPanel();

    expect(await screen.findByText("50,000×")).toBeInTheDocument();
    // Hit rate is an assumption (F-010-15): rendered with a ~ prefix as a
    // percent; the broken percent rendering must be gone.
    expect(screen.getByText("~62.0%")).toBeInTheDocument();
    expect(screen.queryByText("5000000.0%")).not.toBeInTheDocument();
  });

  it("shows the pending-approval indicator when approval is required", async () => {
    getPredictivePreviewMock.mockResolvedValue({
      model_id: "model-1",
      candidates: [CANDIDATE],
    });

    renderPanel({ requiresApproval: true });

    expect(
      await screen.findByTestId("predictive-approval-pending"),
    ).toBeInTheDocument();
  });

  it("hides the pending-approval indicator when approval is not required", async () => {
    getPredictivePreviewMock.mockResolvedValue({
      model_id: "model-1",
      candidates: [CANDIDATE],
    });

    renderPanel();

    await screen.findByText("public.sales");
    expect(
      screen.queryByTestId("predictive-approval-pending"),
    ).not.toBeInTheDocument();
  });

  it("shows a no-stats warning when preview reports no source statistics", async () => {
    getPredictivePreviewMock.mockResolvedValue({
      model_id: "model-1",
      candidates: [],
      had_stats: false,
    });

    renderPanel();

    expect(await screen.findByTestId("predictive-no-stats")).toBeInTheDocument();
    expect(
      screen.getByText(/No source statistics have been collected/i),
    ).toBeInTheDocument();
  });

  it("approves a selected candidate: Build selected sends its grain and measures", async () => {
    getPredictivePreviewMock.mockResolvedValue({
      model_id: "model-1",
      candidates: [CANDIDATE],
    });
    runPredictiveBuildMock.mockResolvedValue({
      model_id: "model-1",
      requested: 1,
      created_aggregate_ids: ["agg-1"],
      skipped_count: 0,
      errors: [],
    });

    renderPanel({ requiresApproval: true });
    const user = userEvent.setup();

    await screen.findByText("public.sales");
    const checkboxes = screen.getAllByRole("checkbox");
    // first checkbox is select-all; second is the candidate row
    await user.click(checkboxes[1]);
    await user.click(
      screen.getByRole("button", { name: /build selected \(1\)/i }),
    );

    await waitFor(() => {
      expect(runPredictiveBuildMock).toHaveBeenCalledWith("model-1", [
        { grain: ["region", "month"], measure_names: ["total_sales"] },
      ]);
    });
    // the result summary surfaces to the user
    expect(
      await screen.findByText(/requested 1, created 1, skipped 0/i),
    ).toBeInTheDocument();
  });

  it("shows a no-stats warning when a build response reports no source statistics", async () => {
    getPredictivePreviewMock.mockResolvedValue({
      model_id: "model-1",
      candidates: [CANDIDATE],
      had_stats: true,
    });
    runPredictiveBuildMock.mockResolvedValue({
      model_id: "model-1",
      requested: 0,
      created_aggregate_ids: [],
      skipped_count: 0,
      errors: [],
      had_stats: false,
    });

    renderPanel();
    const user = userEvent.setup();

    await screen.findByText("public.sales");
    await user.click(screen.getByRole("button", { name: /build all/i }));

    expect(
      await screen.findByText(/No source statistics have been collected/i),
    ).toBeInTheDocument();
  });

  it("non-admin (modeler) sees the candidates read-only — no build controls, no 403 (review F-1)", async () => {
    mockIsTenantAdmin = false;
    getPredictivePreviewMock.mockResolvedValue({
      model_id: "model-1",
      candidates: [CANDIDATE],
    });

    renderPanel({ requiresApproval: true });

    // the what-if analysis itself renders for the modeler
    expect(await screen.findByText("public.sales")).toBeInTheDocument();
    expect(screen.getByText("50,000×")).toBeInTheDocument();

    // the tenant_admin-gated build half is hidden, with an explanation,
    // instead of failing with a 403 alert
    expect(
      screen.getByTestId("predictive-read-only-notice"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /build selected/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /build all/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
    // the approval call-to-action targets admins only
    expect(
      screen.queryByTestId("predictive-approval-pending"),
    ).not.toBeInTheDocument();
  });

  it("admin does not see the read-only notice", async () => {
    getPredictivePreviewMock.mockResolvedValue({
      model_id: "model-1",
      candidates: [CANDIDATE],
    });

    renderPanel();

    await screen.findByText("public.sales");
    expect(
      screen.queryByTestId("predictive-read-only-notice"),
    ).not.toBeInTheDocument();
  });
});
