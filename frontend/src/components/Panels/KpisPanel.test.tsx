import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ConfirmProvider } from "../Confirm";

const listKpisMock = vi.fn();
const createKpiMock = vi.fn();
const updateKpiMock = vi.fn();
const deleteKpiMock = vi.fn();
const evaluateKpiMock = vi.fn();
const listMeasuresMock = vi.fn();
const listDimensionsMock = vi.fn();
const versionsMock = vi.fn();
const revertMock = vi.fn();
const certifyMock = vi.fn();
const deprecateMock = vi.fn();
const validateExpressionMock = vi.fn();
const evaluateAdhocMock = vi.fn();

vi.mock("../../api/client", () => ({
  kpisApi: {
    list: (...args: unknown[]) => listKpisMock(...args),
    create: (...args: unknown[]) => createKpiMock(...args),
    update: (...args: unknown[]) => updateKpiMock(...args),
    delete: (...args: unknown[]) => deleteKpiMock(...args),
    evaluate: (...args: unknown[]) => evaluateKpiMock(...args),
    versions: (...args: unknown[]) => versionsMock(...args),
    revert: (...args: unknown[]) => revertMock(...args),
    certify: (...args: unknown[]) => certifyMock(...args),
    deprecate: (...args: unknown[]) => deprecateMock(...args),
    validateExpression: (...args: unknown[]) => validateExpressionMock(...args),
    evaluateAdhoc: (...args: unknown[]) => evaluateAdhocMock(...args),
    listUsage: vi.fn().mockResolvedValue([]),
    reportUsage: vi.fn().mockResolvedValue({}),
  },
  measuresApi: {
    list: (...args: unknown[]) => listMeasuresMock(...args),
  },
  dimensionsApi: {
    list: (...args: unknown[]) => listDimensionsMock(...args),
  },
  preferencesApi: {
    get: vi.fn().mockResolvedValue({ favourites: { kpi: [], named_set: [] }, recently_used: { kpi: [], named_set: [] } }),
    toggleFavourite: vi.fn().mockResolvedValue({ favourited: true }),
    recordRecentlyUsed: vi.fn().mockResolvedValue({ recorded: true }),
  },
}));

vi.mock("../../auth/currentUser", () => ({
  canEditModelConfig: () => true,
  isTenantAdmin: () => true,
}));

import KpisPanel, { slugify } from "./KpisPanel";

const SAMPLE_MEASURES = [
  { id: "m1", name: "Revenue", display_name: "Revenue", expression: "SUM(revenue)" },
  { id: "m2", name: "Target", display_name: "Target", expression: "SUM(target)" },
];

const SAMPLE_KPIS = [
  {
    id: "k1",
    name: "revenue_target",
    display_name: "Revenue Target",
    description: "Tracks revenue vs target",
    display_folder: "Financial",
    value_measure_id: null,
    goal_measure_id: null,
    status_expression: null,
    trend_expression: null,
    expression: 'safe_div(measure("Revenue"), measure("Target"))',
    kpi_type: "ratio",
    status_graphic: "Traffic Light",
    trend_graphic: "Standard Arrow",
    weight: 1.0,
    parent_kpi_id: null,
    certification_status: "certified",
    owner_user_id: null,
    created_at: "2026-01-01",
    updated_at: "2026-01-01",
  },
];

const NULL_EVAL = {
  kpi_id: null,
  value: null,
  value_str: null,
  target: null,
  goal: null,
  status: null,
  status_label: null,
  status_color: null,
  trend: null,
  trend_label: null,
  trend_pct: null,
  formatted_value: null,
  formatted_target: null,
  formatted_goal: null,
  formatted_variance: null,
  trend_series: null,
  evaluation_ms: null,
};

const VALID_RESPONSE = {
  valid: true,
  errors: [],
  warnings: [],
  referenced_measures: [],
  referenced_kpis: [],
  referenced_dimensions: [],
  has_time_intelligence: false,
  requires_time_dimension: false,
  detected_agg_mode: null,
  expression_tree: null,
  compiled_sql_preview: null,
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
            <Route path="/p/:projectId/m/:modelId" element={<KpisPanel />} />
          </Routes>
        </MemoryRouter>
      </ConfirmProvider>
    </QueryClientProvider>,
  );
}

type User = ReturnType<typeof userEvent.setup>;

async function pickOption(user: User, comboName: RegExp, optionName: RegExp | string) {
  // MUI Select renders a div[role="combobox"] inside a FormControl.
  // The accessible name may not always match getByRole("combobox"),
  // so we try getByRole first, falling back to getByLabelText.
  let trigger: HTMLElement;
  try {
    trigger = screen.getByRole("combobox", { name: comboName });
  } catch {
    trigger = screen.getByLabelText(comboName);
  }
  await user.click(trigger);
  const listbox = await screen.findByRole("listbox");
  const optionRegex = typeof optionName === "string" ? new RegExp(optionName) : optionName;
  await user.click(within(listbox).getByRole("option", { name: optionRegex }));
}

