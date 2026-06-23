import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { BusinessBuilderForm } from "./businessDefinition";
import {
  createDefaultForm,
  formToDefinition,
  definitionToForm,
  FORMULA_FAMILIES,
  TIME_WINDOW_PRESETS,
  TIME_CALC_OPTIONS,
  PERIOD_GRAINS,
} from "./businessDefinition";

const evaluateAdhocMock = vi.fn();
const createKpiMock = vi.fn();
const updateKpiMock = vi.fn();
const queryExecMock = vi.fn();

vi.mock("../../api/client", () => ({
  kpisApi: {
    evaluateAdhoc: (...args: unknown[]) => evaluateAdhocMock(...args),
    create: (...args: unknown[]) => createKpiMock(...args),
    update: (...args: unknown[]) => updateKpiMock(...args),
  },
  queryRouterApiClient: {
    execute: (...args: unknown[]) => queryExecMock(...args),
  },
}));

import { FormulaPicker } from "./FormulaPicker";
import { KpiTimeWindowPicker } from "./KpiTimeWindowPicker";
import { KpiFilterBar } from "./KpiFilterBar";
import { KpiBusinessBuilderDialog } from "./KpiBusinessBuilderDialog";

const MEASURES = [
  { id: "m1", name: "revenue", display_name: "Revenue", expression: "SUM(revenue)" },
  { id: "m2", name: "cost", display_name: "Cost", expression: "SUM(cost)" },
  { id: "m3", name: "target", display_name: "Target", expression: "SUM(target)" },
];

const DIMENSIONS = [
  {
    id: "d1",
    name: "region",
    display_name: "Region",
    is_time_dim: false,
    data_type: "VARCHAR",
    source_table_alias: "customers",
  },
  {
    id: "d2",
    name: "order_date",
    display_name: "Order Date",
    is_time_dim: true,
    data_type: "DATE",
    source_table_alias: "orders",
  },
  {
    id: "d3",
    name: "category",
    display_name: "Category",
    is_time_dim: false,
    data_type: "VARCHAR",
    source_table_alias: "products",
  },
];

// ---------------------------------------------------------------------------
// Pure unit tests
// ---------------------------------------------------------------------------
describe("businessDefinition helpers", () => {
  it("createDefaultForm returns empty form with single_measure type", () => {
    const form = createDefaultForm();
    expect(form.name).toBe("");
    expect(form.formula.type).toBe("single_measure");
    expect(form.filters).toEqual([]);
    expect(form.target).toBeNull();
    expect(form.direction).toBe("higher_is_better");
  });

  it("formToDefinition omits empty optional sections", () => {
    const form = createDefaultForm();
    const defn = formToDefinition(form);
    expect(defn.builder).toBe("business_kpi");
    expect(defn.version).toBe(1);
    expect(defn.filters).toBeUndefined();
    expect(defn.time_window).toBeUndefined();
    expect(defn.target).toBeUndefined();
  });

  it("formToDefinition includes time_window when preset is set", () => {
    const form: BusinessBuilderForm = {
      ...createDefaultForm(),
      timeWindow: { preset: "this_month" },
    };
    const defn = formToDefinition(form);
    expect(defn.time_window).toEqual({ preset: "this_month" });
  });

  it("definitionToForm round-trips formToDefinition", () => {
    const form: BusinessBuilderForm = {
      ...createDefaultForm(),
      formula: { type: "ratio", numerator_measure_id: "m1", denominator_measure_id: "m2" },
      timeWindow: { preset: "last_30_days" },
      filters: [{ dimension_id: "d1", operator: "eq", values: ["US"] }],
      direction: "lower_is_better",
    };
    const defn = formToDefinition(form);
    const restored = definitionToForm(defn);
    expect(restored.formula.type).toBe("ratio");
    expect(restored.formula.numerator_measure_id).toBe("m1");
    expect(restored.timeWindow.preset).toBe("last_30_days");
    expect(restored.filters).toHaveLength(1);
    expect(restored.direction).toBe("lower_is_better");
  });

  it("all 11 formula families are present", () => {
    expect(FORMULA_FAMILIES).toHaveLength(11);
    const types = FORMULA_FAMILIES.map((f) => f.type);
    expect(types).toContain("single_measure");
    expect(types).toContain("ratio");
    expect(types).toContain("count_records");
    expect(types).toContain("count_distinct");
    expect(types).toContain("moving_average");
    expect(types).toContain("compare_periods");
    expect(types).toContain("compare_measures");
    expect(types).toContain("target_comparison");
    expect(types).toContain("exception_sla");
    expect(types).toContain("share_rank");
    expect(types).toContain("composite_score");
  });

  it("all 11 time calculation types are present", () => {
    expect(TIME_CALC_OPTIONS).toHaveLength(11);
    const types = TIME_CALC_OPTIONS.map((o) => o.type);
    expect(types).toContain("current");
    expect(types).toContain("lead");
    expect(types).toContain("cagr");
  });

  it("time window presets total 21", () => {
    expect(TIME_WINDOW_PRESETS).toHaveLength(21);
  });
});

