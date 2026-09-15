import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useBuilderStore } from "../../store/builderStore";
import NotificationBell from "./NotificationBell";

vi.mock("../../i18n", () => ({
  useT: () => (key: string) => key,
}));

// User-requested (2026-08-25): the bell surfaces the exact setGlobalMessage
// history already flowing from Canvas/panels — no new per-caller wiring, so
// these tests exercise the store directly rather than mocking it.
describe("NotificationBell", () => {
  beforeEach(() => act(() => useBuilderStore.getState().reset()));
  afterEach(() => act(() => useBuilderStore.getState().reset()));

  it("shows no unread badge and the empty-icon variant when there is no history", () => {
    render(<NotificationBell />);
    expect(screen.queryByText(/^[1-9]/)).toBeNull();
  });

  it("shows an unread badge after setGlobalMessage fires", () => {
    render(<NotificationBell />);
    act(() => useBuilderStore.getState().setGlobalMessage("Model saved", "success"));
    expect(screen.getByText("1")).toBeTruthy();
  });

  it("lists messages newest-first and clears the unread badge on open", async () => {
    render(<NotificationBell />);
    act(() => {
      useBuilderStore.getState().setGlobalMessage("First message", "warning");
      useBuilderStore.getState().setGlobalMessage("Second message", "error");
    });
    expect(screen.getByText("2")).toBeTruthy();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "builder.notificationsTooltip" }));

    const items = await screen.findAllByText(/message/);
    expect(items[0]!.textContent).toBe("Second message");
    expect(items[1]!.textContent).toBe("First message");
    // Opening the menu marks messages read. Assert the store state directly
    // rather than the Badge's rendered text: MUI's Badge intentionally keeps
    // showing the previous badgeContent during its exit transition, so the
    // DOM text lags the state by design — not a bug to assert against.
    expect(useBuilderStore.getState().unreadMessageCount).toBe(0);
  });

  it("shows the empty state when there is no history", async () => {
    render(<NotificationBell />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "builder.notificationsTooltip" }));
    expect(await screen.findByText("builder.notificationsEmpty")).toBeTruthy();
  });

  it("clears history via the Clear button", async () => {
    render(<NotificationBell />);
    act(() => useBuilderStore.getState().setGlobalMessage("Will be cleared", "warning"));

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "builder.notificationsTooltip" }));
    await user.click(await screen.findByText("builder.notificationsClear"));

    expect(await screen.findByText("builder.notificationsEmpty")).toBeTruthy();
    expect(useBuilderStore.getState().messageHistory).toHaveLength(0);
  });

  it("caps history at 30 entries", () => {
    render(<NotificationBell />);
    act(() => {
      for (let i = 0; i < 35; i++) {
        useBuilderStore.getState().setGlobalMessage(`Message ${i}`, "warning");
      }
    });
    expect(useBuilderStore.getState().messageHistory).toHaveLength(30);
    // Newest-first: the most recent message (34) must be kept, the oldest
    // (0-4) must have been evicted.
    expect(useBuilderStore.getState().messageHistory[0]!.text).toBe("Message 34");
  });

  // User-requested (2026-08-25): the bell is for persistent-worthy outcomes,
  // not transient "info"-severity UI acknowledgments (e.g. Canvas's
  // "read-only, action refused" / "link copied" notices use severity="info"
  // for exactly this reason) — those must still flash as a toast but never
  // enter the rolling history or move the unread badge.
  it("excludes info-severity messages from history and the unread count", () => {
    render(<NotificationBell />);
    act(() => useBuilderStore.getState().setGlobalMessage("Read-only, action refused", "info"));

    expect(useBuilderStore.getState().messageHistory).toHaveLength(0);
    expect(useBuilderStore.getState().unreadMessageCount).toBe(0);
    // The toast itself is unaffected — info messages still surface transiently.
    expect(useBuilderStore.getState().globalMessage?.text).toBe("Read-only, action refused");
  });

  it("mixes info and non-info messages, keeping only non-info ones in history", () => {
    render(<NotificationBell />);
    act(() => {
      useBuilderStore.getState().setGlobalMessage("Info notice", "info");
      useBuilderStore.getState().setGlobalMessage("Save failed", "error");
    });
    expect(useBuilderStore.getState().messageHistory).toHaveLength(1);
    expect(useBuilderStore.getState().messageHistory[0]!.text).toBe("Save failed");
    expect(useBuilderStore.getState().unreadMessageCount).toBe(1);
  });
});
