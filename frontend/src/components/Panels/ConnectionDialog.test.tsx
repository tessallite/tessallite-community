import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ConnectionDialog from "./ConnectionDialog";

const baseProps = {
  open: true,
  mode: "create" as const,
  name: "",
  onNameChange: vi.fn(),
  connType: "postgresql",
  onConnTypeChange: vi.fn(),
  fields: {},
  onFieldChange: vi.fn(),
  testResult: null,
  isError: false,
  isSaving: false,
  isTesting: false,
  onTest: vi.fn(),
  onSave: vi.fn(),
  onClose: vi.fn(),
};

describe("ConnectionDialog", () => {
  it("renders create mode title", () => {
    render(<ConnectionDialog {...baseProps} />);
    expect(screen.getByText("Add Connection")).toBeTruthy();
  });

  it("renders edit mode title", () => {
    render(<ConnectionDialog {...baseProps} mode="edit" name="My Conn" />);
    expect(screen.getByText("Edit Connection")).toBeTruthy();
  });

  it("Save/Create button is disabled when name is empty", () => {
    render(<ConnectionDialog {...baseProps} name="" />);
    const btn = screen.getByRole("button", { name: /create/i });
    expect(btn).toBeDisabled();
  });

  it("Save button is labelled correctly in edit mode", () => {
    render(<ConnectionDialog {...baseProps} mode="edit" name="Existing" />);
    expect(screen.getByRole("button", { name: /save/i })).toBeTruthy();
  });

  it("shows error alert when isError is true in create mode", () => {
    render(<ConnectionDialog {...baseProps} isError={true} />);
    expect(screen.getByText(/failed to create connection/i)).toBeTruthy();
  });

  it("shows error alert when isError is true in edit mode", () => {
    render(<ConnectionDialog {...baseProps} mode="edit" name="x" isError={true} />);
    expect(screen.getByText(/failed to update connection/i)).toBeTruthy();
  });

  it("shows test success alert when testResult is success", () => {
    render(<ConnectionDialog {...baseProps} name="x" testResult="success" />);
    expect(screen.getByText(/connection test passed/i)).toBeTruthy();
  });

  it("shows test failure alert when testResult is an error message", () => {
    render(<ConnectionDialog {...baseProps} name="x" testResult="timeout" />);
    expect(screen.getByText(/timeout/i)).toBeTruthy();
  });

  it("shows password hint in edit mode when passwordHint is true", () => {
    render(<ConnectionDialog {...baseProps} mode="edit" name="x" passwordHint />);
    expect(screen.getByText(/leave sensitive fields/i)).toBeTruthy();
  });

  it("calls onClose when Cancel is clicked", async () => {
    const onClose = vi.fn();
    render(<ConnectionDialog {...baseProps} onClose={onClose} />);
    await userEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onClose).toHaveBeenCalled();
  });

  it("connection type selector is disabled in edit mode", () => {
    render(<ConnectionDialog {...baseProps} mode="edit" name="x" connType="bigquery" />);
    // The FormControl wrapping the Select has disabled attribute
    const selects = document.querySelectorAll(".Mui-disabled");
    expect(selects.length).toBeGreaterThan(0);
  });
});
