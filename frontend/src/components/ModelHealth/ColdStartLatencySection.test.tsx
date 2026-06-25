import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const getColdStartLatencyMock = vi.fn();

vi.mock("../../api/client", () => ({
  optimizerApiClient: {
    getColdStartLatency: (...args: unknown[]) =>
      getColdStartLatencyMock(...args),
  },
}));

import ColdStartLatencySection from "./ColdStartLatencySection";

function renderSection() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ColdStartLatencySection modelId="model-1" />
    </QueryClientProvider>,
  );
}

describe("ColdStartLatencySection", () => {
  beforeEach(() => {
    getColdStartLatencyMock.mockReset();
  });

  it("shows the not-deployed message when last_deployed_at is null", async () => {
    getColdStartLatencyMock.mockResolvedValue({
      model_id: "model-1",
      last_deployed_at: null,
      sample_count: 0,
      median_ms: null,
      p95_ms: null,
      baseline_median_ms: null,
      baseline_window_days: 14,
      samples: [],
    });

    renderSection();

    await waitFor(() => {
      expect(
        screen.getByText(/has not been deployed yet/i),
      ).toBeInTheDocument();
    });
  });

  it("renders stats and the chart when samples are present", async () => {
    getColdStartLatencyMock.mockResolvedValue({
      model_id: "model-1",
      last_deployed_at: "2026-04-20T10:00:00Z",
      sample_count: 3,
      median_ms: 220,
      p95_ms: 450,
      baseline_median_ms: 800,
      baseline_window_days: 14,
      samples: [
        {
          sequence: 1,
          occurred_at: "2026-04-20T10:01:00Z",
          execution_ms: 220,
          fingerprint: "fp1",
          aggregate_id: "agg-1",
        },
        {
          sequence: 2,
          occurred_at: "2026-04-20T10:02:00Z",
          execution_ms: 450,
          fingerprint: "fp2",
          aggregate_id: null,
        },
        {
          sequence: 3,
          occurred_at: "2026-04-20T10:03:00Z",
          execution_ms: 180,
          fingerprint: "fp3",
          aggregate_id: "agg-2",
        },
      ],
    });

    const { container } = renderSection();

    await waitFor(() => {
      expect(screen.getAllByText(/220 ms/).length).toBeGreaterThan(0);
    });
    expect(screen.getAllByText(/450 ms/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/800 ms/).length).toBeGreaterThan(0);
    // Chart drawn — exactly one bar per sample.
    const bars = container.querySelectorAll("svg rect");
    expect(bars.length).toBe(3);
  });
});
