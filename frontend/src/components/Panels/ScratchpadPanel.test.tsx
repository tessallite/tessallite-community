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

import { scratchpadApi } from "../../api/client";
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

describe("ScratchpadPanel save-failure message (Bug-8162)", () => {
  beforeEach(() => vi.clearAllMocks());

  async function submitAndReadError(err: unknown): Promise<string> {
    const user = userEvent.setup();
    vi.mocked(scratchpadApi.create).mockRejectedValueOnce(err);
    renderPanel();
    await user.click(await screen.findByText("New"));
    await user.type(screen.getByLabelText(/Name \(slug\)/), "m1");
    await user.type(screen.getByLabelText(/Expression/), "amount * 0.9");
    await user.click(screen.getByRole("button", { name: "Create" }));
    return (await screen.findByRole("alert")).textContent ?? "";
  }

  it("says 'could not be reached, retry' on a 503, not 'your expression is wrong'", async () => {
    // Bug-8162: the server now REFUSES to save while the query validator is
    // unreachable. Rendering the server's raw detail (or a generic "save
    // failed") would leave a modeller believing their CORRECT expression was
    // rejected. The 503 is what tells the two apart.
    const text = await submitAndReadError({
      response: {
        status: 503,
        data: { detail: "validator_unavailable: the query validator ..." },
      },
    });
    expect(text).toContain("could not be reached");
    expect(text).toContain("has not been rejected");
    // The raw server diagnostic must not leak into the UI.
    expect(text).not.toContain("validator_unavailable:");
  });

  it("still shows the server's verdict when the expression really is wrong", async () => {
    // The other half of Bug-8162: a real rejection must keep reading as a
    // rejection, or an implementation that says "retry" to everything passes.
    const text = await submitAndReadError({
      response: {
        status: 400,
        data: {
          detail: "Expression is not valid for this model: unknown column no_such_col",
        },
      },
    });
    expect(text).toContain("unknown column no_such_col");
    expect(text).not.toContain("could not be reached");
  });
});
