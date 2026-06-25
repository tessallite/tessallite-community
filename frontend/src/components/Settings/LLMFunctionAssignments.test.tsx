import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import LLMFunctionAssignments, { type LLMFunctionRow } from "./LLMFunctionAssignments";
import type { LLMProviderConfig } from "../../api/types";

const PROVIDERS = [
  {
    id: "p1", project_id: "proj", provider: "anthropic", display_name: "Claude",
    base_url: null, model_name: "claude-sonnet", max_tokens: 1, temperature: 0,
    timeout_seconds: 30, config: {}, has_api_key: true,
    created_at: "", updated_at: "",
  },
  {
    id: "p2", project_id: "proj", provider: "openai", display_name: "GPT",
    base_url: null, model_name: "gpt-4o", max_tokens: 1, temperature: 0,
    timeout_seconds: 30, config: {}, has_api_key: true,
    created_at: "", updated_at: "",
  },
] as unknown as LLMProviderConfig[];

const ROWS: LLMFunctionRow[] = [
  { key: "aggregate", label: "Aggregate creator", value: "", emptyLabel: "Inherit from project" },
  { key: "glossary", label: "Glossary creator", value: "p2", emptyLabel: "Inherit from project" },
];

describe("LLMFunctionAssignments", () => {
  it("renders one dropdown per row with the providers as options and reports changes", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<LLMFunctionAssignments rows={ROWS} providers={PROVIDERS} onChange={onChange} />);

    // The labels render (MUI renders each label twice: <label> + fieldset legend).
    expect(screen.getAllByText("Aggregate creator").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Glossary creator").length).toBeGreaterThan(0);

    // One dropdown per row, in order; the second carries its pre-selected value.
    const combos = screen.getAllByRole("combobox");
    expect(combos).toHaveLength(2);
    expect(within(combos[1]).getByText("GPT — openai / gpt-4o")).toBeInTheDocument();

    // Open the aggregate dropdown: empty option + both providers.
    await user.click(combos[0]);
    const options = screen.getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual([
      "Inherit from project",
      "Claude — anthropic / claude-sonnet",
      "GPT — openai / gpt-4o",
    ]);

    await user.click(screen.getByRole("option", { name: "Claude — anthropic / claude-sonnet" }));
    expect(onChange).toHaveBeenCalledWith("aggregate", "p1");
  });
});
