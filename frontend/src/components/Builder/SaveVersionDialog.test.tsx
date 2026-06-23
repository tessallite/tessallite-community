import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import SaveVersionDialog from "./SaveVersionDialog";

describe("SaveVersionDialog (F-013-16)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("passes the trimmed summary to onSave", async () => {
    const onSave = vi.fn();
    render(<SaveVersionDialog open onSave={onSave} onClose={() => {}} />);
    const field = screen.getByTestId("save-version-summary").querySelector("textarea");
    expect(field).not.toBeNull();
    await userEvent.type(field!, "  Added revenue measure  ");
    await userEvent.click(screen.getByTestId("save-version-confirm"));
    expect(onSave).toHaveBeenCalledWith("Added revenue measure");
  });

  it("passes undefined when the summary is blank (optional)", async () => {
    const onSave = vi.fn();
    render(<SaveVersionDialog open onSave={onSave} onClose={() => {}} />);
    await userEvent.click(screen.getByTestId("save-version-confirm"));
    expect(onSave).toHaveBeenCalledWith(undefined);
  });

  it("resets the field each time it reopens", async () => {
    const onSave = vi.fn();
    const { rerender } = render(
      <SaveVersionDialog open onSave={onSave} onClose={() => {}} />,
    );
    const field = () =>
      screen.getByTestId("save-version-summary").querySelector("textarea")!;
    await userEvent.type(field(), "draft note");
    // Close then reopen.
    rerender(<SaveVersionDialog open={false} onSave={onSave} onClose={() => {}} />);
    rerender(<SaveVersionDialog open onSave={onSave} onClose={() => {}} />);
    expect(field()).toHaveValue("");
  });
});
