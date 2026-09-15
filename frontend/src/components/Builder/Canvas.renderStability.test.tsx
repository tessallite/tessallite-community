import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act, waitFor, fireEvent, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// Bug-6373 / F-026-01: the Canvas node/edge hydrate effect depends on
// `flushLayout`, whose dependency array formerly listed the unstable `useT()`
// closure. A new `t` each render meant a new `flushLayout` each render, which
// re-ran the hydrate effect (setNodes/setEdges with fresh arrays) unboundedly —
// a permanent render loop that degenerated into a PATCH storm. No test rendered
// Canvas, which is exactly how the regression shipped green.
//
// This guard mounts Canvas and asserts the hydrate effect settles: the effect
// body calls `partitionJoinsByEndpoints` exactly once per run, so a bounded
// call count proves the loop is gone. Under the bug this count runs into the
// hundreds within the settle window.

const { partitionSpy, updateSpy } = vi.hoisted(() => ({
  partitionSpy: vi.fn(),
  updateSpy: vi.fn().mockResolvedValue({}),
}));

vi.mock("./joinFilter", async (orig) => {
  const actual = await orig<typeof import("./joinFilter")>();
  return {
    ...actual,
    partitionJoinsByEndpoints: (...args: Parameters<typeof actual.partitionJoinsByEndpoints>) => {
      partitionSpy();
      return actual.partitionJoinsByEndpoints(...args);
    },
  };
});

vi.mock("../../api/client", async (orig) => {
  const actual = await orig<typeof import("../../api/client")>();
  return {
    ...actual,
    modelsApi: { ...actual.modelsApi, update: updateSpy },
  };
});

vi.mock("../Confirm/useConfirm", () => ({
  useConfirm: () => vi.fn().mockResolvedValue(true),
}));

// All data hooks return empty result sets — the loop is data-independent.
const emptyQuery = { data: [] } as unknown;
vi.mock("../../api/hooks", () => ({
  useAggregates: () => emptyQuery,
  useDimensions: () => emptyQuery,
  useHierarchiesWithLevels: () => emptyQuery,
  useMeasures: () => emptyQuery,
  usePersona: () => ({ data: null }),
  usePersonas: () => emptyQuery,
  usePockets: () => emptyQuery,
}));

import Canvas from "./Canvas";
import { clearEdgeWaypoints } from "./Canvas";
import { useBuilderStore } from "../../store/builderStore";

function renderCanvas(
  tenantSlug?: string,
  readOnly = false,
  canvasLayout: Record<string, unknown> = {},
  joins: Array<Record<string, unknown>> = [],
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui = (slug?: string, ro = readOnly) => (
    <QueryClientProvider client={qc}>
      <div style={{ width: 800, height: 600 }}>
        <Canvas
          projectId="proj-1"
          modelId="model-1"
          tables={[]}
          joins={joins as never}
          canvasLayout={canvasLayout as never}
          tenantSlug={slug}
          readOnly={ro}
        />
      </div>
    </QueryClientProvider>
  );
  const view = render(ui(tenantSlug));
  return { view, ui };
}

