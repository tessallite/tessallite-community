import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ConfirmProvider } from "../Confirm";
import type { NamedQuery } from "../../api/types";
import NamedSetsPanel from "./NamedSetsPanel";

const listSetsMock = vi.fn();
const listNqMock = vi.fn();
const deleteNqMock = vi.fn();
const mockIsTenantAdmin = vi.fn();
const mockNeedsSaveOrDeploy = vi.fn();

vi.mock("../../api/client", () => ({
  namedSetsApi: {
    _cachedMemberCap: 1000,
    list: (...args: unknown[]) => listSetsMock(...args),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    validate: vi.fn(),
    preview: vi.fn(),
    versions: vi.fn().mockResolvedValue([]),
    revert: vi.fn(),
    certify: vi.fn(),
    deprecate: vi.fn(),
    listUsage: vi.fn().mockResolvedValue([]),
    reportUsage: vi.fn().mockResolvedValue({}),
    refresh: vi.fn().mockResolvedValue({ id: "ns1", builder_definition: { members: [] } }),
  },
  namedQueriesApi: {
    list: (...args: unknown[]) => listNqMock(...args),
    get: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    delete: (...args: unknown[]) => deleteNqMock(...args),
    validate: vi.fn(),
    refresh: vi.fn(),
    listRuns: vi.fn().mockResolvedValue([]),
  },
  projectSettingsApi: {
    list: vi.fn().mockResolvedValue([]),
  },
  dimensionsApi: {
    list: vi.fn().mockResolvedValue([]),
  },
  preferencesApi: {
    get: vi.fn().mockResolvedValue({ favourites: { kpi: [], named_set: [] }, recently_used: { kpi: [], named_set: [] } }),
    toggleFavourite: vi.fn().mockResolvedValue({ favourited: true }),
    recordRecentlyUsed: vi.fn().mockResolvedValue({ recorded: true }),
  },
}));

vi.mock("../../auth/currentUser", () => ({
  isTenantAdmin: () => mockIsTenantAdmin(),
  canEditModelConfig: () => true,
}));

vi.mock("../../store/useModelEditorStore", () => ({
  useModelNeedsSaveOrDeploy: () => mockNeedsSaveOrDeploy(),
}));

const SAMPLE_NQ: NamedQuery = {
  id: "nq1",
  model_id: "m-1",
  name: "top_cities",
  display_name: "Top Cities",
  description: null,
  display_folder: null,
  definition_sql: "SELECT city_name FROM modely",
  output_columns: [{ name: "city_name", type: "string" }],
  shape: "projection",
  row_cap: null,
  column_cap: null,
  certification_status: "draft",
  created_by: null,
  artifact: {
    id: "a1",
    target_id: "t1",
    physical_table_name: "nq_top_cities",
    target_schema: null,
    row_count: 42,
    status: "fresh",
    failure_reason: null,
    last_refresh_at: "2026-08-01T00:00:00Z",
    retired_at: null,
  },
  refresh_policy: null,
  created_at: "2026-01-01",
  updated_at: "2026-01-01",
};

function renderPanel() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ConfirmProvider>
        <MemoryRouter initialEntries={["/p/proj-1/m/model-1"]}>
          <Routes>
            <Route path="/p/:projectId/m/:modelId" element={<NamedSetsPanel />} />
          </Routes>
        </MemoryRouter>
      </ConfirmProvider>
    </QueryClientProvider>,
  );
}

async function switchToNamedQueriesKind(user: ReturnType<typeof userEvent.setup>) {
  const kindSelector = await screen.findByTestId("kind-selector");
  await user.click(within(kindSelector).getByText("Named Queries"));
}

