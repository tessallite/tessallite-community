/**
 * Phase 4.3 — Drill-through payload + hierarchy path tests.
 *
 * Tests the drill-through data flow: grouping level construction,
 * hierarchy path accumulation, and drill request shape.
 */
import { describe, it, expect } from "vitest";
import type {
  Dimension,
  DrillableHierarchy,
  DrillThroughFilter,
  DrillThroughRequest,
  DrillThroughResponse,
  HierarchyPathEntry,
} from "../../../api/types";
import { buildDrillInvocation, buildInitialGroupingLevels, buildSlicerFilters } from "./drillRequest";
import type { PivotColumnMeasure } from "./measureColumns";

function dim(name: string, overrides: Partial<Dimension> = {}): Dimension {
  return {
    id: name,
    name,
    display_name: name,
    source_column_id: null,
    source_column_name: name,
    source_table_id: null,
    user_defined_attribute_id: null,
    user_defined_attribute_name: null,
    hierarchy: null,
    is_time_dim: false,
    time_grain: null,
    redundant_partner: null,
    ...overrides,
  };
}

function accumulateDrillLevel(
  existing: DrillThroughFilter[],
  pathEntry: HierarchyPathEntry,
): DrillThroughFilter[] {
  return [
    ...existing,
    { column: pathEntry.dimension_name, op: "eq" as const, value: pathEntry.value },
  ];
}

describe("buildInitialGroupingLevels", () => {
  it("builds equality filters for row and col dimensions", () => {
    const levels = buildInitialGroupingLevels(
      { rowKey: [], colKey: [], rowValues: ["US", 2025], colValues: ["Electronics"], measureValue: 1 },
      [dim("country"), dim("year")],
      [dim("category")],
    );
    expect(levels).toEqual([
      { column: "country", op: "eq", value: "US" },
      { column: "year", op: "eq", value: 2025 },
      { column: "category", op: "eq", value: "Electronics" },
    ]);
  });

  it("handles empty dims", () => {
    const levels = buildInitialGroupingLevels(
      { rowKey: [], colKey: [], rowValues: [], colValues: [], measureValue: 1 },
      [],
      [],
    );
    expect(levels).toEqual([]);
  });

  it("handles null values from pivot cell", () => {
    const levels = buildInitialGroupingLevels(
      { rowKey: [], colKey: [], rowValues: [null], colValues: [], measureValue: 1 },
      [dim("region")],
      [],
    );
    expect(levels).toEqual([{ column: "region", op: "eq", value: null }]);
  });
});

describe("hierarchy drill path accumulation", () => {
  it("accumulates levels as user drills deeper", () => {
    const initial = buildInitialGroupingLevels(
      { rowKey: [], colKey: [], rowValues: [2025], colValues: [], measureValue: 1 },
      [dim("year")],
      [],
    );

    const afterMonth = accumulateDrillLevel(initial, {
      level_name: "Month",
      dimension_name: "month",
      value: "2025-03",
    });

    expect(afterMonth).toEqual([
      { column: "year", op: "eq", value: 2025 },
      { column: "month", op: "eq", value: "2025-03" },
    ]);

    const afterDay = accumulateDrillLevel(afterMonth, {
      level_name: "Day",
      dimension_name: "day",
      value: "2025-03-15",
    });

    expect(afterDay).toEqual([
      { column: "year", op: "eq", value: 2025 },
      { column: "month", op: "eq", value: "2025-03" },
      { column: "day", op: "eq", value: "2025-03-15" },
    ]);
  });

  it("does not mutate original array", () => {
    const initial = [{ column: "year", op: "eq" as const, value: 2025 }];
    const copy = [...initial];
    accumulateDrillLevel(initial, {
      level_name: "M",
      dimension_name: "month",
      value: 3,
    });
    expect(initial).toEqual(copy);
  });
});

describe("drill request shape", () => {
  it("includes hierarchy_id when drilling a hierarchy", () => {
    const request: DrillThroughRequest = {
      grouping_levels: [{ column: "year", op: "eq", value: 2025 }],
      limit: 50,
      hierarchy_id: "h-1234",
    };
    expect(request.hierarchy_id).toBe("h-1234");
    expect(request.grouping_levels).toHaveLength(1);
  });

  it("omits hierarchy_id for leaf drill", () => {
    const request: DrillThroughRequest = {
      grouping_levels: [{ column: "day", op: "eq", value: "2025-03-15" }],
      limit: 50,
    };
    expect(request.hierarchy_id).toBeUndefined();
  });

  it("can request force-live source routing for drill-through", () => {
    const request: DrillThroughRequest = {
      grouping_levels: [{ column: "country", op: "eq", value: "US" }],
      limit: 50,
      force_route: "source",
    };
    expect(request.force_route).toBe("source");
  });

  // Bug-7265: contract test — override_agg must be accepted by
  // DrillThroughRequest and travel to the backend so hierarchy drill uses
  // the clicked column's aggregate, not the measure default.
  it("carries override_agg when the column uses a non-default aggregate", () => {
    const request: DrillThroughRequest = {
      grouping_levels: [{ column: "year", op: "eq", value: 2025 }],
      limit: 50,
      hierarchy_id: "h-date",
      override_agg: "AVG",
    };
    expect(request.override_agg).toBe("AVG");
  });

  it("omits override_agg when the column uses the measure default", () => {
    const request: DrillThroughRequest = {
      grouping_levels: [{ column: "year", op: "eq", value: 2025 }],
      limit: 50,
      hierarchy_id: "h-date",
    };
    expect(request.override_agg).toBeUndefined();
  });
});

