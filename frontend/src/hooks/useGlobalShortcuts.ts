import { useEffect } from "react";
import { useBuilderStore } from "../store/builderStore";

/**
 * Phase 6.D.D3 — one authoritative place for model-builder keyboard shortcuts.
 *
 * Any element that already swallows typing (inputs, textareas, contenteditable)
 * short-circuits the handler so the shortcuts never hijack a user mid-edit.
 */

type ShortcutContext = {
  openShortcutHelp: () => void;
  focusMiniTabs: () => void;
  onZoomIn?: () => void;
  onZoomOut?: () => void;
  onFitView?: () => void;
};

/**
 * True when the event target is an element that already swallows typing, so a
 * global keyboard shortcut must not hijack the keystroke. Exported so every
 * window-level shortcut handler (including the canvas undo/redo handler in
 * useCanvasHistory) shares one authoritative guard rather than a weaker copy
 * that omits SELECT / contenteditable (Bug-7408).
 */
export function isEditableTarget(t: EventTarget | null): boolean {
  if (!t || !(t instanceof HTMLElement)) return false;
  const tag = t.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  if (t.isContentEditable) return true;
  return false;
}

export function useGlobalShortcuts(ctx: ShortcutContext): void {
  const setMiniTab = useBuilderStore((s) => s.setMiniTab);
  const closePanel = useBuilderStore((s) => s.closePanel);
  const activePanel = useBuilderStore((s) => s.activePanel);
  const miniTab = useBuilderStore((s) => s.miniTab);

  useEffect(() => {
    function handler(ev: KeyboardEvent) {
      if (isEditableTarget(ev.target)) return;
      const mod = ev.metaKey || ev.ctrlKey;

      // Cmd/Ctrl + ? — open shortcut help overlay
      if (mod && ev.key === "?") {
        ev.preventDefault();
        ctx.openShortcutHelp();
        return;
      }

      // Cmd/Ctrl + K — focus miniTab switcher
      if (mod && (ev.key === "k" || ev.key === "K")) {
        ev.preventDefault();
        ctx.focusMiniTabs();
        return;
      }

      // Escape — close open drawer / panel.
      // Skip when a nested MUI dialog is open: that dialog handles its own
      // Escape (closing itself), and the drawer must stay put behind it
      // (F-026-13). Without this guard one Escape closes both at once.
      if (ev.key === "Escape") {
        if (activePanel && !document.querySelector(".MuiDialog-root")) {
          closePanel();
        }
        return;
      }

      // Plain-digit miniTab switch. Keep this sequence in the same order as
      // MiniTabs renders it so the visible tab position is also the shortcut
      // number (L13-9395 / F-026-11).
      if (!mod && !ev.shiftKey && !ev.altKey) {
        if (ev.key === "1") {
          setMiniTab("canvas");
          return;
        }
        if (ev.key === "2") {
          setMiniTab("query");
          return;
        }
        if (ev.key === "3") {
          setMiniTab("kpi-scorecard");
          return;
        }
        if (ev.key === "4") {
          setMiniTab("matrix");
          return;
        }
        if (ev.key === "5") {
          setMiniTab("analytics");
          return;
        }
      }

      // Canvas-only: + / - zoom, 0 fit
      if (miniTab === "canvas" && !mod) {
        if (ev.key === "+" || ev.key === "=") {
          ctx.onZoomIn?.();
          return;
        }
        if (ev.key === "-" || ev.key === "_") {
          ctx.onZoomOut?.();
          return;
        }
        if (ev.key === "0") {
          ctx.onFitView?.();
          return;
        }
      }
    }

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [activePanel, closePanel, ctx, miniTab, setMiniTab]);
}

/**
 * The canonical shortcut table rendered by the help overlay and exported so
 * future pages (README / user-guide) can reference the same source of truth.
 */
// Only WIRED shortcuts appear here — the help dialog renders this table
// verbatim, so an entry with no handler would advertise a no-op (F-026-08).
// The digit labels match the actual tab strip (1 canvas / 2 query /
// 3 KPI scorecard / 4 Model Health / 5 analytics), and the unwired
// "pivot export" entry was removed.
export const SHORTCUTS: Array<{ keys: string; action: string }> = [
  { keys: "Cmd/Ctrl + ?", action: "shortcuts.showHelp" },
  { keys: "Cmd/Ctrl + K", action: "shortcuts.focusMiniTabs" },
  // F-026-11: undo/redo are wired in useCanvasHistory (canvas keydown); they
  // belong in this help table so modellers can discover them.
  { keys: "Cmd/Ctrl + Z", action: "shortcuts.undo" },
  { keys: "Cmd/Ctrl + Shift + Z / Cmd/Ctrl + Y", action: "shortcuts.redo" },
  { keys: "1", action: "shortcuts.switchToCanvas" },
  { keys: "2", action: "shortcuts.switchToQuery" },
  { keys: "3", action: "shortcuts.switchToKpiScorecard" },
  { keys: "4", action: "shortcuts.switchToMatrix" },
  { keys: "5", action: "shortcuts.switchToAnalytics" },
  { keys: "+ / -", action: "shortcuts.zoomInOut" },
  { keys: "0", action: "shortcuts.fitCanvas" },
  { keys: "Esc", action: "shortcuts.closeDrawer" },
];
