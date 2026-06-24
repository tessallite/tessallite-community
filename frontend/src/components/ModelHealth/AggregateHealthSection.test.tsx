import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const listMock = vi.fn();

vi.mock("../../api/client", () => ({
  aggregatesApi: { list: (...a: unknown[]) => listMock(...a) },
}));

import { AggregateHealthSection } from "./ModelHealthPanel";

function agg(
  id: string,
  status: string,
  health?: "healthy" | "unhealthy",
  is_stale = false,
) {
  return {
    id,
    physical_table_name: `agg_${id}`,
    grain: [],
    grain_physical_cols: null,
    invalid_reason: null,
    measure_names: [],
    status,
    health,
    include_quantiles: false,
    estimated_hit_rate: null,
    creation_reason: "demand",
    rationale: null,
    is_stale,
    created_at: "2026-06-15T00:00:00Z",
    retired_at: null,
    persona_id: null,
  };
}

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AggregateHealthSection projectId="p1" modelId="m1" />
    </QueryClientProvider>,
  );
}

describe("AggregateHealthSection", () => {
  beforeEach(() => listMock.mockReset());

  // Each SummaryChip renders its label and value as sibling Typography nodes
  // inside one Box, so scope the value lookup to the label's parent — the
  // pending/invalid/retired counts are all "1" and would otherwise be ambiguous.
  function chipValue(label: string): string {
    const parent = screen.getByText(label).parentElement as HTMLElement;
    // The value is the other text node in the box (the non-label one).
    const texts = within(parent)
      .getAllByText(/^\d+$/)
      .map((n) => n.textContent ?? "");
    return texts[0] ?? "";
  }

  it("computes healthy/pending/invalid/retired/unhealthy counts, not just labels", async () => {
    listMock.mockResolvedValue([
      agg("a", "active", "healthy"),
      agg("b", "disabled", "healthy"),
      agg("c", "pending", "unhealthy"),
      agg("d", "invalid", "unhealthy"),
      agg("e", "retired", "unhealthy"),
    ]);

    renderSection();

    // Pending-rebuild explanatory note appears because a pending agg exists.
    await waitFor(() =>
      expect(screen.getByText(/awaiting rebuild/i)).toBeInTheDocument(),
    );
    // Healthy = active + disabled = 2; unhealthy total = pending+invalid+retired = 3.
    expect(chipValue("Healthy")).toBe("2");
    expect(chipValue("Pending rebuild")).toBe("1");
    expect(chipValue("Invalid")).toBe("1");
    expect(chipValue("Retired")).toBe("1");
    expect(chipValue("Unhealthy total")).toBe("3");
  });

  it("derives health from status when the backend health field is absent", async () => {
    listMock.mockResolvedValue([
      agg("a", "active"),
      agg("b", "disabled"),
      agg("c", "pending"),
    ]);
    renderSection();
    await waitFor(() => expect(screen.getByText("Healthy")).toBeInTheDocument());
    expect(chipValue("Healthy")).toBe("2");
    expect(chipValue("Unhealthy total")).toBe("1");
  });

  it("counts an active-but-stale aggregate as Outdated, not Healthy", async () => {
    listMock.mockResolvedValue([
      agg("a", "active", "healthy"),
      agg("b", "active", "unhealthy", true), // stale -> outdated, not serving
      agg("c", "pending", "unhealthy"),
    ]);
    renderSection();
    await waitFor(() => expect(screen.getByText("Outdated")).toBeInTheDocument());
    expect(chipValue("Healthy")).toBe("1");
    expect(chipValue("Outdated")).toBe("1");
    expect(chipValue("Pending rebuild")).toBe("1");
    // Unhealthy total = stale + pending = 2 (not just the pending status count).
    expect(chipValue("Unhealthy total")).toBe("2");
  });

  it("shows the empty message when there are no aggregates", async () => {
    listMock.mockResolvedValue([]);
    renderSection();
    await waitFor(() =>
      expect(screen.getByText(/no aggregates yet/i)).toBeInTheDocument(),
    );
  });
});
