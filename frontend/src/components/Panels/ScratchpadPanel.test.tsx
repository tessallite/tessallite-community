/**
 * F-029-14 — the scratchpad data_type control must be a select over the six
 * supported types, not a free-text field. A free-text value rendered a raw
 * i18n key (``scratchpad.dataType.<value>``) in the table; constraining it to
 * a fixed list prevents that.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

vi.mock("../../api/client", () => ({
  scratchpadApi: {
    list: vi.fn().mockResolvedValue([]),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
  },
}));

vi.mock("../Confirm", () => ({
  useConfirm: () => vi.fn().mockResolvedValue(true),
}));

import ScratchpadPanel from "./ScratchpadPanel";

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/p/proj-1/m/model-1"]}>
        <Routes>
          <Route path="/p/:projectId/m/:modelId" element={<ScratchpadPanel />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ScratchpadPanel data_type select (F-029-14)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("offers the six supported data types in a select", async () => {
    const user = userEvent.setup();
    renderPanel();
    // Open the create dialog.
    await user.click(await screen.findByText("New"));
    // The Data Type field is a MUI select (role combobox), not a textbox.
    const combo = await screen.findByRole("combobox");
    await user.click(combo);
    const listbox = await screen.findByRole("listbox");
    const options = within(listbox).getAllByRole("option");
    // numeric, integer, string, boolean, date, timestamp
    expect(options).toHaveLength(6);
    const values = options.map((o) => o.getAttribute("data-value"));
    expect(values).toEqual([
      "numeric",
      "integer",
      "string",
      "boolean",
      "date",
      "timestamp",
    ]);
  });
});