describe("Canvas render stability (Bug-6373)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("does not enter a render loop or PATCH storm after mount", async () => {
    renderCanvas();
    // Let the hydrate effect (and any spurious re-runs) settle well past the
    // 600ms flush debounce so a runaway loop would have fired many times.
    await new Promise((r) => setTimeout(r, 800));

    // The hydrate effect calls partitionJoinsByEndpoints once per run. A stable
    // canvas mounts in a small, bounded number of runs. A render loop makes this
    // count explode. The generous ceiling stays clear of the hundreds-of-runs
    // the regression produced while tolerating StrictMode / settle re-runs.
    expect(partitionSpy.mock.calls.length).toBeLessThan(10);

    // No user edit and a persisted (empty) layout with no unplaced tables means
    // no layout PATCH should ever fire on mount.
    expect(updateSpy).not.toHaveBeenCalled();
  });

  it("does not warn about intentionally hidden calendar endpoints", () => {
    renderCanvas(undefined, false, {}, [{
      id: "calendar-join", left_table_id: "calendar-a", right_table_id: "calendar-b",
      hidden_calendar_table_ids: ["calendar-a", "calendar-b"],
    }]);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("places the hidden-joins warning away from Controls and MiniMap (Bug-9533)", () => {
    renderCanvas(undefined, false, {}, [
      { id: "hidden-join", left_table_id: "hidden-a", right_table_id: "hidden-b" },
    ]);

    const warningPanel = screen.getByRole("alert").closest(".react-flow__panel");
    expect(warningPanel).not.toBeNull();
    // RFGPT-001: bottom-center keeps Controls (bottom-left) and MiniMap
    // (bottom-right) free; do not share either corner's positional classes.
    expect(warningPanel).toHaveClass("bottom", "center");
    expect(warningPanel).not.toHaveClass("left");
    expect(warningPanel).not.toHaveClass("right");

    const controls = document.querySelector('[data-testid="rf__controls"]');
    expect(controls).not.toBeNull();
    expect(controls).toHaveClass("bottom", "left");
    expect(controls).not.toBe(warningPanel);

    fireEvent.click(screen.getByTitle("Model annotations"));
    const notesPanel = screen.getByRole("textbox").closest(".react-flow__panel");
    expect(notesPanel).toBe(warningPanel);
    expect(screen.getByRole("alert").parentElement).toHaveStyle({
      display: "flex",
      flexDirection: "column",
    });
  });

  // User-reported (2026-08-24): the hidden-joins warning had no way to
  // dismiss it and stayed on screen permanently, even after the modeller had
  // already seen it and chose not to add the missing tables.
  it("can be dismissed and stays dismissed across a re-render with the same hidden-join set", () => {
    const oneHidden = [{ id: "hidden-join", left_table_id: "hidden-a", right_table_id: "hidden-b" }];
    const { view, ui } = renderCanvas(undefined, false, {}, oneHidden);

    expect(screen.getByRole("alert")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Close"));
    expect(screen.queryByRole("alert")).toBeNull();

    // Re-rendering with the SAME hidden-join set must not resurrect a warning
    // the user already dismissed.
    act(() => {
      view.rerender(ui(undefined, false));
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  // R3 (alert-mechanism audit, 2026-08-25): this banner used to be a
  // hand-rolled <div> with hardcoded hex colors and a raw "&times;" button —
  // the one message-like element in the app that bypassed MUI's Alert
  // component every panel-local alert (ValidationTray, saveError banners,
  // etc.) already uses. Assert it is genuinely MUI's Alert, not just
  // structurally alert-shaped, so a future regression back to a raw div
  // fails this test even though role="alert" would still be satisfiable by
  // a hand-rolled element.
  it("renders the hidden-joins warning as MUI's Alert component, not a hand-rolled div (R3)", () => {
    renderCanvas(undefined, false, {}, [
      { id: "hidden-join", left_table_id: "hidden-a", right_table_id: "hidden-b" },
    ]);
    const alertEl = screen.getByRole("alert");
    expect(alertEl.className).toMatch(/MuiAlert-root/);
    expect(alertEl.className).toMatch(/MuiAlert-standardWarning/);
  });

  // Reproduces the actual reported scenario: after a layout EDIT the save must
  // settle to a single debounced PATCH, not a storm. Under the regression, the
  // unmount-flush effect listed the unstable `t` in its deps, so every re-render
  // re-ran it and its cleanup re-PATCHed the pending layout — an unbounded storm
  // that fired BEFORE the debounce even elapsed. This drives one committed edit
  // through the window-event channel (a node resize calls the debounced
  // flushLayout), forces several re-renders, and asserts exactly one PATCH.
  it("settles to a single debounced PATCH after a layout edit despite re-renders", async () => {
    const { view, ui } = renderCanvas("slug-0");

    // Commit one layout edit. A node resize is reported through this window
    // event, which merges into layoutRef and schedules the 600ms debounce.
    act(() => {
      window.dispatchEvent(
        new CustomEvent("node-resize-end", {
          detail: { id: "tbl-1", w: 320, h: 240 },
        }),
      );
    });

    // Force several Canvas re-renders (each makes useT() return a fresh closure).
    // With stable effect deps no premature PATCH fires; under the bug the
    // unmount-flush cleanup would have PATCHed on every one of these.
    for (let i = 1; i <= 5; i++) {
      act(() => {
        view.rerender(ui(`slug-${i}`));
      });
    }
    // Debounce still pending — the storm would already have fired here.
    expect(updateSpy).not.toHaveBeenCalled();

    // After the 600ms debounce, exactly one PATCH carrying the edit.
    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(1), {
      timeout: 2000,
    });
    expect(updateSpy).toHaveBeenCalledWith("proj-1", "model-1", {
      canvas_layout: expect.objectContaining({
        tables: expect.objectContaining({
          "tbl-1": expect.objectContaining({ w: 320, h: 240 }),
        }),
      }),
    });

    // Unmount: the debounce already fired and nulled its handle, so no second
    // PATCH is emitted (Bug-6373 cleanup guard).
    act(() => view.unmount());
    expect(updateSpy).toHaveBeenCalledTimes(1);
  });

  // Bug-8504: `flushLayout` is the one primitive every canvas-layout write goes
  // through. Read-only was enforced per call site, so the two edge controls the
  // JoinsPanel dispatches (reset waypoints / toggle pathing) persisted layout
  // for a read-only share-link session. The guard now sits on the primitive, so
  // this asserts the whole write channel is closed, not just one button.
  it("never persists canvas layout in read-only mode, from any write entry point", async () => {
    const { view } = renderCanvas("slug-ro", true);

    act(() => {
      window.dispatchEvent(new CustomEvent("reset-edge-path", { detail: "join-1" }));
      window.dispatchEvent(new CustomEvent("toggle-edge-pathing-auto", { detail: "join-1" }));
      window.dispatchEvent(
        new CustomEvent("node-resize-end", { detail: { id: "tbl-1", w: 320, h: 240 } }),
      );
    });

    // Well past the 600ms debounce.
    await new Promise((r) => setTimeout(r, 900));
    expect(updateSpy).not.toHaveBeenCalled();

    // And the unmount drain must not resurrect a suppressed write either.
    act(() => view.unmount());
    expect(updateSpy).not.toHaveBeenCalled();
  });

  // Bug-8504: the immediate (non-debounced) flush the Canvas registers in the
  // builder store is a second, independent write entry point. The store hands
  // it to any caller without knowing about read-only, so the guard has to live
  // in the Canvas write funnel.
  it("no-ops the store-registered immediate layout flush in read-only mode", async () => {
    renderCanvas("slug-ro2", true);

    const flushNow = useBuilderStore.getState().flushCanvasLayoutNow;
    expect(flushNow).toBeTypeOf("function");
    await act(async () => {
      await flushNow!();
    });
    expect(updateSpy).not.toHaveBeenCalled();
  });

  // Bug-8504 (round 2): suppressing the WRITE is not the same as suppressing
  // the EDIT. The two edge handlers still merged their change into the in-memory
  // layout while read-only, and that memory outlives the read-only state — a
  // `?readonly=1` share link dropped by in-place SPA navigation flips readOnly
  // true -> false on the same Canvas mount. The next legitimate flush then
  // carried an edge change the editing user never made. Asserting on the PATCH
  // body (not just the call count) is what makes this a leak test rather than a
  // repeat of the suppression test above.
  it("does not carry a read-only-era layout edit into the next authorised flush (Bug-8504)", async () => {
    // join-2 already has manual routing, so the reset handler has something real
    // to destroy — with an empty layout its delete branch is a no-op and the
    // test could not tell a guarded handler from an unguarded one.
    const saved = { edges: { "join-2": { waypoints: [{ x: 5, y: 6 }] } } };
    const { view, ui } = renderCanvas("slug-leak", true, saved);

    act(() => {
      window.dispatchEvent(new CustomEvent("toggle-edge-pathing-auto", { detail: "join-1" }));
      window.dispatchEvent(new CustomEvent("reset-edge-path", { detail: "join-2" }));
    });
    await new Promise((r) => setTimeout(r, 50));
    expect(updateSpy).not.toHaveBeenCalled();

    // The session becomes editable on the same mount, and the user makes one
    // real edit.
    act(() => {
      view.rerender(ui("slug-leak", false));
    });
    act(() => {
      window.dispatchEvent(
        new CustomEvent("node-resize-end", { detail: { id: "tbl-1", w: 300, h: 200 } }),
      );
    });

    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(1), { timeout: 2000 });
    const [, , body] = updateSpy.mock.calls[0] as [string, string, { canvas_layout: Record<string, unknown> }];
    expect(body.canvas_layout).toEqual(
      expect.objectContaining({
        tables: expect.objectContaining({ "tbl-1": expect.objectContaining({ w: 300, h: 200 }) }),
      }),
    );
    // Nothing from the read-only period may appear in the persisted layout:
    // no pathing flip invented for join-1, and join-2's manual routing intact.
    expect(body.canvas_layout.edges).toEqual(saved.edges);
  });

  it("performs the store-registered immediate layout flush when editing is allowed", async () => {
    renderCanvas("slug-rw", false);

    const flushNow = useBuilderStore.getState().flushCanvasLayoutNow;
    await act(async () => {
      await flushNow!();
    });
    expect(updateSpy).toHaveBeenCalledTimes(1);
  });
  // Bug-8504 (R2): the Notes ControlButton is hidden by `{!readOnly && …}` but
  // the Notes Panel renders on `notesOpen` alone, so an already-open panel
  // survives a false -> true readOnly flip with a live Save button.
  // `handleSaveNotes` had no entry guard, so the edit landed in layoutRef and
  // rode the next authorised flush. Asserts the PATCH BODY, not the call count.
  it("does not carry a read-only-era NOTES edit into the next authorised flush (Bug-8504)", async () => {
    const saved = { notes: "original notes" };
    const { view, ui } = renderCanvas("slug-notes", false, saved);

    act(() => {
      fireEvent.click(screen.getByTitle("Model annotations"));
    });
    expect(view.container.querySelector("textarea")).toBeTruthy();

    act(() => {
      view.rerender(ui("slug-notes", true));
    });
    const ta = view.container.querySelector("textarea") as HTMLTextAreaElement | null;
    if (ta) {
      act(() => {
        fireEvent.change(ta, { target: { value: "UNAUTHORISED read-only edit" } });
      });
      act(() => {
        fireEvent.click(screen.getByText("Save"));
      });
    }
    await new Promise((r) => setTimeout(r, 50));
    expect(updateSpy).not.toHaveBeenCalled();

    act(() => {
      view.rerender(ui("slug-notes", false));
    });
    act(() => {
      window.dispatchEvent(
        new CustomEvent("node-resize-end", { detail: { id: "tbl-1", w: 300, h: 200 } }),
      );
    });

    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(1), { timeout: 2000 });
    const [, , body] = updateSpy.mock.calls[0] as [
      string,
      string,
      { canvas_layout: Record<string, unknown> },
    ];
    expect(body.canvas_layout.notes).toBe("original notes");
  });

  // R10 / spec 5: "never silently drop unknown presentation fields". A saved
  // layout can carry fields this build does not know about — written by a newer
  // build, or by a feature added since — and an ordinary edit must not delete
  // them. The resize channel is used because it is a real write path that can
  // be driven in jsdom; the merge rule itself is covered in
  // presentationEntries.test.ts.
  it("keeps presentation fields it does not understand through a save (R10)", async () => {
    const saved = {
      notes: "kept",
      customTheme: { accent: "#123456" },
      tables: { "tbl-1": { x: 11, y: 22, w: 100, h: 80, pinned: true, collapsed: true } },
      edges: { j1: { waypoints: [{ x: 5, y: 6 }], annotation: "reviewed" } },
    };
    renderCanvas("slug-retain", false, saved);
    updateSpy.mockClear();

    act(() => {
      window.dispatchEvent(
        new CustomEvent("node-resize-end", { detail: { id: "tbl-1", w: 300, h: 200 } }),
      );
    });
    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(1), { timeout: 2000 });

    const [, , body] = updateSpy.mock.calls[0] as [
      string,
      string,
      { canvas_layout: Record<string, any> },
    ];
    const layout = body.canvas_layout;
    // The edit itself applied…
    expect(layout.tables["tbl-1"].w).toBe(300);
    expect(layout.tables["tbl-1"].h).toBe(200);
    // …and nothing else was lost: unknown top-level, unknown per-table,
    // per-table state this build DOES know (the pin), and unknown per-edge.
    expect(layout.customTheme).toEqual({ accent: "#123456" });
    expect(layout.tables["tbl-1"].collapsed).toBe(true);
    expect(layout.tables["tbl-1"].pinned).toBe(true);
    expect(layout.tables["tbl-1"].x).toBe(11);
    expect(layout.tables["tbl-1"].y).toBe(22);
    expect(layout.edges.j1.annotation).toBe("reviewed");
    expect(layout.edges.j1.waypoints).toEqual([{ x: 5, y: 6 }]);
    expect(layout.notes).toBe("kept");
  });

  // R10: the retention rule has to hold at EVERY write site, not just the ones
  // a jsdom test can drive. The drag path is the one that broke it — it rebuilt
  // each table entry listing only w and h, which deleted `pinned` on the first
  // drag of a pinned table — and a drag gesture cannot be driven through React
  // Flow here. So the rule is pinned structurally: no writer may construct a
  // table entry itself.
  it("writes every table entry through the merge that keeps unknown fields (R10)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    const writes = src
      .split("\n")
      .map((line, index) => ({ line: line.trim(), index }))
      .filter(({ line }) => /^tableMap\[[^\]]+\]\s*=/.test(line));

    // Five writers touch the table map: undo/redo apply, the pin toggle, the
    // redraw apply, the resize handler and the drag handler.
    expect(writes.length).toBe(5);
    for (const { line, index } of writes) {
      expect(line, `table write at line ${index + 1} does not use mergeTableEntry`).toMatch(
        /=\s*mergeTableEntry\(/,
      );
    }

    // The edge map has one writer that rewrites a whole entry (the redraw); the
    // rest merge in place with a spread of the previous entry.
    expect(src).toMatch(/edgeMap\[route\.edgeId\] = mergeEdgeEntry\(previous, \{/);
  });

  // R10 / Bug-7634: navigating between models in place must not let model A's
  // layout or preferences reach model B. The history stacks are covered in
  // useCanvasHistory.test.tsx; this is the canvas side — the saved layout it
  // holds, and the panel state seeded from it.
  it("does not carry model A's layout or preferences into model B (R10)", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const ui = (modelId: string, canvasLayout: Record<string, unknown>) => (
      <QueryClientProvider client={qc}>
        <div style={{ width: 800, height: 600 }}>
          <Canvas
            projectId="proj-1"
            modelId={modelId}
            tables={[]}
            joins={[] as never}
            canvasLayout={canvasLayout as never}
            tenantSlug="slug-model-switch"
            readOnly={false}
          />
        </div>
      </QueryClientProvider>
    );

    const modelA = {
      notes: "model A notes",
      layoutOptions: { preset: "radial", direction: "RIGHT", spacing: "dense" },
      tables: { "tbl-1": { x: 1, y: 2 } },
    };
    const modelB = {
      notes: "model B notes",
      layoutOptions: { preset: "compact", direction: "DOWN", spacing: "normal" },
      tables: { "tbl-9": { x: 90, y: 90 } },
    };

    const view = render(ui("model-a", modelA));
    act(() => {
      fireEvent.click(screen.getByTitle("Layout presets"));
    });
    expect(screen.getByRole("button", { name: "Radial" }).getAttribute("aria-pressed")).toBe("true");

    updateSpy.mockClear();
    act(() => {
      view.rerender(ui("model-b", modelB));
    });

    // The panel reseeds from model B's own record, not A's choice. It is still
    // open across the in-place navigation, so it is not reopened here — the
    // toolbar control toggles.
    expect(screen.getByRole("button", { name: "Compact Grid" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Top to bottom" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Normal" }).getAttribute("aria-pressed")).toBe("true");

    // And nothing from A is written anywhere, to either model.
    await new Promise((r) => setTimeout(r, 900));
    for (const call of updateSpy.mock.calls) {
      const [, , body] = call as [string, string, { canvas_layout?: Record<string, any> }];
      expect(body.canvas_layout?.notes).not.toBe("model A notes");
      expect(body.canvas_layout?.tables?.["tbl-1"]).toBeUndefined();
    }
  });

  // R10: opening a model must not rewrite its saved layout. A mount that
  // normalises or re-persists what it read would rewrite every legacy layout
  // the first time it was viewed — including from a session that changed
  // nothing.
  it("mounting a legacy layout writes nothing (R10)", async () => {
    const legacy = {
      tables: { "tbl-1": { x: 40, y: 50 } },
      edges: { j1: { waypoint: { x: 7, y: 8 }, pathing: "straight", sourceSide: "top" } },
    };
    renderCanvas("slug-legacy", false, legacy);

    // Well past the 600 ms flush debounce.
    await new Promise((r) => setTimeout(r, 900));
    expect(updateSpy).not.toHaveBeenCalled();
  });

  // Bug-7401: the notes textarea had no maxLength and no remaining-character
  // counter, so a user typing an unbounded note had no feedback until a save
  // silently accepted (or later truncated) whatever they had written.
  it("caps the notes textarea at 2000 characters and shows a live counter", () => {
    const { view } = renderCanvas("slug-notes-cap", false, { notes: "" });

    act(() => {
      fireEvent.click(screen.getByTitle("Model annotations"));
    });
    const ta = view.container.querySelector("textarea") as HTMLTextAreaElement;
    expect(ta).toBeTruthy();
    expect(ta.maxLength).toBe(2000);
    expect(screen.getByText("0 / 2000")).toBeTruthy();

    act(() => {
      fireEvent.change(ta, { target: { value: "hello" } });
    });
    expect(screen.getByText("5 / 2000")).toBeTruthy();
  });

  // Bug-8504 (R2): same open-panel-survives-the-flip mechanism on the layout
  // preset menu, but destructive — `handleRedrawLayout` strips waypoint /
  // waypoints from EVERY edge. A preset click in a read-only session wiped the
  // editing user's manual edge routing, and the wipe persisted on the next
  // authorised flush.
  it("does not carry a read-only-era LAYOUT REDRAW into the next authorised flush (Bug-8504)", async () => {
    const saved = {
      tables: { "tbl-1": { x: 111, y: 222 } },
      edges: { "join-2": { waypoints: [{ x: 5, y: 6 }] } },
    };
    const { view, ui } = renderCanvas("slug-redraw", false, saved);

    act(() => {
      fireEvent.click(screen.getByTitle("Layout presets"));
    });
    expect(screen.getByText("Radial")).toBeTruthy();

    act(() => {
      view.rerender(ui("slug-redraw", true));
    });
    const radial = screen.queryByText("Radial");
    if (radial) {
      act(() => {
        fireEvent.click(radial);
      });
    }
    await new Promise((r) => setTimeout(r, 50));
    expect(updateSpy).not.toHaveBeenCalled();

    act(() => {
      view.rerender(ui("slug-redraw", false));
    });
    act(() => {
      window.dispatchEvent(
        new CustomEvent("node-resize-end", { detail: { id: "tbl-9", w: 10, h: 10 } }),
      );
    });

    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(1), { timeout: 2000 });
    const [, , body] = updateSpy.mock.calls[0] as [
      string,
      string,
      { canvas_layout: Record<string, unknown> },
    ];
    // The read-only preset click must not have destroyed join-2's manual routing.
    expect(body.canvas_layout.edges).toEqual(saved.edges);
  });
  // Bug-8504 (R3): R2's two leak tests guard their click behind `if (ta)` /
  // `if (radial)` precisely because the panel gate now hides the control, so
  // neither of them can fail if the gate is removed. These two assert the gate
  // itself — the affordance must be withdrawn on the flip, not left live for
  // the entry guard to catch.
  it("withdraws the open Notes panel when the session turns read-only (Bug-8504)", () => {
    const { view, ui } = renderCanvas("slug-notes-gate", false, { notes: "original" });
    act(() => {
      fireEvent.click(screen.getByTitle("Model annotations"));
    });
    expect(view.container.querySelector("textarea")).not.toBeNull();
    expect(screen.queryByText("Save")).not.toBeNull();

    act(() => {
      view.rerender(ui("slug-notes-gate", true));
    });
    expect(view.container.querySelector("textarea")).toBeNull();
    expect(screen.queryByText("Save")).toBeNull();
  });

  it("withdraws the open layout-preset menu when the session turns read-only (Bug-8504)", () => {
    const { view, ui } = renderCanvas("slug-preset-gate", false, {});
    act(() => {
      fireEvent.click(screen.getByTitle("Layout presets"));
    });
    expect(screen.queryByText("Radial")).not.toBeNull();
    expect(screen.queryByText("Hierarchical")).not.toBeNull();
    expect(screen.queryByText("Compact Grid")).not.toBeNull();

    act(() => {
      view.rerender(ui("slug-preset-gate", true));
    });
    expect(screen.queryByText("Radial")).toBeNull();
    expect(screen.queryByText("Hierarchical")).toBeNull();
    expect(screen.queryByText("Compact Grid")).toBeNull();
  });

  // R06: a lock freezes the path the user can SEE. An automatic route has no
  // stored path of its own, so the lock has to write the displayed geometry
  // down — otherwise it freezes nothing and the next reload recomputes a
  // different path. Edge selection cannot be driven through React Flow in
  // jsdom, so the wiring is pinned at the source; the rule itself is covered
  // behaviourally by routeLock.test.ts and edgeRouting.test.ts.
  it("captures the displayed route when locking, and records one undo entry (R06)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );

    // The capture comes from the renderer's own resolution, not a second
    // opinion about where the route ought to go.
    expect(src).toMatch(/const resolved = displayedRouteFor\(edge, live, routeContext\)/);
    expect(src).toMatch(
      /capture: isFreezableRoute\(resolved\.route, resolved\.pathMode\) \? resolved\.route : null/,
    );
    // And the persisted entry comes from the shared lock rule.
    expect(src).toMatch(/const entry = applyRouteLock\(\{/);

    // One undo entry, carrying the complete before/after edge layout. Positions
    // are unchanged, which is why both sides of recordMove are the same map.
    // Bound the slice at the handler's OWN dependency array, not at whatever
    // declaration happens to follow it: a later handler inserted between the
    // two made this read a second recordMove call and report it as this one's.
    const handler = src.slice(src.indexOf("const toggleRouteLockFor = useCallback"));
    const body = handler.slice(0, handler.indexOf("}, [flushLayout, setEdges, recordMove, nodes]);"));
    expect(body).toMatch(/const positions = positionsFromNodes\(nodes\)/);
    expect(body).toMatch(/recordMove\(positions, positions, \{/);
    expect((body.match(/recordMove\(/g) ?? []).length).toBe(1);

    // Undo/redo of the edge layout restores the lock too, or an undo would
    // leave a frozen route unfrozen.
    expect(src).toMatch(/locked: entry\?\.locked === true/);
  });

  // R06: the two refusals a lock imposes, which the specification treats
  // differently on purpose. An endpoint never moves at all; any other card may
  // be moved freely and is only put back if the FINISHED gesture left it across
  // a locked path. Pinned at the source because a React Flow drag gesture
  // cannot be driven in jsdom; the rules themselves are covered behaviourally
  // by lockedRoutes.test.ts.
  it("refuses an endpoint move outright and an intrusion at gesture end (R06)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );

    // The endpoint change is dropped before React Flow sees it, so the card
    // does not travel and snap back — it does not move.
    expect(src).toMatch(/if \(ch\.type === "position" && frozen\.has\(ch\.id\)\)/);
    expect(src).toMatch(/refusedFrozenMove = true/);
    // …and the user is told which action releases it, once per gesture.
    expect(src).toMatch(/frozenMoveNoticeRef\.current = true/);
    expect(src).toMatch(/canvas\.lockedEndpointRefused/);
    // Resizing the same card is refused on the event channel too, so no other
    // caller of it can change the persisted size.
    expect(src).toMatch(/if \(lockedEndpointsRef\.current\.has\(id\)\) \{/);

    // The intrusion check runs after the gesture commits, restores the previous
    // geometry, and records no undo entry — the gesture did not happen.
    const repair = src.slice(src.indexOf("const handleRepairRoutes = useCallback"));
    const body = repair.slice(0, repair.indexOf("}, [layoutController,"));
    // Against the captured RECTANGLES, not the positions. A bottom/right
    // resize leaves x and y untouched, so a position-only comparison reported
    // "no card changed" while the card grew straight across the frozen line —
    // the one obstruction a locked route cannot route around (external review
    // C02).
    expect(body).toMatch(/const intruded = lockedRouteIntrusionsNow\(before\.rects\)/);
    expect(body).toMatch(/restoreLayoutState\(before\)/);
    const refusal = body.slice(body.indexOf("if (intruded.length)"), body.indexOf("pendingLayoutBeforeRef.current = before"));
    expect(refusal).not.toMatch(/recordMove\(/);
  });

  it("marks a card held by a locked relationship, and only when it changes", () => {
    // The flag drives the card's resize handles. The updater must return the
    // same array when nothing changed, or this becomes a render loop on a
    // canvas that has a documented history of them (Bug-6373).
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    expect(src).toMatch(/lockedByRoute: frozen/);
    expect(src).toMatch(/return changed \? next : ns;/);

    const node = readFileSync(
      resolve(process.cwd(), "src/components/Builder/ERDTableNode.tsx"),
      "utf8",
    );
    expect(node).toMatch(/isVisible=\{!readOnly && !lockedByRoute\}/);
    expect(node).toMatch(/if \(readOnly \|\| lockedByRoute\) return;/);
  });

  // R08 / spec 4: manual orthogonal editing must keep every segment
  // horizontal or vertical, and "restore pre-gesture layout rather than
  // persisting a broken connector". A pointer drag cannot be driven through
  // React Flow in jsdom, so the wiring is pinned at the source; the repair and
  // the validity rules are covered behaviourally in edgeRouting.test.ts.
  it("repairs an orthogonal bend drag and refuses to persist a broken one (R08)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/CrowsFootEdge.tsx"),
      "utf8",
    );

    // The bend drag goes through the repair in orthogonal mode.
    expect(src).toMatch(/if \(isOrtho\) \{\s*\n\s*return applyOrthoBendDrag\(/);
    // The commit is gated: an invalid edit is dropped, which leaves the stored
    // route untouched and is therefore the restore.
    expect(src).toMatch(/dr\.data\?\.onWaypointsChange && manualEditIsValid\(finalWps\)/);
    // Validity is the shared rule, plus "not dragged inside its own card".
    expect(src).toMatch(/isDrawableRoute\(\[\{ x: psx, y: psy \}, \.\.\.wps, \{ x: ptx, y: pty \}\]/);
    expect(src).toMatch(/routeEntersCard\(wps, live\.srcRect\)/);
    expect(src).toMatch(/routeEntersCard\(wps, live\.tgtRect\)/);
  });

  // R09: selecting a relationship has to light up the whole thing — the
  // connector and both tables it connects — and expose what it joins on, with
  // the join detail reachable from the keyboard. React Flow selection cannot be
  // driven in jsdom, so the wiring is pinned at the source.
  it("highlights a selected relationship's end cards and exposes its columns (R09)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    // Both end cards of the selected relationship are marked, in the same pass
    // that marks lock-frozen cards, and the pass bails out when nothing changed.
    expect(src).toMatch(/const joinHighlightIds = useMemo/);
    expect(src).toMatch(/joinHighlighted: highlighted/);
    expect(src).toMatch(/return changed \? next : ns;/);
    // The columns and the open action travel with the edge.
    expect(src).toMatch(/sourceColumn: j\.left_column_name \?\? null/);
    expect(src).toMatch(/targetColumn: j\.right_column_name \?\? null/);

    const edge = readFileSync(
      resolve(process.cwd(), "src/components/Builder/CrowsFootEdge.tsx"),
      "utf8",
    );
    // The columns are shown on the connector. This surface is VISUAL only:
    // React Flow renders edge labels inside an `aria-hidden="true"` container,
    // so a control here is invisible to assistive technology however it is
    // built. The comment records that, and the wrapper must not swallow canvas
    // drags.
    expect(edge).toMatch(/<EdgeLabelRenderer>/);
    expect(edge).toMatch(/pointerEvents: "none"/);
    expect(edge).toMatch(/aria-hidden/);
    // No button here: selecting the relationship has already opened its detail.
    expect(edge).not.toMatch(/canvas\.openJoin/);

    const node = readFileSync(
      resolve(process.cwd(), "src/components/Builder/ERDTableNode.tsx"),
      "utf8",
    );
    expect(node).toMatch(/data-join-highlighted/);
    expect(node).toMatch(/erdNode\.joinEndpoint/);
  });

  // A locked relationship's flag is persisted and read by the worker, but it
  // also has to come back into the edge the renderer and the panel read, or the
  // route reopens unlocked.
  it("carries the persisted route lock back into the edge on hydrate (R06, R10)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    expect(src).toMatch(/locked: savedEdge\?\.locked === true/);
  });

  // R06: a pin protects a table from automatic placement. `tables[id].pinned`
  // was persisted and read by the worker's snapshot builder from node data
  // that nothing ever wrote, so a pinned table reopened movable and no control
  // could set one. Node selection cannot be driven in jsdom, so the wiring is
  // pinned at the source; the history half is covered behaviourally in
  // useCanvasHistory.test.tsx.
  it("pins the selected tables as one undo entry, and reloads them pinned (R06)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );

    // Hydration carries the saved pin into node data.
    expect(src).toMatch(/const pinned = saved\?\.pinned === true/);
    expect(src).toMatch(/data: \{ \.\.\.n\.data, pinned \}/);

    // A mixed selection pins the rest rather than unpinning the pinned ones.
    expect(src).toMatch(/const nextPinned = pinState !== "pinned"/);

    // The undo entry carries a TABLE snapshot: a pin changes no coordinate, so
    // a position diff cannot see it and the entry would undo nothing.
    const handler = src.slice(src.indexOf("const handleTogglePin = useCallback"));
    const body = handler.slice(0, handler.indexOf("}, [selectedTablePins,"));
    expect(body).toMatch(/recordMove\(positions, positions, undefined, \{/);
    expect((body.match(/recordMove\(/g) ?? []).length).toBe(1);

    const node = readFileSync(
      resolve(process.cwd(), "src/components/Builder/ERDTableNode.tsx"),
      "utf8",
    );
    expect(node).toMatch(/data-pinned/);
    expect(node).toMatch(/erdNode\.pinnedTable/);
    // A locked object is visually distinguishable, not only announced.
    expect(node).toMatch(/LOCKED_ACCENT/);
  });

  // R2-03/04/05 (external review): a gesture was three loosely-coupled patches —
  // candidate geometry persisted before the routes were validated, resize undo
  // with no dimensions, and one shared before-state that an older gesture could
  // clear out from under a newer one. These pin the transaction the three were
  // replaced with. A React Flow drag cannot be driven in jsdom, so the wiring is
  // pinned at the source; the controller half is covered behaviourally in
  // useCanvasLayout.test.tsx.
  it("captures table geometry before a resize changes it (R2-04)", async () => {
    // Capturing afterwards recorded the NEW width and height as the "before"
    // state, so an undo restored the old connector while leaving the card
    // resized — the attachment mismatch the repair had just corrected.
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    // Bounded at the NEXT handler's declaration, not at the first "};" — the
    // handler body contains several of those.
    const handler = src.slice(src.indexOf("const handleNodeResize = (e: Event)"));
    const body = handler.slice(0, handler.indexOf("const handleCenterNode"));
    const capture = body.indexOf("captureLayoutState()");
    const write = body.indexOf("mergeTableEntry(tableMap[id], { w, h })");
    expect(capture, "the resize must capture state").toBeGreaterThan(0);
    expect(capture, "capture must happen BEFORE the new size is written").toBeLessThan(write);

    // The captured state carries the table map, which is what holds w/h — a
    // position map cannot express a resize.
    expect(src).toMatch(/const captureLayoutState = useCallback\(\(\): LayoutTransactionState/);
    expect(src).toMatch(/tables: JSON\.parse\(JSON\.stringify\(layout\.tables \?\? \{\}\)\)/);
    // …and the commit records it alongside the edge snapshot, in ONE entry.
    expect(src).toMatch(/\{ before: before\.tables, after: JSON\.parse\(JSON\.stringify\(tableMap\)\)/);
  });

  it("does not persist candidate geometry before the routes are repaired (R2-03)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    // A drag no longer flushes per position change: the coordinates are
    // candidate geometry until the routes are repaired against them, and on a
    // drag longer than the debounce it also emitted a PATCH mid-gesture.
    expect(src).toMatch(/if \(dirty && !isDraggingRef\.current\) flushLayout\(\);/);
    // The resize writes the new size without flushing it, for the same reason.
    const handler = src.slice(src.indexOf("const handleNodeResize = (e: Event)"));
    const body = handler.slice(0, handler.indexOf("const handleCenterNode"));
    expect(body).not.toMatch(/flushLayout\(\)/);
  });

  it("lets only the live gesture commit, restore or clear (R2-05)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    // Each gesture takes an identity when it starts…
    expect(src).toMatch(/liveGestureRef\.current = \+\+gestureSeqRef\.current/);

    // …and that identity is now the full ownership token, not the gesture
    // counter alone. The counter says "no newer gesture started"; it says
    // nothing about which MODEL is on the canvas, and the builder reuses this
    // mount when navigating to an already-cached model. A repair started on
    // model A could finish after the shared refs held model B.
    expect(src).toMatch(/const token = claimCanvas\(\);/);
    const handler = src.slice(src.indexOf("const handleRepairRoutes = useCallback"));
    const body = handler.slice(0, handler.indexOf("}, ["));
    expect(body).toMatch(/if \(!stillOwnsCanvas\(token\)\) \{/);

    // An operation that has lost ownership must not write, restore or record.
    const superseded = body.slice(body.indexOf("if (!stillOwnsCanvas(token)) {"));
    const upToReturn = superseded.slice(0, superseded.indexOf("return;"));
    expect(upToReturn, "a superseded gesture must not clear anything").not.toMatch(/= null/);
    expect(upToReturn).not.toMatch(/recordMove\(|restoreLayoutState\(|flushLayout\(/);
  });

  it("restores only when the engine says the geometry cannot be drawn (R2-03)", () => {
    // The distinction that matters: "the engine looked and refused" is evidence
    // the edit is bad, so the gesture is undone. "The engine never answered" is
    // not, and discarding the user's work for an infrastructure failure would
    // be worse than leaving the routes unrepaired.
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    const handler = src.slice(src.indexOf("const handleRepairRoutes = useCallback"));
    const body = handler.slice(0, handler.indexOf("}, ["));

    // The decision comes from ONE policy rather than a list of codes spelled
    // out here. The list was the defect: it read "restore for geometry-invalid
    // or no-route, otherwise keep", and every untyped rejection reached the
    // canvas as `unknown`, so a genuine geometry rejection fell through to the
    // keep-and-save branch. dispositionFor() is covered behaviourally in
    // layout/failurePolicy.test.ts, including that `unknown` never keeps.
    expect(body).toMatch(/const disposition = dispositionFor\(outcome\.failure \?\? "unknown"\);/);

    const restoring = body.slice(body.indexOf('if (disposition === "discard")'));
    const restoreBranch = restoring.slice(0, restoring.indexOf("return;"));
    expect(restoreBranch).toMatch(/restoreLayoutState\(before\)/);
    // No history entry: the gesture did not happen.
    expect(restoreBranch).not.toMatch(/recordMove\(/);
    // The other path keeps the edit and records it.
    const keeping = body.slice(body.indexOf("The engine never rendered a verdict"));
    expect(keeping).toMatch(/recordMove\(before\.positions, after/);
  });

  it("announces a crowded arrangement rather than degrading silently (Bug-10032)", () => {
    // The engine arranges to the best reasonable effort instead of refusing to
    // draw a dense diagram. An unannounced degrade is worse than the refusal it
    // replaced: the modeller cannot tell a crowded arrangement from a correct
    // one.
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    expect(src).toMatch(/result\.metrics\.throughNodeSegmentCount > 0/);
    expect(src).toMatch(/canvas\.layoutCrowded/);
  });

  // Spec 5 (`layoutOptions`): the panel preferences are part of the saved
  // presentation, so reopening a model must reopen it with the arrangement the
  // user last applied — not with the engine defaults.
  it("seeds the panel from the saved layout options (R03, R10)", () => {
    renderCanvas("slug-opts-seed", false, {
      layoutOptions: { preset: "radial", direction: "RIGHT", spacing: "dense" },
    });
    act(() => {
      fireEvent.click(screen.getByTitle("Layout presets"));
    });

    expect(screen.getByRole("button", { name: "Radial" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Left to right" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Compact" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("falls back to validated defaults for absent and unknown saved options (R10)", () => {
    // A value from a newer build, a hand-edited layout, or a partially written
    // record must not put the panel into a state the engine will not honour:
    // the reader is the engine's own validator, applied field by field, so
    // `spacing` survives even though the other two are rejected.
    renderCanvas("slug-opts-unknown", false, {
      layoutOptions: { preset: "spiral", direction: "SIDEWAYS", spacing: "dense" },
    });
    act(() => {
      fireEvent.click(screen.getByTitle("Layout presets"));
    });

    expect(screen.getByRole("button", { name: "Hierarchical" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Top to bottom" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Compact" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("does not persist a preference that was merely chosen (spec 5)", async () => {
    // Spec 5 stores the "last successfully applied" options. Writing on every
    // toggle would reopen the model with an arrangement that was never drawn.
    renderCanvas("slug-opts-choice", false, {});
    act(() => {
      fireEvent.click(screen.getByTitle("Layout presets"));
    });
    updateSpy.mockClear();

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Left to right" }));
    });
    expect(screen.getByRole("button", { name: "Left to right" }).getAttribute("aria-pressed")).toBe("true");

    // Well past the 600 ms flush debounce.
    await new Promise((r) => setTimeout(r, 800));
    const optionWrites = updateSpy.mock.calls.filter(
      (call) => (call[2] as { canvas_layout?: { layoutOptions?: unknown } })?.canvas_layout?.layoutOptions !== undefined,
    );
    expect(optionWrites).toHaveLength(0);
  });

  // The worker transport cannot be driven in jsdom, so the success gate itself
  // is pinned at the source: a regression to persisting before the result is
  // known would save options for an arrangement that failed.
  it("persists the layout options only once the arrangement succeeded (spec 5)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    expect(src).toMatch(/if \(applied\) persistLayoutPreferences\(applying\)/);
    expect(src).toMatch(/if \(applied\) persistLayoutPreferences\(layoutPreferences\)/);
    // And the writer is copy-on-write: layoutRef aliases the query cache
    // object, so an in-place write would desynchronise the cache (Bug-8762).
    expect(src).toMatch(/layoutRef\.current = \{ \.\.\.layoutRef\.current, layoutOptions:/);
  });

  // Bug-8504 (R3): the guard set was derived from the bug report twice and
  // missed a writer both times. The doc-comment table in Canvas.tsx is now the
  // designated enumeration; this pins it to the grep so adding a twelfth
  // writer fails here until it is classified in the table.
  it("keeps the layoutRef write-site table in step with the source (Bug-8504)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    const sites = src
      .split("\n")
      .filter((line) => /layoutRef\.current\s*=/.test(line) && !/^\s*\*/.test(line));
    // 14 after initial auto-placement was removed as a write site (the canvas
    // stopped placing tables on the render thread; the worker batch applies its
    // positions through the guarded handleRedrawLayout writer), then 15 with
    // persistLayoutPreferences, which writes the layoutOptions an arrangement
    // actually succeeded with, then 16 with handleToggleRouteLock, which writes
    // the frozen route geometry alongside the lock flag, then 18 with
    // handleTogglePin and its undo/redo counterpart applyTableLayouts, then 20
    // with the gesture transaction: restoreLayoutState (rollback) and the
    // keep-geometry path taken when the engine never rendered a verdict, then
    // 19 when the two inline attachment-drag writers were replaced by the
    // single commitAttachmentChange transaction (external review C08), then 16
    // when Reset Path, Toggle Path Style and the two bend writers were replaced
    // by the single commitRouteEdit boundary (C01/C09), then 17 when Reroute
    // Links gained the write that returns overridden relationships to the
    // model-wide Edge Pathing setting.
    expect(sites.length).toBe(17);
    expect(src).toContain("ENUMERATION OF EVERY `layoutRef.current =` WRITE SITE");
  });

  // Bug-8763: the count pin proves the table lists the right NUMBER of sites,
  // but a mutation can delete one handler's entry guard without changing the
  // `layoutRef.current =` count — the count-based test stayed green while
  // seven guards were removable. Validate guarded IDENTITY: every writer the
  // enumeration classifies as "guarded" must actually contain its read-only
  // entry guard before its own layoutRef write, and the table must classify
  // each of them (and only the two system-derived writers) correctly.
  it("validates each guarded write site carries its entry guard (Bug-8763)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    const lines = src.split("\n");
    // Writer name -> the line that DEFINES that writer in the source. The
    // regexes pin the definition shapes so a renamed writer fails here rather
    // than silently dropping out of the guard audit.
    const GUARDED_WRITERS: Array<{ name: string; def: RegExp }> = [
      { name: "applyMovePositions", def: /const applyMovePositions\s*=\s*useCallback/ },
      { name: "applyEdgeLayouts", def: /const applyEdgeLayouts\s*=\s*useCallback/ },
      { name: "persistLayoutPreferences", def: /const persistLayoutPreferences\s*=\s*useCallback/ },
      // The writer takes the relationship explicitly. Making the argument
      // optional and falling back to the selection silently broke the panel
      // button, because onClick hands React's MouseEvent to the first
      // parameter — truthy, no capture, so it returned without locking.
      { name: "toggleRouteLockFor", def: /const toggleRouteLockFor\s*=\s*useCallback/ },
      { name: "applyTableLayouts", def: /const applyTableLayouts\s*=\s*useCallback/ },
      { name: "restoreLayoutState", def: /const restoreLayoutState\s*=\s*useCallback/ },
      { name: "handleTogglePin", def: /const handleTogglePin\s*=\s*useCallback/ },
      { name: "handleRedrawLayout", def: /const handleRedrawLayout\s*=\s*useCallback/ },
      { name: "handleSaveNotes", def: /const handleSaveNotes\s*=\s*useCallback/ },
      { name: "handleNodeResize", def: /const handleNodeResize\s*=\s*\(e: Event\)/ },
      { name: "handleNodesChange", def: /const handleNodesChange\s*=\s*useCallback/ },
      // Reset Path, Toggle Path Style, bend editing and bend reset were four
      // inline writers, each responsible for remembering the rules on its own.
      // Two of them forgot the route lock entirely and all four forgot history.
      // They delegate to one guarded, lock-aware, history-recording committer.
      { name: "commitRouteEdit", def: /const commitRouteEdit\s*=\s*useCallback/ },
      // Reroute Links clears the per-relationship pathing overrides an earlier
      // build wrote onto every edge, which is what made the model-wide Edge
      // Pathing setting appear to do nothing.
      { name: "handleApplyLayout", def: /const handleApplyLayout\s*=\s*useCallback/ },
      // Both attachment-drag callbacks now delegate to one guarded transaction
      // rather than writing inline, so there is one writer to audit, not two
      // copies that had to be kept in step.
      { name: "commitAttachmentChange", def: /const commitAttachmentChange\s*=\s*useCallback/ },
    ];

    // Bug-9568 (DR-12): resolve every writer's definition line FIRST, then
    // bound each writer's write-site search at the NEXT writer's definition
    // (source order, not list order) rather than searching unboundedly
    // forward. Without this, a writer that lost its own `layoutRef` write
    // finds the NEXT writer's write instead, and the guard-window check below
    // then passes on that next writer's guard — borrowed evidence, not proof
    // of the reported writer's own guard.
    const defIndices = GUARDED_WRITERS.map(({ name, def }) => {
      const defIdx = lines.findIndex((l) => def.test(l));
      expect(defIdx, `${name}: writer definition not found`).toBeGreaterThanOrEqual(0);
      return defIdx;
    });
    const sortedDefIndices = [...defIndices].sort((a, b) => a - b);

    for (let w = 0; w < GUARDED_WRITERS.length; w++) {
      const { name } = GUARDED_WRITERS[w]!;
      const defIdx = defIndices[w]!;
      const boundIdx = sortedDefIndices.find((idx) => idx > defIdx) ?? lines.length;

      // The writer's own write site is the first layoutRef assignment at or
      // after its definition line, and before the next writer's definition.
      const writeIdx = lines.findIndex(
        (l, i) => i >= defIdx && i < boundIdx && /layoutRef\.current\s*=/.test(l),
      );
      expect(writeIdx, `${name}: layoutRef write not found after its definition (within its own body)`).toBeGreaterThanOrEqual(0);

      // The entry guard must live INSIDE the writer, before its write, AND
      // must actually exit the writer (a guard that only logs/no-ops without
      // returning is not a guard).
      //
      // Bug-9568 round 2 (R2-06): the previous version matched `/\breturn\b/`
      // anywhere in a 6-line vicinity window after the guard line — a `return`
      // token from ANY nearby unrelated code (an inline arrow-function body,
      // a different callback) satisfied it, so a guard disabled to a bare
      // `if (readOnly) { /* no-op */ }` still passed as long as some unrelated
      // `return` happened to sit within 6 lines. Verified by two mutations at
      // handleSaveNotes: removing its `return;` alone made the old check pass
      // (18/18 green) as long as one nearby unrelated `return` existed.
      //
      // The fix asserts on the GUARD STATEMENT itself, not a vicinity window:
      // - the single-line form `if (readOnly...) return;` (11 of 12 writers)
      //   must have `return` ON THE SAME LINE as the `if`;
      // - the one block-form guard (`handleSaveNotes`) opens a `{` and must
      //   contain a `return` before its OWN matching closing `}` (brace-depth
      //   tracked, not a fixed line window).
      // Any guard shape that matches neither pattern fails CLOSED (an
      // "unrecognised guard shape" failure), rather than being silently
      // treated as passing.
      const windowLines = lines.slice(defIdx, writeIdx);
      const guardLineIdx = windowLines.findIndex((l) =>
        /if\s*\(\s*readOnly(?:Ref\.current)?\s*\)/.test(l),
      );
      expect(guardLineIdx, `${name}: entry guard missing before its layoutRef write`).toBeGreaterThanOrEqual(0);
      const guardLine = windowLines[guardLineIdx]!;

      const singleLineReturn = /if\s*\(\s*readOnly(?:Ref\.current)?\s*\)\s*return\b/.test(guardLine);
      const opensBlock = /if\s*\(\s*readOnly(?:Ref\.current)?\s*\)\s*\{/.test(guardLine);

      if (singleLineReturn) {
        // Correct: return is part of the guard statement itself.
      } else if (opensBlock) {
        // Walk forward tracking brace depth CHARACTER BY CHARACTER (not just
        // per-line) and require a `return` before the guard block's own
        // closing brace returns depth to 0.
        //
        // Bug-9581 (R3-09, round-2 recheck): the previous per-line version
        // counted every "{" and "}" on a line BEFORE checking for "return" on
        // that line, so a valid single-line block guard written as
        // `if (readOnly) { return; }` closed its own brace (depth 0 by the
        // time the line's return-check ran) and false-positived as
        // "no return before closing brace" — failing safe (a spurious test
        // failure on correct code), but still a false positive. Tracking the
        // exact character index where depth returns to 0 and scanning only
        // the substring before that point fixes both the single-line and
        // multi-line block forms identically.
        let depth = 0;
        let sawOpenBrace = false;
        let foundReturn = false;
        let closedAt = -1;
        for (let i = guardLineIdx; i < windowLines.length; i++) {
          const l = windowLines[i]!;
          let closedIdxInLine = -1;
          for (let ci = 0; ci < l.length; ci++) {
            const ch = l[ci];
            if (ch === "{") {
              depth++;
              sawOpenBrace = true;
            } else if (ch === "}") {
              depth--;
              if (sawOpenBrace && depth === 0) {
                closedIdxInLine = ci;
                break;
              }
            }
          }
          const scanSegment = closedIdxInLine >= 0 ? l.slice(0, closedIdxInLine) : l;
          if (sawOpenBrace && /\breturn\b/.test(scanSegment)) {
            foundReturn = true;
          }
          if (closedIdxInLine >= 0) {
            closedAt = i;
            break;
          }
        }
        expect(closedAt, `${name}: block-form guard's closing brace not found`).toBeGreaterThanOrEqual(0);
        expect(foundReturn, `${name}: block-form entry guard does not return/exit before its closing brace`).toBe(true);
      } else {
        throw new Error(
          `${name}: entry guard line does not match a recognised shape ` +
            `(single-line "if (readOnly...) return" or block-form "if (readOnly...) {"): ${guardLine}`,
        );
      }
    }

    // Bug-8762: both waypoint-deletion writers must route through the shared
    // copy-on-write primitive — a regression to inline in-place deletion in
    // either writer fails here.
    //
    // They no longer assign `layoutRef.current` themselves: both hand their
    // cleared map to commitRouteEdit, which owns the write, the route-lock
    // refusal and the history entry. So the check is that each still computes
    // its deletion with the primitive AND still delegates.
    for (const { name, def, end } of [
      { name: "handleResetEdge", def: /const handleResetEdge\s*=\s*\(e: Event\)/, end: /const handleTogglePathingAuto/ },
      { name: "onWaypointReset", def: /onWaypointReset:\s*\(/, end: /onSourceSideChange:/ },
    ]) {
      const delDefIdx = lines.findIndex((l) => def.test(l));
      expect(delDefIdx, `${name}: deletion writer definition not found`).toBeGreaterThanOrEqual(0);
      const endIdx = lines.findIndex((l, i) => i > delDefIdx && end.test(l));
      expect(endIdx, `${name}: end of writer body not found`).toBeGreaterThan(delDefIdx);
      const body = lines.slice(delDefIdx, endIdx).join("\n");
      expect(body, `${name}: deletion must route through clearEdgeWaypoints`).toContain("clearEdgeWaypoints(");
      expect(body, `${name}: deletion must commit through the shared boundary`).toMatch(/commitRouteEdit/);
    }

    // Pin the enumeration table's classifications: every guarded writer must be
    // NAMED in the table, and each deliberate unguarded exception must stay
    // classified "no guard" (removing a row, or reclassifying a guarded writer,
    // fails here rather than silently weakening the audit).
    const tableStart = src.indexOf("ENUMERATION OF EVERY `layoutRef.current =` WRITE SITE");
    expect(tableStart).toBeGreaterThanOrEqual(0);
    const tableEnd = src.indexOf("*/", tableStart);
    const table = src.slice(tableStart, tableEnd);
    for (const { name } of GUARDED_WRITERS) {
      expect(table, `${name}: missing from the write-site enumeration`).toContain(name);
    }
    expect(table).toMatch(/server hydrate \/ model-change reset\s+no guard/);
    // Initial auto-placement is no longer a write site: the canvas stopped
    // computing placement on the render thread, and the worker batch applies its
    // positions through the guarded `handleRedrawLayout` writer. The row must
    // therefore record the relocation — deleting it outright would look like a
    // lost site to the next reader, which is the blind spot this table exists for.
    expect(table).not.toMatch(/hydrate auto-placement of unplaced nodes\s+no guard/);
    expect(table).toMatch(/hydrate auto-placement of unplaced nodes[^\n]*NO LONGER A WRITE SITE/);
  });

  // R03: the direction and spacing controls must actually steer the arrangement.
  // The worker transport cannot be driven in jsdom, so the wiring is pinned at
  // the source: the panel's arrange callback must pass both values through, or
  // the controls would be decorative and the requirement unmet while the panel
  // still looked correct.
  it("sends the chosen direction and spacing with the arrange action (R03)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    // The whole preference object travels with the action — the preset chosen
    // by the click overrides it, and direction and spacing come along. A
    // regression to passing the preset alone leaves the two controls
    // decorative and fails here.
    expect(src).toMatch(/const applying\s*=\s*\{\s*\.\.\.layoutPreferences,\s*preset\s*\}/);
    expect(src).toMatch(/handleApplyLayout\("arrange-all",\s*applying\)/);
  });

  // R04: Arrange selected must arrange the selection with the same preferences,
  // and must be offered only when the worker would accept the batch. The worker
  // transport cannot be driven in jsdom, so both halves are pinned at the
  // source: a raw `selected` count here would enable the control for a pinned
  // or lock-docked selection that the worker then refuses.
  it("arranges the selection with the chosen preferences, gated by the shared movable rule (R04)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    // Arranging a selection uses the same chosen preferences as arranging all.
    expect(src).toMatch(/handleApplyLayout\("arrange-selected",\s*layoutPreferences\)/);

    // The count the control is gated on comes from the worker's own rule.
    expect(src).toMatch(/movableIdsFor\(/);
    expect(src).toMatch(/movableSelectedCount={movableSelectedCount}/);
    expect(src).toMatch(/"arrange-selected",\n\s*\)\.size/);
  });

  // Spec 5 (routeMode): provenance of the stored bends must survive a redraw and
  // must not be credited to the wrong author. The worker transport cannot be
  // driven in jsdom, so the invariant is pinned at the source: the apply path
  // persists what the engine computed, and the two writer groups that establish
  // or discard user geometry set the field accordingly. A regression to
  // "persist the bends but not their provenance" fails here.
  it("persists route provenance and marks user-authored geometry as manual (spec 5)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );

    // The apply path writes provenance returned by the engine.
    expect(src).toMatch(/routeMode:\s*route\.routeMode/);
    // Hydration and the worker snapshot both carry it, or a reload loses it.
    expect(src).toMatch(/routeMode:\s*savedEdge\?\.routeMode/);
    expect(src).toMatch(/routeMode:\s*data\.routeMode/);
    // User-authored geometry is manual.
    const manualWrites = src.match(/routeMode:\s*"manual"/g) ?? [];
    expect(manualWrites.length).toBeGreaterThanOrEqual(3);
    // Clearing user geometry returns the route to engine control: the apply
    // path must not leave a stale "manual" claim behind.
    expect(src).toMatch(/routeMode:\s*undefined/);
  });

  // F08: an edge with no stored `pathing` inherits the global preference. The
  // apply path must not write the resolved mode back as an explicit override
  // (that would freeze the inherited mode); only an edge that already had an
  // explicit override keeps one. Pinned at the source because the worker
  // transport cannot be driven in jsdom.
  it("preserves the explicit-vs-inherited pathing distinction on apply (F08)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    // Both writers (persisted map and React Flow edge data) gate on a captured
    // "did this edge already have an explicit pathing" flag.
    expect(src).toMatch(/hadExplicitPathing/);
    expect(src).toMatch(/pathing:\s*hadExplicitPathing\s*\?\s*route\.pathMode\s*:\s*undefined/);
    // The snapshot boundary resolves the global preference so the worker lays
    // an inherited edge out with the mode the renderer actually displays.
    expect(src).toMatch(/globalPathing:\s*relationPathing/);
  });

  // F05: moving or resizing a card invalidates the stored absolute automatic
  // waypoints, so both gesture ends must defer the worker `repair-routes`
  // batch (which recomputes only auto routes) to a post-commit effect, and the
  // pre/post-gesture state must be captured for a single combined undo entry.
  // Pinned at the source because the worker transport cannot be driven in jsdom.
  it("repairs auto routes after a card move or resize (F05)", () => {
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    expect(src).toMatch(/repairPendingRef\.current = true/);
    expect(src).toMatch(/run\("repair-routes"/);
    // The repair runs after the geometry change commits, not synchronously in
    // the gesture handler, and records one combined position+route entry.
    expect(src).toMatch(/handleRepairRoutesRef\.current\(\)/);
    expect(src).toMatch(/pendingRepairBeforeRef\.current =/);
    expect(src).toMatch(/pendingRepairAfterRef\.current = after/);
  });

  // Bug-8762: layoutRef aliases the React Query cache object. handleResetEdge
  // used to delete nested waypoint keys in place, so a failed persistence PATCH
  // left the client cache inconsistent with the server. Copy-on-write means the
  // cached snapshot stays intact while the flushed layout carries the deletion.
  it("reset-edge-path does not mutate the cached layout object (Bug-8762)", async () => {
    const saved = {
      edges: { "join-1": { pathing: "orthogonal", waypoints: [{ x: 5, y: 6 }] } },
    };
    renderCanvas("slug-cow-window", false, saved);

    act(() => {
      window.dispatchEvent(new CustomEvent("reset-edge-path", { detail: "join-1" }));
    });

    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(1), { timeout: 2000 });
    const [, , body] = updateSpy.mock.calls[0] as [
      string,
      string,
      { canvas_layout: Record<string, unknown> },
    ];
    const layoutEdges = (body.canvas_layout.edges ?? {}) as Record<
      string,
      Record<string, unknown>
    >;
    // The flushed layout carries the deletion...
    expect(layoutEdges["join-1"].waypoints).toBeUndefined();
    expect(layoutEdges["join-1"].waypoint).toBeUndefined();
    expect(layoutEdges["join-1"].pathing).toBe("orthogonal");
    // ...while the cache stand-in object the layout aliases is untouched.
    expect(saved.edges["join-1"].waypoints).toEqual([{ x: 5, y: 6 }]);
  });

  // Bug-8762 (second deletion path): onWaypointReset shares the same
  // copy-on-write primitive as handleResetEdge. ReactFlow never renders edges
  // in jsdom (its EdgeRenderer bails when the store width is 0), so the
  // callback cannot be driven through a real edge double-click; the invariant
  // is therefore pinned at the shared primitive both paths call (below) plus
  // the source-level check that BOTH writers route through it (above the
  // enumeration pin).
  it("clearEdgeWaypoints removes waypoint fields without mutating the input layout (Bug-8762)", () => {
    // The input object is the React Query cache stand-in layoutRef aliases.
    const cached = {
      "join-1": { pathing: "orthogonal", waypoints: [{ x: 5, y: 6 }] },
    };
    const result = clearEdgeWaypoints(cached, "join-1");
    // The result carries the deletion...
    expect(result["join-1"]).toEqual({ pathing: "orthogonal" });
    // ...while the cache stand-in is untouched — the exact in-place mutation
    // the pre-fix code performed (and a failed PATCH then persisted locally).
    expect(cached["join-1"]).toEqual({
      pathing: "orthogonal",
      waypoints: [{ x: 5, y: 6 }],
    });
    // An entry emptied by the deletion is dropped from the map entirely.
    expect(
      clearEdgeWaypoints({ "join-2": { waypoints: [{ x: 1, y: 2 }] } }, "join-2"),
    ).toEqual({});
    // An unknown join id is a no-op copy.
    expect(clearEdgeWaypoints(cached, "missing")).toEqual(cached);
  });
  // Bug-8504 (R3): withdrawing the panel is necessary but not sufficient — an
  // open panel holding unsaved text must not just vanish mid-keystroke. The
  // render gate is the fail-closed backstop; this pins the explanation that
  // goes with it, so the Canvas half matches the JoinsPanel half rather than
  // reintroducing the silent refusal one layer up.
  it("explains the withdrawal instead of letting the panel silently vanish (Bug-8504)", () => {
    useBuilderStore.getState().clearGlobalMessage();
    const { view, ui } = renderCanvas("slug-notes-msg", false, { notes: "original" });
    act(() => {
      fireEvent.click(screen.getByTitle("Model annotations"));
    });
    expect(useBuilderStore.getState().globalMessage).toBeNull();

    act(() => {
      view.rerender(ui("slug-notes-msg", true));
    });

    expect(useBuilderStore.getState().globalMessage?.text).toBe(
      "This model is open read-only, so the canvas cannot be changed.",
    );
    expect(useBuilderStore.getState().globalMessage?.severity).toBe("info");
  });
});

describe("canvas view controls (Bug-10034 interim)", () => {
  // Selecting a relationship opens the Joins drawer over the layout panel, so
  // the Lock Route control the user went looking for disappears behind it.
  // Until the panels are rearranged, the drawer can be held back for as long as
  // someone is working on routes.
  it("offers both view controls and starts with the drawer enabled", () => {
    renderCanvas("slug-0");

    const joins = screen.getByTestId("toggle-joins-drawer");
    const minimap = screen.getByTestId("toggle-minimap");

    // Not suppressed on open: the drawer is the normal behaviour, and the
    // switch is an escape hatch rather than a setting.
    expect(joins).toHaveAttribute("aria-pressed", "false");
    // Shown on open, for the same reason in the other direction.
    expect(minimap).toHaveAttribute("aria-pressed", "true");
  });

  it("reports its state, so the canvas says which way the switch is set", () => {
    renderCanvas("slug-0");
    const joins = screen.getByTestId("toggle-joins-drawer");

    act(() => { joins.click(); });
    expect(joins).toHaveAttribute("aria-pressed", "true");

    act(() => { joins.click(); });
    expect(joins).toHaveAttribute("aria-pressed", "false");
  });

  it("withholds only the drawer, never the selection", () => {
    // The relationship must still be selected while the drawer is held back:
    // selection is what the layout panel acts on and what draws the highlight.
    // Suppressing the selection too would make the control useless for the one
    // workflow it exists to unblock.
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    const handler = src.slice(src.indexOf("const onEdgeClick: EdgeMouseHandler"));
    const body = handler.slice(0, handler.indexOf("}, ["));
    const select = body.indexOf('selectObject(edge.id, "join")');
    const guard = body.indexOf("if (joinsDrawerSuppressed) return;");
    expect(select, "the relationship is selected").toBeGreaterThan(0);
    expect(guard, "and the drawer is withheld AFTER it").toBeGreaterThan(select);
  });

  it("does not persist either control", () => {
    // These are "get this out of my way for a minute" controls, not
    // preferences. A hidden setting that survived a reload would leave one
    // modeller's canvas behaving differently from a colleague's with no visible
    // reason why.
    const src = readFileSync(
      resolve(process.cwd(), "src/components/Builder/Canvas.tsx"),
      "utf8",
    );
    const declarations = src.slice(
      src.indexOf("const [joinsDrawerSuppressed"),
      src.indexOf("const minimapVisible"),
    );
    expect(declarations).not.toMatch(/localStorage|safeLocalSet|layoutRef|persist/i);
    // …and a model change returns both to their defaults.
    expect(declarations).toMatch(/setJoinsDrawerSuppressed\(false\)/);
    expect(declarations).toMatch(/setMinimapDismissed\(false\)/);
    expect(declarations).toMatch(/\}, \[projectId, modelId\]\)/);
  });
});
