import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useState } from "react";
import type { Node } from "reactflow";
import { QueryClient } from "@tanstack/react-query";

const joinsCreateMock = vi.fn();
const joinsDeleteMock = vi.fn();
const tablesUpdateMock = vi.fn();

vi.mock("../../api/client", () => ({
  joinsApi: {
    create: (...args: unknown[]) => joinsCreateMock(...args),
    delete: (...args: unknown[]) => joinsDeleteMock(...args),
  },
  modelTablesApi: {
    update: (...args: unknown[]) => tablesUpdateMock(...args),
  },
}));

import { useCanvasHistory, type NodePositions } from "./useCanvasHistory";

function makeNodes(): Node[] {
  return [
    { id: "A", position: { x: 0, y: 0 }, data: {} },
    { id: "B", position: { x: 100, y: 100 }, data: {} },
  ];
}

function positions(nodes: Node[]): NodePositions {
  const out: NodePositions = {};
  for (const n of nodes) out[n.id] = { x: n.position.x, y: n.position.y };
  return out;
}

function setup(onApplyMove = vi.fn(), onApplyEdges = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = renderHook(() => {
    const [nodes, setNodes] = useState<Node[]>(makeNodes());
    const history = useCanvasHistory(
      nodes,
      setNodes,
      "p1",
      "m1",
      qc,
      onApplyMove,
      onApplyEdges,
    );
    return { nodes, setNodes, history };
  });
  return { view, onApplyMove, onApplyEdges };
}

/** Simulate one complete drag gesture: capture, move, commit. */
function drag(
  view: ReturnType<typeof setup>["view"],
  nodeId: string,
  to: { x: number; y: number },
) {
  act(() => {
    view.result.current.history.beginMove();
  });
  act(() => {
    view.result.current.setNodes((ns) =>
      ns.map((n) => (n.id === nodeId ? { ...n, position: to } : n)),
    );
  });
  act(() => {
    view.result.current.history.endMove(positions(view.result.current.nodes));
  });
}

describe("useCanvasHistory — move gestures (F-026-02)", () => {
  beforeEach(() => {
    joinsCreateMock.mockReset();
    joinsDeleteMock.mockReset();
    tablesUpdateMock.mockReset();
  });

  it("the very first drag is immediately undoable", () => {
    const { view } = setup();
    expect(view.result.current.history.canUndo).toBe(false);

    drag(view, "A", { x: 50, y: 60 });

    expect(view.result.current.history.canUndo).toBe(true);
  });

  it("undo restores the pre-drag position and persists it; redo restores the move", async () => {
    const { view, onApplyMove } = setup();
    drag(view, "A", { x: 50, y: 60 });

    await act(async () => {
      await view.result.current.history.undo();
    });
    expect(positions(view.result.current.nodes)).toEqual({
      A: { x: 0, y: 0 },
      B: { x: 100, y: 100 },
    });
    // The undone position must flow through the layout-persistence path.
    expect(onApplyMove).toHaveBeenCalledWith({ A: { x: 0, y: 0 } });
    expect(view.result.current.history.canRedo).toBe(true);

    await act(async () => {
      await view.result.current.history.redo();
    });
    expect(positions(view.result.current.nodes)).toEqual({
      A: { x: 50, y: 60 },
      B: { x: 100, y: 100 },
    });
    expect(onApplyMove).toHaveBeenCalledWith({ A: { x: 50, y: 60 } });
  });

  it("two consecutive drags are two distinct undo entries, undone newest-first", async () => {
    const { view } = setup();
    drag(view, "A", { x: 50, y: 60 });
    drag(view, "B", { x: 300, y: 310 });

    await act(async () => {
      await view.result.current.history.undo();
    });
    // Only the second gesture (B) is reverted.
    expect(positions(view.result.current.nodes)).toEqual({
      A: { x: 50, y: 60 },
      B: { x: 100, y: 100 },
    });

    await act(async () => {
      await view.result.current.history.undo();
    });
    expect(positions(view.result.current.nodes)).toEqual({
      A: { x: 0, y: 0 },
      B: { x: 100, y: 100 },
    });
    expect(view.result.current.history.canUndo).toBe(false);
  });

  it("a gesture without movement records nothing", () => {
    const { view } = setup();
    act(() => {
      view.result.current.history.beginMove();
    });
    act(() => {
      view.result.current.history.endMove(positions(view.result.current.nodes));
    });
    expect(view.result.current.history.canUndo).toBe(false);
  });

  it("move entries store only the nodes that changed", async () => {
    const { view, onApplyMove } = setup();
    drag(view, "A", { x: 5, y: 5 });
    await act(async () => {
      await view.result.current.history.undo();
    });
    // B never moved, so undo must not touch (or persist) it.
    expect(onApplyMove).toHaveBeenCalledTimes(1);
    expect(Object.keys(onApplyMove.mock.calls[0][0])).toEqual(["A"]);
  });
});

