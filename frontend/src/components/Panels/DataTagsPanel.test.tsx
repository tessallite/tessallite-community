import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ConfirmProvider } from "../Confirm";

const useDataTagsMock = vi.fn();
const useSourcesMock = vi.fn();
const useAllModelTablesMock = vi.fn();
const createMock = vi.fn();
const updateMock = vi.fn();
const deleteMock = vi.fn();
const tableAttributesListMock = vi.fn();

vi.mock("../../api/hooks", () => ({
  useDataTags: (...args: unknown[]) => useDataTagsMock(...args),
  useSources: (...args: unknown[]) => useSourcesMock(...args),
  useAllModelTables: (...args: unknown[]) => useAllModelTablesMock(...args),
}));

vi.mock("../../api/client", () => ({
  dataTagsApi: {
    create: (...args: unknown[]) => createMock(...args),
    update: (...args: unknown[]) => updateMock(...args),
    delete: (...args: unknown[]) => deleteMock(...args),
  },
  tableAttributesApi: {
    list: (...args: unknown[]) => tableAttributesListMock(...args),
  },
}));

import DataTagsPanel from "./DataTagsPanel";

const CUSTOMERS_TABLE = {
  id: "t1",
  model_id: "model-1",
  source_id: "s1",
  table_type: "dim",
  physical_name: "customers_raw",
  alias: "customers",
  display_name: "Customers",
  row_count_estimate: null,
  last_stats_at: null,
  created_at: "2026-04-24T00:00:00Z",
  updated_at: "2026-04-24T00:00:00Z",
};

const EMAIL_ATTR = {
  kind: "physical",
  id: "col-email",
  table_id: "t1",
  name: "email",
  data_type: "varchar",
  is_user_defined: false,
  expression: null,
  validated: null,
  validation_error: null,
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
            <Route
              path="/p/:projectId/m/:modelId"
              element={<DataTagsPanel />}
            />
          </Routes>
        </MemoryRouter>
      </ConfirmProvider>
    </QueryClientProvider>,
  );
}

describe("DataTagsPanel — column picker (F-008-08)", () => {
  beforeEach(() => {
    useDataTagsMock.mockReset();
    useSourcesMock.mockReset();
    useAllModelTablesMock.mockReset();
    createMock.mockReset();
    updateMock.mockReset();
    deleteMock.mockReset();
    tableAttributesListMock.mockReset();

    useSourcesMock.mockReturnValue({ data: [{ id: "s1" }], isLoading: false });
    useAllModelTablesMock.mockReturnValue({
      data: [CUSTOMERS_TABLE],
      isLoading: false,
    });
    tableAttributesListMock.mockResolvedValue([EMAIL_ATTR]);
    createMock.mockResolvedValue({});
    updateMock.mockResolvedValue({});
  });

  it("creates a tag with the ticked columns in column_ids", async () => {
    const user = userEvent.setup();
    useDataTagsMock.mockReturnValue({ data: [], isLoading: false });

    renderPanel();

    await user.click(screen.getByRole("button", { name: /add tag/i }));
    await user.type(screen.getByLabelText(/tag name/i), "PII");

    // Pick the table, then tick the email column.
    await user.click(screen.getByLabelText(/^table$/i));
    await user.click(await screen.findByRole("option", { name: /customers/i }));
    await user.click(await screen.findByRole("checkbox", { name: /email/i }));

    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /^create$/i }));

    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(1));
    expect(createMock).toHaveBeenCalledWith(
      "proj-1",
      "model-1",
      expect.objectContaining({
        tag_name: "PII",
        column_ids: ["col-email"],
      }),
    );
  });

  it("removing an assigned column chip drops it from the update payload", async () => {
    const user = userEvent.setup();
    useDataTagsMock.mockReturnValue({
      data: [
        {
          id: "tag-1",
          model_id: "model-1",
          tag_name: "PII",
          description: null,
          created_at: "2026-04-24T00:00:00Z",
          columns: [
            {
              column_id: "col-email",
              table_name: "customers",
              column_name: "email",
            },
          ],
        },
      ],
      isLoading: false,
    });

    renderPanel();

    await user.click(screen.getByRole("button", { name: /edit/i }));
    const dialog = await screen.findByRole("dialog");
    // The assigned column shows as a removable chip.
    const chip = within(dialog).getByText("customers.email");
    expect(chip).toBeInTheDocument();
    await user.click(
      within(chip.closest(".MuiChip-root") as HTMLElement).getByTestId(
        "CancelIcon",
      ),
    );

    await user.click(within(dialog).getByRole("button", { name: /update/i }));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    expect(updateMock).toHaveBeenCalledWith("proj-1", "model-1", "tag-1", {
      tag_name: "PII",
      description: null,
      column_ids: [],
    });
  });
});
