import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nContext } from "../../i18n";
import en from "../../i18n";
import { useBuilderStore } from "../../store/builderStore";
import { pocketsApi } from "../../api/client";
import PocketTablesPanel from "./PocketTablesPanel";

// Bug-6999: DELETE pocket is require_role("admin") on the backend
// (api/pockets.py), while create/edit are require_role("modeler"). The panel
// must gate the Delete button on admin and surface a delete failure.

const POCKET = {
  id: "pocket-1",
  model_id: "model-1",
  target_id: "target-1",
  physical_table_name: "pkt_orders_slice",
  target_schema: null,
  defining_sql: "SELECT 1",
  query_fingerprint: "fp",
  predicate_set_hash: "ph",
  row_count: 10,
  storage_bytes: 1000,
  refresh_policy: "manual",
  refresh_cron: null,
  incremental_column: null,
  incremental_lookback_hours: null,
  ttl_days: 14,
  status: "fresh",
  failure_reason: null,
  last_refresh_at: null,
  last_access_at: null,
  last_match_at: null,
  hit_count: 3,
  time_saved_ms_total: 0,
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
  retired_at: null,
  predicates: [],
};

vi.mock("../../api/hooks", () => ({
  usePockets: () => ({ data: [POCKET], isLoading: false }),
}));

const deleteMock = vi.fn();
vi.mock("../../api/client", () => ({
  pocketsApi: {
    delete: (...args: unknown[]) => deleteMock(...args),
    getMetrics: vi.fn().mockResolvedValue({ total_pockets: 0, top_pockets: [] }),
  },
  dataQualityApi: {
    pocketViolationSummary: vi.fn().mockResolvedValue({}),
  },
}));

vi.mock("../Confirm", () => ({
  useConfirm: () => vi.fn().mockResolvedValue(true),
}));

vi.mock("../Refresh", () => ({
  RefreshTriggerButton: () => null,
}));

vi.mock("./PocketDrawer", () => ({ default: () => null }));
vi.mock("./PocketSuggestionsPanel", () => ({ default: () => null }));

function setRole(role: string) {
  localStorage.setItem("user_role", role);
}

function renderPanel() {
  useBuilderStore.getState().reset();
  useBuilderStore.getState().setReadOnly(false);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/projects/project-1/models/model-1"]}>
      <QueryClientProvider client={qc}>
        <I18nContext.Provider value={en}>
          <Routes>
            <Route
              path="/projects/:projectId/models/:modelId"
              element={<PocketTablesPanel />}
            />
          </Routes>
        </I18nContext.Provider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("PocketTablesPanel delete gating (Bug-6999)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    useBuilderStore.getState().reset();
    useBuilderStore.getState().setReadOnly(false);
    deleteMock.mockResolvedValue(undefined);
  });

  it("hides the Delete button for a modeler (backend requires admin)", async () => {
    setRole("modeler");
    renderPanel();
    // Edit is visible for a modeler; Delete must not be.
    await screen.findByRole("button", { name: /edit/i });
    expect(
      screen.queryByRole("button", { name: /delete/i }),
    ).not.toBeInTheDocument();
  });

  it("shows the Delete button for a tenant admin", async () => {
    setRole("tenant_admin");
    renderPanel();
    expect(
      await screen.findByRole("button", { name: /delete/i }),
    ).toBeInTheDocument();
  });

  it("surfaces an error when delete fails instead of failing silently", async () => {
    setRole("tenant_admin");
    deleteMock.mockRejectedValue({
      response: { data: { detail: "pocket in use" } },
    });
    renderPanel();
    const del = await screen.findByRole("button", { name: /delete/i });
    await userEvent.click(del);
    await waitFor(() =>
      expect(screen.getByText(/could not delete pocket table/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/pocket in use/i)).toBeInTheDocument();
  });
});