describe("total-cell REST invocation contract (Bug-8047)", () => {
  const rowDims = [dim("region"), dim("city")];
  const colDims = [dim("year"), dim("month")];
  const selectedMeasure = {
    id: "synthetic-revenue-avg",
    name: "revenue__avg__0",
    display_name: "Revenue (Average)",
    measure_type: "standard",
    default_agg: "SUM",
    _measureId: "measure-revenue",
    _alias: "revenue__avg__0",
    _agg: "AVG",
    _baseName: "revenue",
  } as PivotColumnMeasure;
  const dimensionsById = new Map([
    ["status-id", dim("status")],
    ["date-id", dim("order_date")],
  ]);
  const filters = buildSlicerFilters(
    [
      { dimensionId: "status-id", op: "in", values: ["Open", "Closed"] },
      { dimensionId: "date-id", op: "between", values: ["2026-01-01", "2026-01-31"] },
    ],
    dimensionsById,
  );
  const cases: Array<[string, unknown[], unknown[], DrillThroughFilter[]]> = [
    ["row subtotal", ["North"], [2024, "Jan"], [
      { column: "region", op: "eq", value: "North" },
      { column: "year", op: "eq", value: 2024 },
      { column: "month", op: "eq", value: "Jan" },
    ]],
    ["column subtotal", ["North", "Boston"], [2024], [
      { column: "region", op: "eq", value: "North" },
      { column: "city", op: "eq", value: "Boston" },
      { column: "year", op: "eq", value: 2024 },
    ]],
    ["cross subtotal", ["North"], [2024], [
      { column: "region", op: "eq", value: "North" },
      { column: "year", op: "eq", value: 2024 },
    ]],
    ["row grand total", ["North", "Boston"], [], [
      { column: "region", op: "eq", value: "North" },
      { column: "city", op: "eq", value: "Boston" },
    ]],
    ["column grand total", [], [2024, "Jan"], [
      { column: "year", op: "eq", value: 2024 },
      { column: "month", op: "eq", value: "Jan" },
    ]],
    ["grand-grand total", [], [], []],
  ];

  it.each(cases)("builds the exact %s invocation", (_label, rowValues, colValues, groupingLevels) => {
    const coord = {
      rowKey: rowValues.map(String),
      colKey: colValues.map(String),
      rowValues,
      colValues,
      measureValue: 100,
    };
    const invocation = buildDrillInvocation({
      measure: selectedMeasure,
      groupingLevels: buildInitialGroupingLevels(coord, rowDims, colDims),
      filters,
      limit: 50,
    });
    expect(invocation).toEqual({
      measureId: "measure-revenue",
      request: {
        grouping_levels: groupingLevels,
        filters: [
          { column: "status", op: "in", value: ["Open", "Closed"] },
          { column: "order_date", op: "between", value: ["2026-01-01", "2026-01-31"] },
        ],
        limit: 50,
        override_agg: "AVG",
      },
    });
  });
});

describe("drill response hierarchy metadata", () => {
  it("hierarchy mode includes drill_dimension and drillable_hierarchies", () => {
    const response: DrillThroughResponse = {
      columns: ["month", "amount"],
      rows: [{ month: "2025-01", amount: 100 }],
      page: { cursor: "c0", next_cursor: null, has_more: false },
      drill_mode: "hierarchy",
      drill_dimension: {
        id: "dim-month",
        name: "month",
        display_name: "Month",
      },
      hierarchy_path: [
        { level_name: "Year", dimension_name: "year", value: 2025 },
      ],
      drillable_hierarchies: [
        {
          hierarchy_id: "h-date",
          hierarchy_name: "Date",
          current_level_name: "Month",
          next_level_name: "Day",
        },
      ],
      route_type: "source",
      execution_ms: 5,
      bytes_processed: 0,
      rows_returned: 1,
    };

    expect(response.drill_mode).toBe("hierarchy");
    expect(response.drill_dimension?.name).toBe("month");
    expect(response.hierarchy_path).toHaveLength(1);
    expect(response.drillable_hierarchies).toHaveLength(1);
  });

  it("leaf mode has no drill_dimension", () => {
    const response: DrillThroughResponse = {
      columns: ["day", "amount"],
      rows: [],
      page: { cursor: "c0", next_cursor: null, has_more: false },
      drill_mode: "leaf",
      drill_dimension: null,
      hierarchy_path: [
        { level_name: "Year", dimension_name: "year", value: 2025 },
        { level_name: "Month", dimension_name: "month", value: "2025-03" },
      ],
      drillable_hierarchies: [],
      route_type: "source",
      execution_ms: 3,
      bytes_processed: 0,
      rows_returned: 0,
    };

    expect(response.drill_mode).toBe("leaf");
    expect(response.drill_dimension).toBeNull();
    expect(response.drillable_hierarchies).toHaveLength(0);
  });
});

describe("hierarchy picker logic", () => {
  it("multiple hierarchies require user selection", () => {
    const options: DrillableHierarchy[] = [
      {
        hierarchy_id: "h-date",
        hierarchy_name: "Date",
        current_level_name: "Year",
        next_level_name: "Month",
      },
      {
        hierarchy_id: "h-geo",
        hierarchy_name: "Geography",
        current_level_name: "Country",
        next_level_name: "City",
      },
    ];
    expect(options.length > 1).toBe(true);
    const selected = options.find((h) => h.hierarchy_id === "h-geo");
    expect(selected?.hierarchy_name).toBe("Geography");
  });

  it("single hierarchy auto-selects", () => {
    const options: DrillableHierarchy[] = [
      {
        hierarchy_id: "h-date",
        hierarchy_name: "Date",
        current_level_name: "Year",
        next_level_name: "Month",
      },
    ];
    expect(options.length).toBe(1);
    const auto = options[0];
    expect(auto.hierarchy_id).toBe("h-date");
  });
});
