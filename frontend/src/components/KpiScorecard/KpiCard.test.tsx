import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import KpiCard from "./KpiCard";
import type { Kpi, KpiEvaluateResponse } from "../../api/types";

vi.mock("../../i18n", () => ({
  useT: () => (key: string) => key,
}));

function makeKpi(overrides: Partial<Kpi> = {}): Kpi {
  return {
    id: "kpi-1",
    name: "revenue_growth",
    display_name: "Revenue Growth",
    description: "Year-over-year revenue growth rate",
    certification_status: null,
    display_folder: null,
    ...overrides,
  } as Kpi;
}

function makeEval(overrides: Partial<KpiEvaluateResponse> = {}): KpiEvaluateResponse {
  return {
    kpi_id: "kpi-1",
    value: 125000,
    value_str: null,
    target: 100000,
    status: 1,
    status_label: "On Track",
    status_color: "#2e7d32",
    trend: 1,
    trend_label: "Improving",
    trend_pct: 0.125, // API contract: a fraction (0.125 renders as +12.5%)
    // F-017-02: direction-normalised percent (equals trend_pct for a
    // higher-is-better KPI); the chip renders this so sign agrees with colour.
    trend_pct_normalised: 0.125,
    formatted_value: "$125,000",
    formatted_target: "$100,000",
    formatted_variance: "+$25,000",
    trend_series: [
      { period: "Q1", value: 95000 },
      { period: "Q2", value: 105000 },
      { period: "Q3", value: 115000 },
      { period: "Q4", value: 125000 },
    ],
    evaluation_ms: 42,
    goal: null,
    formatted_goal: null,
    ...overrides,
  };
}

