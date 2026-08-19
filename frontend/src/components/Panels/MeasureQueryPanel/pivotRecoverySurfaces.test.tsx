/**
 * Bug-8182 (review B4): every pivot recovery surface — the parent panel PLUS the
 * slicer member lookup, the primary drill-through, and the calculated-measure
 * drill — leads with a friendly message and keeps raw backend/transport text
 * behind the collapsed (unmounted) accordion. These tests render each surface
 * with a failing query and assert the friendly-primary / raw-collapsed contract.
 */
import type { ReactNode } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, type Mock } from "vitest";
import { I18nContext } from "../../../i18n";
import en from "../../../i18n";
import type { Dimension, Measure } from "../../../api/types";

vi.mock("../../../api/client", () => ({
  queryRouterApiClient: { drillThrough: vi.fn(), execute: vi.fn() },
}));

import { queryRouterApiClient } from "../../../api/client";
import DrillThroughPanel from "./drawer/DrillThroughPanel";
import DrillMiniPanel from "./drawer/DrillMiniPanel";
import SlicerBar from "./controls/SlicerBar";

const drillThroughMock = queryRouterApiClient.drillThrough as unknown as Mock;
const executeMock = queryRouterApiClient.execute as unknown as Mock;

function wrap(node: ReactNode) {
  return render(<I18nContext.Provider value={en}>{node}</I18nContext.Provider>);
}

beforeEach(() => {
  drillThroughMock.mockReset();
  executeMock.mockReset();
});

const t = (k: string): string => (en as Record<string, string>)[k] ?? k;

describe("B4: primary drill-through surface (DrillThroughPanel)", () => {
  it("renders the friendly message and hides raw detail until expanded", () => {
    wrap(
      <DrillThroughPanel
        open
        loading={false}
        error={{ message: t("drill.loadFailed"), detail: "raw drill traceback 42" }}
        result={null}
        context={null}
        rowDims={[]}
        colDims={[]}
        pageSize={50}
        hasPrev={false}
        modelId="m1"
        hierarchyId={null}
        hierarchyOptions={[]}
        onClose={vi.fn()}
        onLoadNextPage={vi.fn()}
        onLoadPrevPage={vi.fn()}
        onPageSizeChange={vi.fn()}
        onSelectHierarchy={vi.fn()}
      />,
    );
    expect(screen.getByText(t("drill.loadFailed"))).toBeInTheDocument();
    expect(screen.queryByText("raw drill traceback 42")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /technical details/i }));
    expect(screen.getByText("raw drill traceback 42")).toBeInTheDocument();
  });
});

describe("B4: calculated-measure drill surface (DrillMiniPanel)", () => {
  const measure = {
    id: "m-calc",
    name: "ratio",
    display_name: "Ratio",
    measure_type: "standard",
    default_agg: "SUM",
    is_additive: true,
    format: null,
  } as Measure;
  const coord = {
    rowKey: ["North"],
    colKey: [],
    rowValues: ["North"],
    colValues: [],
    measureValue: 1,
  };

  it("shows the generic drill-failed message with raw error collapsed", async () => {
    drillThroughMock.mockRejectedValueOnce({
      response: { data: { detail: "raw mini drill boom" } },
    });
    wrap(
      <DrillMiniPanel
        measure={measure}
        coord={coord}
        rowDims={[{ id: "d1", name: "region", display_name: "Region" } as Dimension]}
        colDims={[]}
      />,
    );
    expect(await screen.findByText(t("drillMini.failed"))).toBeInTheDocument();
    // Not a pivot-config message, and raw text stays collapsed/unmounted.
    expect(screen.queryByText(t("pivot.loadViewInvalidConfig"))).toBeNull();
    expect(screen.queryByText("raw mini drill boom")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /technical details/i }));
    expect(screen.getByText("raw mini drill boom")).toBeInTheDocument();
  });
});

describe("B4: slicer member-lookup surface (SlicerBar)", () => {
  it("shows the generic lookup-failed message with raw error collapsed", async () => {
    executeMock.mockRejectedValueOnce({
      response: { data: { detail: "raw slicer boom" } },
    });
    const dim = {
      id: "d1",
      name: "region",
      display_name: "Region",
      data_type: "string",
      is_time_dim: false,
    } as Dimension;
    wrap(
      <SlicerBar
        projectId="p1"
        modelId="m1"
        modelSlug="model"
        dimensions={[dim]}
        slicers={[{ dimensionId: "d1", op: "eq", values: [] }]}
        onChange={vi.fn()}
      />,
    );
    // Open the slicer editor (the chip), which triggers the member-lookup query.
    fireEvent.click(screen.getByText(/Region/));
    expect(await screen.findByText(t("slicer.lookupFailed"))).toBeInTheDocument();
    expect(screen.queryByText("raw slicer boom")).toBeNull();
    const toggle = screen.getByRole("button", { name: /technical details/i });
    fireEvent.click(toggle);
    expect(screen.getByText("raw slicer boom")).toBeInTheDocument();
    // executeMock was the lookup that failed.
    expect(executeMock).toHaveBeenCalled();
  });
});
