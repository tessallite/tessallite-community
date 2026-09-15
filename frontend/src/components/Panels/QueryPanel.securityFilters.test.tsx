import { beforeEach, describe, expect, it, vi } from "vitest";
import { StrictMode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { I18nContext, getMessages } from "../../i18n";
import { useBuilderStore } from "../../store/builderStore";

// Bug-7389: the execute envelope's `security_rules_applied` was the only place
// a security filter having fired at all was visible, and nothing rendered it
// next to the trace SQL — an analyst had no way to tell "the router applied a
// row-security filter" from "no filter was in play" for a given execution.

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

describe("Bug-7389 Query Trace security-filter indicator", () => {
  beforeEach(() => {
    executeMock.mockReset();
    useBuilderStore.getState().reset();
    useBuilderStore.getState().setPendingSql("SELECT 42");
  });

  it("shows a security-filters-applied caption when the envelope carries rule ids", async () => {
    executeMock.mockResolvedValue({
      columns: [],
      rows: [],
      row_count: 0,
      route: "source",
      execution_time_ms: 1,
      trace: { steps: [{ stage: "rewriter", data: { rewritten_sql: "SELECT 1" } }] },
      security_rules_applied: ["rule-abc"],
    });

    renderPanel();

    await waitFor(() => expect(executeMock).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Row security filters applied: 1")).toBeTruthy();
  });

  it("shows the deny-all message when the sentinel is present", async () => {
    executeMock.mockResolvedValue({
      columns: [],
      rows: [],
      row_count: 0,
      route: "source",
      execution_time_ms: 1,
      trace: { steps: [{ stage: "rewriter", data: { rewritten_sql: "SELECT 1 WHERE 0=1" } }] },
      security_rules_applied: ["__deny_all__"],
    });

    renderPanel();

    await waitFor(() => expect(executeMock).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Row security denied all rows for this query")).toBeTruthy();
  });

  it("shows nothing when no security rules fired", async () => {
    executeMock.mockResolvedValue({
      columns: [],
      rows: [],
      row_count: 0,
      route: "source",
      execution_time_ms: 1,
      trace: { steps: [{ stage: "rewriter", data: { rewritten_sql: "SELECT 1" } }] },
      security_rules_applied: [],
    });

    renderPanel();

    await waitFor(() => expect(executeMock).toHaveBeenCalledTimes(1));
    await screen.findByTestId("sql-editor");
    expect(screen.queryByText(/Row security filters applied/)).toBeNull();
    expect(screen.queryByText(/Row security denied all rows/)).toBeNull();
  });
});
