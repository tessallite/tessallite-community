import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import CanvasLayoutPanel from "./CanvasLayoutPanel";

// Assert on keys, not English copy: the panel's job is to expose the controls,
// and pinning English strings here would make a copy change look like a
// regression in the control surface.
vi.mock("../../../i18n", () => ({
  useT: () => (key: string) => key,
}));

const preferences = { preset: "hierarchical" as const, direction: "DOWN" as const, spacing: "normal" as const };

function setup(overrides: Partial<Parameters<typeof CanvasLayoutPanel>[0]> = {}) {
  const props = {
    preferences,
    busy: false,
    tableCount: 5,
    selectedTableCount: 0,
    movableSelectedCount: 0,
    onPreferenceChange: vi.fn(),
    onArrangeAll: vi.fn(),
    onArrangeSelected: vi.fn(),
    onRerouteLinks: vi.fn(),
    routeLock: "none" as const,
    onToggleRouteLock: vi.fn(),
    tablePins: "none" as const,
    onTogglePin: vi.fn(),
    ...overrides,
  };
  render(<CanvasLayoutPanel {...props} />);
  return props;
}

describe("CanvasLayoutPanel", () => {
  it("exposes the direction and spacing controls that had no home (R03)", () => {
    // Spec 3 requires "Arrange all with useful hierarchical direction/spacing
    // controls". Both values were honoured by the engine but unreachable from
    // the interface, because the previous menu carried presets only.
    setup();

    expect(screen.getByRole("group", { name: "canvas.layoutDirection" })).toBeTruthy();
    expect(screen.getByRole("group", { name: "canvas.layoutSpacing" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "canvas.layoutDirectionDown" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "canvas.layoutDirectionRight" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "canvas.layoutSpacingNormal" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "canvas.layoutSpacingDense" })).toBeTruthy();
  });

  it("reports the current choice in every group through aria-pressed", () => {
    setup();

    expect(screen.getByRole("button", { name: "canvas.layoutDirectionDown" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "canvas.layoutDirectionRight" }).getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByRole("button", { name: "canvas.layoutSpacingNormal" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "canvas.layoutSpacingDense" }).getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByRole("button", { name: "canvas.layoutHierarchical" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "canvas.layoutRadial" }).getAttribute("aria-pressed")).toBe("false");
  });

  it("updates aria-pressed when the preference changes", () => {
    setup({ preferences: { ...preferences, direction: "RIGHT", spacing: "dense" } });

    expect(screen.getByRole("button", { name: "canvas.layoutDirectionRight" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "canvas.layoutSpacingDense" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("reports a direction or spacing choice without arranging anything", () => {
    const props = setup();

    fireEvent.click(screen.getByRole("button", { name: "canvas.layoutDirectionRight" }));
    fireEvent.click(screen.getByRole("button", { name: "canvas.layoutSpacingDense" }));

    expect(props.onPreferenceChange).toHaveBeenNthCalledWith(1, { direction: "RIGHT" });
    expect(props.onPreferenceChange).toHaveBeenNthCalledWith(2, { spacing: "dense" });
    // Choosing an option is not an instruction to relayout the diagram.
    expect(props.onArrangeAll).not.toHaveBeenCalled();
  });

  it("requests the arrangement with the chosen preset, and reroute separately", () => {
    const props = setup();

    fireEvent.click(screen.getByRole("button", { name: "canvas.layoutRadial" }));
    expect(props.onArrangeAll).toHaveBeenCalledWith("radial");

    fireEvent.click(screen.getByRole("button", { name: "canvas.layoutRerouteLinks" }));
    expect(props.onRerouteLinks).toHaveBeenCalledTimes(1);
  });

  it("offers every control as a real disabled button while a batch runs", () => {
    setup({ busy: true });

    const buttons = screen.getAllByRole("button");
    expect(buttons.length).toBeGreaterThan(0);
    for (const button of buttons) {
      // A second batch must not be startable, and the disabled state must reach
      // assistive technology rather than only changing the cursor.
      expect(button).toBeDisabled();
    }
  });

  it("offers Arrange selected only once the selection contains a movable table (R04)", () => {
    const props = setup({ movableSelectedCount: 0 });
    const disabled = screen.getByRole("button", { name: "canvas.layoutArrangeSelected" });
    expect(disabled).toBeDisabled();
    fireEvent.click(disabled);
    expect(props.onArrangeSelected).not.toHaveBeenCalled();
  });

  it("arranges the selection when one is movable, without touching the preset action", () => {
    const props = setup({ selectedTableCount: 3, movableSelectedCount: 3 });
    const enabled = screen.getByRole("button", { name: "canvas.layoutArrangeSelected" });
    expect(enabled).not.toBeDisabled();

    fireEvent.click(enabled);
    expect(props.onArrangeSelected).toHaveBeenCalledTimes(1);
    // Arranging a selection is not an arrange-all with a different name.
    expect(props.onArrangeAll).not.toHaveBeenCalled();
  });

  it("explains why Arrange selected is unavailable, and describes the control with it", () => {
    // A disabled button is not focusable, so the reason has to be readable in
    // ordinary reading order — not only announced on focus.
    setup({ movableSelectedCount: 0 });
    const hint = screen.getByText("canvas.layoutArrangeSelectedHint");
    expect(
      screen.getByRole("button", { name: "canvas.layoutArrangeSelected" }).getAttribute("aria-describedby"),
    ).toBe(hint.getAttribute("id"));
  });

  it("says the selection is held, not that nothing is selected (R04)", () => {
    // The wrong message here tells a user to do what they have already done.
    setup({ selectedTableCount: 3, movableSelectedCount: 0 });
    expect(screen.getByText("canvas.layoutArrangeSelectedHeld")).toBeTruthy();
    expect(screen.queryByText("canvas.layoutArrangeSelectedHint")).toBeNull();
    expect(screen.getByRole("button", { name: "canvas.layoutArrangeSelected" })).toBeDisabled();
  });

  it("states what a ready selection will move", () => {
    setup({ selectedTableCount: 2, movableSelectedCount: 2 });
    expect(screen.getByText("canvas.layoutArrangeSelectedReady")).toBeTruthy();
    expect(screen.queryByText("canvas.layoutArrangeSelectedHint")).toBeNull();
  });

  it("disables every arrangement on an empty canvas and says so", () => {
    // With no tables there is nothing to place, so an enabled control would only
    // produce a failure the user cannot act on.
    const props = setup({ tableCount: 0, movableSelectedCount: 0 });

    expect(screen.getByRole("button", { name: "canvas.layoutHierarchical" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "canvas.layoutArrangeSelected" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "canvas.layoutRerouteLinks" })).toBeDisabled();
    expect(screen.getByText("canvas.layoutEmptyCanvasHint")).toBeTruthy();

    // Direction and spacing remain usable: choosing them is a preference, not an
    // arrangement, and the user may set them before adding any table.
    expect(screen.getByRole("button", { name: "canvas.layoutDirectionRight" })).not.toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "canvas.layoutDirectionRight" }));
    expect(props.onPreferenceChange).toHaveBeenCalledWith({ direction: "RIGHT" });
  });

  it("offers no route lock until exactly one relationship is selected (R06)", () => {
    const props = setup({ routeLock: "none" });
    const button = screen.getByRole("button", { name: "canvas.routeLock" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(props.onToggleRouteLock).not.toHaveBeenCalled();
    expect(screen.getByText("canvas.routeLockNoSelection")).toBeTruthy();
  });

  it("refuses to lock a route that is not drawn correctly (R06)", () => {
    // A lock persists the exact polyline and stops anything recomputing it, so
    // freezing a broken shape would strand the relationship in it.
    const props = setup({ routeLock: "invalid" });
    expect(screen.getByRole("button", { name: "canvas.routeLock" })).toBeDisabled();
    expect(screen.getByText("canvas.routeLockInvalid")).toBeTruthy();
    expect(props.onToggleRouteLock).not.toHaveBeenCalled();
  });

  it("locks a valid selected route and reports the state", () => {
    const props = setup({ routeLock: "unlocked" });
    const button = screen.getByRole("button", { name: "canvas.routeLock" });
    expect(button).not.toBeDisabled();
    expect(button.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(button);
    expect(props.onToggleRouteLock).toHaveBeenCalledTimes(1);
  });

  it("offers the reverse action, and the pressed state, once locked", () => {
    setup({ routeLock: "locked" });
    const button = screen.getByRole("button", { name: "canvas.routeUnlock" });
    expect(button).not.toBeDisabled();
    expect(button.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText("canvas.routeLockedState")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "canvas.routeLock" })).toBeNull();
  });

  it("describes the lock control with the reason it is in that state", () => {
    setup({ routeLock: "unlocked" });
    const hint = screen.getByText("canvas.routeUnlockedState");
    expect(
      screen.getByRole("button", { name: "canvas.routeLock" }).getAttribute("aria-describedby"),
    ).toBe(hint.getAttribute("id"));
  });

  it("keeps the lock usable on an empty-of-tables canvas gate", () => {
    // The lock belongs to a relationship, not to a layout batch, so the
    // empty-canvas gate that disables arrangement must not disable it.
    const props = setup({ tableCount: 0, routeLock: "locked" });
    const button = screen.getByRole("button", { name: "canvas.routeUnlock" });
    expect(button).not.toBeDisabled();
    fireEvent.click(button);
    expect(props.onToggleRouteLock).toHaveBeenCalledTimes(1);
  });

  it("offers no pin until tables are selected (R06)", () => {
    const props = setup({ tablePins: "none" });
    const button = screen.getByRole("button", { name: "canvas.tablePin" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(props.onTogglePin).not.toHaveBeenCalled();
    expect(screen.getByText("canvas.tablePinNoSelection")).toBeTruthy();
  });

  it("pins a selection that is not yet fully pinned", () => {
    // A mixed selection reads as "unpinned", so the action pins the rest —
    // what a user asking to pin these tables expects.
    const props = setup({ tablePins: "unpinned" });
    const button = screen.getByRole("button", { name: "canvas.tablePin" });
    expect(button.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(button);
    expect(props.onTogglePin).toHaveBeenCalledTimes(1);
  });

  it("offers the reverse action once the whole selection is pinned", () => {
    setup({ tablePins: "pinned" });
    const button = screen.getByRole("button", { name: "canvas.tableUnpin" });
    expect(button.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText("canvas.tablePinnedState")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "canvas.tablePin" })).toBeNull();
  });

  it("keeps the two locks as separate controls in one group (spec 4)", () => {
    // They share a group and a verb because they are one idea applied to two
    // subjects, but they stay separate CONTROLS with separate state: locking a
    // route never locks a table's position, and releasing one never releases
    // the other.
    const props = setup({ tablePins: "unpinned", routeLock: "unlocked" });

    fireEvent.click(screen.getByRole("button", { name: "canvas.tablePin" }));
    expect(props.onTogglePin).toHaveBeenCalledTimes(1);
    expect(props.onToggleRouteLock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "canvas.routeLock" }));
    expect(props.onToggleRouteLock).toHaveBeenCalledTimes(1);
    expect(props.onTogglePin).toHaveBeenCalledTimes(1);
  });

  it("does not announce a second working status (the canvas owns that live region)", () => {
    setup({ busy: true });

    // Two polite regions announcing the same work make a screen reader say it
    // twice; the canvas' existing busy indicator is the single owner.
    expect(screen.queryByRole("status")).toBeNull();
  });
});
