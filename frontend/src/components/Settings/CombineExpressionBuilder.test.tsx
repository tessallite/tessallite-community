import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";

vi.mock("../../i18n", () => ({
  useT: () => (key: string) => key,
}));

import CombineExpressionBuilder from "./CombineExpressionBuilder";
import { ExprNode, RecipeStep } from "../../api/agentApi";

const STEPS: RecipeStep[] = [
  { name: "sales", model_id: "m1", measures: ["revenue"], dimensions: [], filters: [], limit: 100 },
  // Bug-5346: a step named after a reserved word is just data here.
  { name: "global", model_id: "m1", measures: ["revenue"], dimensions: [], filters: [], limit: 100 },
];

// round(sales.revenue / global.revenue * 100, 2)
const TREE: ExprNode = {
  op: "round",
  args: [
    {
      op: "mul",
      args: [
        { op: "div", args: [
          { ref: { step: "sales", measure: "revenue" } },
          { ref: { step: "global", measure: "revenue" } },
        ] },
        { const: 100 },
      ],
    },
    { const: 2 },
  ],
};

describe("CombineExpressionBuilder", () => {
  it("renders an existing expression tree, including a step named like a keyword", () => {
    render(<CombineExpressionBuilder value={TREE} steps={STEPS} onChange={() => {}} />);
    // The operation labels from the tree appear in the rendered selects.
    expect(screen.getAllByText("round()").length).toBeGreaterThan(0);
    expect(screen.getAllByText("÷ (divide)").length).toBeGreaterThan(0);
    // The "global" step (a Python keyword) is selectable data, not code.
    expect(screen.getAllByText("global").length).toBeGreaterThan(0);
  });

  it("emits null when the root node kind is set to None", () => {
    const onChange = vi.fn();
    render(
      <CombineExpressionBuilder value={{ const: 5 }} steps={STEPS} onChange={onChange} />,
    );
    // Open the root Node-kind select (the first combobox) and choose None.
    const comboboxes = screen.getAllByRole("combobox");
    fireEvent.mouseDown(comboboxes[0]);
    const listbox = within(screen.getByRole("listbox"));
    fireEvent.click(listbox.getByText("recipes.combineKindNone"));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("builds a reference node referencing a step measure", () => {
    const onChange = vi.fn();
    render(
      <CombineExpressionBuilder value={null} steps={STEPS} onChange={onChange} />,
    );
    const comboboxes = screen.getAllByRole("combobox");
    fireEvent.mouseDown(comboboxes[0]);
    const listbox = within(screen.getByRole("listbox"));
    fireEvent.click(listbox.getByText("recipes.combineKindRef"));
    // Default ref points at the first step + its first measure.
    expect(onChange).toHaveBeenCalledWith({ ref: { step: "sales", measure: "revenue" } });
  });
});