describe("useCanvasHistory — redraw restores edge routing (F-026-11 / LOW-2)", () => {
  beforeEach(() => {
    joinsCreateMock.mockReset();
    joinsDeleteMock.mockReset();
    tablesUpdateMock.mockReset();
  });

  it("undo of a layout redraw restores the manual edge waypoints the redraw cleared", async () => {
    const { view, onApplyEdges } = setup();
    // A modeller hand-routed an edge (waypoints saved), then ran a preset
    // redraw which clears waypoints and overwrites anchors.
    const beforeEdges = {
      e1: { waypoints: [{ x: 40, y: 40 }], pathing: "orthogonal" as const },
    };
    const afterEdges = {
      e1: { sourceSide: "right", targetSide: "left", sourceRatio: 0.5, targetRatio: 0.5 },
    };

    act(() => {
      // handleRedrawLayout records one move entry carrying the edge snapshots.
      view.result.current.history.recordMove(
        { A: { x: 0, y: 0 } },
        { A: { x: 200, y: 200 } },
        { before: beforeEdges, after: afterEdges },
      );
    });
    expect(view.result.current.history.canUndo).toBe(true);

    await act(async () => {
      await view.result.current.history.undo();
    });
    // Undo must hand the BEFORE edge layout (with the waypoints) back to the
    // canvas — not just the table positions.
    expect(onApplyEdges).toHaveBeenCalledWith(beforeEdges);

    onApplyEdges.mockClear();
    await act(async () => {
      await view.result.current.history.redo();
    });
    // Redo re-applies the AFTER (redrawn) edge layout.
    expect(onApplyEdges).toHaveBeenCalledWith(afterEdges);
  });

  it("a redraw that moves no table but rewrites edges is still recorded as one undo step", () => {
    const { view } = setup();
    act(() => {
      view.result.current.history.recordMove(
        { A: { x: 0, y: 0 } },
        { A: { x: 0, y: 0 } }, // tables unchanged
        {
          before: { e1: { waypoints: [{ x: 1, y: 1 }] } },
          after: { e1: {} },
        },
      );
    });
    expect(view.result.current.history.canUndo).toBe(true);
  });

  it("a plain drag with no edge snapshot never invokes the edge-restore path", async () => {
    const { view, onApplyEdges } = setup();
    drag(view, "A", { x: 9, y: 9 });
    await act(async () => {
      await view.result.current.history.undo();
    });
    expect(onApplyEdges).not.toHaveBeenCalled();
  });
});

describe("useCanvasHistory — distinct operations stay distinct (F-026-02)", () => {
  beforeEach(() => {
    joinsCreateMock.mockReset();
    joinsDeleteMock.mockReset();
    tablesUpdateMock.mockReset();
  });

  it("move + add-join → first undo reverses exactly the join, second exactly the move; redo replays both", async () => {
    const { view } = setup();
    joinsDeleteMock.mockResolvedValue(undefined);
    joinsCreateMock.mockResolvedValue({ id: "j2" });

    // 1) move A
    drag(view, "A", { x: 50, y: 60 });
    // 2) add a join (dispatched by JoinsPanel in production)
    act(() => {
      window.dispatchEvent(
        new CustomEvent("canvas-history-action", {
          detail: {
            action: {
              type: "addLink",
              joinId: "j1",
              createData: {
                left_table_id: "A",
                right_table_id: "B",
                join_type: "inner",
                left_column_name: "id",
                right_column_name: "a_id",
              },
            },
          },
        }),
      );
    });

    // Undo #1 — reverses ONLY the join add; positions untouched.
    await act(async () => {
      await view.result.current.history.undo();
    });
    expect(joinsDeleteMock).toHaveBeenCalledWith("p1", "m1", "j1");
    expect(positions(view.result.current.nodes).A).toEqual({ x: 50, y: 60 });

    // Undo #2 — reverses ONLY the move.
    await act(async () => {
      await view.result.current.history.undo();
    });
    expect(positions(view.result.current.nodes).A).toEqual({ x: 0, y: 0 });
    expect(joinsDeleteMock).toHaveBeenCalledTimes(1);

    // Redo replays in original order: move first, then join create.
    await act(async () => {
      await view.result.current.history.redo();
    });
    expect(positions(view.result.current.nodes).A).toEqual({ x: 50, y: 60 });
    expect(joinsCreateMock).not.toHaveBeenCalled();

    await act(async () => {
      await view.result.current.history.redo();
    });
    expect(joinsCreateMock).toHaveBeenCalledWith("p1", "m1", {
      left_table_id: "A",
      right_table_id: "B",
      join_type: "inner",
      left_column_name: "id",
      right_column_name: "a_id",
    });
    expect(view.result.current.history.canRedo).toBe(false);
  });
});
