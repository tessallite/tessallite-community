import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

const useImpactCatalogueMock = vi.fn();
const useImpactQueryMock = vi.fn();

vi.mock("../../api/hooks", () => ({
  useImpactCatalogue: (...args: unknown[]) => useImpactCatalogueMock(...args),
  useImpactQuery: (...args: unknown[]) => useImpactQueryMock(...args),
}));

import ImpactAnalysisPanel from "./ImpactAnalysisPanel";

function renderPanel() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/p/proj-1/m/model-1"]}>
        <Routes>
          <Route path="/p/:projectId/m/:modelId" element={<ImpactAnalysisPanel />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ImpactAnalysisPanel", () => {
  beforeEach(() => {
    useImpactCatalogueMock.mockReset();
    useImpactQueryMock.mockReset();
  });

  it("renders title and description", () => {
    useImpactCatalogueMock.mockReturnValue({
      data: { items: [], total: 0, next_cursor: null },
      isLoading: false,
    });
    useImpactQueryMock.mockReturnValue({ data: null, isLoading: false });

    renderPanel();
    expect(screen.getByText("Impact Analysis")).toBeTruthy();
  });

  it("shows select-object prompt when no object selected", () => {
    useImpactCatalogueMock.mockReturnValue({
      data: { items: [], total: 0, next_cursor: null },
      isLoading: false,
    });
    useImpactQueryMock.mockReturnValue({ data: null, isLoading: false });

    renderPanel();
    expect(
      screen.getByText("Select a model object above to see its dependents."),
    ).toBeTruthy();
  });

  it("renders impact summary when data present", async () => {
    useImpactCatalogueMock.mockReturnValue({
      data: {
        items: [
          {
            object_type: "measure",
            object_id: "m1",
            model_id: "model-1",
            name: "Gross Sales",
            display_name: "Gross Sales",
            container_ids: {},
            route: null,
          },
        ],
        total: 1,
        next_cursor: null,
      },
      isLoading: false,
    });
    useImpactQueryMock.mockReturnValue({
      data: {
        analysis_id: "sha256:abc",
        authority: "live_draft",
        project_id: "proj-1",
        model_id: "model-1",
        dependency_revision: 1,
        operation: "inspect",
        target: {
          object_type: "measure",
          object_id: "m1",
          model_id: "model-1",
          name: "Gross Sales",
          display_name: "Gross Sales",
          route: null,
        },
        guard: {
          decision: "blocked",
          blocking_impact_ids: ["kpi:k1"],
          acknowledgement_required: false,
        },
        summary: {
          total: 2,
          hard_break: 1,
          soft_degrade: 1,
          cascade_deleted: 0,
          direct: 2,
          max_depth: 1,
          by_object_type: { kpi: 1, persona: 1 },
          truncated: false,
          unresolved: 0,
        },
        impacts: [
          {
            impact_id: "kpi:k1",
            object: {
              object_type: "kpi",
              object_id: "k1",
              model_id: "model-1",
              name: "Margin",
              display_name: "Margin",
              route: null,
            },
            severity: "hard_break",
            effect: "breaks_reference",
            delete_policy: "restrict",
            direct: true,
            min_depth: 1,
            reason_key: "impactAnalysis.reason.targetBindingBroken",
            reason_params: {},
            paths: [],
            scc_id: null,
          },
          {
            impact_id: "persona:p1",
            object: {
              object_type: "persona",
              object_id: "p1",
              model_id: "model-1",
              name: "Analyst",
              display_name: "Analyst",
              route: null,
            },
            severity: "soft_degrade",
            effect: "changes_semantics",
            delete_policy: "detach",
            direct: true,
            min_depth: 1,
            reason_key: "",
            reason_params: {},
            paths: [],
            scc_id: null,
          },
        ],
        cycles: [],
        diagnostics: [],
      },
      isLoading: false,
    });

    renderPanel();
    // Summary chips should be visible
    await waitFor(() => {
      expect(screen.getByText(/Hard breaks: 1/)).toBeTruthy();
      expect(screen.getByText(/Soft changes: 1/)).toBeTruthy();
      expect(screen.getByText(/Total: 2/)).toBeTruthy();
    });
  });

  it("shows blocked guard alert", async () => {
    useImpactCatalogueMock.mockReturnValue({
      data: { items: [], total: 0, next_cursor: null },
      isLoading: false,
    });
    useImpactQueryMock.mockReturnValue({
      data: {
        analysis_id: "sha256:abc",
        authority: "live_draft",
        project_id: "proj-1",
        model_id: "model-1",
        dependency_revision: 1,
        operation: "delete",
        target: {
          object_type: "column",
          object_id: "c1",
          model_id: "model-1",
          name: "gross",
          display_name: "gross",
          route: null,
        },
        guard: {
          decision: "blocked",
          blocking_impact_ids: ["measure:m1"],
          acknowledgement_required: false,
        },
        summary: {
          total: 1,
          hard_break: 1,
          soft_degrade: 0,
          cascade_deleted: 0,
          direct: 1,
          max_depth: 1,
          by_object_type: { measure: 1 },
          truncated: false,
          unresolved: 0,
        },
        impacts: [
          {
            impact_id: "measure:m1",
            object: {
              object_type: "measure",
              object_id: "m1",
              model_id: "model-1",
              name: "Gross Sales",
              display_name: "Gross Sales",
              route: null,
            },
            severity: "hard_break",
            effect: "breaks_reference",
            delete_policy: "restrict",
            direct: true,
            min_depth: 1,
            reason_key: "",
            reason_params: {},
            paths: [],
            scc_id: null,
          },
        ],
        cycles: [],
        diagnostics: [],
      },
      isLoading: false,
    });

    renderPanel();
    await waitFor(() => {
      expect(
        screen.getByText(/This action is blocked/),
      ).toBeTruthy();
    });
  });

  it("shows no-dependents message when allowed and zero total", () => {
    useImpactCatalogueMock.mockReturnValue({
      data: { items: [], total: 0, next_cursor: null },
      isLoading: false,
    });
    useImpactQueryMock.mockReturnValue({
      data: {
        analysis_id: "sha256:abc",
        authority: "live_draft",
        project_id: "proj-1",
        model_id: "model-1",
        dependency_revision: 1,
        operation: "inspect",
        target: {
          object_type: "measure",
          object_id: "m1",
          model_id: "model-1",
          name: "Revenue",
          display_name: "Revenue",
          route: null,
        },
        guard: {
          decision: "allowed",
          blocking_impact_ids: [],
          acknowledgement_required: false,
        },
        summary: {
          total: 0,
          hard_break: 0,
          soft_degrade: 0,
          cascade_deleted: 0,
          direct: 0,
          max_depth: 0,
          by_object_type: {},
          truncated: false,
          unresolved: 0,
        },
        impacts: [],
        cycles: [],
        diagnostics: [],
      },
      isLoading: false,
    });

    renderPanel();
    expect(
      screen.getByText("No dependents found. This object can be safely deleted."),
    ).toBeTruthy();
  });
});
