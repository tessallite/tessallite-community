import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ConfirmProvider } from "../Confirm";

// ---------------------------------------------------------------------------
// API client mocks (create/update capture the submitted payload).
// ---------------------------------------------------------------------------
const createDimMock = vi.fn();
const updateDimMock = vi.fn();
const deleteDimMock = vi.fn();
// The calendar-association PATCH. Held at module scope so a test can make it
// reject with a real server 422 body.
const updateTableMock = vi.fn();

vi.mock("../../api/client", () => ({
  dimensionsApi: {
    create: (...args: unknown[]) => createDimMock(...args),
    update: (...args: unknown[]) => updateDimMock(...args),
    delete: (...args: unknown[]) => deleteDimMock(...args),
  },
  modelTablesApi: {
    update: (...args: unknown[]) => updateTableMock(...args),
  },
}));

vi.mock("../../auth/currentUser", () => ({
  canEditModelConfig: () => true,
}));

vi.mock("../../store/builderStore", () => ({
  useBuilderStore: (selector: (s: { readOnly: boolean }) => unknown) =>
    selector({ readOnly: false }),
}));

// DimensionCalendarAssociation pulls its own hooks; stub it out so the test
// focuses on the dimension editor itself. The stub still exposes the real
// `onCalendarSelect` callback (behind a button that only appears when the
// section is visible) so a test can drive the genuine calendar-binding path
// instead of reaching into component state.
vi.mock("../Builder/DimensionCalendarAssociation", () => ({
  default: ({
    visible,
    onCalendarSelect,
  }: {
    visible?: boolean;
    onCalendarSelect?: (calId: string | null, calType: string | null) => void;
  }) =>
    visible ? (
      <button type="button" onClick={() => onCalendarSelect?.("cal-1", "gregorian")}>
        stub-pick-calendar
      </button>
    ) : null,
}));

// ---------------------------------------------------------------------------
// Hooks mocks — feed controlled tables / attributes / dimensions.
// ---------------------------------------------------------------------------
const SAMPLE_TABLE = {
  id: "t1",
  alias: "customer",
  display_name: "Customer",
  physical_name: "customer",
  source_id: "src1",
  calendar_table_id: null,
};

const SAMPLE_ATTRS = [
  { id: "a1", name: "customer_key", data_type: "integer", is_user_defined: false },
  { id: "a2", name: "customer_name", data_type: "text", is_user_defined: false },
  { id: "a3", name: "fx_calc", data_type: "text", is_user_defined: true },
];

const FLAT_DIM = {
  id: "d1",
  name: "customer",
  display_name: "Customer",
  description: null,
  display_folder: null,
  source_column_id: "a1",
  source_column_name: "customer_key",
  display_column_id: "a2",
  display_column_name: "customer_name",
  data_type: "integer",
  source_table_id: "t1",
  source_table_alias: "customer",
  source_table_display_name: "Customer",
  user_defined_attribute_id: null,
  user_defined_attribute_name: null,
  is_time_dim: false,
  time_grain: null,
  calc_expression: null,
  redundant_partner: null,
};

const useDimensionsMock = vi.fn();

vi.mock("../../api/hooks", () => ({
  useDimensions: (...args: unknown[]) => useDimensionsMock(...args),
  useSources: () => ({ data: [{ id: "src1" }] }),
  useAllModelTables: () => ({ data: [SAMPLE_TABLE] }),
  useModelSourceStatistics: () => ({ columnStatsMap: {} }),
  useTableAttributes: () => ({ data: SAMPLE_ATTRS, isLoading: false }),
  // The edit dialog renders the attribute-relationships section, which reads
  // these hooks. They have no bearing on the display-column picker under test.
  useAttributeRelationships: () => ({ data: [], isLoading: false }),
  useJoins: () => ({ data: [], isLoading: false, isError: false }),
}));

import DimensionsPanel from "./DimensionsPanel";

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ConfirmProvider>
        <MemoryRouter initialEntries={["/p/proj-1/m/model-1"]}>
          <Routes>
            <Route
              path="/p/:projectId/m/:modelId"
              element={<DimensionsPanel />}
            />
          </Routes>
        </MemoryRouter>
      </ConfirmProvider>
    </QueryClientProvider>,
  );
}

