import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const healthMock = vi.fn();

vi.mock("../../api/client", () => ({
  joinPopulationHealthApi: {
    get: (...args: unknown[]) => healthMock(...args),
  },
}));

import { JoinPopulationHealthSection } from "./ModelHealthPanel";

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <JoinPopulationHealthSection projectId="project-1" modelId="model-1" />
    </QueryClientProvider>,
  );
}

describe("JoinPopulationHealthSection", () => {
  beforeEach(() => healthMock.mockReset());

  it("renders the model rollup, enforcement help, and join evidence", async () => {
    healthMock.mockResolvedValue({
      model_id: "model-1",
      status: "BLOCKED",
      evaluated: true,
      join_count: 1,
      evaluated_count: 1,
      warning_count: 0,
      blocked_count: 1,
      warn_only: false,
      items: [{
        join_id: "join-1",
        left_table_name: "orders",
        right_table_name: "customers",
        left_column_name: "customer_id",
        right_column_name: "id",
        join_type: "left",
        population_participation: "undeclared",
        checked_population_participation: "undeclared",
        declaration_changed_since_check: false,
        inputs_changed_since_check: false,
        classification: "filtering",
        status: "BLOCKED",
        measured: true,
        row_loss_ratio: 0.2,
        row_mult_ratio: 0,
        row_effect_ratio: 0.2,
        reason: "Join filters 20% of the model population.",
        checked_at: "2026-08-22T10:00:00Z",
        stale: false,
      }],
    });

    renderSection();

    await waitFor(() => {
      expect(screen.getAllByText("Blocked")).toHaveLength(2);
    });
    expect(screen.getByText("Measured blockers prevent deployment until you declare the join's population role accurately or fix the join/source data. Unmeasured checks never block.")).toBeInTheDocument();
    expect(screen.getByText("orders → customers")).toBeInTheDocument();
    expect(screen.getByText("Join filters 20% of the model population.")).toBeInTheDocument();
    expect(screen.getByText("Left outer")).toBeInTheDocument();
    expect(screen.getByText("Not decided yet")).toBeInTheDocument();
    expect(screen.getByText("Filtering")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Classification" })).toBeInTheDocument();
    expect(screen.queryByText("undeclared")).not.toBeInTheDocument();
    expect(screen.getByText("1 of 1 joins evaluated")).toBeInTheDocument();
    expect(healthMock).toHaveBeenCalledWith("project-1", "model-1");
  });

  it("renders stale and unavailable classification states without raw enums", async () => {
    healthMock.mockResolvedValue({
      model_id: "model-1",
      status: "WARNING",
      evaluated: false,
      join_count: 1,
      evaluated_count: 0,
      warning_count: 0,
      blocked_count: 0,
      warn_only: false,
      items: [{
        join_id: "join-2",
        left_table_name: "orders",
        right_table_name: "customers",
        left_column_name: "customer_id",
        right_column_name: "id",
        join_type: "inner",
        population_participation: "preserve_base_rows",
        checked_population_participation: null,
        declaration_changed_since_check: false,
        inputs_changed_since_check: false,
        classification: null,
        status: null,
        measured: false,
        row_loss_ratio: null,
        row_mult_ratio: null,
        row_effect_ratio: null,
        reason: null,
        checked_at: null,
        stale: true,
      }],
    });

    renderSection();
    await waitFor(() => expect(screen.getByText("Stale — redeploy to recheck")).toBeInTheDocument());
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText("Inner")).toBeInTheDocument();
    expect(screen.queryByText("preserve_base_rows")).not.toBeInTheDocument();
  });

  it("exposes loading failures and empty evidence clearly", async () => {
    healthMock.mockRejectedValueOnce(new Error("network"));
    renderSection();
    await waitFor(() => expect(screen.getByText("Could not load join-population health. Try again shortly.")).toBeInTheDocument());

    healthMock.mockResolvedValueOnce({
      model_id: "model-1", status: "OK", evaluated: true, join_count: 0,
      evaluated_count: 0, warning_count: 0, blocked_count: 0, warn_only: false,
      items: [],
    });
    renderSection();
    await waitFor(() => expect(screen.getByText("No declared joins have population evidence yet.")).toBeInTheDocument());
  });
});
