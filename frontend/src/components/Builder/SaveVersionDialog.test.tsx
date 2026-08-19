import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import SaveVersionDialog from "./SaveVersionDialog";

describe("SaveVersionDialog", () => {
  beforeEach(() => vi.clearAllMocks());

  it("passes trimmed summary with mode 'all' when isDirty", async () => {
    const onSave = vi.fn();
    render(
      <SaveVersionDialog open isDirty={true} onSave={onSave} onClose={() => {}} />,
    );
    const field = screen.getByTestId("save-version-summary").querySelector("textarea");
    expect(field).not.toBeNull();
    await userEvent.type(field!, "  Added revenue measure  ");
    await userEvent.click(screen.getByTestId("save-version-confirm"));
    expect(onSave).toHaveBeenCalledWith("all", "Added revenue measure");
  });

  it("passes undefined summary when the summary is blank", async () => {
    const onSave = vi.fn();
    render(
      <SaveVersionDialog open isDirty={true} onSave={onSave} onClose={() => {}} />,
    );
    await userEvent.click(screen.getByTestId("save-version-confirm"));
    expect(onSave).toHaveBeenCalledWith("all", undefined);
  });

  it("resets the field each time it reopens", async () => {
    const onSave = vi.fn();
    const { rerender } = render(
      <SaveVersionDialog open isDirty={true} onSave={onSave} onClose={() => {}} />,
    );
    const field = () =>
      screen.getByTestId("save-version-summary").querySelector("textarea")!;
    await userEvent.type(field(), "draft note");
    rerender(
      <SaveVersionDialog open={false} isDirty={true} onSave={onSave} onClose={() => {}} />,
    );
    rerender(
      <SaveVersionDialog open isDirty={true} onSave={onSave} onClose={() => {}} />,
    );
    expect(field()).toHaveValue("");
  });

  it("defaults to 'all' when isDirty is true", () => {
    render(
      <SaveVersionDialog open isDirty={true} onSave={() => {}} onClose={() => {}} />,
    );
    const allInput = screen.getByTestId("save-mode-all").querySelector("input")!;
    const layoutInput = screen.getByTestId("save-mode-layout").querySelector("input")!;
    expect(allInput).toBeChecked();
    expect(layoutInput).not.toBeChecked();
  });

  it("defaults to 'layout' when isDirty is false", () => {
    render(
      <SaveVersionDialog open isDirty={false} onSave={() => {}} onClose={() => {}} />,
    );
    const allInput = screen.getByTestId("save-mode-all").querySelector("input")!;
    const layoutInput = screen.getByTestId("save-mode-layout").querySelector("input")!;
    expect(layoutInput).toBeChecked();
    expect(allInput).not.toBeChecked();
  });

  it("hides summary field when layout-only is selected", async () => {
    render(
      <SaveVersionDialog open isDirty={true} onSave={() => {}} onClose={() => {}} />,
    );
    // Initially in 'all' mode, summary is visible
    expect(screen.getByTestId("save-version-summary")).toBeInTheDocument();

    // Switch to layout-only
    await userEvent.click(screen.getByTestId("save-mode-layout"));
    expect(screen.queryByTestId("save-version-summary")).not.toBeInTheDocument();
  });

  it("calls onSave with mode 'layout' when layout-only is selected", async () => {
    const onSave = vi.fn();
    render(
      <SaveVersionDialog open isDirty={false} onSave={onSave} onClose={() => {}} />,
    );
    // Default is layout when not dirty
    await userEvent.click(screen.getByTestId("save-version-confirm"));
    expect(onSave).toHaveBeenCalledWith("layout");
  });

  it("shows summary and calls onSave with mode 'all' after switching radio", async () => {
    const onSave = vi.fn();
    render(
      <SaveVersionDialog open isDirty={false} onSave={onSave} onClose={() => {}} />,
    );
    // Switch from layout (default when not dirty) to all
    await userEvent.click(screen.getByTestId("save-mode-all"));
    expect(screen.getByTestId("save-version-summary")).toBeInTheDocument();

    const field = screen.getByTestId("save-version-summary").querySelector("textarea");
    await userEvent.type(field!, "schema update");
    await userEvent.click(screen.getByTestId("save-version-confirm"));
    expect(onSave).toHaveBeenCalledWith("all", "schema update");
  });
});
