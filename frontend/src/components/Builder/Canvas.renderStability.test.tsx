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
    expect(sites.length).toBe(15);
    expect(src).toContain("ENUMERATION OF EVERY `layoutRef.current =` WRITE SITE");
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
