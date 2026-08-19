import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const useQueryLogsMock = vi.fn();
const useAIOptimizerRunsMock = vi.fn().mockReturnValue({ data: [], isLoading: false, isFetching: false, refetch: vi.fn() });
// F-011-15: the list returns lightweight summaries; full detail (recommendations,
// decision log, raw response) is fetched on demand via useAIOptimizerRun when a
// row is expanded.
const useAIOptimizerRunMock = vi.fn().mockReturnValue({ data: undefined, isLoading: false });

vi.mock("../../api/hooks", () => ({
  useQueryLogs: (...args: unknown[]) => useQueryLogsMock(...args),
  useModelRefreshRuns: () => ({ data: [], isLoading: false }),
  useOptimizerRuns: () => ({ data: [], isLoading: false, isFetching: false, refetch: vi.fn() }),
  useAIOptimizerRuns: (...args: unknown[]) => useAIOptimizerRunsMock(...args),
  useAIOptimizerRun: (...args: unknown[]) => useAIOptimizerRunMock(...args),
}));

vi.mock("../../api/client", () => ({
  // F-030-25: the query-detail dialog now lazily fetches a stored route trace.
  logsApi: { exportCsv: vi.fn(), queryTrace: vi.fn().mockResolvedValue([]) },
}));

import DiagnosticsPanel from "./DiagnosticsPanel";

