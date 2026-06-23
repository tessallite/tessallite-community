import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import KpiScorecardTab from "./KpiScorecardTab";
import type { Kpi, KpiBatchResponse } from "../../api/types";

const useKpisMock = vi.fn();
const useKpiBatchEvaluationMock = vi.fn();

vi.mock("../../api/hooks", () => ({
  useKpis: (...args: unknown[]) => useKpisMock(...args),
  useKpiBatchEvaluation: (...args: unknown[]) => useKpiBatchEvaluationMock(...args),
  // F-017-25: the scorecard now reads personas for the "view as persona"
  // switcher. Default to an empty list so the switcher is simply hidden.
  usePersonas: () => ({ data: [], isLoading: false }),
}));

vi.mock("../../i18n", () => ({
  useT: () => (key: string) => key,
}));

vi.mock("../HelpIconButton", () => ({
  default: () => null,
}));

function makeKpi(id: string, name: string, overrides: Partial<Kpi> = {}): Kpi {
  return {
    id,
    name,
    display_name: name,
    description: `Description of ${name}`,
    certification_status: null,
    display_folder: null,
    ...overrides,
  } as Kpi;
}

function makeBatchResponse(kpis: Kpi[]): KpiBatchResponse {
  return {
    results: kpis.map((k) => ({
      kpi_id: k.id,
      value: 100,
      value_str: null,
      target: 80,
      status: 1,
      status_label: "Good",
      status_color: "#2e7d32",
      trend: 1,
      trend_label: "Improving",
      trend_pct: 5.0,
      formatted_value: "100",
      formatted_target: "80",
      formatted_variance: "+20",
      trend_series: [
        { period: "Q1", value: 90 },
        { period: "Q2", value: 100 },
      ],
      evaluation_ms: 10,
      goal: null,
      formatted_goal: null,
    })),
    evaluation_ms: 50,
  };
}

function renderTab() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <KpiScorecardTab projectId="proj-1" modelId="model-1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useKpisMock.mockReset();
  useKpiBatchEvaluationMock.mockReset();
});

describe("KpiScorecardTab", () => {
  it("shows loading spinner while KPIs load", () => {
    useKpisMock.mockReturnValue({ data: undefined, isLoading: true });
    useKpiBatchEvaluationMock.mockReturnValue({ data: undefined, isLoading: false });

    const { container } = renderTab();
    expect(container.querySelector("[role='progressbar']")).toBeTruthy();
  });

  it("shows empty state when no KPIs exist", () => {
    useKpisMock.mockReturnValue({ data: [], isLoading: false });
    useKpiBatchEvaluationMock.mockReturnValue({ data: undefined, isLoading: false });

    renderTab();
    expect(screen.getByText("kpiScorecard.noKpisTitle")).toBeTruthy();
    expect(screen.getByText("kpiScorecard.noKpisDescription")).toBeTruthy();
  });

  it("renders KPI cards with batch evaluation data", () => {
    const kpis = [
      makeKpi("k1", "Revenue"),
      makeKpi("k2", "Profit"),
    ];
    useKpisMock.mockReturnValue({ data: kpis, isLoading: false });
    useKpiBatchEvaluationMock.mockReturnValue({
      data: makeBatchResponse(kpis),
      isLoading: false,
    });

    renderTab();
    expect(screen.getByText("Revenue")).toBeTruthy();
    expect(screen.getByText("Profit")).toBeTruthy();
  });

  it("renders summary bar with status counts", () => {
    const kpis = [
      makeKpi("k1", "Revenue"),
      makeKpi("k2", "Margin"),
    ];
    useKpisMock.mockReturnValue({ data: kpis, isLoading: false });
    useKpiBatchEvaluationMock.mockReturnValue({
      data: makeBatchResponse(kpis),
      isLoading: false,
    });

    renderTab();
    // Both KPIs have status=1 (Good)
    expect(screen.getByText(/2 kpiScorecard\.statusGood/)).toBeTruthy();
  });

  it("groups KPIs by display_folder", () => {
    const kpis = [
      makeKpi("k1", "Revenue", { display_folder: "Financial" }),
      makeKpi("k2", "CAC", { display_folder: "Marketing" }),
      makeKpi("k3", "Profit", { display_folder: "Financial" }),
    ];
    useKpisMock.mockReturnValue({ data: kpis, isLoading: false });
    useKpiBatchEvaluationMock.mockReturnValue({
      data: makeBatchResponse(kpis),
      isLoading: false,
    });

    renderTab();
    expect(screen.getAllByText(/Financial/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Marketing/).length).toBeGreaterThan(0);
  });

  it("passes all KPI IDs to batch evaluation hook", () => {
    const kpis = [
      makeKpi("k1", "Revenue"),
      makeKpi("k2", "Profit"),
      makeKpi("k3", "Growth"),
    ];
    useKpisMock.mockReturnValue({ data: kpis, isLoading: false });
    useKpiBatchEvaluationMock.mockReturnValue({
      data: makeBatchResponse(kpis),
      isLoading: false,
    });

    renderTab();
    // F-017-25: the hook also receives an `enabled` flag and the selected
    // persona id (null by default = "view as default").
    expect(useKpiBatchEvaluationMock).toHaveBeenCalledWith(
      "proj-1",
      "model-1",
      ["k1", "k2", "k3"],
      true,
      null,
    );
  });

  it("renders title from i18n", () => {
    const kpis = [makeKpi("k1", "Revenue")];
    useKpisMock.mockReturnValue({ data: kpis, isLoading: false });
    useKpiBatchEvaluationMock.mockReturnValue({
      data: makeBatchResponse(kpis),
      isLoading: false,
    });

    renderTab();
    expect(screen.getByText("kpiScorecard.title")).toBeTruthy();
  });

  it("renders filter dropdown with i18n labels", () => {
    const kpis = [makeKpi("k1", "Revenue")];
    useKpisMock.mockReturnValue({ data: kpis, isLoading: false });
    useKpiBatchEvaluationMock.mockReturnValue({
      data: makeBatchResponse(kpis),
      isLoading: false,
    });

    renderTab();
    expect(screen.getAllByText("kpiScorecard.filterLabel").length).toBeGreaterThan(0);
  });
});