describe("KpiCard", () => {
  it("renders display name", () => {
    render(<KpiCard kpi={makeKpi()} evalData={makeEval()} loading={false} />);
    expect(screen.getByText("Revenue Growth")).toBeTruthy();
  });

  it("renders description", () => {
    render(<KpiCard kpi={makeKpi()} evalData={makeEval()} loading={false} />);
    expect(screen.getByText("Year-over-year revenue growth rate")).toBeTruthy();
  });

  it("renders business definition summary when present", () => {
    render(
      <KpiCard
        kpi={makeKpi({
          business_definition: {
            builder: "business_kpi",
            version: 1,
            formula: { type: "single_measure" },
            _compiled: {
              expression: "measure(\"revenue\")",
              filter_predicates: [],
              time_window_predicates: [],
              where_clause: null,
              summary: "Average Revenue for last month",
            },
          },
        } as Partial<Kpi>)}
        evalData={makeEval()}
        loading={false}
      />,
    );
    expect(screen.getByText("Average Revenue for last month")).toBeTruthy();
  });

  it("renders formatted value and target", () => {
    render(<KpiCard kpi={makeKpi()} evalData={makeEval()} loading={false} />);
    expect(screen.getByText("$125,000")).toBeTruthy();
    expect(screen.getByText("$100,000")).toBeTruthy();
  });

  it("renders variance with trend percentage", () => {
    render(<KpiCard kpi={makeKpi()} evalData={makeEval()} loading={false} />);
    expect(screen.getByText(/\+\$25,000/)).toBeTruthy();
    expect(screen.getByText(/12\.5%/)).toBeTruthy();
  });

  it("renders status label", () => {
    render(<KpiCard kpi={makeKpi()} evalData={makeEval()} loading={false} />);
    expect(screen.getByText("On Track")).toBeTruthy();
  });

  // F-017-02 (Bug-7988): a lower-is-better cost dropping 100 -> 80 improves.
  // The backend emits trend=1 (improving), raw trend_pct=-0.2, and normalised
  // trend_pct_normalised=+0.2. The chip MUST render the normalised "+20.0%" so
  // its sign agrees with the improving (green) colour — never the raw "-20.0%".
  it("renders direction-normalised trend percent so sign agrees with colour", () => {
    render(
      <KpiCard
        kpi={makeKpi()}
        evalData={makeEval({
          trend: 1,
          trend_label: "Improving",
          trend_pct: -0.2,
          trend_pct_normalised: 0.2,
        })}
        loading={false}
      />,
    );
    // Improving percent is shown positive; the raw "-20.0%" must NOT be the chip.
    expect(screen.getByText("+20.0%")).toBeTruthy();
    expect(screen.queryByText("-20.0%")).toBeNull();
  });

  // F-017-02: legacy responses that predate the normalised field fall back to
  // the raw percent so the chip still renders (older backend during rollout).
  it("falls back to raw trend percent when normalised is null", () => {
    render(
      <KpiCard
        kpi={makeKpi()}
        evalData={makeEval({
          trend: 1,
          trend_pct: 0.08,
          trend_pct_normalised: null,
        })}
        loading={false}
      />,
    );
    expect(screen.getByText("+8.0%")).toBeTruthy();
  });

  // F-017-17: the backend numeric status is authoritative; the English label
  // must NOT override it (it previously did, the regression of Bug-853). A
  // status code of 1 ("good") shows the success icon even when a custom band
  // label happens to read "Off Target", and the label still renders as text.
  it("trusts the backend numeric status over the English label", () => {
    const { container } = render(
      <KpiCard
        kpi={makeKpi()}
        evalData={makeEval({
          status: 1,
          status_label: "Off Target",
          status_color: "#2e7d32",
        })}
        loading={false}
      />,
    );

    expect(screen.getByText("Off Target")).toBeTruthy();
    // status=1 -> success icon, regardless of the label keyword.
    expect(container.querySelector('[data-testid="CheckCircleIcon"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="ErrorIcon"]')).toBeNull();
  });

  it("drives the status icon from the numeric status code", () => {
    const { container } = render(
      <KpiCard
        kpi={makeKpi()}
        evalData={makeEval({
          status: 0,
          status_label: "Near Target",
          status_color: "#ed6c02",
        })}
        loading={false}
      />,
    );

    expect(screen.getByText("Near Target")).toBeTruthy();
    // status=0 -> warning icon.
    expect(container.querySelector('[data-testid="WarningIcon"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="CheckCircleIcon"]')).toBeNull();
  });

  it("renders sparkline when trend_series has data", () => {
    const { container } = render(
      <KpiCard kpi={makeKpi()} evalData={makeEval()} loading={false} />,
    );
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("shows loading spinner when loading", () => {
    const { container } = render(
      <KpiCard kpi={makeKpi()} evalData={null} loading={true} />,
    );
    expect(container.querySelector("[role='progressbar']")).toBeTruthy();
  });

  it("shows no data message when evalData is null", () => {
    render(<KpiCard kpi={makeKpi()} evalData={null} loading={false} />);
    expect(screen.getByText("kpiScorecard.noDataAvailable")).toBeTruthy();
  });

  it("renders Certified chip for certified KPIs", () => {
    render(
      <KpiCard
        kpi={makeKpi({ certification_status: "certified" })}
        evalData={makeEval()}
        loading={false}
      />,
    );
    expect(screen.getByText("kpiScorecard.certified")).toBeTruthy();
  });

  it("renders Deprecated chip with line-through for deprecated KPIs", () => {
    render(
      <KpiCard
        kpi={makeKpi({ certification_status: "deprecated" })}
        evalData={makeEval()}
        loading={false}
      />,
    );
    expect(screen.getByText("kpiScorecard.deprecated")).toBeTruthy();
  });

  it("falls back to raw value when formatted_value is null", () => {
    render(
      <KpiCard
        kpi={makeKpi()}
        evalData={makeEval({ formatted_value: null, value: 42 })}
        loading={false}
      />,
    );
    expect(screen.getByText("42")).toBeTruthy();
  });

  it("shows em dash when both formatted_value and value are null", () => {
    render(
      <KpiCard
        kpi={makeKpi()}
        evalData={makeEval({ formatted_value: null, value: null })}
        loading={false}
      />,
    );
    expect(screen.getByText("\u2014")).toBeTruthy();
  });

  it("uses legacy goal fields when target is null", () => {
    render(
      <KpiCard
        kpi={makeKpi()}
        evalData={makeEval({
          formatted_target: null,
          target: null,
          formatted_goal: "$80,000",
          goal: 80000,
        })}
        loading={false}
      />,
    );
    expect(screen.getByText("$80,000")).toBeTruthy();
  });

  it("does not render sparkline when trend_series is null", () => {
    const { container } = render(
      <KpiCard
        kpi={makeKpi()}
        evalData={makeEval({ trend_series: null })}
        loading={false}
      />,
    );
    const echarts = container.querySelectorAll("[_echarts_instance_]");
    expect(echarts.length).toBe(0);
  });

  it("does not render sparkline with single data point", () => {
    const { container } = render(
      <KpiCard
        kpi={makeKpi()}
        evalData={makeEval({ trend_series: [{ period: "Q1", value: 100 }] })}
        loading={false}
      />,
    );
    const echarts = container.querySelectorAll("[_echarts_instance_]");
    expect(echarts.length).toBe(0);
  });

  it("localized summary includes time window and filter context from tokens", () => {
    render(
      <KpiCard
        kpi={makeKpi({
          business_definition: {
            builder: "business_kpi",
            version: 1,
            formula: { type: "single_measure" },
            _compiled: {
              expression: 'measure("revenue")',
              filter_predicates: [],
              time_window_predicates: [],
              where_clause: null,
              summary: "Sum of revenue for last month where Country = Germany",
              summary_tokens: {
                formula_type: "single_measure",
                aggregation: "sum",
                measure_name: "revenue",
                time_window_preset: "last_month",
                filter_dimensions: ["Country = Germany"],
              },
            },
          },
        } as Partial<Kpi>)}
        evalData={makeEval()}
        loading={false}
      />,
    );
    // The i18n mock returns keys, so we check the structural parts are present
    const summaryText = screen.getByText(
      /revenue.*kpiBusiness\.summaryFor.*last month.*kpiBusiness\.summaryWhere.*Country = Germany/,
    );
    expect(summaryText).toBeTruthy();
  });

  it("localized summary includes time_calc_type from tokens", () => {
    render(
      <KpiCard
        kpi={makeKpi({
          business_definition: {
            builder: "business_kpi",
            version: 1,
            formula: { type: "single_measure" },
            _compiled: {
              expression: 'measure("revenue")',
              filter_predicates: [],
              time_window_predicates: [],
              where_clause: null,
              summary: "Sum of revenue YoY",
              summary_tokens: {
                formula_type: "single_measure",
                aggregation: "sum",
                measure_name: "revenue",
                time_calc_type: "yoy_value",
              },
            },
          },
        } as Partial<Kpi>)}
        evalData={makeEval()}
        loading={false}
      />,
    );
    expect(screen.getByText(/revenue.*YoY/)).toBeTruthy();
  });

  // Bug-4255: a composite whose child KPI errored shows a Degraded badge.
  it("shows a degraded badge when a composite child errored", () => {
    render(
      <KpiCard
        kpi={makeKpi()}
        evalData={makeEval({
          composite_status: "degraded",
          errored_children: [
            {
              kpi_id: "child-broken",
              kpi_name: "Broken KPI",
              error_reason: "Evaluation failed",
            },
          ],
        })}
        loading={false}
      />,
    );
    expect(screen.getByText("kpiScorecard.degradedBadge")).toBeTruthy();
  });

  it("does NOT show a degraded badge for an ok composite or no-data child", () => {
    render(
      <KpiCard
        kpi={makeKpi()}
        evalData={makeEval({ composite_status: "ok", errored_children: [] })}
        loading={false}
      />,
    );
    expect(screen.queryByText("kpiScorecard.degradedBadge")).toBeNull();
  });

  // Bug-8449 / Bug-8427: a KPI with no value BECAUSE row security denies the
  // caller every row must say so, not render as an ordinary empty card. The
  // live symptom was value:null / "N/A" / "Insufficient Data" with no hint that
  // the cause was a governance policy rather than missing data.
  it("shows a Restricted badge when row security denied every row", () => {
    render(
      <KpiCard
        kpi={makeKpi()}
        evalData={makeEval({
          value: null,
          formatted_value: "N/A",
          row_security_restricted: true,
        })}
        loading={false}
      />,
    );
    expect(screen.getByText("kpiScorecard.rowSecurityRestrictedBadge")).toBeTruthy();
  });

  it("does NOT show a Restricted badge for an ordinary no-data KPI", () => {
    render(
      <KpiCard
        kpi={makeKpi()}
        evalData={makeEval({ value: null, formatted_value: "N/A" })}
        loading={false}
      />,
    );
    expect(screen.queryByText("kpiScorecard.rowSecurityRestrictedBadge")).toBeNull();
  });

  it("does NOT show a Restricted badge when a real value came back", () => {
    render(
      <KpiCard
        kpi={makeKpi()}
        evalData={makeEval({ value: 852672.8, formatted_value: "852,672.80" })}
        loading={false}
      />,
    );
    expect(screen.queryByText("kpiScorecard.rowSecurityRestrictedBadge")).toBeNull();
  });
});
