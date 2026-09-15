import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ConfirmProvider } from "../Confirm";

// Bug-8937: "Add Rule" always showed the fixed sentence
// "Failed to create rule. Please check your input." on any create failure,
// never the server's actual 422 reason (e.g. the body-FK dict shape naming
// the offending id) — an operator could not tell a duplicate-name rejection
// from a transport failure.

const createMock = vi.fn();
const listMock = vi.fn();

vi.mock("../../api/client", () => ({
  dataQualityApi: {
    list: (...args: unknown[]) => listMock(...args),
    create: (...args: unknown[]) => createMock(...args),
    update: vi.fn(),
    delete: vi.fn(),
    validate: vi.fn(),
    clearViolations: vi.fn(),
    listViolations: vi.fn(),
  },
}));

import DataQualityPanel from "./DataQualityPanel";

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ConfirmProvider>
        <MemoryRouter initialEntries={["/p/proj-1/m/model-1"]}>
          <Routes>
            <Route path="/p/:projectId/m/:modelId" element={<DataQualityPanel />} />
          </Routes>
        </MemoryRouter>
      </ConfirmProvider>
    </QueryClientProvider>,
  );
}

async function openDialogAndFillMinimalForm(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByText("Add Rule"));
  await waitFor(() => expect(screen.getByRole("dialog")).toBeTruthy());
  await user.type(screen.getByLabelText("Rule Name"), "unique_email");
  await user.type(screen.getByLabelText("Target ID"), "col-123");
}

describe("DataQualityPanel surfaces the server's create-rule failure reason (Bug-8937)", () => {
  beforeEach(() => {
    createMock.mockReset();
    listMock.mockReset().mockResolvedValue([]);
  });

  it("renders the server's 422 message, not the fixed sentence", async () => {
    const SERVER_MESSAGE = "A rule named \"unique_email\" already exists for this target.";
    createMock.mockRejectedValue({
      response: { status: 422, data: { detail: SERVER_MESSAGE } },
    });
    renderPanel();
    const user = userEvent.setup();
    await waitFor(() => expect(listMock).toHaveBeenCalled());

    await openDialogAndFillMinimalForm(user);
    await user.click(screen.getByText("Create Rule"));

    await waitFor(() => expect(createMock).toHaveBeenCalled());
    expect(await screen.findByText(SERVER_MESSAGE)).toBeTruthy();
    expect(screen.queryByText("Failed to create rule. Please check your input.")).toBeNull();
  });

  it("falls back to the fixed sentence when the failure carries no server detail", async () => {
    createMock.mockRejectedValue({ message: "Network Error" });
    renderPanel();
    const user = userEvent.setup();
    await waitFor(() => expect(listMock).toHaveBeenCalled());

    await openDialogAndFillMinimalForm(user);
    await user.click(screen.getByText("Create Rule"));

    await waitFor(() => expect(createMock).toHaveBeenCalled());
    expect(
      await screen.findByText("Failed to create rule. Please check your input."),
    ).toBeTruthy();
  });
});
