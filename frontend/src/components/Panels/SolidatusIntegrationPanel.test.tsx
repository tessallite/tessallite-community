import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SolidatusIntegrationPanel } from "./SolidatusIntegrationPanel";

// Mock the API client
vi.mock("../../api/client", () => ({
  solidatusApi: {
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

import { solidatusApi } from "../../api/client";

const mockListConfigs = solidatusApi.listConfigs as ReturnType<typeof vi.fn>;
const mockListRuns = solidatusApi.listRuns as ReturnType<typeof vi.fn>;
const mockCreateConfig = solidatusApi.createConfig as ReturnType<typeof vi.fn>;
const mockDeleteConfig = solidatusApi.deleteConfig as ReturnType<typeof vi.fn>;
const mockValidate = solidatusApi.validate as ReturnType<typeof vi.fn>;
const mockExportPreview = solidatusApi.exportPreview as ReturnType<typeof vi.fn>;
const mockSync = solidatusApi.sync as ReturnType<typeof vi.fn>;

function solidatusConnection(overrides: Partial<{
  id: string;
  display_name: string;
  base_url: string;
  workspace_id: string | null;
  model_ref: string | null;
  is_active: boolean;
}> = {}) {
  return {
    id: overrides.id ?? "conn-1",
    project_id: "proj-1",
    model_id: "model-1",
    display_name: overrides.display_name ?? "Solidatus Dev",
    base_url: overrides.base_url ?? "https://solidatus.example.com",
    auth_type: "bearer_token",
    workspace_id: overrides.workspace_id ?? null,
    model_ref: overrides.model_ref ?? null,
    sync_scope: "model",
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
          <Route path="/p/:projectId/m/:modelId" element={<SolidatusIntegrationPanel />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("SolidatusIntegrationPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the panel title", async () => {
    mockListConfigs.mockResolvedValue([]);
    mockListRuns.mockResolvedValue([]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("Solidatus Integration")).toBeTruthy();
    });
  });

  it("shows empty state when no connections", async () => {
    mockListConfigs.mockResolvedValue([]);
    mockListRuns.mockResolvedValue([]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText(/No Solidatus connections configured/)).toBeTruthy();
    });
  });

  it("shows existing connections", async () => {
    mockListConfigs.mockResolvedValue([
      solidatusConnection({ workspace_id: "ws-123", model_ref: "tessallite-sales" }),
    ]);
    mockListRuns.mockResolvedValue([]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("Solidatus Dev")).toBeTruthy();
      expect(screen.getByText("https://solidatus.example.com")).toBeTruthy();
    });
  });

  it("shows action buttons when connection exists", async () => {
    mockListConfigs.mockResolvedValue([
      solidatusConnection(),
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
    mockListConfigs.mockResolvedValue([solidatusConnection({ id: "conn-1" })]);
    mockListRuns.mockResolvedValue([]);
    mockExportPreview.mockResolvedValue({
      nodes_total: 3,
      edges_total: 2,
      by_type: {},
      warnings: [],
    });

    renderPanel();
    await userEvent.click(await screen.findByText("Preview Export"));

    await waitFor(() => {
      expect(mockExportPreview).toHaveBeenCalledWith("proj-1", "model-1", {});
    });
  });

  it("dry run sends mode dry_run and succeeds", async () => {
    mockListConfigs.mockResolvedValue([solidatusConnection({ id: "conn-1" })]);
    mockListRuns.mockResolvedValue([]);
    mockSync.mockResolvedValue({
      run_id: "run-1",
      status: "succeeded",
      nodes_total: 2,
      edges_total: 1,
      nodes_created: 0,
      nodes_updated: 0,
      edges_created: 0,
      edges_updated: 0,
      error_message: null,
    });

    renderPanel();
    await userEvent.click(await screen.findByText("Dry Run"));

    await waitFor(() => {
      expect(mockSync).toHaveBeenCalledWith("proj-1", "model-1", {
        connection_id: "conn-1",
        mode: "dry_run",
      });
      expect(screen.getByText(/Solidatus Dev dry run completed/)).toBeTruthy();
    });
  });

  it("keeps live Solidatus push disabled", async () => {
    mockListConfigs.mockResolvedValue([solidatusConnection()]);
    mockListRuns.mockResolvedValue([]);

    renderPanel();
    const livePush = await screen.findByRole("button", { name: /Sync Unavailable/ });

    expect(livePush).toBeDisabled();
    expect(mockSync).not.toHaveBeenCalled();
  });

  it("validates the selected Solidatus connection", async () => {
    mockListConfigs.mockResolvedValue([
      solidatusConnection({ id: "conn-1", display_name: "Solidatus Dev" }),
      solidatusConnection({
        id: "conn-2",
        display_name: "Solidatus Prod",
        base_url: "https://solidatus-prod.example.com",
      }),
    ]);
    mockListRuns.mockResolvedValue([]);
    mockValidate.mockResolvedValue({
      ok: true,
      base_url: "https://solidatus-prod.example.com",
      workspace_found: true,
      model_ref_found: true,
      warnings: [],
    });

    renderPanel();
    const card = await screen.findByTestId("solidatus-connection-conn-2");
    await userEvent.click(within(card).getByText("Test Connection"));

    await waitFor(() => {
      expect(mockValidate).toHaveBeenCalledWith("proj-1", "model-1", {
        connection_id: "conn-2",
      });
    });
  });

  it("maps a failed Solidatus dry run response to localized fallback text", async () => {
    mockListConfigs.mockResolvedValue([solidatusConnection()]);
    mockListRuns.mockResolvedValue([]);
    mockSync.mockResolvedValue({
      run_id: "run-1",
      status: "failed",
      nodes_total: 0,
      edges_total: 0,
      nodes_created: 0,
      nodes_updated: 0,
      edges_created: 0,
      edges_updated: 0,
      error_message: "Solidatus client is not implemented",
    });

    renderPanel();
    await userEvent.click(await screen.findByText("Dry Run"));

    await waitFor(() => {
      expect(mockSync).toHaveBeenCalledWith("proj-1", "model-1", {
        connection_id: "conn-1",
        mode: "dry_run",
      });
      expect(screen.getByText(/backend could not complete the Solidatus dry run/)).toBeTruthy();
      expect(screen.queryByText(/Solidatus client is not implemented/)).toBeNull();
    });
  });

  it("renders preview breakdown stats (F-CSI-03)", async () => {
    mockListConfigs.mockResolvedValue([solidatusConnection({ id: "conn-1" })]);
    mockListRuns.mockResolvedValue([]);
    mockExportPreview.mockResolvedValue({
      nodes_total: 7,
      edges_total: 3,
      by_type: { measure: 4, column: 3 },
      warnings: [],
    });

    renderPanel();
    await userEvent.click(await screen.findByText("Preview Export"));

    await waitFor(() => {
      expect(screen.getByText("By node type")).toBeTruthy();
      expect(screen.getByText("measure: 4")).toBeTruthy();
      expect(screen.getByText("column: 3")).toBeTruthy();
    });
  });

  it("shows a simulated (not green) result for placeholder validation (F-CSI-01)", async () => {
    mockListConfigs.mockResolvedValue([solidatusConnection({ id: "conn-1" })]);
    mockListRuns.mockResolvedValue([]);
    mockValidate.mockResolvedValue({
      ok: null,
      simulated: true,
      base_url: "https://solidatus.example.com",
      workspace_found: null,
      model_ref_found: null,
      warnings: ["Validation simulated — the Solidatus connector is not yet contacted."],
    });

    renderPanel();
    await userEvent.click(await screen.findByText("Test Connection"));

    await waitFor(() => {
      expect(screen.getByText(/Validation simulated for Solidatus Dev/)).toBeTruthy();
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

  it("shows run history when runs exist", async () => {
    mockListConfigs.mockResolvedValue([]);
    mockListRuns.mockResolvedValue([
      {
        id: "run-1",
        connection_id: "conn-1",
        project_id: "proj-1",
        model_id: "model-1",
        mode: "push",
        status: "succeeded",
        started_at: "2026-01-01T00:00:00Z",
        finished_at: "2026-01-01T00:01:00Z",
        tessallite_snapshot_hash: null,
        solidatus_target_ref: null,
        nodes_total: 10,
        edges_total: 5,
        nodes_created: 10,
        nodes_updated: 0,
        edges_created: 5,
        edges_updated: 0,
        error_message: null,
      },
    ]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("Run History")).toBeTruthy();
    });
  });
});