describe("NamedSetsPanel — Named Queries kind", () => {
  beforeEach(() => {
    listSetsMock.mockReset();
    listSetsMock.mockResolvedValue([]);
    listNqMock.mockReset();
    listNqMock.mockResolvedValue([]);
    deleteNqMock.mockReset();
    deleteNqMock.mockResolvedValue(undefined);
    mockIsTenantAdmin.mockReset();
    mockIsTenantAdmin.mockReturnValue(true);
    mockNeedsSaveOrDeploy.mockReset();
    mockNeedsSaveOrDeploy.mockReturnValue(false);
  });

  it("kind selector offers MDX Named Sets, Named Lists and Named Queries", async () => {
    renderPanel();
    const kindSelector = await screen.findByTestId("kind-selector");
    expect(within(kindSelector).getByText("MDX Named Sets")).toBeTruthy();
    expect(within(kindSelector).getByText("Tessallite Named Lists")).toBeTruthy();
    expect(within(kindSelector).getByText("Named Queries")).toBeTruthy();
  });

  it("lists Named Queries with channel, health badges and refresh metadata", async () => {
    listNqMock.mockResolvedValue([SAMPLE_NQ]);
    renderPanel();
    const user = userEvent.setup();

    await switchToNamedQueriesKind(user);

    await waitFor(() => {
      expect(screen.getByText("Top Cities")).toBeTruthy();
    });
    expect(screen.getByText("@top_cities")).toBeTruthy();
    // Channel badge mirrors the Named List path badge.
    expect(screen.getByTestId("nq-channel-nq1").textContent).toBe(
      "SQL / JDBC / REST",
    );
    // Health badge + last refreshed + row count.
    expect(screen.getByTestId("nq-health-nq1").textContent).toBe("Fresh");
    expect(screen.getByText(/Last refreshed:/)).toBeTruthy();
    expect(screen.getByText(/42 rows/)).toBeTruthy();
  });

  it("shows failed health with the server reason on the card", async () => {
    const failed: NamedQuery = {
      ...SAMPLE_NQ,
      artifact: {
        ...SAMPLE_NQ.artifact!,
        status: "failed",
        failure_reason: "ROW_CAP_EXCEEDED",
        last_refresh_at: null,
        row_count: null,
      },
    };
    listNqMock.mockResolvedValue([failed]);
    renderPanel();
    const user = userEvent.setup();

    await switchToNamedQueriesKind(user);

    await waitFor(() => {
      expect(screen.getByTestId("nq-health-nq1").textContent).toBe("Failed");
    });
    expect(screen.getByText(/ROW_CAP_EXCEEDED/)).toBeTruthy();
  });

  it("shows the pending-deploy indicator when the model needs save or deploy", async () => {
    mockNeedsSaveOrDeploy.mockReturnValue(true);
    listNqMock.mockResolvedValue([SAMPLE_NQ]);
    renderPanel();
    const user = userEvent.setup();

    await switchToNamedQueriesKind(user);

    await waitFor(() => {
      expect(screen.getByTestId("nq-pending-deploy-nq1")).toBeTruthy();
    });
  });

  it("empty Named Queries list shows the kind empty state", async () => {
    renderPanel();
    const user = userEvent.setup();

    await switchToNamedQueriesKind(user);

    await waitFor(() => {
      expect(screen.getByTestId("nq-none")).toBeTruthy();
    });
    expect(screen.getByTestId("nq-none").textContent).toBe(
      "No Named Queries defined.",
    );
  });

  it("admin can delete; a non-admin modeller never sees the delete button", async () => {
    listNqMock.mockResolvedValue([SAMPLE_NQ]);
    mockIsTenantAdmin.mockReturnValue(false);
    renderPanel();
    const user = userEvent.setup();

    await switchToNamedQueriesKind(user);

    await waitFor(() => {
      expect(screen.getByText("Top Cities")).toBeTruthy();
    });
    expect(screen.getByText("Edit")).toBeTruthy();
    expect(screen.queryByText("Delete")).toBeNull();
  });

  it("admin delete confirms and calls the API", async () => {
    listNqMock.mockResolvedValue([SAMPLE_NQ]);
    renderPanel();
    const user = userEvent.setup();

    await switchToNamedQueriesKind(user);

    await waitFor(() => {
      expect(screen.getByText("Top Cities")).toBeTruthy();
    });
    await user.click(screen.getByText("Delete"));

    // Confirm dialog
    const confirmButton = await screen.findByText("Delete this Named Query?");
    expect(confirmButton).toBeTruthy();
    const dialog = confirmButton.closest("[role=dialog]") as HTMLElement;
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(deleteNqMock).toHaveBeenCalledWith("proj-1", "model-1", "nq1");
    });
  });

  it("Add on the Named Queries kind opens the create editor", async () => {
    renderPanel();
    const user = userEvent.setup();

    await switchToNamedQueriesKind(user);
    await user.click(screen.getByText("Add Set"));

    await waitFor(() => {
      expect(screen.getByText("Add Named Query")).toBeTruthy();
    });
    expect(screen.getByTestId("nq-definition-sql")).toBeTruthy();
  });

  it("Edit opens the editor pre-filled with the saved definition", async () => {
    listNqMock.mockResolvedValue([SAMPLE_NQ]);
    renderPanel();
    const user = userEvent.setup();

    await switchToNamedQueriesKind(user);
    await waitFor(() => {
      expect(screen.getByText("Top Cities")).toBeTruthy();
    });
    await user.click(screen.getByText("Edit"));

    await waitFor(() => {
      expect(screen.getByText("Edit Named Query")).toBeTruthy();
    });
    const sql = screen.getByTestId("nq-definition-sql") as HTMLTextAreaElement;
    expect(sql.value).toBe("SELECT city_name FROM modely");
  });
});
