import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PivotSort } from "../../../../api/client";
import type { Dimension, ExecuteResponse, Measure } from "../../../../api/types";
import { I18nContext } from "../../../../i18n";
import en from "../../../../i18n";
import { computePivot } from "../pivot";
import { computeTotals } from "../totals";
import type { CellCoord } from "../types";
import PivotGrid from "./PivotGrid";
import { resolvePivotSort } from "./sortState";

const measure = {
  id: "measure-revenue",
  name: "revenue",
  display_name: "Revenue",
  measure_type: "standard",
  default_agg: "SUM",
  is_additive: true,
  format: null,
} as Measure;

function dimension(id: string): Dimension {
  return { id, name: id, display_name: id } as Dimension;
}

const response = {
  rows: [
    { region: "North", city: "Boston", year: 2024, month: "Jan", revenue: 10 },
    { region: "North", city: "New York", year: 2024, month: "Feb", revenue: 20 },
    { region: "South", city: "Austin", year: 2025, month: "Jan", revenue: 30 },
    { region: "South", city: "Dallas", year: 2025, month: "Feb", revenue: 40 },
  ],
} as ExecuteResponse;

function renderTotals(onCellClick: (coord: CellCoord, clicked: Measure) => void) {
  const model = computePivot(
    response,
    measure,
    [dimension("region"), dimension("city")],
    [dimension("year"), dimension("month")],
  );
  return render(
    <I18nContext.Provider value={en}>
      <PivotGrid
        model={model}
        measure={measure}
        allTotals={new Map([[measure.name, computeTotals(model, measure)]])}
        showSubtotals
        showGrandTotals
        emptyCellMode="blank"
        conditionalFormat={{ kind: "none" }}
        sort={null}
        onSortChange={vi.fn()}
        onCellClick={onCellClick}
      />
    </I18nContext.Provider>,
  );
}

describe("PivotGrid total-cell drill coordinates (Bug-8047)", () => {
  const expected: Array<[string, unknown[], unknown[]]> = [
    ["row-subtotal", ["North"], [2024, "Feb"]],
    ["column-subtotal", ["North", "Boston"], [2024]],
    ["cross-subtotal", ["North"], [2024]],
    ["row-grand", ["North", "Boston"], []],
    ["column-grand", [], [2024, "Feb"]],
    ["grand-grand", [], []],
  ];

  it.each(expected)("makes the %s total drillable at its exact retained grain", (kind, rowValues, colValues) => {
    const onCellClick = vi.fn();
    const { container } = renderTotals(onCellClick);
    const cell = container.querySelector<HTMLElement>(`[data-total-kind="${kind}"]`);
    expect(cell).not.toBeNull();
    expect(cell).toHaveAttribute("tabindex", "0");
    fireEvent.click(cell!);
    expect(onCellClick).toHaveBeenCalledTimes(1);
    expect(onCellClick.mock.calls[0][0]).toMatchObject({ rowValues, colValues });
    expect(onCellClick.mock.calls[0][1]).toBe(measure);
  });

  it("supports keyboard activation for total cells", () => {
    const onCellClick = vi.fn();
    const { container } = renderTotals(onCellClick);
    const grandGrand = container.querySelector<HTMLElement>('[data-total-kind="grand-grand"]')!;
    fireEvent.keyDown(grandGrand, { key: "Enter" });
    expect(onCellClick).toHaveBeenCalledWith(
      expect.objectContaining({ rowValues: [], colValues: [] }),
      measure,
    );
  });
});

