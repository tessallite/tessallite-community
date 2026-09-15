import { describe, it, expect, vi } from "vitest";
import { render, screen, act } from "@testing-library/react";
import StaleBundleGuard from "./StaleBundleGuard";

vi.mock("../i18n", () => ({
  useT: () => (key: string) => key,
}));

describe("StaleBundleGuard", () => {
  it("renders nothing until a stale-bundle signal fires", () => {
    const { container } = render(<StaleBundleGuard />);
    expect(container.firstChild).toBeNull();
  });

  it("shows a warning once a vite:preloadError for a JS chunk fires", () => {
    render(<StaleBundleGuard />);
    act(() => {
      const event = new Event("vite:preloadError") as Event & { payload?: unknown };
      event.payload = { url: "/assets/index-abc123.js" };
      window.dispatchEvent(event);
    });
    expect(screen.getByText("errors.newerVersionAvailable")).toBeTruthy();
  });

  // R3 (round-3 alert-mechanism audit, 2026-08-25): this Alert is one of two
  // deliberate exceptions to the shared setGlobalMessage toast (it must
  // persist with no auto-hide and sit above modals, which that mechanism
  // cannot currently express) — but it must still LOOK like every other
  // toast in the app. It was previously missing variant="filled", the same
  // prop the global Snackbar's Alert (App.tsx) always sets.
  it("uses variant=filled to match the global toast's appearance", () => {
    render(<StaleBundleGuard />);
    act(() => {
      const event = new Event("vite:preloadError") as Event & { payload?: unknown };
      event.payload = { url: "/assets/index-abc123.js" };
      window.dispatchEvent(event);
    });
    const alert = screen.getByRole("alert");
    expect(alert.className).toMatch(/MuiAlert-filledWarning/);
  });

  it("ignores an error event unrelated to a stale bundle", () => {
    render(<StaleBundleGuard />);
    act(() => {
      window.dispatchEvent(new ErrorEvent("error", { error: new Error("unrelated failure") }));
    });
    expect(screen.queryByText("errors.newerVersionAvailable")).toBeNull();
  });
});
