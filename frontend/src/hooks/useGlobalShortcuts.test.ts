import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useGlobalShortcuts, SHORTCUTS } from "./useGlobalShortcuts";
import { useBuilderStore } from "../store/builderStore";

function fireKey(
  key: string,
  opts: { ctrlKey?: boolean; metaKey?: boolean; shiftKey?: boolean; altKey?: boolean } = {},
) {
  const ev = new KeyboardEvent("keydown", {
    key,
    bubbles: true,
    ...opts,
  });
  window.dispatchEvent(ev);
}

// Dispatch a keydown whose event.target is a specific element (the handler is
// bound to window; the event bubbles up from the element and carries it as
// `target`), so the editable-target guard can be exercised.
function fireKeyFrom(
  el: Element,
  key: string,
  opts: { ctrlKey?: boolean; metaKey?: boolean; shiftKey?: boolean; altKey?: boolean } = {},
) {
  const ev = new KeyboardEvent("keydown", {
    key,
    bubbles: true,
    ...opts,
  });
  el.dispatchEvent(ev);
}

describe("useGlobalShortcuts", () => {
  const ctx = {
    openShortcutHelp: vi.fn(),
    focusMiniTabs: vi.fn(),
    onZoomIn: vi.fn(),
    onZoomOut: vi.fn(),
    onFitView: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    act(() => useBuilderStore.getState().reset());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("Cmd+? opens shortcut help", () => {
    renderHook(() => useGlobalShortcuts(ctx));
    fireKey("?", { metaKey: true });
    expect(ctx.openShortcutHelp).toHaveBeenCalledOnce();
  });

  it("Ctrl+K focuses miniTab switcher", () => {
    renderHook(() => useGlobalShortcuts(ctx));
    fireKey("k", { ctrlKey: true });
    expect(ctx.focusMiniTabs).toHaveBeenCalledOnce();
  });

  // Digits map to the visible tab strip (canvas / query / KPI scorecard /
  // Model Health / analytics).
  it("digit 1 switches to canvas tab", () => {
    act(() => useBuilderStore.getState().setMiniTab("matrix"));
    renderHook(() => useGlobalShortcuts(ctx));
    act(() => fireKey("1"));
    expect(useBuilderStore.getState().miniTab).toBe("canvas");
  });

  it("digit 2 switches to query tab", () => {
    renderHook(() => useGlobalShortcuts(ctx));
    act(() => fireKey("2"));
    expect(useBuilderStore.getState().miniTab).toBe("query");
  });

  it("digit 3 switches to the KPI scorecard tab", () => {
    renderHook(() => useGlobalShortcuts(ctx));
    act(() => fireKey("3"));
    expect(useBuilderStore.getState().miniTab).toBe("kpi-scorecard");
  });

  it("Escape closes the active panel", () => {
    act(() => useBuilderStore.getState().openPanel("measures"));
    renderHook(() => useGlobalShortcuts(ctx));
    act(() => fireKey("Escape"));
    expect(useBuilderStore.getState().activePanel).toBeNull();
  });

  it("Escape does nothing when no panel is open", () => {
    renderHook(() => useGlobalShortcuts(ctx));
    fireKey("Escape");
    expect(useBuilderStore.getState().activePanel).toBeNull();
  });

  it("Escape does NOT close the panel when a nested dialog is open (F-026-13)", () => {
    // Simulate an open MUI dialog inside a panel.
    const dialog = document.createElement("div");
    dialog.className = "MuiDialog-root";
    document.body.appendChild(dialog);
    try {
      act(() => useBuilderStore.getState().openPanel("measures"));
      renderHook(() => useGlobalShortcuts(ctx));
      act(() => fireKey("Escape"));
      // The dialog handles its own Escape; the drawer behind it stays open.
      expect(useBuilderStore.getState().activePanel).toBe("measures");
    } finally {
      document.body.removeChild(dialog);
    }
  });

  it("+ triggers zoom in on canvas tab", () => {
    act(() => useBuilderStore.getState().setMiniTab("canvas"));
    renderHook(() => useGlobalShortcuts(ctx));
    fireKey("+");
    expect(ctx.onZoomIn).toHaveBeenCalledOnce();
  });

  it("- triggers zoom out on canvas tab", () => {
    act(() => useBuilderStore.getState().setMiniTab("canvas"));
    renderHook(() => useGlobalShortcuts(ctx));
    fireKey("-");
    expect(ctx.onZoomOut).toHaveBeenCalledOnce();
  });

  it("0 triggers fit view on canvas tab", () => {
    act(() => useBuilderStore.getState().setMiniTab("canvas"));
    renderHook(() => useGlobalShortcuts(ctx));
    fireKey("0");
    expect(ctx.onFitView).toHaveBeenCalledOnce();
  });

  it("zoom keys do nothing outside canvas tab", () => {
    act(() => useBuilderStore.getState().setMiniTab("query"));
    renderHook(() => useGlobalShortcuts(ctx));
    fireKey("+");
    fireKey("-");
    expect(ctx.onZoomIn).not.toHaveBeenCalled();
    expect(ctx.onZoomOut).not.toHaveBeenCalled();
  });

  // Editable-target guard: the hook must never hijack a key while the user is
  // typing into an input / textarea / select / contenteditable element. Without
  // this, e.g. "k" in a text field would trigger focusMiniTabs and Escape would
  // close the drawer mid-edit.
  describe("does not fire while typing in an editable element", () => {
    function withElement(el: HTMLElement, run: () => void) {
      document.body.appendChild(el);
      try {
        run();
      } finally {
        document.body.removeChild(el);
      }
    }

    it("ignores Ctrl+K dispatched from an <input>", () => {
      renderHook(() => useGlobalShortcuts(ctx));
      const input = document.createElement("input");
      withElement(input, () => fireKeyFrom(input, "k", { ctrlKey: true }));
      expect(ctx.focusMiniTabs).not.toHaveBeenCalled();
    });

    it("ignores a digit switch dispatched from a <textarea>", () => {
      act(() => useBuilderStore.getState().setMiniTab("canvas"));
      renderHook(() => useGlobalShortcuts(ctx));
      const textarea = document.createElement("textarea");
      withElement(textarea, () => fireKeyFrom(textarea, "2"));
      expect(useBuilderStore.getState().miniTab).toBe("canvas");
    });

    it("ignores Escape dispatched from a <select>", () => {
      act(() => useBuilderStore.getState().openPanel("measures"));
      renderHook(() => useGlobalShortcuts(ctx));
      const select = document.createElement("select");
      withElement(select, () => fireKeyFrom(select, "Escape"));
      expect(useBuilderStore.getState().activePanel).toBe("measures");
    });

    it("ignores keys dispatched from a contenteditable element", () => {
      renderHook(() => useGlobalShortcuts(ctx));
      const div = document.createElement("div");
      div.setAttribute("contenteditable", "true");
      // jsdom does not derive isContentEditable from the attribute; set it.
      Object.defineProperty(div, "isContentEditable", { value: true });
      withElement(div, () => fireKeyFrom(div, "?", { metaKey: true }));
      expect(ctx.openShortcutHelp).not.toHaveBeenCalled();
    });
  });
});

describe("SHORTCUTS constant", () => {
  it("has entries for all documented shortcuts", () => {
    expect(SHORTCUTS.length).toBeGreaterThanOrEqual(8);
    const keys = SHORTCUTS.map((s) => s.keys);
    expect(keys).toContain("Cmd/Ctrl + ?");
    expect(keys).toContain("Esc");
  });

  it("F-026-11: documents the wired undo/redo canvas shortcuts", () => {
    const actions = SHORTCUTS.map((s) => s.action);
    expect(actions).toContain("shortcuts.undo");
    expect(actions).toContain("shortcuts.redo");
  });

  it("every shortcut has both keys and action", () => {
    for (const shortcut of SHORTCUTS) {
      expect(shortcut.keys).toBeTruthy();
      expect(shortcut.action).toBeTruthy();
    }
  });
});
