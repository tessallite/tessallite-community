import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nContext } from "../i18n";
import en from "../i18n";
import { ConfirmProvider } from "../components/Confirm";
import { useModelEditorStore } from "../store/useModelEditorStore";
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
});