// ---------------------------------------------------------------------------
// FormulaPicker rendering
// ---------------------------------------------------------------------------
describe("FormulaPicker", () => {
  it("renders with no model data and defaults to single_measure", () => {
    const onChange = vi.fn();
    render(
      <FormulaPicker
        formula={{ type: "single_measure" }}
        onChange={onChange}
        measures={[]}
        dimensions={[]}
      />,
    );
    expect(screen.getAllByText("Formula").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole("group")).toBeInTheDocument();
    expect(screen.getAllByText("Measure").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Compare")).toBeInTheDocument();
    expect(screen.getByText("Analyze")).toBeInTheDocument();
  });

  it("single_measure formula shows measure picker and aggregation", async () => {
    const onChange = vi.fn();
    render(
      <FormulaPicker
        formula={{ type: "single_measure", measure_id: "m1", aggregation: "sum" }}
        onChange={onChange}
        measures={MEASURES as never[]}
        dimensions={DIMENSIONS as never[]}
      />,
    );
    expect(screen.getByDisplayValue("Revenue")).toBeTruthy();
  });

  it("ratio formula shows numerator and denominator pickers", () => {
    const onChange = vi.fn();
    render(
      <FormulaPicker
        formula={{ type: "ratio", numerator_measure_id: "m1", denominator_measure_id: "m2" }}
        onChange={onChange}
        measures={MEASURES as never[]}
        dimensions={DIMENSIONS as never[]}
      />,
    );
    expect(screen.getByDisplayValue("Revenue")).toBeTruthy();
    expect(screen.getByDisplayValue("Cost")).toBeTruthy();
  });

  it("count_distinct formula shows dimension picker", () => {
    const onChange = vi.fn();
    render(
      <FormulaPicker
        formula={{ type: "count_distinct", dimension_id: "d1" }}
        onChange={onChange}
        measures={MEASURES as never[]}
        dimensions={DIMENSIONS as never[]}
      />,
    );
    expect(screen.getByDisplayValue("Region")).toBeTruthy();
  });

  it("moving_average formula shows window size and grain controls", () => {
    const onChange = vi.fn();
    render(
      <FormulaPicker
        formula={{ type: "moving_average", measure_id: "m1", window_size: 3, grain: "month" }}
        onChange={onChange}
        measures={MEASURES as never[]}
        dimensions={DIMENSIONS as never[]}
      />,
    );
    expect(screen.getByDisplayValue("3")).toBeTruthy();
  });

  it("shows naive-average warning for percentage measure with moving_average formula", () => {
    const pctMeasures = [
      ...MEASURES,
      { id: "m_pct", name: "margin_pct", display_name: "Margin %", expression: "x", format: "percent", is_additive: false },
    ];
    render(
      <FormulaPicker
        formula={{ type: "moving_average", measure_id: "m_pct", window_size: 3, grain: "month" }}
        onChange={vi.fn()}
        measures={pctMeasures as never[]}
        dimensions={DIMENSIONS as never[]}
      />,
    );
    expect(screen.getByText(/non-additive value/)).toBeInTheDocument();
  });

  it("shows naive-average warning for single_measure with moving_average time calculation", () => {
    const pctMeasures = [
      ...MEASURES,
      { id: "m_pct", name: "margin_pct", display_name: "Margin %", expression: "x", format: "percent_2dp", is_additive: false },
    ];
    render(
      <FormulaPicker
        formula={{ type: "single_measure", measure_id: "m_pct", time_calculation: { type: "moving_average", grain: "month", periods: 3 } }}
        onChange={vi.fn()}
        measures={pctMeasures as never[]}
        dimensions={DIMENSIONS as never[]}
      />,
    );
    expect(screen.getByText(/non-additive value/)).toBeInTheDocument();
  });

  it("does not show naive-average warning for additive measure", () => {
    render(
      <FormulaPicker
        formula={{ type: "moving_average", measure_id: "m1", window_size: 3, grain: "month" }}
        onChange={vi.fn()}
        measures={MEASURES as never[]}
        dimensions={DIMENSIONS as never[]}
      />,
    );
    expect(screen.queryByText(/non-additive value/)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Time window serialization
// ---------------------------------------------------------------------------
describe("KpiTimeWindowPicker", () => {
  it("renders preset selector with time dimension picker", async () => {
    const onChange = vi.fn();
    render(
      <KpiTimeWindowPicker
        timeWindow={{}}
        onChange={onChange}
        timeDimensions={DIMENSIONS.filter((d) => d.is_time_dim) as never[]}
      />,
    );
    expect(screen.getAllByText("Time window").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Time dimension").length).toBeGreaterThanOrEqual(1);
  });

  it("custom_range shows date inputs", () => {
    const onChange = vi.fn();
    render(
      <KpiTimeWindowPicker
        timeWindow={{ preset: "custom_range", start: "2026-01-01", end: "2026-06-01" }}
        onChange={onChange}
        timeDimensions={DIMENSIONS.filter((d) => d.is_time_dim) as never[]}
      />,
    );
    expect(screen.getByDisplayValue("2026-01-01")).toBeTruthy();
    expect(screen.getByDisplayValue("2026-06-01")).toBeTruthy();
  });

  it("selecting a preset shows include_incomplete_period checkbox", () => {
    const onChange = vi.fn();
    render(
      <KpiTimeWindowPicker
        timeWindow={{ preset: "this_month" }}
        onChange={onChange}
        timeDimensions={DIMENSIONS.filter((d) => d.is_time_dim) as never[]}
      />,
    );
    expect(screen.getByText("Include incomplete (current) period")).toBeTruthy();
  });

  it("time window serializes preset correctly in definition", () => {
    const form: BusinessBuilderForm = {
      ...createDefaultForm(),
      timeWindow: { preset: "last_90_days", dimension_id: "d2" },
    };
    const defn = formToDefinition(form);
    expect(defn.time_window?.preset).toBe("last_90_days");
    expect(defn.time_window?.dimension_id).toBe("d2");
  });
});

// ---------------------------------------------------------------------------
// Time-variant controls serialization
// ---------------------------------------------------------------------------
describe("Time-variant controls", () => {
  it("time_calculation serializes into formula", () => {
    const form: BusinessBuilderForm = {
      ...createDefaultForm(),
      formula: {
        type: "single_measure",
        measure_id: "m1",
        aggregation: "sum",
        time_calculation: { type: "trailing_sum", periods: 6, grain: "month" },
      },
    };
    const defn = formToDefinition(form);
    expect(defn.formula.time_calculation?.type).toBe("trailing_sum");
    expect(defn.formula.time_calculation?.periods).toBe(6);
    expect(defn.formula.time_calculation?.grain).toBe("month");
  });

  it("round-trips time_calculation through definitionToForm", () => {
    const form: BusinessBuilderForm = {
      ...createDefaultForm(),
      formula: {
        type: "ratio",
        numerator_measure_id: "m1",
        denominator_measure_id: "m2",
        time_calculation: { type: "yoy_growth_pct" },
      },
    };
    const defn = formToDefinition(form);
    const restored = definitionToForm(defn);
    expect(restored.formula.time_calculation?.type).toBe("yoy_growth_pct");
  });
});

// ---------------------------------------------------------------------------
// Filter chips
// ---------------------------------------------------------------------------
describe("KpiFilterBar", () => {
  beforeEach(() => {
    queryExecMock.mockReset().mockResolvedValue({ rows: [] });
  });

  it("renders filter chips for existing filters", () => {
    const onChange = vi.fn();
    render(
      <KpiFilterBar
        filters={[
          { dimension_id: "d1", operator: "eq", values: ["US"], label: "Region" },
          { dimension_id: "d3", operator: "in", values: ["A", "B", "C"], label: "Category" },
        ]}
        onChange={onChange}
        dimensions={DIMENSIONS as never[]}
        projectId="p1"
        modelId="m1"
      />,
    );
    expect(screen.getByText(/Region.*US/)).toBeTruthy();
    expect(screen.getByText(/Category.*A, B/)).toBeTruthy();
  });

  it("shows add filter button", () => {
    const onChange = vi.fn();
    render(
      <KpiFilterBar
        filters={[]}
        onChange={onChange}
        dimensions={DIMENSIONS as never[]}
        projectId="p1"
        modelId="m1"
      />,
    );
    expect(screen.getByRole("button", { name: /Add filter/i })).toBeTruthy();
  });

  it("adds a filter when dimension is selected from menu", async () => {
    const onChange = vi.fn();
    render(
      <KpiFilterBar
        filters={[]}
        onChange={onChange}
        dimensions={DIMENSIONS as never[]}
        projectId="p1"
        modelId="m1"
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Add filter/i }));
    const menu = await screen.findByRole("menu");
    await user.click(within(menu).getByText("Region"));
    expect(onChange).toHaveBeenCalledWith([
      expect.objectContaining({ dimension_id: "d1", operator: "eq" }),
    ]);
  });

  it("removes a filter chip via delete", async () => {
    const onChange = vi.fn();
    render(
      <KpiFilterBar
        filters={[
          { dimension_id: "d1", operator: "eq", values: ["US"], label: "Region" },
        ]}
        onChange={onChange}
        dimensions={DIMENSIONS as never[]}
        projectId="p1"
        modelId="m1"
      />,
    );
    const user = userEvent.setup();
    const chip = screen.getByText(/Region.*US/).closest(".MuiChip-root")!;
    const deleteBtn = within(chip as HTMLElement).getByTestId("CancelIcon");
    await user.click(deleteBtn);
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it("categorical filter autocomplete fetches distinct values", async () => {
    queryExecMock.mockResolvedValue({
      rows: [{ region: "US" }, { region: "UK" }, { region: "DE" }],
    });
    const onChange = vi.fn();
    render(
      <KpiFilterBar
        filters={[
          { dimension_id: "d1", operator: "in", values: ["US"], label: "Region" },
        ]}
        onChange={onChange}
        dimensions={DIMENSIONS as never[]}
        projectId="p1"
        modelId="m1"
      />,
    );
    const user = userEvent.setup();
    const chip = screen.getByText(/Region.*US/);
    await user.click(chip);
    await waitFor(() => {
      expect(queryExecMock).toHaveBeenCalledWith(
        expect.objectContaining({
          model_id: "m1",
          raw_query: expect.stringContaining('SELECT DISTINCT "region"'),
        }),
      );
    });
  });

  it("date dimension defaults to between operator", async () => {
    const onChange = vi.fn();
    render(
      <KpiFilterBar
        filters={[]}
        onChange={onChange}
        dimensions={DIMENSIONS as never[]}
        projectId="p1"
        modelId="m1"
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Add filter/i }));
    const menu = await screen.findByRole("menu");
    await user.click(within(menu).getByText("Order Date"));
    expect(onChange).toHaveBeenCalledWith([
      expect.objectContaining({ dimension_id: "d2", operator: "between" }),
    ]);
  });

  it("filter serialization round-trips through form/definition", () => {
    const form: BusinessBuilderForm = {
      ...createDefaultForm(),
      filters: [
        { dimension_id: "d1", operator: "in", values: ["US", "UK", "DE"] },
        { dimension_id: "d2", operator: "between", values: ["2026-01-01", "2026-06-01"] },
      ],
    };
    const defn = formToDefinition(form);
    expect(defn.filters).toHaveLength(2);
    expect(defn.filters![0].operator).toBe("in");
    expect(defn.filters![0].values).toEqual(["US", "UK", "DE"]);
    expect(defn.filters![1].operator).toBe("between");

    const restored = definitionToForm(defn);
    expect(restored.filters).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// Parameterized filters
// ---------------------------------------------------------------------------
describe("Parameterized filters", () => {
  it("parameter mode fields serialized into filter", () => {
    const form: BusinessBuilderForm = {
      ...createDefaultForm(),
      filters: [
        {
          dimension_id: "d1",
          operator: "eq",
          values: [],
          mode: "parameter",
          parameter_name: "region_param",
          default_value: "US",
        },
      ],
    };
    const defn = formToDefinition(form);
    expect(defn.filters![0].mode).toBe("parameter");
    expect(defn.filters![0].parameter_name).toBe("region_param");
    expect(defn.filters![0].default_value).toBe("US");
  });
});

// ---------------------------------------------------------------------------
// Relative date filters
// ---------------------------------------------------------------------------
describe("Relative date filters", () => {
  it("relative mode fields serialized into filter", () => {
    const form: BusinessBuilderForm = {
      ...createDefaultForm(),
      filters: [
        {
          dimension_id: "d2",
          operator: "between",
          mode: "relative",
          values: ["last_30_days"],
        },
      ],
    };
    const defn = formToDefinition(form);
    expect(defn.filters![0].mode).toBe("relative");
    expect(defn.filters![0].values).toEqual(["last_30_days"]);
  });

  it("relative filter round-trips through form/definition", () => {
    const form: BusinessBuilderForm = {
      ...createDefaultForm(),
      filters: [
        {
          dimension_id: "d2",
          operator: "between",
          mode: "relative",
          values: ["last_month"],
        },
      ],
    };
    const defn = formToDefinition(form);
    const restored = definitionToForm(defn);
    expect(restored.filters[0].mode).toBe("relative");
    expect(restored.filters[0].values).toEqual(["last_month"]);
  });
});

// ---------------------------------------------------------------------------
// Dialog-level integration
// ---------------------------------------------------------------------------
describe("KpiBusinessBuilderDialog", () => {
  beforeEach(() => {
    evaluateAdhocMock.mockReset().mockRejectedValue(new Error("not wired"));
    createKpiMock.mockReset().mockResolvedValue({ id: "new" });
    updateKpiMock.mockReset().mockResolvedValue({ id: "k1" });
    queryExecMock.mockReset().mockResolvedValue({ rows: [] });
  });

  it("renders with no model data and shows formula picker", () => {
    render(
      <KpiBusinessBuilderDialog
        open
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projectId="p1"
        modelId="m1"
        measures={[]}
        dimensions={[]}
      />,
    );
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getAllByText(/Create KPI/i).length).toBeGreaterThanOrEqual(1);
    expect(within(dialog).getByText("Formula")).toBeTruthy();
  });

  it("single-measure KPI can be created", async () => {
    render(
      <KpiBusinessBuilderDialog
        open
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projectId="p1"
        modelId="m1"
        measures={MEASURES as never[]}
        dimensions={DIMENSIONS as never[]}
      />,
    );
    const user = userEvent.setup();
    const dialog = screen.getByRole("dialog");

    const nameInput = within(dialog).getByLabelText(/KPI name/i);
    await user.type(nameInput, "Test Revenue KPI");

    const createBtn = within(dialog).getByRole("button", { name: /Create KPI/i });
    await user.click(createBtn);

    await waitFor(() => expect(createKpiMock).toHaveBeenCalledTimes(1));
    const payload = createKpiMock.mock.calls[0][2];
    expect(payload.name).toBe("Test Revenue KPI");
    expect(payload.business_definition.formula.type).toBe("single_measure");
  });

  it("target and direction controls are saved from the business builder", async () => {
    render(
      <KpiBusinessBuilderDialog
        open
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projectId="p1"
        modelId="m1"
        measures={MEASURES as never[]}
        dimensions={DIMENSIONS as never[]}
      />,
    );
    const user = userEvent.setup();
    const dialog = screen.getByRole("dialog");

    await user.type(within(dialog).getByLabelText(/KPI name/i), "Targeted KPI");

    await user.click(within(dialog).getByLabelText("Direction"));
    await user.click(await screen.findByRole("option", { name: "Lower is better" }));

    await user.click(within(dialog).getByLabelText("Target type"));
    await user.click(await screen.findByRole("option", { name: "Static value" }));
    await user.type(within(dialog).getByLabelText("Target value"), "10");

    await user.click(within(dialog).getByRole("button", { name: /Create KPI/i }));

    await waitFor(() => expect(createKpiMock).toHaveBeenCalledTimes(1));
    const payload = createKpiMock.mock.calls[0][2];
    expect(payload.direction).toBe("lower_is_better");
    expect(payload.target_type).toBe("static");
    expect(payload.target_value).toBe(10);
    expect(payload.business_definition.direction).toBe("lower_is_better");
    expect(payload.business_definition.target).toEqual({ type: "static", value: 10 });
  });

  it("escape hatch button opens advanced warning", async () => {
    const onOpenAdvanced = vi.fn();
    render(
      <KpiBusinessBuilderDialog
        open
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projectId="p1"
        modelId="m1"
        measures={MEASURES as never[]}
        dimensions={DIMENSIONS as never[]}
        onOpenAdvanced={onOpenAdvanced}
      />,
    );
    const user = userEvent.setup();
    const dialog = screen.getByRole("dialog");

    const advancedBtn = within(dialog).getByRole("button", { name: /Open advanced editor/i });
    await user.click(advancedBtn);

    const warning = await within(dialog).findByText(/may detach this KPI/);
    expect(warning).toBeTruthy();

    const continueBtn = within(dialog).getByRole("button", { name: /Continue/i });
    await user.click(continueBtn);

    expect(onOpenAdvanced).toHaveBeenCalledWith(null);
  });

  it("escape hatch is hidden when onOpenAdvanced is not provided", () => {
    render(
      <KpiBusinessBuilderDialog
        open
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projectId="p1"
        modelId="m1"
        measures={[]}
        dimensions={[]}
      />,
    );
    expect(screen.queryByRole("button", { name: /Open advanced editor/i })).toBeNull();
  });

  it("renders visual display type picker", () => {
    render(
      <KpiBusinessBuilderDialog
        open
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projectId="p1"
        modelId="m1"
        measures={[]}
        dimensions={[]}
      />,
    );
    const dialog = screen.getByRole("dialog");
    const comboboxes = within(dialog).getAllByRole("combobox");
    expect(comboboxes.length).toBeGreaterThanOrEqual(2);
  });

  it("default form includes presentationType and presentationMeta", () => {
    const form = createDefaultForm();
    expect(form.presentationType).toBe("");
    expect(form.presentationMeta).toBeNull();
  });

  it("save is disabled when name is empty", () => {
    render(
      <KpiBusinessBuilderDialog
        open
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projectId="p1"
        modelId="m1"
        measures={[]}
        dimensions={[]}
      />,
    );
    const dialog = screen.getByRole("dialog");
    const createBtn = within(dialog).getByRole("button", { name: /Create KPI/i });
    expect(createBtn).toBeDisabled();
  });
});
