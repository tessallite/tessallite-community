import { describe, it, expect, beforeEach } from "vitest";
import { useBuilderStore, PANEL_IDS } from "./builderStore";
import { act } from "@testing-library/react";

describe("builderStore", () => {
  beforeEach(() => {
    act(() => {
      useBuilderStore.getState().reset();
    });
  });

  // F-026-09 / F-026-10: PANEL_IDS is the single source of truth that the
  // Drawer title/help maps and the ModelBuilder deep-link allow-list derive
  // from, so the three previously-dropped panels can't drift back out.
  describe("PANEL_IDS source of truth", () => {
    it("includes the panels that previously drifted (named-sets / saved-queries / alerts)", () => {
      expect(PANEL_IDS).toContain("named-sets");
      expect(PANEL_IDS).toContain("saved-queries");
      expect(PANEL_IDS).toContain("alerts");
    });

    it("has no duplicate ids", () => {
      expect(new Set(PANEL_IDS).size).toBe(PANEL_IDS.length);
    });
  });

  // F-026-18: relation display preferences and per-join terminal overrides are
  // persisted to localStorage and must survive reset() (which runs on every
  // model mount) instead of silently reverting.
  describe("relation settings persist across reset (F-026-18)", () => {
    it("keeps notation, pathing, and terminal overrides after reset", () => {
      act(() => {
        useBuilderStore.getState().setRelationNotation("uml");
        useBuilderStore.getState().setRelationPathing("straight");
        useBuilderStore.getState().setRelationTerminalOverride("join-1", {
          source: "many",
          target: "one",
        });
      });
      act(() => useBuilderStore.getState().reset());
      const s = useBuilderStore.getState();
      expect(s.relationNotation).toBe("uml");
      expect(s.relationPathing).toBe("straight");
      expect(s.relationTerminalOverrides["join-1"]).toEqual({ source: "many", target: "one" });
      // Restore defaults so this test doesn't bleed into others.
      act(() => {
        useBuilderStore.getState().setRelationNotation("crowsfoot");
        useBuilderStore.getState().setRelationPathing("orthogonal");
      });
      localStorage.removeItem("tsl.relation.terminalOverrides");
    });
  });

  // Bug-6374 / F-026-02: displayLocale is a persisted user preference and must
  // survive reset() (which runs on every model mount). Before the fix, reset()
  // spread initialState (displayLocale: null), so opening any model silently
  // reverted the whole UI to English for non-English users.
  describe("display locale persists across reset (Bug-6374)", () => {
    it("keeps the chosen locale after reset", () => {
      act(() => useBuilderStore.getState().setDisplayLocale("fr"));
      expect(localStorage.getItem("display_locale")).toBe("fr");
      act(() => useBuilderStore.getState().reset());
      expect(useBuilderStore.getState().displayLocale).toBe("fr");
      // Restore default English so this test doesn't bleed into others.
      act(() => useBuilderStore.getState().setDisplayLocale(null));
    });

    it("resets to null when no locale is persisted (default English)", () => {
      act(() => useBuilderStore.getState().setDisplayLocale(null));
      expect(localStorage.getItem("display_locale")).toBeNull();
      act(() => useBuilderStore.getState().reset());
      expect(useBuilderStore.getState().displayLocale).toBeNull();
    });
  });

  describe("read-only share-link state persists across reset (Bug-6377)", () => {
    it("keeps readOnly enabled after reset", () => {
      act(() => useBuilderStore.getState().setReadOnly(true));
      act(() => useBuilderStore.getState().reset());
      expect(useBuilderStore.getState().readOnly).toBe(true);
      act(() => useBuilderStore.getState().setReadOnly(false));
    });
  });

  describe("panel management", () => {
    it("starts with no active panel", () => {
      expect(useBuilderStore.getState().activePanel).toBeNull();
    });

    it("opens a panel", () => {
      act(() => useBuilderStore.getState().openPanel("sources"));
      expect(useBuilderStore.getState().activePanel).toBe("sources");
    });

    it("closes the active panel and collapses the drawer", () => {
      act(() => {
        useBuilderStore.getState().openPanel("measures");
        useBuilderStore.getState().setDrawerExpanded(true);
      });
      act(() => useBuilderStore.getState().closePanel());
      expect(useBuilderStore.getState().activePanel).toBeNull();
      expect(useBuilderStore.getState().drawerExpanded).toBe(false);
    });

    it("switches between panels", () => {
      act(() => useBuilderStore.getState().openPanel("dimensions"));
      act(() => useBuilderStore.getState().openPanel("measures"));
      expect(useBuilderStore.getState().activePanel).toBe("measures");
    });
  });

  describe("drawer expansion", () => {
    it("toggles drawer expansion", () => {
      expect(useBuilderStore.getState().drawerExpanded).toBe(false);
      act(() => useBuilderStore.getState().toggleDrawerExpanded());
      expect(useBuilderStore.getState().drawerExpanded).toBe(true);
      act(() => useBuilderStore.getState().toggleDrawerExpanded());
      expect(useBuilderStore.getState().drawerExpanded).toBe(false);
    });
  });

  describe("object selection", () => {
    it("selects an object", () => {
      act(() => useBuilderStore.getState().selectObject("dim-1", "dimension"));
      const s = useBuilderStore.getState();
      expect(s.selectedObjectId).toBe("dim-1");
      expect(s.selectedObjectType).toBe("dimension");
    });

    it("clears selection", () => {
      act(() => useBuilderStore.getState().selectObject("dim-1", "dimension"));
      act(() => useBuilderStore.getState().selectObject(null, null));
      const s = useBuilderStore.getState();
      expect(s.selectedObjectId).toBeNull();
      expect(s.selectedObjectType).toBeNull();
    });
  });

  describe("mini tab", () => {
    it("defaults to canvas", () => {
      expect(useBuilderStore.getState().miniTab).toBe("canvas");
    });

    it("switches tabs", () => {
      act(() => useBuilderStore.getState().setMiniTab("pivot"));
      expect(useBuilderStore.getState().miniTab).toBe("pivot");
    });
  });

  describe("validation issues", () => {
    const issue = {
      id: "v1",
      severity: "error" as const,
      message: "Missing key column",
      affectedObject: "dim-1",
      affectedType: "dimension" as const,
    };

    it("starts with no issues", () => {
      expect(useBuilderStore.getState().validationIssues).toHaveLength(0);
    });

    it("adds a single issue", () => {
      act(() => useBuilderStore.getState().addValidationIssue(issue));
      expect(useBuilderStore.getState().validationIssues).toHaveLength(1);
      expect(useBuilderStore.getState().validationIssues[0].id).toBe("v1");
    });

    it("sets all issues at once", () => {
      act(() => useBuilderStore.getState().setValidationIssues([issue, { ...issue, id: "v2" }]));
      expect(useBuilderStore.getState().validationIssues).toHaveLength(2);
    });

    it("clears all issues", () => {
      act(() => useBuilderStore.getState().addValidationIssue(issue));
      act(() => useBuilderStore.getState().clearValidation());
      expect(useBuilderStore.getState().validationIssues).toHaveLength(0);
    });

    it("toggles validation expansion", () => {
      expect(useBuilderStore.getState().validationExpanded).toBe(false);
      act(() => useBuilderStore.getState().toggleValidationExpanded());
      expect(useBuilderStore.getState().validationExpanded).toBe(true);
    });
  });

  describe("focus table", () => {
    it("sets focus and opens sources panel", () => {
      act(() => useBuilderStore.getState().focusTable("tbl-1", "src-1"));
      const s = useBuilderStore.getState();
      expect(s.focusedTableId).toBe("tbl-1");
      expect(s.focusedSourceId).toBe("src-1");
      expect(s.activePanel).toBe("sources");
    });

    it("clears focus", () => {
      act(() => useBuilderStore.getState().focusTable("tbl-1", "src-1"));
      act(() => useBuilderStore.getState().clearFocusedTable());
      expect(useBuilderStore.getState().focusedTableId).toBeNull();
      expect(useBuilderStore.getState().focusedSourceId).toBeNull();
    });
  });

  describe("global message", () => {
    it("sets a message with default severity", () => {
      act(() => useBuilderStore.getState().setGlobalMessage("Saved"));
      const msg = useBuilderStore.getState().globalMessage;
      expect(msg?.text).toBe("Saved");
      expect(msg?.severity).toBe("info");
      expect(msg?.at).toBeGreaterThan(0);
    });

    it("sets a message with custom severity", () => {
      act(() => useBuilderStore.getState().setGlobalMessage("Error", "error"));
      expect(useBuilderStore.getState().globalMessage?.severity).toBe("error");
    });

    it("clears the message", () => {
      act(() => useBuilderStore.getState().setGlobalMessage("msg"));
      act(() => useBuilderStore.getState().clearGlobalMessage());
      expect(useBuilderStore.getState().globalMessage).toBeNull();
    });
  });

  describe("pending join", () => {
    it("sets a pending join", () => {
      const join = { leftTableId: "a", rightTableId: "b" };
      act(() => useBuilderStore.getState().setPendingJoin(join));
      expect(useBuilderStore.getState().pendingJoin).toEqual(join);
    });

    it("clears pending join", () => {
      act(() => useBuilderStore.getState().setPendingJoin({ leftTableId: "a", rightTableId: "b" }));
      act(() => useBuilderStore.getState().setPendingJoin(null));
      expect(useBuilderStore.getState().pendingJoin).toBeNull();
    });
  });

  describe("relation settings", () => {
    it("defaults to crowsfoot notation and orthogonal pathing", () => {
      const s = useBuilderStore.getState();
      expect(s.relationNotation).toBe("crowsfoot");
      expect(s.relationPathing).toBe("orthogonal");
    });

    it("changes notation", () => {
      act(() => useBuilderStore.getState().setRelationNotation("uml"));
      expect(useBuilderStore.getState().relationNotation).toBe("uml");
    });

    it("sets terminal override for a join", () => {
      act(() =>
        useBuilderStore.getState().setRelationTerminalOverride("j1", {
          source: "one",
          target: "many",
        }),
      );
      expect(useBuilderStore.getState().relationTerminalOverrides["j1"]).toEqual({
        source: "one",
        target: "many",
      });
    });
  });

  describe("table drawer", () => {
    it("opens and closes", () => {
      act(() => useBuilderStore.getState().openTableDrawer("tbl-1"));
      expect(useBuilderStore.getState().tableDrawerTableId).toBe("tbl-1");
      act(() => useBuilderStore.getState().closeTableDrawer());
      expect(useBuilderStore.getState().tableDrawerTableId).toBeNull();
    });
  });

  describe("connecting mode", () => {
    it("defaults to off", () => {
      expect(useBuilderStore.getState().isConnectingMode).toBe(false);
    });

    it("toggles on and off", () => {
      act(() => useBuilderStore.getState().setConnectingMode(true));
      expect(useBuilderStore.getState().isConnectingMode).toBe(true);
      act(() => useBuilderStore.getState().setConnectingMode(false));
      expect(useBuilderStore.getState().isConnectingMode).toBe(false);
    });

    it("clears connecting mode when the panel is closed", () => {
      act(() => {
        useBuilderStore.getState().openPanel("joins");
        useBuilderStore.getState().setConnectingMode(true);
      });
      act(() => useBuilderStore.getState().closePanel());
      expect(useBuilderStore.getState().activePanel).toBeNull();
      expect(useBuilderStore.getState().isConnectingMode).toBe(false);
    });
  });

  describe("pivot state", () => {
    it("updates partial pivot state", () => {
      act(() => useBuilderStore.getState().setPivotState({ measureId: "m1" }));
      const ps = useBuilderStore.getState().pivotState;
      expect(ps.measureId).toBe("m1");
      expect(ps.rowDimIds).toEqual([]);
    });
  });

  describe("reset", () => {
    it("restores all state to initial values", () => {
      act(() => {
        useBuilderStore.getState().openPanel("measures");
        useBuilderStore.getState().selectObject("x", "source");
        useBuilderStore.getState().setMiniTab("pivot");
      });
      act(() => useBuilderStore.getState().reset());

      const s = useBuilderStore.getState();
      expect(s.activePanel).toBeNull();
      expect(s.selectedObjectId).toBeNull();
      expect(s.miniTab).toBe("canvas");
    });
  });
});
