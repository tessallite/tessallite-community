/**
 * KpiWizard save-gate tests.
 *
 * A time-intelligence KPI cannot evaluate without a time dimension; the
 * wizard must block save (needsTimeDimension) and show the warning until
 * one is picked. Backend fail-closed behaviour is covered server-side;
 * these tests cover the UI gate the user actually touches.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const validateExpressionMock = vi.fn();
const versionsMock = vi.fn();
const evaluateAdhocMock = vi.fn();

vi.mock("../../api/client", () => ({
  kpisApi: {
    validateExpression: (...args: unknown[]) => validateExpressionMock(...args),
    versions: (...args: unknown[]) => versionsMock(...args),
    evaluateAdhoc: (...args: unknown[]) => evaluateAdhocMock(...args),
  },
}));

// Bug-5517: KpiPreviewCard debounces a live evaluate-adhoc call behind a real
// 600ms setTimeout.  Under full-suite parallel load that timer can fire after
// test cleanup, triggering a setState-after-unmount warning and intermittent
// flakes.  No test in this file asserts preview output, so we stub it to a
// no-op — same pattern as KpisPanel.test.tsx (Bug-5486).
vi.mock("./KpiPreviewCard", () => ({
  __esModule: true,
  default: () => null,
}));

import KpiWizard from "./KpiWizard";

const TI_VALIDATION = {
  valid: true,
  errors: [],
  warnings: [],
  referenced_measures: ["Revenue"],
  referenced_kpis: [],
  referenced_dimensions: [],
  has_time_intelligence: true,
  requires_time_dimension: true,
  detected_agg_mode: null,
  expression_tree: null,
  compiled_sql_preview: null,
};

const MEASURES = [
  { id: "m1", name: "Revenue", display_name: "Revenue", expression: "SUM(revenue)" },
];

const TIME_DIMENSIONS = [
  { id: "d1", name: "order_date", display_name: "Order Date", is_time_dim: true },
];

function tiKpi(overrides: Record<string, unknown> = {}) {
  return {
    id: "k1",
    name: "revenue_growth",
    display_name: "Revenue Growth",
    description: "",
    display_folder: "",
    value_measure_id: null,
    goal_measure_id: null,
    status_expression: null,
    trend_expression: null,
    expression: 'pct_change(measure("Revenue"), "year")',
    kpi_type: "growth_rate",
    status_graphic: "Traffic Light",
    trend_graphic: "Standard Arrow",
    weight: null,
    parent_kpi_id: null,
    certification_status: "draft",
    direction: "higher_is_better",
    target_type: null,
    target_value: null,
    target_measure_id: null,
    target_expression: null,
    target_period: null,
    presentation_type: null,
    presentation_meta: null,
    calc_agg_mode: "automatic",
    inner_agg: null,
    inner_grain: null,
    outer_agg: null,
    trend_period: "month",
    trend_threshold: 0.01,
    trend_sparkline_periods: 12,
    format_token: null,
    format_custom: null,
    unit_label: null,
    null_display_value: "N/A",
    indicator_type: "none",
    time_dimension_id: null,
    snapshot_frequency: null,
    snapshot_retention: null,
    owner_user_id: null,
    ...overrides,
  };
}

function renderWizard(editKpi: ReturnType<typeof tiKpi>) {
  return render(
    <KpiWizard
      open
      onClose={vi.fn()}
      onSaved={vi.fn()}
      projectId="p1"
      modelId="mod1"
      measures={MEASURES as never}
      dimensions={TIME_DIMENSIONS as never}
      kpis={[]}
      editKpi={editKpi as never}
      isAdmin
      canEdit
    />,
  );
}

async function goToReviewStep() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /review/i }));
  // Validation fires when the review step mounts
  await waitFor(() => expect(validateExpressionMock).toHaveBeenCalled());
}

describe("KpiWizard time-dimension save gate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    versionsMock.mockResolvedValue([]);
    evaluateAdhocMock.mockResolvedValue({ value: 0.1, formatted_value: "0.10" });
    validateExpressionMock.mockResolvedValue(TI_VALIDATION);
  });

  it("blocks save and shows the warning when a TI KPI has no time dimension", async () => {
    renderWizard(tiKpi({ time_dimension_id: null }));
    await goToReviewStep();

    // Warning message shown (en.json value for kpis.wizard.v2.timeDimensionRequired)
    expect(
      await screen.findByText(
        "This KPI uses time intelligence. Pick a time dimension in the Display step before saving.",
      ),
    ).toBeInTheDocument();

    // Save is gated
    const saveButton = screen.getByRole("button", { name: "Save" });
    expect(saveButton).toBeDisabled();
  });

  it("enables save and hides the warning once a time dimension is set", async () => {
    renderWizard(tiKpi({ time_dimension_id: "d1" }));
    await goToReviewStep();

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save" })).toBeEnabled(),
    );
    expect(
      screen.queryByText(
        "This KPI uses time intelligence. Pick a time dimension in the Display step before saving.",
      ),
    ).not.toBeInTheDocument();
  });
});
