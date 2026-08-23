import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// Bug-7459: failed analytics requests must be shown as an explicit, retryable
// error rather than being silently collapsed into empty/zero "no activity"
// states. Mock the analytics API so individual endpoints can fail.
const summary = vi.fn();
const queryVolume = vi.fn();
const topMeasures = vi.fn();
const topAggregates = vi.fn();
const routingBreakdown = vi.fn();
const estimatedSavings = vi.fn();
const topUsers = vi.fn();

vi.mock("../../api/client", () => ({
  analyticsApi: {
    summary: (...a: unknown[]) => summary(...a),
    queryVolume: (...a: unknown[]) => queryVolume(...a),
    topMeasures: (...a: unknown[]) => topMeasures(...a),
    topAggregates: (...a: unknown[]) => topAggregates(...a),
    routingBreakdown: (...a: unknown[]) => routingBreakdown(...a),
    estimatedSavings: (...a: unknown[]) => estimatedSavings(...a),
    topUsers: (...a: unknown[]) => topUsers(...a),
  },
}));

import UsageAnalyticsTab from "./UsageAnalyticsTab";

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <UsageAnalyticsTab projectId="p1" modelId="m1" />
    </QueryClientProvider>,
  );
}

function resolveAllEmpty() {
  summary.mockResolvedValue({
    total_queries: 0,
    acceleration_rate: 0,
    aggregate_hit_rate: 0,
    top_measure: null,
    avg_response_ms: null,
  });
  queryVolume.mockResolvedValue([]);
  topMeasures.mockResolvedValue([]);
  topAggregates.mockResolvedValue([]);
  routingBreakdown.mockResolvedValue([]);
  estimatedSavings.mockResolvedValue(null);
  topUsers.mockResolvedValue([]);
}

describe("UsageAnalyticsTab error vs empty (Bug-7459)", () => {
  beforeEach(() => {
    for (const fn of [
      summary, queryVolume, topMeasures, topAggregates,
      routingBreakdown, estimatedSavings, topUsers,
    ]) fn.mockReset();
  });

  it("shows the top-level load-error banner when any query fails", async () => {
    resolveAllEmpty();
    routingBreakdown.mockRejectedValue(new Error("boom"));
    renderTab();
    expect(
      await screen.findByTestId("usage-analytics-load-error"),
    ).toBeInTheDocument();
  });

  it("does NOT show the load-error banner when all queries succeed (even if empty)", async () => {
    resolveAllEmpty();
    renderTab();
    // wait for content to settle
    await screen.findByText("Usage Analytics");
    await waitFor(() =>
      expect(routingBreakdown).toHaveBeenCalled(),
    );
    expect(
      screen.queryByTestId("usage-analytics-load-error"),
    ).not.toBeInTheDocument();
  });

  it("renders a dash (not fabricated zeros) for summary cards when the summary read fails", async () => {
    resolveAllEmpty();
    summary.mockRejectedValue(new Error("summary down"));
    renderTab();
    // The banner appears, and the summary values must be "—", never "0" / "0%".
    expect(
      await screen.findByTestId("usage-analytics-load-error"),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText("0%")).not.toBeInTheDocument();
    });
    // At least one dash is shown where a summary value would be.
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});
