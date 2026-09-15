import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { I18nContext, getMessages } from "../../i18n";
import PersonaPicker from "./PersonaPicker";

vi.mock("../../api/hooks", () => ({
  usePersonas: () => ({ isLoading: false, data: [{ id: "technical", name: "Technical" }] }),
}));

describe("PersonaPicker initial selection", () => {
  it("shows a readable full-model label instead of the internal sentinel", () => {
    render(<I18nContext.Provider value={getMessages("en")}>
      <PersonaPicker projectId="p1" modelId="m1" value={null} onChange={vi.fn()} />
    </I18nContext.Provider>);
    expect(screen.getByRole("combobox")).toHaveTextContent("None (full model)");
    expect(screen.queryByText("__none__")).not.toBeInTheDocument();
  });
});