describe("PivotGrid drillable total-cell accessible names (Bug-8505)", () => {
  // The aria-label REPLACES the cell text for assistive technology, so it must
  // restate the value and identify which total this is; otherwise every total
  // cell announces the same string and the number is lost entirely.
  const expectedLabels: Array<[string, string]> = [
    ["row-subtotal", "20. Revenue. Row: Subtotal: North. Column: 2024 / Feb. Activate to drill through."],
    ["column-subtotal", "10. Revenue. Row: North / Boston. Column: Subtotal: 2024. Activate to drill through."],
    ["cross-subtotal", "30. Revenue. Row: Subtotal: North. Column: Subtotal: 2024. Activate to drill through."],
    ["row-grand", "10. Revenue. Row: North / Boston. Column: Total. Activate to drill through."],
    ["column-grand", "20. Revenue. Row: Total. Column: 2024 / Feb. Activate to drill through."],
    ["grand-grand", "100. Revenue. Row: Total. Column: Total. Activate to drill through."],
  ];

  it.each(expectedLabels)("names the %s cell with its value and grain", (kind, label) => {
    const { container } = renderTotals(vi.fn());
    const cell = container.querySelector<HTMLElement>(`[data-total-kind="${kind}"]`);
    expect(cell).not.toBeNull();
    expect(cell).toHaveAttribute("aria-label", label);
  });

  it("gives every drillable total cell a distinct accessible name", () => {
    const { container } = renderTotals(vi.fn());
    const labels = Array.from(container.querySelectorAll<HTMLElement>("[data-total-kind]"))
      .map((cell) => cell.getAttribute("aria-label"))
      .filter((label): label is string => label !== null);
    expect(labels.length).toBeGreaterThan(1);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it("omits the column clause when the pivot has no column dimensions", () => {
    const flat = {
      rows: [{ region: "North", revenue: 10 }],
    } as ExecuteResponse;
    const model = computePivot(flat, measure, [dimension("region")], []);
    const { container } = render(
      <I18nContext.Provider value={en}>
        <PivotGrid
          model={model}
          measure={measure}
          allTotals={new Map([[measure.name, computeTotals(model, measure)]])}
          showSubtotals={false}
          showGrandTotals
          emptyCellMode="blank"
          conditionalFormat={{ kind: "none" }}
          sort={null}
          onSortChange={vi.fn()}
          onCellClick={vi.fn()}
        />
      </I18nContext.Provider>,
    );
    const grand = container.querySelector<HTMLElement>('[data-total-kind="grand-grand"]');
    expect(grand).not.toBeNull();
    expect(grand).toHaveAttribute(
      "aria-label",
      "10. Revenue. Row: Total. Activate to drill through.",
    );
  });
});

describe("PivotGrid controlled saved sort (Bug-8069)", () => {
  it.each([
    ["asc" as const, [["North", "Boston"], ["North", "New York"]]],
    ["desc" as const, [["North", "New York"], ["North", "Boston"]]],
  ])("applies a restored %s sort visibly and reports the rendered order", async (direction, expectedOrder) => {
    const flat = {
      rows: [
        { region: "North", city: "Boston", revenue: 10 },
        { region: "North", city: "New York", revenue: 20 },
      ],
    } as ExecuteResponse;
    const model = computePivot(flat, measure, [dimension("region"), dimension("city")], []);
    const sort: PivotSort = {
      measure: { measureId: measure.id, aggregation: "SUM", occurrence: 0 },
      target: { kind: "column", columnKey: [] },
      direction,
    };
    const onRowOrderChange = vi.fn();
    render(
      <I18nContext.Provider value={en}>
        <PivotGrid
          model={model}
          measure={measure}
          showSubtotals={false}
          showGrandTotals={false}
          emptyCellMode="blank"
          conditionalFormat={{ kind: "none" }}
          sort={sort}
          onSortChange={vi.fn()}
          onRowOrderChange={onRowOrderChange}
        />
      </I18nContext.Provider>,
    );
    expect(screen.getByRole("columnheader", { name: /Revenue/i })).toHaveAttribute(
      "aria-sort",
      direction === "asc" ? "ascending" : "descending",
    );
    await waitFor(() => {
      expect(onRowOrderChange).toHaveBeenLastCalledWith(expectedOrder);
    });
  });

  it("resolves the exact repeated-measure occurrence, column tuple, and grand target", () => {
    const columns = [
      { ...measure, name: "revenue__avg__0", _measureId: measure.id, _agg: "AVG" },
      { ...measure, name: "revenue__sum__1", _measureId: measure.id, _agg: "SUM" },
      { ...measure, name: "revenue__avg__2", _measureId: measure.id, _agg: "AVG" },
    ] as Measure[];
    expect(resolvePivotSort({
      measure: { measureId: measure.id, aggregation: "AVG", occurrence: 1 },
      target: { kind: "column", columnKey: ["2026", "Q1"] },
      direction: "desc",
    }, columns, [["2025", "Q4"], ["2026", "Q1"]])).toMatchObject({
      measureIndex: 2,
      ckIndex: 1,
      dir: "desc",
    });
    expect(resolvePivotSort({
      measure: { measureId: measure.id, aggregation: "SUM", occurrence: 0 },
      target: { kind: "grand" },
      direction: "asc",
    }, columns, [])).toMatchObject({ measureIndex: 1, ckIndex: "grand", dir: "asc" });
  });

  it("reports a stale saved sort whose measure or exact column tuple is missing", async () => {
    const flat = {
      rows: [{ region: "North", year: 2024, revenue: 10 }],
    } as ExecuteResponse;
    const model = computePivot(flat, measure, [dimension("region")], [dimension("year")]);
    const onSortInvalid = vi.fn();
    const { rerender } = render(
      <I18nContext.Provider value={en}>
        <PivotGrid
          model={model}
          measure={measure}
          showSubtotals={false}
          showGrandTotals={false}
          emptyCellMode="blank"
          conditionalFormat={{ kind: "none" }}
          sort={{
            measure: { measureId: measure.id, aggregation: "SUM", occurrence: 0 },
            target: { kind: "column", columnKey: ["2099"] },
            direction: "asc",
          }}
          onSortChange={vi.fn()}
          onSortInvalid={onSortInvalid}
        />
      </I18nContext.Provider>,
    );
    await waitFor(() => expect(onSortInvalid).toHaveBeenCalledTimes(1));

    onSortInvalid.mockClear();
    rerender(
      <I18nContext.Provider value={en}>
        <PivotGrid
          model={model}
          measure={measure}
          showSubtotals={false}
          showGrandTotals={false}
          emptyCellMode="blank"
          conditionalFormat={{ kind: "none" }}
          sort={{
            measure: { measureId: "missing-measure", aggregation: "SUM", occurrence: 0 },
            target: { kind: "column", columnKey: ["2024"] },
            direction: "desc",
          }}
          onSortChange={vi.fn()}
          onSortInvalid={onSortInvalid}
        />
      </I18nContext.Provider>,
    );
    await waitFor(() => expect(onSortInvalid).toHaveBeenCalledTimes(1));
  });
});
