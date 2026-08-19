import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const listMock = vi.fn();
const autoCreateMock = vi.fn();
const scriptMock = vi.fn();
const bindMock = vi.fn();
const deleteMock = vi.fn();
const typesMock = vi.fn();

vi.mock("../api/client", () => ({
  calendarApi: {
    list: (...args: unknown[]) => listMock(...args),
    autoCreate: (...args: unknown[]) => autoCreateMock(...args),
    script: (...args: unknown[]) => scriptMock(...args),
    bind: (...args: unknown[]) => bindMock(...args),
    delete: (...args: unknown[]) => deleteMock(...args),
    types: (...args: unknown[]) => typesMock(...args),
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
    bindMock.mockReset().mockResolvedValue({ auto_created_aliases: [] });
    scriptMock.mockReset().mockResolvedValue({ ddl: "SELECT 1" });
    deleteMock.mockReset().mockResolvedValue({});
    typesMock.mockReset().mockResolvedValue([
      { calendar_type: "standard", available: true },
      { calendar_type: "fiscal", available: true },
      { calendar_type: "iso_week", available: true },
      { calendar_type: "retail_445", available: true },
      { calendar_type: "hijri", available: false },
      { calendar_type: "thai_buddhist", available: true },
    ]);
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

  // Bug-5920: calendar type availability (e.g. Hijri requiring the
  // optional hijri-converter package) must come from the backend
  // /calendars/types endpoint, not a hardcoded frontend flag.
  it("disables a calendar type option reported unavailable by the backend", async () => {
    const user = userEvent.setup();
    const qc = new QueryClient();

    renderDialog(qc);

    await user.click(screen.getByRole("tab", { name: "Auto-create" }));
    await waitFor(() => expect(typesMock).toHaveBeenCalled());

    // Bug-6014: the calendar-type Select is now wired to its InputLabel via
    // labelId/aria-labelledby, so it is addressable by its accessible name.
    // MUI folds the selected value into the name, hence the /Calendar type/ regex.
    await user.click(screen.getByRole("combobox", { name: /Calendar type/ }));
    const hijriOption = await screen.findByRole("option", { name: /Hijri/ });
    expect(hijriOption).toHaveAttribute("aria-disabled", "true");

    const standardOption = screen.getByRole("option", { name: /Standard/ });
    expect(standardOption).not.toHaveAttribute("aria-disabled", "true");
  });

  it("enables a calendar type option once the backend reports it available", async () => {
    typesMock.mockReset().mockResolvedValue([
      { calendar_type: "standard", available: true },
      { calendar_type: "fiscal", available: true },
      { calendar_type: "iso_week", available: true },
      { calendar_type: "retail_445", available: true },
      { calendar_type: "hijri", available: true },
      { calendar_type: "thai_buddhist", available: true },
    ]);
    const user = userEvent.setup();
    const qc = new QueryClient();

    renderDialog(qc);

    await user.click(screen.getByRole("tab", { name: "Auto-create" }));
    await waitFor(() => expect(typesMock).toHaveBeenCalled());

    // Bug-6014: the calendar-type Select is now addressable by its accessible
    // name (InputLabel wired via labelId/aria-labelledby). It is the only
    // combobox rendered while calendarType === "standard" (the fiscal-month
    // selector only appears for calendarType === "fiscal").
    await user.click(screen.getByRole("combobox", { name: /Calendar type/ }));
    const hijriOption = await screen.findByRole("option", { name: /Hijri/ });
    expect(hijriOption).not.toHaveAttribute("aria-disabled", "true");
  });

  // Bug-6014 regression guard: the calendar-type Select must expose an
  // accessible name via aria-labelledby so screen readers announce the field
  // and getByLabelText / getByRole(name) can find it. Before the fix the
  // rendered combobox had no aria-labelledby and both queries failed.
  it("wires the calendar-type Select label for accessibility", async () => {
    const user = userEvent.setup();
    const qc = new QueryClient();

    renderDialog(qc);

    await user.click(screen.getByRole("tab", { name: "Auto-create" }));
    await waitFor(() => expect(typesMock).toHaveBeenCalled());

    const combo = await screen.findByRole("combobox", { name: /Calendar type/ });
    expect(combo).toHaveAttribute("aria-labelledby");
    const labelledBy = combo.getAttribute("aria-labelledby") ?? "";
    expect(labelledBy).toContain("calendar-type-label");
  });

  it("binds existing calendars with explicit source column names only", async () => {
    const user = userEvent.setup();
    const qc = new QueryClient();

    renderDialog(qc);

    await user.click(screen.getByRole("tab", { name: "Bind existing" }));
    await user.clear(screen.getByLabelText("Physical table name"));
    await user.type(screen.getByLabelText("Physical table name"), "inventory_analytics.dim_date");
    await user.type(screen.getByLabelText("Date column"), "full_date");
    await user.type(screen.getByLabelText("Year column"), "year");
    await user.click(screen.getByRole("button", { name: "Bind" }));

    await waitFor(() => expect(bindMock).toHaveBeenCalled());
    const payload = bindMock.mock.calls[0][3];
    expect(payload).toMatchObject({
      table_name: "inventory_analytics.dim_date",
      date_column: "full_date",
      year_column: "year",
    });
    expect(payload).not.toHaveProperty("month_column");
    expect(payload).not.toHaveProperty("week_column");
  });
});
