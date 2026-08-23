import { beforeEach, describe, expect, it, vi } from "vitest";
import { StrictMode } from "react";
import { render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { I18nContext, getMessages } from "../../i18n";
import { useBuilderStore } from "../../store/builderStore";

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

describe("Bug-9396 saved-query Play", () => {
  beforeEach(() => {
    executeMock.mockReset();
    executeMock.mockResolvedValue({
      columns: [],
      rows: [],
      row_count: 0,
      route: "source",
      execution_time_ms: 1,
    });
    useBuilderStore.getState().reset();
    useBuilderStore.getState().setPendingSql("SELECT 42");
  });

  it("executes the saved SQL when Query Panel consumes the Play request", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <StrictMode>
        <MemoryRouter initialEntries={["/projects/project-1/models/model-1"]}>
          <QueryClientProvider client={client}>
            <I18nContext.Provider value={en}>
              <Routes>
                <Route
                  path="/projects/:projectId/models/:modelId"
                  element={<QueryPanel />}
                />
              </Routes>
            </I18nContext.Provider>
          </QueryClientProvider>
        </MemoryRouter>
      </StrictMode>,
    );

    await waitFor(() => {
      expect(executeMock).toHaveBeenCalledWith(
        expect.objectContaining({
          model_id: "model-1",
          raw_query: "SELECT 42",
          dialect: "postgresql",
        }),
        null,
      );
      expect(executeMock).toHaveBeenCalledTimes(1);
    });
  });
});
