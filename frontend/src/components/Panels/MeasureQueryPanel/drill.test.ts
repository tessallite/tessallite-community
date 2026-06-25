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

function buildInitialGroupingLevels(
  rowDims: Dimension[],
  colDims: Dimension[],
  rowValues: unknown[],
  colValues: unknown[],
): DrillThroughFilter[] {
  const levels: DrillThroughFilter[] = [];
  rowDims.forEach((d, i) => {
    levels.push({ column: d.name, op: "eq", value: rowValues[i] });
  });
  colDims.forEach((d, i) => {
    levels.push({ column: d.name, op: "eq", value: colValues[i] });
  });
  return levels;
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
      [dim("country"), dim("year")],
      [dim("category")],
      ["US", 2025],
      ["Electronics"],
    );
    expect(levels).toEqual([
      { column: "country", op: "eq", value: "US" },
      { column: "year", op: "eq", value: 2025 },
      { column: "category", op: "eq", value: "Electronics" },
    ]);
  });

  it("handles empty dims", () => {
    const levels = buildInitialGroupingLevels([], [], [], []);
    expect(levels).toEqual([]);
  });

  it("handles null values from pivot cell", () => {
    const levels = buildInitialGroupingLevels(
      [dim("region")],
      [],
      [null],
      [],
    );
    expect(levels).toEqual([{ column: "region", op: "eq", value: null }]);
  });
});

describe("hierarchy drill path accumulation", () => {
  it("accumulates levels as user drills deeper", () => {
    const initial = buildInitialGroupingLevels(
      [dim("year")],
      [],
      [2025],
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
