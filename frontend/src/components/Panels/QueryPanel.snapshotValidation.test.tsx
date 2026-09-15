import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { I18nContext, getMessages } from "../../i18n";
import { useBuilderStore } from "../../store/builderStore";

const validateMock = vi.fn();

vi.mock("../../api/client", () => ({
  queryRouterApiClient: {
    namedObjects: vi.fn().mockResolvedValue({
      parameters: [],
      named_sets: [],
      named_queries: [],
    }),
    validate: (...args: unknown[]) => validateMock(...args),
    explain: vi.fn(),
    execute: vi.fn(),
  },
  savedQueriesApi: { create: vi.fn() },
}));

vi.mock("../../api/hooks", () => ({
  useModel: () => ({ data: { slug: "model-1" } }),
  useMeasures: () => ({ data: [] }),
}));

vi.mock("../Persona/PersonaPicker", () => ({ default: () => null }));
vi.mock("../Builder/PipelineDiagram", () => ({ default: () => null }));
vi.mock("../Builder/UnsavedDeployWarning", () => ({ default: () => null }));
vi.mock("../CalendarBindingHint", () => ({ default: () => null }));
vi.mock("../Sql/SqlQueryEditor", () => ({
  default: ({ onValidate }: { onValidate: () => void }) => (
    <button type="button" onClick={onValidate}>Validate</button>
  ),
  formatSql: (sql: string) => sql,
}));

import QueryPanel from "./QueryPanel";

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={["/projects/project-1/models/model-1"]}>
      <QueryClientProvider client={client}>
        <I18nContext.Provider value={getMessages("en")}>
          <Routes>
            <Route
              path="/projects/:projectId/models/:modelId"
              element={<QueryPanel />}
            />
          </Routes>
        </I18nContext.Provider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("QueryPanel deployed-snapshot validation response (Bug-8530)", () => {
  beforeEach(() => {
    validateMock.mockReset();
    useBuilderStore.getState().reset();
  });

  it("shows the redeployment instruction and retains backend error detail", async () => {
    validateMock.mockResolvedValue({
      ok: false,
      error_type: "deployed_snapshot_unavailable",
      errors: ["The deployed snapshot could not be read."],
      warnings: [],
      requested_measures: [],
      requested_dimensions: [],
    });
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByRole("button", { name: "Validate" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(
      "The deployed model snapshot is unavailable. Redeploy the model, then validate again.",
    );
    expect(alert.textContent).toContain("The deployed snapshot could not be read.");
    expect(validateMock).toHaveBeenCalledTimes(1);
  });

  it("does not show the redeployment instruction for ordinary validation errors", async () => {
    validateMock.mockResolvedValue({
      ok: false,
      errors: ["Unknown column: order_total"],
      warnings: [],
      requested_measures: [],
      requested_dimensions: [],
    });
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByRole("button", { name: "Validate" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Unknown column: order_total");
    expect(alert.textContent).not.toContain(
      "The deployed model snapshot is unavailable. Redeploy the model, then validate again.",
    );
  });

  it("keeps the disabled Save explanation reachable from the keyboard (Bug-9603)", async () => {
    const user = userEvent.setup();
    renderPanel();

    const validate = screen.getByRole("button", { name: "Validate" });
    const saveButton = screen.getByRole("button", { name: "Save" });
    const saveWrapper = saveButton.parentElement;
    expect(saveButton).toBeDisabled();
    expect(saveWrapper).not.toBeNull();
    expect(saveWrapper).toHaveAttribute("tabindex", "0");

    validate.focus();
    await user.tab();

    expect(saveWrapper).toHaveFocus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Save query");
  });
});
