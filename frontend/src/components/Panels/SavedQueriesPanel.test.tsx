/**
 * F-029-03 — saved-query delete must be confirmed and never fire on a single
 * mis-click, and API errors must surface the server ``detail`` (not the raw
 * axios message). Regression for the panel that wired the delete icon directly
 * to the mutation with no confirm dialog.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

const deleteMock = vi.fn();
const confirmMock = vi.fn();

vi.mock("../../api/client", () => ({
  savedQueriesApi: {
    create: vi.fn(),
    update: vi.fn(),
    delete: (...args: unknown[]) => deleteMock(...args),
  },
}));

const queriesData = [
  {
    id: "q1",
    model_id: "model-1",
    name: "Top accounts",
    description: "shared",
    query_text: "SELECT 1",
    query_type: "sql",
    created_by: "owner@example.com",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
];

vi.mock("../../api/hooks", () => ({
  useSavedQueries: () => ({ data: queriesData, isLoading: false }),
}));

vi.mock("../Confirm", () => ({
  useConfirm: () => confirmMock,
}));

import SavedQueriesPanel from "./SavedQueriesPanel";

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/p/proj-1/m/model-1"]}>
        <Routes>
          <Route path="/p/:projectId/m/:modelId" element={<SavedQueriesPanel />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SavedQueriesPanel", () => {
  beforeEach(() => {
    deleteMock.mockReset();
    confirmMock.mockReset();
  });

  it("asks for confirmation before deleting and does nothing on cancel", async () => {
    confirmMock.mockResolvedValue(false);
    renderPanel();
    const delBtn = screen.getAllByRole("button").find((b) =>
      b.querySelector('[data-testid="DeleteIcon"]'),
    )!;
    await userEvent.click(delBtn);
    expect(confirmMock).toHaveBeenCalledTimes(1);
    expect(deleteMock).not.toHaveBeenCalled();
  });

  it("deletes only after the user confirms", async () => {
    confirmMock.mockResolvedValue(true);
    deleteMock.mockResolvedValue(undefined);
    renderPanel();
    const delBtn = screen.getAllByRole("button").find((b) =>
      b.querySelector('[data-testid="DeleteIcon"]'),
    )!;
    await userEvent.click(delBtn);
    await waitFor(() =>
      expect(deleteMock).toHaveBeenCalledWith("proj-1", "model-1", "q1"),
    );
  });

  it("surfaces the API detail message on a failed delete", async () => {
    confirmMock.mockResolvedValue(true);
    deleteMock.mockRejectedValue({
      response: { data: { detail: "Only the query owner or a modeler can modify this saved query" } },
    });
    renderPanel();
    const delBtn = screen.getAllByRole("button").find((b) =>
      b.querySelector('[data-testid="DeleteIcon"]'),
    )!;
    await userEvent.click(delBtn);
    expect(
      await screen.findByText(
        "Only the query owner or a modeler can modify this saved query",
      ),
    ).toBeInTheDocument();
  });
});
