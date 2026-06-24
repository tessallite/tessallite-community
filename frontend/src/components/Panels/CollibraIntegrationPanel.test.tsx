import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CollibraIntegrationPanel } from "./CollibraIntegrationPanel";

vi.mock("../../api/client", () => ({
  collibraApi: {
    listConfigs: vi.fn(),
    listRuns: vi.fn(),
    createConfig: vi.fn(),
    updateConfig: vi.fn(),
    deleteConfig: vi.fn(),
    validate: vi.fn(),
    exportPreview: vi.fn(),
    sync: vi.fn(),
  },
}));

import { collibraApi } from "../../api/client";

const mockListConfigs = collibraApi.listConfigs as ReturnType<typeof vi.fn>;
const mockListRuns = collibraApi.listRuns as ReturnType<typeof vi.fn>;
const mockValidate = collibraApi.validate as ReturnType<typeof vi.fn>;
const mockExportPreview = collibraApi.exportPreview as ReturnType<typeof vi.fn>;
const mockSync = collibraApi.sync as ReturnType<typeof vi.fn>;

function collibraConnection(overrides: Partial<{
  id: string;
  display_name: string;
  base_url: string;
  community_id: string | null;
  domain_id: string | null;
  is_active: boolean;
}> = {}) {
  return {
    id: overrides.id ?? "conn-1",
    project_id: "proj-1",
    model_id: "model-1",
    display_name: overrides.display_name ?? "Collibra Prod",
    base_url: overrides.base_url ?? "https://company.collibra.com",
    auth_type: "bearer_token",
    community_id: overrides.community_id ?? null,
    domain_id: overrides.domain_id ?? null,
    sync_scope: "model",
    sync_mode: "rest_api",
    is_active: overrides.is_active ?? true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

function renderPanel() {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/p/proj-1/m/model-1"]}>
        <Routes>
          <Route path="/p/:projectId/m/:modelId" element={<CollibraIntegrationPanel />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("CollibraIntegrationPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the panel title", async () => {
    mockListConfigs.mockResolvedValue([]);
    mockListRuns.mockResolvedValue([]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("Collibra Integration")).toBeTruthy();
    });
  });

  it("shows empty state when no connections", async () => {
    mockListConfigs.mockResolvedValue([]);
    mockListRuns.mockResolvedValue([]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText(/No Collibra connections configured/)).toBeTruthy();
    });
  });

  it("shows existing connections with community and domain", async () => {
    mockListConfigs.mockResolvedValue([
      collibraConnection({ community_id: "community-123", domain_id: "domain-456" }),
    ]);
    mockListRuns.mockResolvedValue([]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("Collibra Prod")).toBeTruthy();
      expect(screen.getByText(/Community: community-123/)).toBeTruthy();
      expect(screen.getByText(/Domain: domain-456/)).toBeTruthy();
    });
  });

  it("shows action buttons when connection exists", async () => {
    mockListConfigs.mockResolvedValue([
      collibraConnection(),
    ]);
    mockListRuns.mockResolvedValue([]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("Test Connection")).toBeTruthy();
      expect(screen.getByText("Preview Export")).toBeTruthy();
      expect(screen.getByText("Dry Run")).toBeTruthy();
      expect(screen.getByRole("button", { name: /Sync Unavailable/ })).toBeDisabled();
    });
  });

  it("previews export without a fake connection_id contract", async () => {
    mockListConfigs.mockResolvedValue([collibraConnection({ id: "conn-1" })]);
    mockListRuns.mockResolvedValue([]);
    mockExportPreview.mockResolvedValue({
      assets_total: 3,
      relations_total: 2,
      attributes_total: 0,
      responsibilities_total: 0,
      by_asset_type: {},
      warnings: [],
    });

    renderPanel();
    await userEvent.click(await screen.findByText("Preview Export"));

    await waitFor(() => {
      expect(mockExportPreview).toHaveBeenCalledWith("proj-1", "model-1", {});
    });
  });

  it("keeps live Collibra push disabled", async () => {
    mockListConfigs.mockResolvedValue([collibraConnection()]);
    mockListRuns.mockResolvedValue([]);

    renderPanel();
    const livePush = await screen.findByRole("button", { name: /Sync Unavailable/ });

    expect(livePush).toBeDisabled();
    expect(mockSync).not.toHaveBeenCalled();
  });

  it("validates the selected Collibra connection", async () => {
    mockListConfigs.mockResolvedValue([
      collibraConnection({ id: "conn-1", display_name: "Collibra Dev" }),
      collibraConnection({
        id: "conn-2",
        display_name: "Collibra Prod",
        base_url: "https://prod.collibra.com",
      }),
    ]);
    mockListRuns.mockResolvedValue([]);
    mockValidate.mockResolvedValue({
      ok: true,
      base_url: "https://prod.collibra.com",
      community_found: true,
      domain_found: true,
      missing_asset_types: [],
      missing_relation_types: [],
      warnings: [],
    });

    renderPanel();
    const card = await screen.findByTestId("collibra-connection-conn-2");
    await userEvent.click(within(card).getByText("Test Connection"));

    await waitFor(() => {
      expect(mockValidate).toHaveBeenCalledWith("proj-1", "model-1", {
        connection_id: "conn-2",
      });
    });
  });

  it("maps a failed Collibra dry run response to localized fallback text", async () => {
    mockListConfigs.mockResolvedValue([collibraConnection()]);
    mockListRuns.mockResolvedValue([]);
    mockSync.mockResolvedValue({
      run_id: "run-1",
      status: "failed",
      assets_total: 0,
      relations_total: 0,
      attributes_total: 0,
      responsibilities_total: 0,
      assets_created: 0,
      assets_updated: 0,
      relations_created: 0,
      relations_updated: 0,
      warnings: [],
      error_message: "Collibra client is not implemented",
    });

    renderPanel();
    await userEvent.click(await screen.findByText("Dry Run"));

    await waitFor(() => {
      expect(mockSync).toHaveBeenCalledWith("proj-1", "model-1", {
        connection_id: "conn-1",
        dry_run: true,
      });
      expect(screen.getByText(/backend could not complete the Collibra dry run/)).toBeTruthy();
      expect(screen.queryByText(/Collibra client is not implemented/)).toBeNull();
    });
  });

  it("renders preview breakdown stats (F-CSI-03)", async () => {
    mockListConfigs.mockResolvedValue([collibraConnection({ id: "conn-1" })]);
    mockListRuns.mockResolvedValue([]);
    mockExportPreview.mockResolvedValue({
      assets_total: 9,
      relations_total: 4,
      attributes_total: 30,
      responsibilities_total: 2,
      by_asset_type: { Metric: 5, Column: 4 },
      warnings: [],
    });

    renderPanel();
    await userEvent.click(await screen.findByText("Preview Export"));

    await waitFor(() => {
      expect(screen.getByText(/30 attributes/)).toBeTruthy();
      expect(screen.getByText("By asset type")).toBeTruthy();
      expect(screen.getByText("Metric: 5")).toBeTruthy();
      expect(screen.getByText("Column: 4")).toBeTruthy();
    });
  });

  it("shows a simulated (not green) result for placeholder validation (F-CSI-01)", async () => {
    mockListConfigs.mockResolvedValue([collibraConnection({ id: "conn-1" })]);
    mockListRuns.mockResolvedValue([]);
    mockValidate.mockResolvedValue({
      ok: null,
      simulated: true,
      base_url: "https://company.collibra.com",
      community_found: null,
      domain_found: null,
      missing_asset_types: [],
      missing_relation_types: [],
      warnings: ["Validation simulated — the Collibra connector is not yet contacted."],
    });

    renderPanel();
    await userEvent.click(await screen.findByText("Test Connection"));

    await waitFor(() => {
      expect(screen.getByText(/Validation simulated for Collibra Prod/)).toBeTruthy();
    });
  });

  it("opens dialog when Add Connection is clicked", async () => {
    mockListConfigs.mockResolvedValue([]);
    mockListRuns.mockResolvedValue([]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("Add Connection")).toBeTruthy();
    });
    await userEvent.click(screen.getByText("Add Connection"));
    expect(screen.getByText("New Connection")).toBeTruthy();
  });
});
