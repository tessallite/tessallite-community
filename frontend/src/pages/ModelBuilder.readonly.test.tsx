import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nContext } from "../i18n";
import en from "../i18n";
import { ConfirmProvider } from "../components/Confirm";
import { useModelEditorStore } from "../store/useModelEditorStore";
import { useBuilderStore } from "../store/builderStore";
import { versionsApi } from "../api/versionsApi";
import { ModelToolbarActions } from "./ModelBuilder";

function renderToolbar(readOnly: boolean, isDeployed = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <I18nContext.Provider value={en}>
          <ConfirmProvider>
            <ModelToolbarActions
              projectId="project-1"
              projectSlug="project"
              modelId="model-1"
              isDeployed={isDeployed}
              lastDeployedAt={null}
              readOnly={readOnly}
            />
          </ConfirmProvider>
        </I18nContext.Provider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("ModelBuilder read-only toolbar controls (Bug-5966)", () => {
  beforeEach(() => {
    useModelEditorStore.getState().clearModel();
    useModelEditorStore.getState().setModel({
      modelId: "model-1",
      lastSavedVersion: 2,
      deployedVersion: null,
      lastDeployedAt: null,
    });
    useModelEditorStore.getState().markDirty();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("disables save and deploy in read-only mode", () => {
    renderToolbar(true, false);

    expect(screen.getByTestId("btn-save")).toBeDisabled();
    expect(screen.getByTestId("btn-deploy")).toBeDisabled();
  });

  it("disables undeploy in read-only mode", () => {
    useModelEditorStore.getState().markClean({
      lastSavedVersion: 2,
      deployedVersion: 2,
    });

    renderToolbar(true, true);

    expect(screen.getByTestId("btn-undeploy")).toBeDisabled();
  });

  it("renders the structured refusal without marking the editor deployed", async () => {
    vi.spyOn(versionsApi, "deploy").mockRejectedValue({
      response: {
        data: {
          detail: {
            code: "JOIN_POPULATION_BLOCKED",
            message: "deployment refused",
            threshold: 0.15,
            joins: [
              {
                join_id: "join-1",
                join_label: "Fact.customer_id ↔ Customer.id",
                population_participation: "undeclared",
                status: "BLOCKED",
                row_effect_ratio: 0.2,
                reason: "measured row effect exceeds threshold",
              },
              {
                join_id: "join-2",
                join_label: "Fact.region_id ↔ Region.id",
                population_participation: "enrichment_only",
                status: "BLOCKED",
                row_effect_ratio: 0.18,
                reason: "filtering enrichment effect exceeds threshold",
              },
            ],
          },
        },
      },
    });
    useModelEditorStore.getState().markClean({
      lastSavedVersion: 2,
      deployedVersion: null,
    });

    renderToolbar(false, false);
    await userEvent.click(screen.getByTestId("btn-deploy"));

    await waitFor(() =>
      expect(screen.getByTestId("join-population-blocked-notice")).toBeInTheDocument(),
    );
    expect(screen.getByText("Fact.customer_id ↔ Customer.id")).toBeInTheDocument();
    expect(screen.getByText("Fact.region_id ↔ Region.id")).toBeInTheDocument();
    expect(useModelEditorStore.getState().deployedVersion).toBeNull();
    expect(useBuilderStore.getState().activePanel).not.toBe("joins");

    await userEvent.click(screen.getByRole("button", { name: "Open Joins" }));
    expect(useBuilderStore.getState().activePanel).toBe("joins");
  });
});
