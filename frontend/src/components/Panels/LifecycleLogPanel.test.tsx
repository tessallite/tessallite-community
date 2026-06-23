import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

const getAggregateLifecycleMock = vi.fn();

vi.mock("../../api/client", () => ({
  optimizerApiClient: {
    getAggregateLifecycle: (...args: unknown[]) =>
      getAggregateLifecycleMock(...args),
  },
}));

import LifecycleLogPanel from "./LifecycleLogPanel";

function renderPanel() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/m/model-1"]}>
        <Routes>
          <Route path="/m/:modelId" element={<LifecycleLogPanel />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("LifecycleLogPanel", () => {
  beforeEach(() => {
    getAggregateLifecycleMock.mockReset();
  });

  it("shows the empty-state when there are no events", async () => {
    getAggregateLifecycleMock.mockResolvedValue({
      model_id: "model-1",
      events: [],
    });

    renderPanel();

    await waitFor(() => {
      expect(
        screen.getByText(/no lifecycle events yet/i),
      ).toBeInTheDocument();
    });
  });

  it("renders rows for created and validated events", async () => {
    getAggregateLifecycleMock.mockResolvedValue({
      model_id: "model-1",
      events: [
        {
          id: "ev-1",
          aggregate_id: "11111111-1111-1111-1111-111111111111",
          event_type: "validated",
          reason: "feedback_sweep",
          payload: { hit_count: 5 },
          occurred_at: "2026-04-25T12:00:00Z",
        },
        {
          id: "ev-2",
          aggregate_id: "22222222-2222-2222-2222-222222222222",
          event_type: "created",
          reason: "predictive",
          payload: {},
          occurred_at: "2026-04-24T12:00:00Z",
        },
      ],
    });

    renderPanel();

    await waitFor(() => {
      expect(screen.getByText("validated")).toBeInTheDocument();
    });
    expect(screen.getByText("created")).toBeInTheDocument();
    expect(screen.getByText("feedback_sweep")).toBeInTheDocument();
  });
});