function renderPanel() {
  useQueryLogsMock.mockReturnValue({
    data: {
      items: [
        {
          id: "q-1",
          model_id: "model-1",
          user_identity: "analyst@example.com",
          protocol: "jdbc",
          client_kind: "looker_cloud",
          raw_query: "SELECT payment_id FROM modelx",
          query_fingerprint: "fp",
          route_type: "source",
          aggregate_id: null,
          rewritten_query: null,
          execution_ms: 12,
          rows_returned: 1,
          bytes_processed: 10,
          status: "success",
          error_type: null,
          error_detail: null,
          created_at: "2026-05-25T12:00:00Z",
        },
      ],
    },
    isLoading: false,
    isFetching: false,
    refetch: vi.fn(),
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        initialEntries={["/p/project-1/m/model-1"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/p/:projectId/m/:modelId" element={<DiagnosticsPanel />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("DiagnosticsPanel client-kind telemetry", () => {
  it("filters Looker Cloud query logs and displays the selected query label", async () => {
    const user = userEvent.setup();
    renderPanel();

    fireEvent.mouseDown(screen.getByLabelText("Client"));
    await user.click(await screen.findByRole("option", { name: "Looker Cloud" }));

    await waitFor(() => {
      expect(
        useQueryLogsMock.mock.calls.some(
          (call) => (call[1] as { clientKind?: string }).clientKind === "looker_cloud",
        ),
      ).toBe(true);
    });

    await user.click(screen.getByText("SELECT payment_id FROM modelx"));
    expect((await screen.findAllByText("Looker Cloud")).length).toBeGreaterThan(0);
  });
});

describe("DiagnosticsPanel probe exclusion (Bug-7451)", () => {
  it("passes includeProbes=undefined by default (probes excluded)", () => {
    renderPanel();
    // The initial call should NOT pass includeProbes (undefined = excluded).
    const initialCall = useQueryLogsMock.mock.calls[0];
    expect(initialCall[1].includeProbes).toBeUndefined();
  });

  it("passes includeProbes=true when the checkbox is checked", async () => {
    const user = userEvent.setup();
    renderPanel();
    const checkbox = screen.getByRole("checkbox", { name: /include probes/i });
    await user.click(checkbox);
    await waitFor(() => {
      const lastCall = useQueryLogsMock.mock.calls[useQueryLogsMock.mock.calls.length - 1];
      expect(lastCall[1].includeProbes).toBe(true);
    });
  });
});

describe("DiagnosticsPanel expanded client-kind filter (Bug-7451)", () => {
  it("shows expanded client-kind options including headless, agent, mcp", async () => {
    const user = userEvent.setup();
    renderPanel();
    fireEvent.mouseDown(screen.getByLabelText("Client"));
    const options = await screen.findAllByRole("option");
    const optionTexts = options.map((o) => o.textContent);
    expect(optionTexts).toContain("Headless");
    expect(optionTexts).toContain("Agent");
    expect(optionTexts).toContain("MCP");
    expect(optionTexts).toContain("Excel Plugin");
    expect(optionTexts).toContain("Drill-through");
  });
});

describe("DiagnosticsPanel AI optimizer run expansion", () => {
  it("renders diagnostics_log entries with code_map when an AI run is expanded", async () => {
    const user = userEvent.setup();

    // List summary (no heavy blobs — F-011-15).
    const summary = {
      id: "ai-run-1",
      model_id: "model-1",
      triggered_by: "manual",
      status: "completed",
      is_dry_run: false,
      started_at: "2026-06-01T10:00:00Z",
      completed_at: "2026-06-01T10:01:00Z",
      llm_provider: "anthropic",
      llm_model: "claude-sonnet-4-6",
      telemetry_snapshot_id: null,
      recommendations_count: 1,
      aggregates_created: 1,
      aggregates_skipped: 0,
      error_message: null,
      raw_llm_response: null,
      analysis_notes: null,
      diagnostics_log: null,
      recommendations: [],
    };
    useAIOptimizerRunsMock.mockReturnValue({
      data: [summary],
      isLoading: false,
      isFetching: false,
      refetch: vi.fn(),
    });
    // Detail-on-expand returns the full run with the decision log + analysis notes.
    useAIOptimizerRunMock.mockReturnValue({
      data: {
        ...summary,
        analysis_notes: "Reviewed the top miss patterns and recommended one grain.",
        diagnostics_log: [
          { step: "masking", message: "Masked 2 opaque dimensions", code_map: { D001: "customer_country", D002: "product_category" } },
          { step: "dedup", message: "Skipped 1 canonical duplicate" },
        ],
      },
      isLoading: false,
    });

    renderPanel();

    await user.click(screen.getByRole("tab", { name: /optimisation/i }));

    const aiRow = screen.getByText("claude-sonnet-4-6").closest("tr")!;
    await user.click(aiRow);

    expect(await screen.findByText("Masked 2 opaque dimensions")).toBeInTheDocument();
    expect(screen.getByText("Skipped 1 canonical duplicate")).toBeInTheDocument();

    expect(screen.getByText(/D001/)).toBeInTheDocument();
    expect(screen.getByText(/customer_country/)).toBeInTheDocument();
    expect(screen.getByText(/D002/)).toBeInTheDocument();
    expect(screen.getByText(/product_category/)).toBeInTheDocument();

    // F-011-07: analysis_notes is now rendered in the expanded detail.
    expect(screen.getByText("Reviewed the top miss patterns and recommended one grain.")).toBeInTheDocument();
  });
});

describe("DiagnosticsPanel AI run status labels (F-559-04)", () => {
  it("renders the queued status through i18n, not the raw backend token", async () => {
    const user = userEvent.setup();

    // Bug-8034: a manual advisor run is durably accepted as `queued` and stays
    // there until the scheduler's dispatcher claims it, so this is what a user
    // polling the run history sees straight after triggering a run.
    useAIOptimizerRunsMock.mockReturnValue({
      data: [
        {
          id: "ai-run-queued",
          model_id: "model-1",
          triggered_by: "manual",
          status: "queued",
          is_dry_run: false,
          started_at: "2026-08-11T10:00:00Z",
          completed_at: null,
          llm_provider: null,
          llm_model: "claude-pending-marker",
          telemetry_snapshot_id: null,
          recommendations_count: 0,
          aggregates_created: 0,
          aggregates_skipped: 0,
          error_message: null,
          raw_llm_response: null,
          analysis_notes: null,
          diagnostics_log: null,
          recommendations: [],
        },
      ],
      isLoading: false,
      isFetching: false,
      refetch: vi.fn(),
    });
    useAIOptimizerRunMock.mockReturnValue({ data: undefined, isLoading: false });

    renderPanel();
    await user.click(screen.getByRole("tab", { name: /optimisation/i }));

    const row = (await screen.findByText("claude-pending-marker")).closest("tr")!;
    expect(row.textContent).toContain("Queued");
    expect(row.textContent).not.toContain("queued");
  });
});