async function clickNext(user: User) {
  await user.click(screen.getByRole("button", { name: "Next" }));
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------
describe("KPI helpers", () => {
  it("slugify derives a technical name", () => {
    expect(slugify("Revenue vs Target")).toBe("revenue_vs_target");
    expect(slugify("  Margin %  ")).toBe("margin");
    expect(slugify("")).toBe("kpi");
  });
});

describe("KpisPanel", () => {
  beforeEach(() => {
    listKpisMock.mockReset();
    createKpiMock.mockReset();
    updateKpiMock.mockReset();
    deleteKpiMock.mockReset();
    evaluateKpiMock.mockReset();
    listMeasuresMock.mockReset();
    listDimensionsMock.mockReset().mockResolvedValue([]);
    versionsMock.mockReset();
    revertMock.mockReset();
    certifyMock.mockReset();
    deprecateMock.mockReset();
    validateExpressionMock.mockReset().mockResolvedValue(VALID_RESPONSE);
    evaluateAdhocMock.mockReset().mockRejectedValue(new Error("not implemented"));
  });

  it("renders heading", async () => {
    listKpisMock.mockResolvedValue([]);
    listMeasuresMock.mockResolvedValue([]);
    renderPanel();
    expect(screen.getByText("KPIs")).toBeTruthy();
  });

  it("shows empty state when no KPIs", async () => {
    listKpisMock.mockResolvedValue([]);
    listMeasuresMock.mockResolvedValue([]);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText(/No KPIs defined/)).toBeTruthy();
    });
  });

  it("shows KPI cards with certification badge", async () => {
    listKpisMock.mockResolvedValue(SAMPLE_KPIS);
    listMeasuresMock.mockResolvedValue(SAMPLE_MEASURES);
    evaluateKpiMock.mockResolvedValue({
      value: 100, goal: 80, status: 1, trend: 1,
      status_label: "In Target", trend_label: "Improving",
      formatted_value: "100.00", formatted_goal: "80.00",
    });
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("Revenue Target")).toBeTruthy();
    });
    expect(screen.getByText("certified")).toBeTruthy();
    expect(screen.getByText("Traffic Light")).toBeTruthy();
  });

  it("shows live evaluation data on cards", async () => {
    listKpisMock.mockResolvedValue(SAMPLE_KPIS);
    listMeasuresMock.mockResolvedValue(SAMPLE_MEASURES);
    evaluateKpiMock.mockResolvedValue({
      value: 100, goal: 80, status: 1, trend: 1,
      status_label: "In Target", trend_label: "Improving",
      formatted_value: "100.00", formatted_goal: "80.00",
    });
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("Value: 100.00")).toBeTruthy();
      expect(screen.getByText("Goal: 80.00")).toBeTruthy();
      expect(screen.getByText("In Target")).toBeTruthy();
      expect(screen.getByText("Improving")).toBeTruthy();
    });
  });

  it("hides the trend chip when trend is null (Insufficient Data)", async () => {
    listKpisMock.mockResolvedValue(SAMPLE_KPIS);
    listMeasuresMock.mockResolvedValue(SAMPLE_MEASURES);
    evaluateKpiMock.mockResolvedValue({
      value: 100, goal: 80, status: 1, trend: null,
      status_label: "In Target", trend_label: "Insufficient Data",
      status_color: "#388E3C",
      formatted_value: "100.00", formatted_goal: "80.00",
    });
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText("In Target")).toBeTruthy();
    });
    // Newly-created KPIs have no snapshot history; the trend is genuinely
    // unknown, so the chip must be suppressed (consistent with KpiCard).
    expect(screen.queryByText("Insufficient Data")).toBeNull();
  });

  it("resolves the status chip from the authoritative numeric status (F-017-17)", async () => {
    listKpisMock.mockResolvedValue(SAMPLE_KPIS);
    listMeasuresMock.mockResolvedValue(SAMPLE_MEASURES);
    // F-017-17 / F-017-02: the backend now derives status from the matched band
    // (absolute_value via colour), so an "Off Target" red band reports status
    // -1 — the int and the label agree. The chip must trust that numeric status
    // (a failure icon), NOT keyword-match the English label (which broke for
    // custom and non-English labels).
    evaluateKpiMock.mockResolvedValue({
      value: 0.84, goal: null, status: -1, trend: null,
      status_label: "Off Target", trend_label: null,
      status_color: "#D32F2F",
      formatted_value: "0.84",
    });
    renderPanel();
    const chip = (await screen.findByText("Off Target")).closest(
      ".MuiChip-root",
    ) as HTMLElement;
    expect(chip).toBeTruthy();
    // status -1 -> bad -> ErrorIcon.
    expect(within(chip).getByTestId("ErrorIcon")).toBeTruthy();
    expect(within(chip).queryByTestId("CheckCircleIcon")).toBeNull();
  });

  it("does not let an English label override a good numeric status (F-017-17)", async () => {
    listKpisMock.mockResolvedValue(SAMPLE_KPIS);
    listMeasuresMock.mockResolvedValue(SAMPLE_MEASURES);
    // A custom band labelled with the word "target" must not be forced to a
    // failure icon when the backend status is good (1).
    evaluateKpiMock.mockResolvedValue({
      value: 1.2, goal: null, status: 1, trend: null,
      status_label: "Above Target", trend_label: null,
      status_color: "#388E3C",
      formatted_value: "1.2",
    });
    renderPanel();
    const chip = (await screen.findByText("Above Target")).closest(
      ".MuiChip-root",
    ) as HTMLElement;
    expect(chip).toBeTruthy();
    expect(within(chip).getByTestId("CheckCircleIcon")).toBeTruthy();
    expect(within(chip).queryByTestId("ErrorIcon")).toBeNull();
  });

  it("shows deprecated warning banner on deprecated KPI card", async () => {
    listKpisMock.mockResolvedValue([{ ...SAMPLE_KPIS[0], certification_status: "deprecated" }]);
    listMeasuresMock.mockResolvedValue(SAMPLE_MEASURES);
    evaluateKpiMock.mockResolvedValue(NULL_EVAL);
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText(/This KPI is deprecated/)).toBeTruthy();
    });
  });

  it("Add KPI opens the business builder dialog", async () => {
    listKpisMock.mockResolvedValue([]);
    listMeasuresMock.mockResolvedValue(SAMPLE_MEASURES);
    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Add KPI" }));
    await waitFor(() => expect(screen.getByRole("dialog")).toBeTruthy());
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Create KPI" })).toBeTruthy();
  });

  it("Advanced KPI opens the v2 wizard on the Type step", async () => {
    listKpisMock.mockResolvedValue([]);
    listMeasuresMock.mockResolvedValue(SAMPLE_MEASURES);
    renderPanel();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Advanced KPI" }));
    await screen.findByRole("heading", { name: "What kind of KPI?" });
    // Cannot advance until type is chosen.
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("builds a KPI through the v2 wizard and submits", async () => {
    listKpisMock.mockResolvedValue([]);
    listMeasuresMock.mockResolvedValue(SAMPLE_MEASURES);
    createKpiMock.mockResolvedValue({ id: "new" });
    renderPanel();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Advanced KPI" }));
    await screen.findByRole("heading", { name: "What kind of KPI?" });

    // Step 1: pick type and measure using MUI Select (div[role=combobox])
    const comboboxes = screen.getAllByRole("combobox");
    // First combobox is the KPI type selector
    await user.click(comboboxes[0]);
    const typeListbox = await screen.findByRole("listbox");
    await user.click(within(typeListbox).getByText(/Simple measure/));

    // Now a value measure dropdown should appear
    await waitFor(() => expect(screen.getAllByRole("combobox").length).toBeGreaterThan(1));
    const updatedComboboxes = screen.getAllByRole("combobox");
    // The new combobox is the value measure selector
    await user.click(updatedComboboxes[updatedComboboxes.length - 1]);
    const measureListbox = await screen.findByRole("listbox");
    await user.click(within(measureListbox).getByText("Revenue"));

    await waitFor(() => expect(screen.getByRole("button", { name: "Next" })).toBeEnabled());
    await clickNext(user); // -> Target & direction

    // Step 2: leave defaults (no target, higher is better)
    await clickNext(user); // -> Thresholds

    // Step 3: leave defaults
    await clickNext(user); // -> Name & format

    // Step 4: fill in display name
    const nameField = await screen.findByLabelText(/Display name/i);
    await user.type(nameField, "My KPI");

    await clickNext(user); // -> Review

    // Wait for validation to complete
    await waitFor(() => expect(validateExpressionMock).toHaveBeenCalled());

    // Submit
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(createKpiMock).toHaveBeenCalled());

    const payload = createKpiMock.mock.calls[0][2] as Record<string, unknown>;
    expect(payload.kpi_type).toBe("simple_measure");
    expect(payload.name).toBe("my_kpi");
    expect(payload.expression).toBe('measure("Revenue")');
  });

  it("edit opens v2 wizard with pre-filled form", async () => {
    listKpisMock.mockResolvedValue(SAMPLE_KPIS);
    listMeasuresMock.mockResolvedValue(SAMPLE_MEASURES);
    evaluateKpiMock.mockResolvedValue(NULL_EVAL);
    versionsMock.mockResolvedValue([]);
    renderPanel();
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText("Revenue Target")).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "Edit" }));

    // V2 wizard opens with the type step
    await screen.findByRole("heading", { name: "What kind of KPI?" });
  });

  it("delete removes a KPI after confirmation", async () => {
    listKpisMock.mockResolvedValue(SAMPLE_KPIS);
    listMeasuresMock.mockResolvedValue(SAMPLE_MEASURES);
    evaluateKpiMock.mockResolvedValue(NULL_EVAL);
    deletKpiMock_local();
    renderPanel();
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByText("Revenue Target")).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "Delete" }));
    // Confirm dialog
    const confirmBtn = await screen.findByRole("button", { name: /delete/i });
    await user.click(confirmBtn);
    await waitFor(() => expect(deleteKpiMock).toHaveBeenCalledWith("proj-1", "model-1", "k1"));
  });
});

function deletKpiMock_local() {
  deleteKpiMock.mockResolvedValue({});
}
