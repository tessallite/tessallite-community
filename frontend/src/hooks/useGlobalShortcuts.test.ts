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

  // Digits map to the ACTUAL tab strip (canvas / query / health). The old
  // "3 -> pivot" pointed at a value with no matching <Tab> (F-026-08).
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

  it("digit 3 switches to the health (matrix) tab", () => {
    renderHook(() => useGlobalShortcuts(ctx));
    act(() => fireKey("3"));
    expect(useBuilderStore.getState().miniTab).toBe("matrix");
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
});

describe("SHORTCUTS constant", () => {
  it("has entries for all documented shortcuts", () => {
    expect(SHORTCUTS.length).toBeGreaterThanOrEqual(8);
    const keys = SHORTCUTS.map((s) => s.keys);
    expect(keys).toContain("Cmd/Ctrl + ?");
    expect(keys).toContain("Esc");
  });

  it("every shortcut has both keys and action", () => {
    for (const shortcut of SHORTCUTS) {
      expect(shortcut.keys).toBeTruthy();
      expect(shortcut.action).toBeTruthy();
    }
  });
});
