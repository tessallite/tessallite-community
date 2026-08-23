import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const metricsMock = vi.fn();

vi.mock("../../api/client", () => ({
  modelsApi: {
    metrics: (...args: unknown[]) => metricsMock(...args),
  },
}));

import { QueryRoutingMetricsSection } from "./ModelHealthPanel";

function renderSection() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <QueryRoutingMetricsSection projectId="project-1" modelId="model-1" />
    </QueryClientProvider>,
  );
}

const fullMetrics = {
  model_id: "model-1",
  window_hours: 24,
  total_queries: 100,
  aggregate_hits: 60,
  pocket_hits: 20,
  source_hits: 20,
  hit_rate: 0.8,
  bytes_avoided: 5_368_709_120, // 5 GB
  hourly_volume: [
    { hour: "2026-06-13T08:00:00Z", total: 40, aggregate_hits: 30, pocket_hits: 5, source_hits: 5 },
    { hour: "2026-06-13T09:00:00Z", total: 60, aggregate_hits: 30, pocket_hits: 15, source_hits: 15 },
  ],
  refresh_health: [],
  miss_summary: [],
  pocket_hit_rate: 0.2,
  pocket_time_saved_ms: 90_000, // 1.5 min
  pocket_storage_bytes: 1_048_576, // 1 MB
  pocket_evictions_24h: 3,
  top_pockets: [],
};

describe("QueryRoutingMetricsSection", () => {
  beforeEach(() => {
    metricsMock.mockReset();
  });

  it("renders the combined acceleration rate and routing split from the metrics endpoint", async () => {
    metricsMock.mockResolvedValue(fullMetrics);
    renderSection();

    await waitFor(() => {
      expect(screen.getByText("80.0%")).toBeInTheDocument();
    });
    // bytes avoided rendered human-readable
    expect(screen.getByText(/5\.0 GB/)).toBeInTheDocument();
    // pocket savings block renders when pocket hits > 0
    expect(screen.getByText(/1\.5 min/)).toBeInTheDocument();
    expect(screen.getByText(/1\.0 MB/)).toBeInTheDocument();
  });

  it("requests metrics for the default 24h window and re-requests on window change", async () => {
    metricsMock.mockResolvedValue(fullMetrics);
    renderSection();

    await waitFor(() => {
      expect(metricsMock).toHaveBeenCalledWith("project-1", "model-1", 24);
    });
  });

  it("shows a no-traffic message when the window has zero queries", async () => {
    metricsMock.mockResolvedValue({
      ...fullMetrics,
      total_queries: 0,
      aggregate_hits: 0,
      pocket_hits: 0,
      source_hits: 0,
      hit_rate: 0,
      hourly_volume: [],
    });
    renderSection();

    await waitFor(() => {
      expect(screen.getByText(/No queries recorded in this window/i)).toBeInTheDocument();
    });
  });

  it("surfaces a load-failed alert on error", async () => {
    metricsMock.mockRejectedValue(new Error("boom"));
    renderSection();

    await waitFor(() => {
      expect(screen.getByText(/Could not load routing metrics/i)).toBeInTheDocument();
    });
  });

  it("hides the pocket savings block when there are no pocket hits", async () => {
    metricsMock.mockResolvedValue({ ...fullMetrics, pocket_hits: 0 });
    renderSection();

    await waitFor(() => {
      expect(screen.getByText("80.0%")).toBeInTheDocument();
    });
    expect(screen.queryByText(/Pocket savings/i)).not.toBeInTheDocument();
  });

  it("Bug-8180: renders eligible_hit_rate distinctly from the raw hit_rate, plus the unacceleratable_queries count", async () => {
    // Known values: 100 total, 60 aggregate + 20 pocket hits (raw hit_rate =
    // 80/100 = 80.0%, unchanged). 15 of those queries are route_type="raw"
    // (unacceleratable — no aggregate/pocket could ever serve them), so
    // eligible_queries = 100 - 15 = 85 and eligible_hit_rate = 80/85 = 94.1%.
    // Before the fix, the panel only ever rendered hit_rate; this proves the
    // eligibility-scoped figure is ALSO on screen, distinctly, per Bug-8180.
    metricsMock.mockResolvedValue({
      ...fullMetrics,
      total_queries: 100,
      aggregate_hits: 60,
      pocket_hits: 20,
      source_hits: 20,
      hit_rate: 0.8,
      unacceleratable: 0.15,
      unacceleratable_queries: 15,
      eligible_queries: 85,
      eligible_hit_rate: 0.9411764705882353,
    });
    renderSection();

    await waitFor(() => {
      expect(screen.getByText("80.0%")).toBeInTheDocument(); // raw hit_rate, unchanged
    });
    expect(screen.getByTestId("metrics-eligible-hit-rate")).toHaveTextContent("94.1%");
    // unacceleratable_queries rendered as its own chip, distinct from every
    // other figure on screen (100 / 60 / 20 / 20 / 5.0 GB).
    expect(screen.getByText("15")).toBeInTheDocument();
  });
});
