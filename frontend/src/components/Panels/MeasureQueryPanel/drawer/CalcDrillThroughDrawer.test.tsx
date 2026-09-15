import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { I18nContext } from "../../../../i18n";
import en from "../../../../i18n";
import type { Dimension, Measure } from "../../../../api/types";
import type { DrillContext } from "../types";
import CalcDrillThroughDrawer from "./CalcDrillThroughDrawer";

function dim(name: string, displayName: string): Dimension {
  return { id: name, name, display_name: displayName } as Dimension;
}

const calcMeasure = {
  id: "calc-1",
  name: "margin_pct",
  display_name: "Margin %",
  measure_type: "calculated",
  expression: null,
  format: null,
} as Measure;

const region = dim("region", "Region");
const city = dim("city", "City");

const context: DrillContext = {
  measure: calcMeasure,
  coord: {
    rowKey: ["north"],
    colKey: ["2024"],
    rowValues: ["north"],
    colValues: ["2024"],
    measureValue: 0.42,
  },
};

// Bug-7282: the coordinate chips interpolated the technical dimension name
// (`d.name`) directly — the sibling DrillThroughPanel already fixed the exact
// same class of bug (Bug-6285) by preferring `display_name`, but this drawer
// was missed.
describe("CalcDrillThroughDrawer coordinate chips show display names (Bug-7282)", () => {
  it("uses display_name for the row/column coordinate chips, not the technical name", () => {
    render(
      <I18nContext.Provider value={en}>
        <CalcDrillThroughDrawer
          open
          context={context}
          rowDims={[region]}
          colDims={[city]}
          allMeasures={[calcMeasure]}
          onClose={vi.fn()}
        />
      </I18nContext.Provider>,
    );
    expect(screen.getByText("Region = north")).toBeTruthy();
    expect(screen.getByText("City = 2024")).toBeTruthy();
    expect(screen.queryByText("region = north")).toBeNull();
    expect(screen.queryByText("city = 2024")).toBeNull();
  });
});