// The Model Builder Selects render an MUI combobox whose InputLabel is not
// wired via `for`/`aria-labelledby`, so we locate the combobox through the
// FormControl that contains the visible label text.
function comboByLabel(labelText: string | RegExp): HTMLElement {
  const label = screen.getByText(labelText, {
    selector: "label.MuiInputLabel-root",
  });
  const control = label.closest(".MuiFormControl-root");
  const combo = control?.querySelector('[role="combobox"]');
  if (!combo) throw new Error(`No combobox found for label ${labelText}`);
  return combo as HTMLElement;
}

function queryComboByLabel(labelText: string | RegExp): HTMLElement | null {
  const label = screen.queryByText(labelText, {
    selector: "label.MuiInputLabel-root",
  });
  const control = label?.closest(".MuiFormControl-root");
  return (control?.querySelector('[role="combobox"]') as HTMLElement) ?? null;
}

async function selectOption(
  user: ReturnType<typeof userEvent.setup>,
  labelText: string | RegExp,
  optionName: string | RegExp,
) {
  await user.click(comboByLabel(labelText));
  const listbox = await screen.findByRole("listbox");
  await user.click(within(listbox).getByRole("option", { name: optionName }));
}

describe("DimensionsPanel display-column picker (Bug-5502)", () => {
  beforeEach(() => {
    createDimMock.mockReset().mockResolvedValue({ id: "new" });
    updateDimMock.mockReset().mockResolvedValue({ id: "d1" });
    deleteDimMock.mockReset().mockResolvedValue({});
    updateTableMock.mockReset().mockResolvedValue({});
    useDimensionsMock.mockReset();
  });

  it("renders the display-column picker only after a flat source column is chosen", async () => {
    useDimensionsMock.mockReturnValue({ data: [], isLoading: false });
    renderPanel();
    const user = userEvent.setup();

    await user.click(screen.getByText("Add"));
    await waitFor(() => expect(screen.getByText("Source Column")).toBeTruthy());

    // Not shown before a source column is selected.
    expect(queryComboByLabel("Display column")).toBeNull();

    await selectOption(user, "Table", /customer \(customer\)/);
    await selectOption(user, "Attribute", "customer_key");

    await waitFor(() =>
      expect(queryComboByLabel("Display column")).not.toBeNull(),
    );
  });

  it("populates the picker from an existing dimension on edit", async () => {
    useDimensionsMock.mockReturnValue({ data: [FLAT_DIM], isLoading: false });
    renderPanel();
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText("customer")).toBeTruthy());
    await user.click(screen.getByTestId("EditIcon").closest("button")!);

    await waitFor(() =>
      expect(screen.getByText("Edit", { selector: "h2" })).toBeTruthy(),
    );
    // The picker reflects the saved display_column_name caption.
    await waitFor(() =>
      expect(comboByLabel("Display column").textContent).toContain(
        "customer_name",
      ),
    );
  });

  it("includes display_column_name in the submitted create payload", async () => {
    useDimensionsMock.mockReturnValue({ data: [], isLoading: false });
    renderPanel();
    const user = userEvent.setup();

    await user.click(screen.getByText("Add"));
    await waitFor(() => expect(screen.getByText("Source Column")).toBeTruthy());

    await user.type(screen.getByLabelText(/name \(snake_case\)/i), "customer");
    await selectOption(user, "Table", /customer \(customer\)/);
    await selectOption(user, "Attribute", "customer_key");

    await waitFor(() =>
      expect(queryComboByLabel("Display column")).not.toBeNull(),
    );
    await selectOption(user, "Display column", "customer_name");

    // Submit button lives in the dialog footer (role=button, name "Add").
    const addButtons = screen.getAllByRole("button", { name: "Add" });
    await user.click(addButtons[addButtons.length - 1]);

    await waitFor(() => expect(createDimMock).toHaveBeenCalled());
    const payload = createDimMock.mock.calls[0][2];
    expect(payload.display_column_name).toBe("customer_name");
    expect(payload.source_column_name).toBe("customer_key");
  });

  it("sends display_column_name as null when cleared back to none", async () => {
    useDimensionsMock.mockReturnValue({ data: [FLAT_DIM], isLoading: false });
    renderPanel();
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText("customer")).toBeTruthy());
    await user.click(screen.getByTestId("EditIcon").closest("button")!);

    await waitFor(() =>
      expect(queryComboByLabel("Display column")).not.toBeNull(),
    );
    await selectOption(user, "Display column", "-- none --");

    await user.click(
      screen.getByRole("button", { name: "Save" }),
    );

    await waitFor(() => expect(updateDimMock).toHaveBeenCalled());
    const payload = updateDimMock.mock.calls[0][3];
    expect(payload.display_column_name).toBeNull();
  });
});

