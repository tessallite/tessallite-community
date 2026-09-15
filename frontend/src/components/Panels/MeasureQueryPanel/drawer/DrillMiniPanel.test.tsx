import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { I18nContext } from "../../../../i18n";
import en from "../../../../i18n";
import type { Measure } from "../../../../api/types";
import type { CellCoord } from "../types";
import type { DrillThroughResponse } from "../../../../api/types_domains/drill_refresh";

const drillThroughMock = vi.fn();

vi.mock("../../../../api/client", () => ({
  queryRouterApiClient: {
    drillThrough: (...args: unknown[]) => drillThroughMock(...args),
  },
}));

import DrillMiniPanel from "./DrillMiniPanel";

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const measure = {
  id: "m1",
  name: "revenue",
  display_name: "Revenue",
  measure_type: "standard",
} as Measure;

const coord: CellCoord = {
  rowKey: ["North"],
  colKey: [],
  rowValues: ["North"],
  colValues: [],
  measureValue: 10,
};

function response(routeType: string): DrillThroughResponse {
  return {
    columns: ["region", "revenue"],
    rows: [{ region: "North", revenue: 10 }],
    page: { cursor: "", next_cursor: null, has_more: false },
    drill_mode: "leaf",
    drill_dimension: null,
    hierarchy_path: [],
    drillable_hierarchies: [],
    route_type: routeType,
    execution_ms: 5,
    bytes_processed: 0,
    rows_returned: 1,
  };
}

// Bug-7281: DrillMiniPanel interpolated the raw backend `route_type` English
// string directly ("aggregate"/"pocket"/"source") instead of routing it
// through the shared translated label helper the pivot badge already uses
// (Bug-6282 precedent) — the drill panel and the pivot badge disagreed.
describe("DrillMiniPanel route-type label (Bug-7281)", () => {
  beforeEach(() => {
    drillThroughMock.mockReset();
  });

  it.each([
    ["aggregate", "Aggregate"],
    ["pocket", "Pocket"],
    ["source", "live (source)"],
  ])("translates route_type %s instead of printing it raw", async (routeType, expectedLabel) => {
    drillThroughMock.mockResolvedValue(response(routeType));
    render(
      <I18nContext.Provider value={en}>
        <DrillMiniPanel measure={measure} coord={coord} rowDims={[]} colDims={[]} />
      </I18nContext.Provider>,
    );
    expect(await screen.findByText(new RegExp(`${escapeRegExp(expectedLabel)} ·`))).toBeTruthy();
    expect(screen.queryByText(new RegExp(`^${escapeRegExp(routeType)} ·`))).toBeNull();
  });
});
