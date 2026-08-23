import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

const listMock = vi.fn();
const acknowledgeMock = vi.fn();

vi.mock("../../api/client", () => ({
  schemaChangesApi: {
    list: (...args: unknown[]) => listMock(...args),
    acknowledge: (...args: unknown[]) => acknowledgeMock(...args),
  },
}));

import SchemaChangesPanel from "./SchemaChangesPanel";

function renderPanel() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/p/proj-1/m/model-1"]}>
        <Routes>
          <Route
            path="/p/:projectId/m/:modelId"
            element={<SchemaChangesPanel />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SchemaChangesPanel", () => {
  beforeEach(() => {
    listMock.mockReset();
    acknowledgeMock.mockReset();
  });

  it("renders empty state when no events", async () => {
    listMock.mockResolvedValue([]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText(/no schema drift detected/i)).toBeTruthy();
    });
  });

  it("renders unresolved event with table name", async () => {
    listMock.mockResolvedValue([
      {
        id: "e1",
        model_id: "model-1",
        table_name: "orders",
        change_type: "column_removed",
        is_breaking: true,
        // The scheduler producer's live contract uses ``column_name``;
        // ``column`` remains accepted for older persisted events.
        detail: { column_name: "old_col" },
        detected_at: "2026-05-01T00:00:00Z",
        acknowledged_at: null,
      },
    ]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("orders")).toBeTruthy();
    });
    expect(screen.getByText("Column removed")).toBeTruthy();
  });

  it("shows warning badge count for unresolved events", async () => {
    listMock.mockResolvedValue([
      {
        id: "e1",
        table_name: "t1",
        change_type: "column_added",
        is_breaking: false,
        detail: {},
        detected_at: null,
        acknowledged_at: null,
      },
      {
        id: "e2",
        table_name: "t2",
        change_type: "type_changed",
        is_breaking: true,
        detail: {},
        detected_at: null,
        acknowledged_at: null,
      },
    ]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("2")).toBeTruthy();
    });
  });

  it("calls acknowledge when button clicked", async () => {
    const user = userEvent.setup();
    acknowledgeMock.mockResolvedValue(undefined);
    listMock.mockResolvedValue([
      {
        id: "e1",
        table_name: "orders",
        change_type: "column_removed",
        is_breaking: false,
        detail: {},
        detected_at: null,
        acknowledged_at: null,
      },
    ]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("orders")).toBeTruthy();
    });
    await user.click(screen.getByRole("button", { name: /acknowledge/i }));
    await waitFor(() => {
      expect(acknowledgeMock).toHaveBeenCalledWith("proj-1", "model-1", "e1");
    });
  });

  it("shows acknowledged count for resolved events", async () => {
    listMock.mockResolvedValue([
      {
        id: "e1",
        table_name: "t1",
        change_type: "column_added",
        is_breaking: false,
        detail: {},
        detected_at: null,
        acknowledged_at: "2026-05-01T00:00:00Z",
      },
    ]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText(/1 acknowledged/i)).toBeTruthy();
    });
  });
});
