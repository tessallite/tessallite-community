import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { MeasureRenameImpactResponse } from "../../api/types";
import { MeasureRenameImpactDialog } from "./MeasureRenameImpactDialog";

vi.mock("../../i18n", () => ({
  useT: () => (key: string, vars?: Record<string, string | number>) => {
    const values: Record<string, string> = {
      "measures.renameImpact.title": "Review measure rename",
      "measures.renameImpact.summary": `Rename ${vars?.current} to ${vars?.next}?`,
      "measures.renameImpact.rewrites": `Automatic rewrites (${vars?.count})`,
      "measures.renameImpact.blockers": `Rename blockers (${vars?.count})`,
      "measures.renameImpact.consumerField": `${vars?.type} · ${vars?.field}`,
      "measures.renameImpact.confirm": "Apply rename",
      "common.cancel": "Cancel",
    };
    return values[key] ?? key;
  },
}));

const impact: MeasureRenameImpactResponse = {
  measure_id: "m1",
  current_name: "revenue",
  new_name: "net_revenue",
  safe: false,
  rewrites: [
    { consumer_type: "kpi", consumer_id: "k1", consumer_name: "Margin", field: "expression" },
  ],
  blockers: [
    { consumer_type: "named_set", consumer_id: "s1", consumer_name: "Top sellers", field: "definition" },
  ],
};

describe("MeasureRenameImpactDialog", () => {
  it("L13-9394-FE: renders the shipped rewrite and blocker contract", () => {
    render(
      <MeasureRenameImpactDialog
        impact={impact}
        open
        onCancel={vi.fn()}
        onConfirm={vi.fn()}
      />,
    );

    expect(screen.getByText("Margin")).toBeTruthy();
    expect(screen.getByText("kpi · expression")).toBeTruthy();
    expect(screen.getByText("Top sellers")).toBeTruthy();
    expect(screen.getByText("named_set · definition")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Apply rename" })).toBeDisabled();
  });
});
