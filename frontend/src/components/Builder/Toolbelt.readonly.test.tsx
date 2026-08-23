import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import { I18nContext } from "../../i18n";
import en from "../../i18n";
import { useBuilderStore } from "../../store/builderStore";
import Toolbelt, { TOOLBELT_SCROLL_SX } from "./Toolbelt";

// F-026-09: the Joins tool starts a canvas write (connection-drawing mode). In a
// read-only builder session (viewer role / ?readonly=1) the canvas handles are
// inert, so the tool must be disabled and must not enter connecting mode.
function renderToolbelt() {
  return render(
    <I18nContext.Provider value={en as Record<string, string>}>
      <Toolbelt />
    </I18nContext.Provider>,
  );
}

describe("Toolbelt scrollbar visibility contract (Bug-9537)", () => {
  const sx = TOOLBELT_SCROLL_SX as unknown as Record<string, Record<string, unknown>>;

  it("hides the vertical scrollbar by default so it cannot cover the collapsed icons", () => {
    expect(sx.scrollbarWidth).toBe("none");
    expect(sx["&::-webkit-scrollbar"].width).toBe(0);
  });

  it("reveals the scrollbar while the mouse hovers over the toolbelt", () => {
    expect(sx["&:hover"].scrollbarWidth).toBe("thin");
    expect(sx["&:hover::-webkit-scrollbar"].width).toBe(5);
  });

  it("keeps the scroll path enabled unconditionally (Bug-7407)", () => {
    expect(sx.overflowY).toBe("auto");
  });
});

describe("Toolbelt read-only gating (F-026-09)", () => {
  beforeEach(() => act(() => useBuilderStore.getState().reset()));
  afterEach(() => act(() => useBuilderStore.getState().reset()));

  it("disables the Joins tool when the session is read-only", () => {
    act(() => useBuilderStore.getState().setReadOnly(true));
    renderToolbelt();
    expect(screen.getByTestId("tool-joins")).toBeDisabled();
  });

  it("enables the Joins tool when the session is editable", () => {
    act(() => useBuilderStore.getState().setReadOnly(false));
    renderToolbelt();
    expect(screen.getByTestId("tool-joins")).not.toBeDisabled();
  });

  it("clicking the disabled Joins tool never enters connecting mode", () => {
    act(() => useBuilderStore.getState().setReadOnly(true));
    renderToolbelt();
    fireEvent.click(screen.getByTestId("tool-joins"));
    expect(useBuilderStore.getState().isConnectingMode).toBe(false);
  });
});
