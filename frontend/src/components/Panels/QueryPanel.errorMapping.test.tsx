import { beforeEach, describe, expect, it, vi } from "vitest";
import { StrictMode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { I18nContext, getMessages } from "../../i18n";
import { useBuilderStore } from "../../store/builderStore";

// Bug-8993: QueryPanel's error extraction was consolidated onto the canonical
// extractApiError, but this panel's distinctive error_type -> translated
// lead-line mapping had to be PRESERVED, not lost in the consolidation.

const executeMock = vi.fn();

vi.mock("../../api/client", () => ({
  queryRouterApiClient: {
    namedObjects: vi.fn().mockResolvedValue({
      parameters: [],
      named_sets: [],
      named_queries: [],
    }),
    validate: vi.fn(),
    explain: vi.fn(),
    execute: (...args: unknown[]) => executeMock(...args),
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
  default: ({ value }: { value: string }) => <div data-testid="sql-editor">{value}</div>,
  formatSql: (sql: string) => sql,
}));

import QueryPanel from "./QueryPanel";

const en = getMessages("en");

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <StrictMode>
      <MemoryRouter initialEntries={["/projects/project-1/models/model-1"]}>
        <QueryClientProvider client={client}>
          <I18nContext.Provider value={en}>
            <Routes>
              <Route path="/projects/:projectId/models/:modelId" element={<QueryPanel />} />
            </Routes>
          </I18nContext.Provider>
        </QueryClientProvider>
      </MemoryRouter>
    </StrictMode>,
  );
}

describe("QueryPanel error_type mapping survives consolidation onto extractApiError (Bug-8993)", () => {
  beforeEach(() => {
    executeMock.mockReset();
    useBuilderStore.getState().reset();
    useBuilderStore.getState().setPendingSql("SELECT 42");
  });

  it("maps a typed error_type to its translated lead line, with the raw message appended", async () => {
    executeMock.mockRejectedValue({
      response: {
        data: {
          detail: {
            error_type: "no_aggregate_match",
            message: "no aggregate covers grain [region, month]",
          },
        },
      },
    });

    renderPanel();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("No matching aggregate was found for this query.");
    expect(alert.textContent).toContain("no aggregate covers grain [region, month]");
  });

  it("falls through to the canonical extractor for an untyped 422 array (FastAPI validation errors)", async () => {
    executeMock.mockRejectedValue({
      response: {
        data: {
          detail: [{ loc: ["body", "raw_query"], msg: "field required" }],
        },
      },
    });

    renderPanel();

    // Bug-8993 improvement: the canonical extractor joins array-of-{msg}
    // validation errors into a clean string; the old local implementation
    // JSON.stringify'd the raw array instead.
    expect(await screen.findByText("field required")).toBeTruthy();
    await waitFor(() => expect(executeMock).toHaveBeenCalledTimes(1));
  });

  it("falls back to the generic message for a plain transport failure", async () => {
    executeMock.mockRejectedValue({ message: "connect ECONNREFUSED" });

    renderPanel();

    expect(await screen.findByText("connect ECONNREFUSED")).toBeTruthy();
  });
});
