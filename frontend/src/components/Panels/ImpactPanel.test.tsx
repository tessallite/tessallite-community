import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ConfirmProvider } from "../Confirm";

const useDownstreamAssetsMock = vi.fn();
const useDownstreamAssetSummaryMock = vi.fn();
const useGatewayQueryReferencesMock = vi.fn();
const createMock = vi.fn();
const deleteMock = vi.fn();
const scanMock = vi.fn();

vi.mock("../../api/hooks", () => ({
  useDownstreamAssets: (...args: unknown[]) => useDownstreamAssetsMock(...args),
  useDownstreamAssetSummary: (...args: unknown[]) =>
    useDownstreamAssetSummaryMock(...args),
  useGatewayQueryReferences: (...args: unknown[]) =>
    useGatewayQueryReferencesMock(...args),
}));

vi.mock("../../api/client", () => ({
  downstreamAssetsApi: {
    create: (...args: unknown[]) => createMock(...args),
    delete: (...args: unknown[]) => deleteMock(...args),
  },
  impactScanApi: {
    scan: (...args: unknown[]) => scanMock(...args),
  },
}));

import ImpactPanel from "./ImpactPanel";

function renderPanel() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ConfirmProvider>
        <MemoryRouter initialEntries={["/p/proj-1/m/model-1"]}>
          <Routes>
            <Route path="/p/:projectId/m/:modelId" element={<ImpactPanel />} />
          </Routes>
        </MemoryRouter>
      </ConfirmProvider>
    </QueryClientProvider>,
  );
}

describe("ImpactPanel", () => {
  beforeEach(() => {
    useDownstreamAssetsMock.mockReset();
    useDownstreamAssetSummaryMock.mockReset();
    useGatewayQueryReferencesMock.mockReset();
    createMock.mockReset();
    deleteMock.mockReset();
    scanMock.mockReset();
  });

  it("renders heading", () => {
    useDownstreamAssetsMock.mockReturnValue({ data: [], isLoading: false });
    useDownstreamAssetSummaryMock.mockReturnValue({
      data: { total: 0, by_type: {} },
    });
    useGatewayQueryReferencesMock.mockReturnValue({ data: [], isLoading: false });

    renderPanel();
    expect(screen.getByText("Impact Analysis")).toBeTruthy();
  });

  it("shows empty state when no assets", () => {
    useDownstreamAssetsMock.mockReturnValue({ data: [], isLoading: false });
    useDownstreamAssetSummaryMock.mockReturnValue({
      data: { total: 0, by_type: {} },
    });
    useGatewayQueryReferencesMock.mockReturnValue({ data: [], isLoading: false });

    renderPanel();
    expect(
      screen.getByText(/No downstream assets tagged yet/),
    ).toBeTruthy();
  });

  it("shows asset list when data present", () => {
    useDownstreamAssetsMock.mockReturnValue({
      data: [
        {
          id: "a1",
          model_id: "m1",
          asset_type: "dashboard",
          asset_name: "Sales Dashboard",
          asset_url: null,
          owner: "alice",
          notes: null,
          created_at: "2026-01-01",
          updated_at: "2026-01-01",
          column_ids: [],
        },
      ],
      isLoading: false,
    });
    useDownstreamAssetSummaryMock.mockReturnValue({
      data: { total: 1, by_type: { dashboard: 1 } },
    });
    useGatewayQueryReferencesMock.mockReturnValue({ data: [], isLoading: false });

    renderPanel();
    expect(screen.getByText("Sales Dashboard")).toBeTruthy();
    expect(screen.getByText("alice")).toBeTruthy();
  });

  it("shows summary alert with counts", () => {
    useDownstreamAssetsMock.mockReturnValue({ data: [], isLoading: false });
    useDownstreamAssetSummaryMock.mockReturnValue({
      data: { total: 3, by_type: { dashboard: 2, report: 1 } },
    });
    useGatewayQueryReferencesMock.mockReturnValue({ data: [], isLoading: false });

    renderPanel();
    expect(
      screen.getByText(/3 downstream assets depend on this model/),
    ).toBeTruthy();
  });

  it("opens add asset dialog", async () => {
    useDownstreamAssetsMock.mockReturnValue({ data: [], isLoading: false });
    useDownstreamAssetSummaryMock.mockReturnValue({
      data: { total: 0, by_type: {} },
    });
    useGatewayQueryReferencesMock.mockReturnValue({ data: [], isLoading: false });

    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByText("Add Asset"));
    await waitFor(() => {
      expect(screen.getByText("Add Downstream Asset")).toBeTruthy();
    });
  });

  it("switches to Query Audit tab", async () => {
    useDownstreamAssetsMock.mockReturnValue({ data: [], isLoading: false });
    useDownstreamAssetSummaryMock.mockReturnValue({
      data: { total: 0, by_type: {} },
    });
    useGatewayQueryReferencesMock.mockReturnValue({
      data: [
        {
          id: "r1",
          model_id: "m1",
          queried_table: "sales",
          query_user: "analyst@corp",
          query_text_hash: "abc",
          last_seen_at: "2026-01-01",
          hit_count: 42,
        },
      ],
      isLoading: false,
    });

    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByText("Query Audit"));
    await waitFor(() => {
      expect(screen.getByText("sales")).toBeTruthy();
      expect(screen.getByText("42")).toBeTruthy();
    });
  });
});
