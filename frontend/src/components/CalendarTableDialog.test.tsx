import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const listMock = vi.fn();
const autoCreateMock = vi.fn();
const scriptMock = vi.fn();
const bindMock = vi.fn();
const deleteMock = vi.fn();

vi.mock("../api/client", () => ({
  calendarApi: {
    list: (...args: unknown[]) => listMock(...args),
    autoCreate: (...args: unknown[]) => autoCreateMock(...args),
    script: (...args: unknown[]) => scriptMock(...args),
    bind: (...args: unknown[]) => bindMock(...args),
    delete: (...args: unknown[]) => deleteMock(...args),
  },
}));

import CalendarTableDialog from "./CalendarTableDialog";

function renderDialog(qc: QueryClient) {
  return render(
    <QueryClientProvider client={qc}>
      <CalendarTableDialog
        open
        onClose={() => {}}
        projectId="p1"
        modelId="m1"
        sourceId="s1"
        dialect="postgres"
      />
    </QueryClientProvider>,
  );
}

describe("CalendarTableDialog", () => {
  beforeEach(() => {
    listMock.mockReset().mockResolvedValue([]);
    autoCreateMock.mockReset().mockResolvedValue({ auto_created_aliases: ["x_calendar"] });
  });

  // Regression guard: creating a calendar provisions both an alias ModelTable
  // (canvas node) and a join (canvas edge) on the backend. The canvas reads
  // edges from the ["joins", projectId, modelId] query, so that cache MUST be
  // invalidated after auto-create — otherwise the new join's edge stays hidden
  // until the model is closed and reopened.
  it("invalidates the joins query after auto-creating a calendar", async () => {
    const user = userEvent.setup();
    const qc = new QueryClient();
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    renderDialog(qc);

    await user.click(screen.getByRole("tab", { name: "Auto-create" }));
    await user.click(screen.getByRole("button", { name: "Generate" }));

    await waitFor(() => expect(autoCreateMock).toHaveBeenCalled());
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: ["joins", "p1", "m1"],
      }),
    );
  });
});
