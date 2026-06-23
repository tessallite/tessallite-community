import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { AggregateDefinition } from "../../api/types";
import AggregateCard from "./AggregateCard";

const BASE_AGG: AggregateDefinition = {
  id: "agg-1",
  physical_table_name: "agg_sales_region",
  grain: ["region"],
  grain_physical_cols: null,
  invalid_reason: null,
  measure_names: ["revenue"],
  status: "active",
  include_quantiles: false,
  include_stats: false,
  estimated_hit_rate: null,
  creation_reason: "manual",
  rationale: null,
  is_stale: false,
  created_at: "2026-06-13T00:00:00Z",
  retired_at: null,
  persona_id: null,
};

function renderCard(agg: AggregateDefinition) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <AggregateCard
        agg={agg}
        projectId="project-1"
        modelId="model-1"
        onEdit={() => undefined}
        onDelete={() => undefined}
      />
    </QueryClientProvider>,
  );
}

describe("AggregateCard predictive validation badge", () => {
  it("shows a validated badge for predictive aggregates validated by feedback", () => {
    renderCard({
      ...BASE_AGG,
      creation_reason: "predictive",
      predictive_validated_at: "2026-06-13T10:00:00Z",
    });

    expect(screen.getByTestId("agg-predictive-validated-agg-1")).toBeInTheDocument();
    expect(screen.getByText("Validated")).toBeInTheDocument();
  });

  it("does not show the badge for manual aggregates", () => {
    renderCard({
      ...BASE_AGG,
      predictive_validated_at: "2026-06-13T10:00:00Z",
    });

    expect(screen.queryByTestId("agg-predictive-validated-agg-1")).not.toBeInTheDocument();
  });
});

describe("AggregateCard health chip (Task 4)", () => {
  it("marks active and disabled rows healthy with a status chip", () => {
    const { unmount } = renderCard({ ...BASE_AGG, status: "active", health: "healthy" });
    const activeChip = screen.getByTestId("agg-health-agg-1");
    expect(activeChip).toHaveAttribute("data-health", "healthy");
    expect(activeChip).toHaveTextContent("Active");
    unmount();

    renderCard({ ...BASE_AGG, status: "disabled", health: "healthy" });
    const disabledChip = screen.getByTestId("agg-health-agg-1");
    expect(disabledChip).toHaveAttribute("data-health", "healthy");
    expect(disabledChip).toHaveTextContent("Disabled");
  });

  it.each([
    ["pending", "Pending first build"],
    ["invalid", "Needs attention"],
    ["retired", "Retired"],
  ] as const)("marks %s rows unhealthy and visible with the right label", (status, label) => {
    renderCard({
      ...BASE_AGG,
      status,
      health: "unhealthy",
      invalid_reason: status === "invalid" ? "grain dimension no longer exists" : null,
      retired_at: status === "retired" ? "2026-06-14T00:00:00Z" : null,
    });
    const chip = screen.getByTestId("agg-health-agg-1");
    expect(chip).toHaveAttribute("data-health", "unhealthy");
    expect(chip).toHaveTextContent(label);
  });

  it("falls back to status when the backend health field is absent", () => {
    renderCard({ ...BASE_AGG, status: "pending", health: undefined });
    expect(screen.getByTestId("agg-health-agg-1")).toHaveAttribute("data-health", "unhealthy");
  });

  it("marks an active-but-stale row unhealthy and labels it Outdated", () => {
    // Backend reports health=unhealthy for an active stale aggregate (not
    // routable until rebuilt); the card shows the Outdated label.
    renderCard({ ...BASE_AGG, status: "active", is_stale: true, health: "unhealthy" });
    const chip = screen.getByTestId("agg-health-agg-1");
    expect(chip).toHaveAttribute("data-health", "unhealthy");
    expect(chip).toHaveTextContent("Outdated");
  });

  it("derives unhealthy from is_stale when the backend health field is absent", () => {
    renderCard({ ...BASE_AGG, status: "active", is_stale: true, health: undefined });
    expect(screen.getByTestId("agg-health-agg-1")).toHaveAttribute("data-health", "unhealthy");
  });
});