describe("DimensionsPanel surfaces the server's calendar rejection reason", () => {
  // The calendar binding is a SECOND, non-atomic PATCH issued after the
  // dimension row is already written. When the server refuses it, the modeller
  // used to see one fixed sentence regardless of the reason — so "that calendar
  // belongs to another model" and a transport failure were indistinguishable,
  // and nothing told the modeller what to change. The body-FK guards answer 422
  // with a structured detail; that message must reach the dialog.
  const SERVER_MESSAGE =
    "calendar_table_id does not reference a calendar table in this model.";

  function bodyFkRejection() {
    return {
      response: {
        status: 422,
        data: {
          detail: {
            error_code: "CALENDAR_TABLE_NOT_IN_MODEL",
            field: "calendar_table_id",
            ids: ["cal-1"],
            message: SERVER_MESSAGE,
          },
        },
      },
    };
  }

  beforeEach(() => {
    createDimMock.mockReset().mockResolvedValue({ id: "new" });
    updateDimMock.mockReset().mockResolvedValue({ id: "d1" });
    deleteDimMock.mockReset().mockResolvedValue({});
    updateTableMock.mockReset().mockResolvedValue({});
    useDimensionsMock.mockReset();
  });

  async function openCreateDialogAndPickCalendar(
    user: ReturnType<typeof userEvent.setup>,
  ) {
    await user.click(screen.getByText("Add"));
    await waitFor(() => expect(screen.getByText("Source Column")).toBeTruthy());
    await user.type(screen.getByLabelText(/^Name/), "order_date");
    await selectOption(user, "Table", /customer \(customer\)/);
    await selectOption(user, "Attribute", "customer_key");
    // Make the calendar-association section visible, then bind a calendar so
    // persistCalendarAssociation actually issues its PATCH.
    await user.click(screen.getByRole("checkbox", { name: /Time dimension/i }));
    await user.click(await screen.findByText("stub-pick-calendar"));
  }

  it("renders the server's 422 message, not just the generic sentence", async () => {
    useDimensionsMock.mockReturnValue({ data: [], isLoading: false });
    updateTableMock.mockRejectedValue(bodyFkRejection());
    renderPanel();
    const user = userEvent.setup();

    await openCreateDialogAndPickCalendar(user);
    // The dialog's submit button reads "Add" when creating (and so does the
    // panel toolbar button), so scope the query to the dialog.
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: /^Add$/ }),
    );

    // The dimension row was written; the calendar PATCH was refused.
    await waitFor(() => expect(createDimMock).toHaveBeenCalled());
    await waitFor(() => expect(updateTableMock).toHaveBeenCalled());

    // The specific server reason is what the modeller must act on.
    expect(await screen.findByText(SERVER_MESSAGE)).toBeTruthy();
    // The dialog stays open — a refused binding is not a completed save.
    expect(screen.getByText("Source Column")).toBeTruthy();
  });

  it("falls back to the generic sentence when the failure carries no message", async () => {
    useDimensionsMock.mockReturnValue({ data: [], isLoading: false });
    updateTableMock.mockRejectedValue({ message: "Network Error" });
    renderPanel();
    const user = userEvent.setup();

    await openCreateDialogAndPickCalendar(user);
    // The dialog's submit button reads "Add" when creating (and so does the
    // panel toolbar button), so scope the query to the dialog.
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: /^Add$/ }),
    );

    await waitFor(() => expect(updateTableMock).toHaveBeenCalled());
    expect(
      await screen.findByText(/linking it to the selected calendar failed/i),
    ).toBeTruthy();
    // No server text to show, so the generic sentence is not duplicated.
    expect(screen.queryByText(SERVER_MESSAGE)).toBeNull();
  });
});
