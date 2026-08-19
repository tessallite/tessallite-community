import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import { I18nContext } from "../../i18n";
import en from "../../i18n";
import { useBuilderStore } from "../../store/builderStore";
import Toolbelt from "./Toolbelt";

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
