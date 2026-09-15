import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ConfirmProvider } from "../Confirm";

const useDownstreamAssetsMock = vi.fn();
const useDownstreamAssetSummaryMock = vi.fn();
const useGatewayQueryReferencesMock = vi.fn();
const useColumnUsageMock = vi.fn();
const createMock = vi.fn();
const updateMock = vi.fn();
const deleteMock = vi.fn();
const scanMock = vi.fn();

vi.mock("../../api/hooks", () => ({
  useDownstreamAssets: (...args: unknown[]) => useDownstreamAssetsMock(...args),
  useDownstreamAssetSummary: (...args: unknown[]) =>
    useDownstreamAssetSummaryMock(...args),
  useGatewayQueryReferences: (...args: unknown[]) =>
    useGatewayQueryReferencesMock(...args),
  useColumnUsage: (...args: unknown[]) => useColumnUsageMock(...args),
}));

vi.mock("../../api/client", () => ({
  downstreamAssetsApi: {
    create: (...args: unknown[]) => createMock(...args),
    update: (...args: unknown[]) => updateMock(...args),
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
    useColumnUsageMock.mockReset();
    useColumnUsageMock.mockReturnValue({
      data: {
        model_id: "model-1",
        total_queries_parsed: 0,
        total_queries_skipped: 0,
        logs_available: 0,
        truncated: false,
        columns: [],
      },
      isLoading: false,
      isError: false,
    });
    createMock.mockReset();
    updateMock.mockReset();
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
    expect(screen.getByText("Usage & Downstream Assets")).toBeTruthy();
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

  // Bug-8937/R2-B01: createMut/updateMut had no onError at all, so a failed
  // save closed the dialog silently with no error surfaced to the user.
  it("shows the server's structured error when creating an asset fails", async () => {
    useDownstreamAssetsMock.mockReturnValue({ data: [], isLoading: false });
    useDownstreamAssetSummaryMock.mockReturnValue({
      data: { total: 0, by_type: {} },
    });
    useGatewayQueryReferencesMock.mockReturnValue({ data: [], isLoading: false });
    createMock.mockRejectedValue({
      response: { data: { detail: { message: "asset_name already exists" } } },
    });

    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByText("Add Asset"));
    await waitFor(() => {
      expect(screen.getByText("Add Downstream Asset")).toBeTruthy();
    });
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "Executive Dashboard");
    await user.click(within(dialog).getByText("Create"));

    expect(await screen.findByText("asset_name already exists")).toBeTruthy();
    // The dialog must stay open on failure — a silent close on error was
    // exactly the defect (the mutation had no onError to prevent it).
    expect(screen.getByText("Add Downstream Asset")).toBeTruthy();
  });

  // R3 (round-3 external review): updateMut shares the exact same onError
  // wiring as createMut, but no test exercised the edit/update path at all
  // (success or failure) — updateMock was not even mocked in this file. This
  // is the direct behavior test for the update side of Bug-8937's fix.
  it("shows the server's structured error when updating an asset fails", async () => {
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
    updateMock.mockRejectedValue({
      response: { data: { detail: { message: "asset_name already exists" } } },
    });

    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await waitFor(() => {
      expect(screen.getByText("Edit Asset")).toBeTruthy();
    });
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByText("Update"));

    expect(await screen.findByText("asset_name already exists")).toBeTruthy();
    // The dialog must stay open on failure, exactly like the create path.
    expect(screen.getByText("Edit Asset")).toBeTruthy();
    expect(updateMock).toHaveBeenCalledWith("proj-1", "model-1", "a1", expect.any(Object));
  });

  // Bug-9576 (R3-04, round-2 recheck): deleteMut had no onError — a failed
  // delete removed nothing but gave the user no indication anything went
  // wrong.
  it("shows the server's error when deleting an asset fails", async () => {
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
    deleteMock.mockRejectedValue({
      response: { data: { detail: { message: "asset is referenced elsewhere" } } },
    });

    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(await screen.findByRole("button", { name: /delete/i }));

    expect(await screen.findByText("asset is referenced elsewhere")).toBeTruthy();
    // The asset must still be visible — the delete did not silently succeed.
    expect(screen.getByText("Sales Dashboard")).toBeTruthy();
  });

  // Bug-9576 (R3-04, round-2 recheck): scanMut had no onError either.
  it("shows the server's error when the usage scan fails", async () => {
    mockEmptyPanel();
    scanMock.mockRejectedValue({
      response: { data: { detail: { message: "scan target unreachable" } } },
    });

    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByText("Query Usage"));
    await user.click(screen.getByText("Run scan"));

    expect(await screen.findByText("scan target unreachable")).toBeTruthy();
  });

  it("switches to Query Usage tab", async () => {
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
    await user.click(screen.getByText("Query Usage"));
    await waitFor(() => {
      expect(screen.getByText("sales")).toBeTruthy();
      expect(screen.getByText("42")).toBeTruthy();
    });
  });

  it("shows stable table attribution in the Column Usage tab", async () => {
    useDownstreamAssetsMock.mockReturnValue({ data: [], isLoading: false });
    useDownstreamAssetSummaryMock.mockReturnValue({
      data: { total: 0, by_type: {} },
    });
    useGatewayQueryReferencesMock.mockReturnValue({ data: [], isLoading: false });
    useColumnUsageMock.mockReturnValue({
      data: {
        model_id: "model-1",
        total_queries_parsed: 4,
        total_queries_skipped: 1,
        logs_available: 5,
        truncated: false,
        columns: [{
          table_name: "demo_data.orders",
          column_name: "region",
          query_count: 3,
          hit_count: 5,
          last_seen_at: "2026-08-01T12:00:00Z",
          ambiguous: false,
          candidate_tables: [],
        }],
      },
      isLoading: false,
      isError: false,
    });

    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByText("Column Usage"));

    expect(await screen.findByText("demo_data.orders")).toBeTruthy();
    expect(screen.getByText("region")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getByText("5")).toBeTruthy();
  });

  // -------------------------------------------------------------------------
  // Incremental-scan reporting. The scan resumes from the newest usage already
  // recorded, so a second press legitimately finds nothing new. Reporting that
  // as "0 tables checked" reads as "this model is unused" — the opposite of the
  // question the panel exists to answer.
  // -------------------------------------------------------------------------

  function mockEmptyPanel() {
    useDownstreamAssetsMock.mockReturnValue({ data: [], isLoading: false });
    useDownstreamAssetSummaryMock.mockReturnValue({
      data: { total: 0, by_type: {} },
    });
    useGatewayQueryReferencesMock.mockReturnValue({ data: [], isLoading: false });
  }

  it("does not report an idle rescan as zero usage", async () => {
    mockEmptyPanel();
    scanMock.mockResolvedValue({
      references_upserted: 0,
      tables_matched: 0,
      columns_matched: 0,
      logs_scanned: 0,
      more_remaining: false,
    });

    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByText("Query Usage"));
    await user.click(screen.getByText("Run scan"));

    expect(
      await screen.findByText(/No query activity since the last scan/),
    ).toBeTruthy();
    expect(screen.queryByText(/0 tables checked/)).toBeNull();
  });

  it("tells the modeller when more log entries remain to scan", async () => {
    mockEmptyPanel();
    scanMock.mockResolvedValue({
      references_upserted: 12,
      tables_matched: 3,
      columns_matched: 9,
      logs_scanned: 5000,
      more_remaining: true,
    });

    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByText("Query Usage"));
    await user.click(screen.getByText("Run scan"));

    expect(
      await screen.findByText(/Read 5000 query log entries so far\. There may be more/),
    ).toBeTruthy();
  });

  it("says the window matched nothing rather than reporting zero usage", async () => {
    mockEmptyPanel();
    scanMock.mockResolvedValue({
      references_upserted: 0,
      tables_matched: 0,
      columns_matched: 0,
      logs_scanned: 4200,
      more_remaining: false,
    });

    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByText("Query Usage"));
    await user.click(screen.getByText("Run scan"));

    expect(
      await screen.findByText(
        /Read 4200 query log entries; none of them referenced this model's tables/,
      ),
    ).toBeTruthy();
    // "0 tables checked" reads as "this model is unused" — the opposite answer.
    expect(screen.queryByText(/0 tables checked/)).toBeNull();
  });

  it("omits the continue prompt when the log is fully scanned", async () => {
    mockEmptyPanel();
    scanMock.mockResolvedValue({
      references_upserted: 12,
      tables_matched: 3,
      columns_matched: 9,
      logs_scanned: 120,
      more_remaining: false,
    });

    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByText("Query Usage"));
    await user.click(screen.getByText("Run scan"));

    expect(await screen.findByText(/3 tables checked/)).toBeTruthy();
    expect(screen.queryByText(/More remain/)).toBeNull();
  });

  it("warns when Column Usage only read part of the query log", async () => {
    mockEmptyPanel();
    useColumnUsageMock.mockReturnValue({
      data: {
        model_id: "model-1",
        total_queries_parsed: 4813,
        total_queries_skipped: 187,
        logs_available: 15670,
        truncated: true,
        columns: [],
      },
      isLoading: false,
      isError: false,
    });

    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByText("Column Usage"));

    // An empty column list plus no warning reads as "this model uses nothing".
    expect(
      await screen.findByText(
        /Only the most recent 5000 of 15670 query log entries were analysed/,
      ),
    ).toBeTruthy();
  });

  it("does not warn when Column Usage read the whole query log", async () => {
    mockEmptyPanel();
    useColumnUsageMock.mockReturnValue({
      data: {
        model_id: "model-1",
        total_queries_parsed: 12,
        total_queries_skipped: 0,
        logs_available: 12,
        truncated: false,
        columns: [],
      },
      isLoading: false,
      isError: false,
    });

    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByText("Column Usage"));

    expect(await screen.findByText(/No column usage found/)).toBeTruthy();
    expect(screen.queryByText(/Only the most recent/)).toBeNull();
  });
});