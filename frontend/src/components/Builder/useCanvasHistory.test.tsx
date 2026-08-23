import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useState } from "react";
import type { Node } from "reactflow";
import { QueryClient } from "@tanstack/react-query";

const joinsCreateMock = vi.fn();
const joinsDeleteMock = vi.fn();
const tablesUpdateMock = vi.fn();

// Bug-8227: the command-replay registry imports all drawer-entity APIs.
// vi.mock is hoisted, so the mock objects must be created via vi.hoisted to be
// referenceable both inside the factory and in the tests below.
const {
  measuresApiMock,
  dimensionsApiMock,
  hierarchiesApiMock,
  personasApiMock,
  kpisApiMock,
  namedSetsApiMock,
  namedQueriesApiMock,
  pocketsApiMock,
  rowSecurityApiMock,
  parametersApiMock,
  glossaryApiMock,
  attributeRelationshipsApiMock,
  calendarApiMock,
  userDefinedAttributesApiMock,
} = vi.hoisted(() => {
  const makeEntityMock = () => ({
    create: vi.fn(async () => ({ id: "new-id" })),
    update: vi.fn(async () => ({ id: "existing-id" })),
    delete: vi.fn(async () => ({})),
    updateDrillThroughSet: vi.fn(async () => ({ id: "drill-id" })),
    resetDrillThroughSet: vi.fn(async () => ({})),
    setPolicy: vi.fn(async () => ({ id: "policy-id" })),
    updateCompound: vi.fn(async () => ({ id: "pocket-id" })),
  });
  return {
    measuresApiMock: makeEntityMock(),
    dimensionsApiMock: makeEntityMock(),
    hierarchiesApiMock: makeEntityMock(),
    personasApiMock: makeEntityMock(),
    kpisApiMock: makeEntityMock(),
    namedSetsApiMock: makeEntityMock(),
    namedQueriesApiMock: makeEntityMock(),
    pocketsApiMock: makeEntityMock(),
    rowSecurityApiMock: makeEntityMock(),
    parametersApiMock: makeEntityMock(),
    glossaryApiMock: makeEntityMock(),
    attributeRelationshipsApiMock: makeEntityMock(),
    calendarApiMock: {
      create: vi.fn(async () => ({ id: "calendar-id" })),
      bind: vi.fn(async () => ({ id: "calendar-id" })),
      autoCreate: vi.fn(async () => ({ id: "calendar-id" })),
      undoAutoCreate: vi.fn(async () => undefined),
      update: vi.fn(async () => ({ id: "calendar-id" })),
      delete: vi.fn(async () => ({})),
    },
    userDefinedAttributesApiMock: makeEntityMock(),
  };
});

vi.mock("../../api/client", () => ({
  joinsApi: {
    create: (...args: unknown[]) => joinsCreateMock(...args),
    delete: (...args: unknown[]) => joinsDeleteMock(...args),
  },
  modelTablesApi: {
    update: (...args: unknown[]) => tablesUpdateMock(...args),
  },
  measuresApi: measuresApiMock,
  dimensionsApi: dimensionsApiMock,
  hierarchiesApi: hierarchiesApiMock,
  personasApi: personasApiMock,
  kpisApi: kpisApiMock,
  namedSetsApi: namedSetsApiMock,
  namedQueriesApi: namedQueriesApiMock,
  pocketsApi: pocketsApiMock,
  rowSecurityApi: rowSecurityApiMock,
  parametersApi: parametersApiMock,
  glossaryApi: glossaryApiMock,
  attributeRelationshipsApi: attributeRelationshipsApiMock,
  calendarApi: calendarApiMock,
  userDefinedAttributesApi: userDefinedAttributesApiMock,
}));

import { useCanvasHistory, ENTITY_QUERY_KEYS, type NodePositions } from "./useCanvasHistory";
import { useModelEditorStore } from "../../store/useModelEditorStore";
import { NAMED_SETS_QUERY_KEY_PREFIX } from "../Panels/NamedSetsPanel";
import { NAMED_QUERIES_QUERY_KEY_PREFIX } from "../Panels/NamedQueryEditor";

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

// Bug-7634: verify that navigating from model A to model B clears the
// undo/redo stacks so A's history cannot corrupt B's canvas_layout.
describe("useCanvasHistory — model change resets stacks (Bug-7634)", () => {
  beforeEach(() => {
    joinsCreateMock.mockReset();
    joinsDeleteMock.mockReset();
    tablesUpdateMock.mockReset();
  });

  function setupWithModelSwitch(onApplyMove = vi.fn()) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = renderHook(
      ({ modelId }: { modelId: string }) => {
        const [nodes, setNodes] = useState<Node[]>(makeNodes());
        const history = useCanvasHistory(
          nodes,
          setNodes,
          "p1",
          modelId,
          qc,
          onApplyMove,
        );
        return { nodes, setNodes, history };
      },
      { initialProps: { modelId: "m1" } },
    );
    return { view, onApplyMove };
  }

  it("clears undo stack when model changes — A's drag cannot be undone on B", () => {
    const { view } = setupWithModelSwitch();

    // Perform a drag on model A (m1)
    act(() => view.result.current.history.beginMove());
    act(() =>
      view.result.current.setNodes((ns) =>
        ns.map((n) => (n.id === "A" ? { ...n, position: { x: 50, y: 60 } } : n)),
      ),
    );
    act(() =>
      view.result.current.history.endMove(positions(view.result.current.nodes)),
    );
    expect(view.result.current.history.canUndo).toBe(true);

    // Navigate to model B (m2)
    view.rerender({ modelId: "m2" });

    // Undo stack should be empty — A's drag must not be undoable in B's context
    expect(view.result.current.history.canUndo).toBe(false);
    expect(view.result.current.history.canRedo).toBe(false);
  });

  it("clears redo stack when model changes — A's redoable action cannot be redone on B", async () => {
    const { view } = setupWithModelSwitch();

    // Drag on model A, then undo it to create a redo entry
    act(() => view.result.current.history.beginMove());
    act(() =>
      view.result.current.setNodes((ns) =>
        ns.map((n) => (n.id === "A" ? { ...n, position: { x: 50, y: 60 } } : n)),
      ),
    );
    act(() =>
      view.result.current.history.endMove(positions(view.result.current.nodes)),
    );
    await act(async () => {
      await view.result.current.history.undo();
    });
    expect(view.result.current.history.canRedo).toBe(true);

    // Navigate to model B
    view.rerender({ modelId: "m2" });

    // Both stacks should be empty
    expect(view.result.current.history.canUndo).toBe(false);
    expect(view.result.current.history.canRedo).toBe(false);
  });

  it("fresh history works correctly on model B after clearing model A state — only B's entry undoes", async () => {
    const { view, onApplyMove } = setupWithModelSwitch();

    // Build up history on model A
    act(() => view.result.current.history.beginMove());
    act(() =>
      view.result.current.setNodes((ns) =>
        ns.map((n) => (n.id === "A" ? { ...n, position: { x: 50, y: 60 } } : n)),
      ),
    );
    act(() =>
      view.result.current.history.endMove(positions(view.result.current.nodes)),
    );

    // Switch to model B
    view.rerender({ modelId: "m2" });

    // New drag on model B should work independently
    act(() => view.result.current.history.beginMove());
    act(() =>
      view.result.current.setNodes((ns) =>
        ns.map((n) => (n.id === "B" ? { ...n, position: { x: 200, y: 200 } } : n)),
      ),
    );
    act(() =>
      view.result.current.history.endMove(positions(view.result.current.nodes)),
    );
    expect(view.result.current.history.canUndo).toBe(true);

    // Undo model B's drag
    onApplyMove.mockClear();
    await act(async () => {
      await view.result.current.history.undo();
    });
    // B was restored — verify the undo applied B's before-position
    expect(onApplyMove).toHaveBeenCalledTimes(1);
    expect(onApplyMove).toHaveBeenCalledWith({ B: { x: 100, y: 100 } });

    // Stack is now empty — model A's history was not carried over
    expect(view.result.current.history.canUndo).toBe(false);
  });

  it("in-flight async undo from model A does not repopulate model B stacks", async () => {
    // Simulate a slow async undo (e.g. join delete) that resolves after
    // the model has changed. The resolved action must not be pushed onto
    // model B's (cleared) redo stack.
    let resolveDelete: () => void;
    const deletePromise = new Promise<void>((r) => { resolveDelete = r; });
    joinsDeleteMock.mockReturnValueOnce(deletePromise);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const onApplyMove = vi.fn();
    const view = renderHook(
      ({ modelId }: { modelId: string }) => {
        const [nodes, setNodes] = useState<Node[]>(makeNodes());
        const history = useCanvasHistory(
          nodes,
          setNodes,
          "p1",
          modelId,
          qc,
          onApplyMove,
        );
        return { nodes, setNodes, history };
      },
      { initialProps: { modelId: "m1" } },
    );

    // Add a join-delete action to model A's undo stack
    act(() => {
      window.dispatchEvent(
        new CustomEvent("canvas-history-action", {
          detail: {
            action: {
              type: "deleteLink",
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
    expect(view.result.current.history.canUndo).toBe(true);

    // Start the undo (begins the async join recreate)
    let undoPromise: Promise<void>;
    act(() => {
      undoPromise = view.result.current.history.undo();
    });

    // Navigate to model B WHILE the undo is in-flight
    view.rerender({ modelId: "m2" });
    expect(view.result.current.history.canUndo).toBe(false);
    expect(view.result.current.history.canRedo).toBe(false);

    // Now resolve the in-flight delete
    await act(async () => {
      resolveDelete!();
      await undoPromise!;
    });

    // The resolved action must NOT have been pushed onto model B's redo stack
    expect(view.result.current.history.canRedo).toBe(false);
    expect(view.result.current.history.canUndo).toBe(false);
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

// Bug-7408: the window-level Ctrl/Cmd+Z/Y handler must share the same
// editable-target guard as the global shortcut policy — it must NOT fire undo
// while focus is in a SELECT or contenteditable element (the old local guard
// only excluded INPUT/TEXTAREA and hijacked those controls mid-edit).
describe("useCanvasHistory — keyboard undo target guard (Bug-7408)", () => {
  beforeEach(() => {
    joinsCreateMock.mockReset();
    joinsDeleteMock.mockReset();
    tablesUpdateMock.mockReset();
  });

  function pressUndo(target: EventTarget) {
    const ev = new KeyboardEvent("keydown", {
      key: "z",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    // jsdom KeyboardEvent has a read-only target until dispatched; dispatch
    // from the intended target so event.target is set correctly.
    (target as HTMLElement).dispatchEvent(ev);
  }

  it("does not undo when focus is in a SELECT element", () => {
    const { view } = setup();
    drag(view, "A", { x: 50, y: 60 });
    expect(view.result.current.history.canUndo).toBe(true);

    const select = document.createElement("select");
    document.body.appendChild(select);
    act(() => pressUndo(select));

    // Undo suppressed — the move entry is still on the stack.
    expect(view.result.current.history.canUndo).toBe(true);
    expect(positions(view.result.current.nodes).A).toEqual({ x: 50, y: 60 });
    document.body.removeChild(select);
  });

  it("does not undo when focus is in a contenteditable element", () => {
    const { view } = setup();
    drag(view, "A", { x: 50, y: 60 });

    const editable = document.createElement("div");
    editable.setAttribute("contenteditable", "true");
    // jsdom does not compute isContentEditable from the attribute, so pin it.
    Object.defineProperty(editable, "isContentEditable", { value: true });
    document.body.appendChild(editable);
    act(() => pressUndo(editable));

    expect(view.result.current.history.canUndo).toBe(true);
    expect(positions(view.result.current.nodes).A).toEqual({ x: 50, y: 60 });
    document.body.removeChild(editable);
  });

  it("undoes when focus is on a non-editable element (canvas surface)", async () => {
    const { view } = setup();
    drag(view, "A", { x: 50, y: 60 });

    const div = document.createElement("div");
    document.body.appendChild(div);
    await act(async () => {
      pressUndo(div);
      // allow the async undo() microtasks to settle
      await Promise.resolve();
    });

    expect(positions(view.result.current.nodes).A).toEqual({ x: 0, y: 0 });
    document.body.removeChild(div);
  });
});

// ---------------------------------------------------------------------------
// Bug-8227 — drawer-edit undo/redo + F-026-02 dirty reconciliation.
// ---------------------------------------------------------------------------

/** Dispatch a drawer-edit command exactly as emitDrawerHistory does. */
function emitCommand(action: unknown) {
  act(() => {
    window.dispatchEvent(
      new CustomEvent("canvas-history-action", { detail: { action } }),
    );
  });
}

describe("useCanvasHistory — drawer-edit commands (Bug-8227)", () => {
  beforeEach(() => {
    joinsCreateMock.mockReset();
    joinsCreateMock.mockResolvedValue({ id: "j-recreated" });
    joinsDeleteMock.mockReset();
    for (const m of [measuresApiMock, dimensionsApiMock, hierarchiesApiMock, personasApiMock, kpisApiMock, namedSetsApiMock, namedQueriesApiMock, pocketsApiMock, rowSecurityApiMock, parametersApiMock, glossaryApiMock, attributeRelationshipsApiMock, calendarApiMock, userDefinedAttributesApiMock]) {
      m.create.mockClear();
      m.update.mockClear();
      m.delete.mockClear();
    }
    pocketsApiMock.setPolicy.mockClear();
    pocketsApiMock.updateCompound.mockClear();
    calendarApiMock.bind.mockClear();
    calendarApiMock.autoCreate.mockClear();
    calendarApiMock.undoAutoCreate.mockClear();
    calendarApiMock.update.mockClear();
    calendarApiMock.delete.mockClear();
    // Reset the editor store to a clean baseline for the open model.
    useModelEditorStore.setState({
      modelId: "m1",
      isDirty: false,
      currentRevision: 0,
      savedRevision: 0,
      lastSavedVersion: 1,
      deployedVersion: 1,
      lastDeployedAt: null,
    });
  });

  it("undoing a measure update replays the inverse (prior) payload; redo replays the new one", async () => {
    const { view } = setup();
    // Simulate the panel's write bumping the content revision, then emitting.
    act(() => useModelEditorStore.getState().markDirty());
    emitCommand({
      type: "command",
      entity: "measure",
      redo: { kind: "update", id: "meas-1", data: { expression: "SUM(b)" } },
      undo: { kind: "update", id: "meas-1", data: { expression: "SUM(a)" } },
    });
    expect(view.result.current.history.canUndo).toBe(true);

    await act(async () => {
      await view.result.current.history.undo();
    });
    // Inverse (prior formula) applied.
    expect(measuresApiMock.update).toHaveBeenCalledWith("p1", "m1", "meas-1", { expression: "SUM(a)" });

    await act(async () => {
      await view.result.current.history.redo();
    });
    expect(measuresApiMock.update).toHaveBeenLastCalledWith("p1", "m1", "meas-1", { expression: "SUM(b)" });
  });

  it("Bug-9395 F-026-10 registers model-scoped omitted drawer entities", async () => {
    const { view } = setup();
    act(() => useModelEditorStore.getState().markDirty());
    emitCommand({
      type: "command",
      entity: "parameter",
      redo: { kind: "update", id: "param-1", data: { display_name: "Region" } },
      undo: { kind: "update", id: "param-1", data: { display_name: "Area" } },
    });

    expect(ENTITY_QUERY_KEYS.parameter).toEqual(["parameters"]);
    expect(ENTITY_QUERY_KEYS.pocket).toEqual(["pockets", "pocket-policy", "metrics"]);
    expect(ENTITY_QUERY_KEYS.rowSecurity).toEqual(["row-security"]);
    expect(ENTITY_QUERY_KEYS.glossary).toEqual(["glossary"]);
    expect(ENTITY_QUERY_KEYS.dimension).toEqual([
      "dimensions", "modelTables", "allModelTables", "sources",
    ]);
    expect(ENTITY_QUERY_KEYS.hierarchy).toEqual([
      "hierarchies", "hierarchy", "dimensions", "sources", "modelTables",
      "allModelTables", "joins", "hierarchy-health",
    ]);
    expect(ENTITY_QUERY_KEYS.attributeRelationship).toEqual([
      "attributeRelationships", "dimensions",
    ]);
    expect(ENTITY_QUERY_KEYS.calendar).toEqual([
      "calendars", "sources", "modelTables", "allModelTables", "joins",
    ]);
    expect(ENTITY_QUERY_KEYS.userDefinedAttribute).toEqual([
      "userDefinedAttributes", "dimensions", "measures",
    ]);
    expect(ENTITY_QUERY_KEYS.drillThroughSet).toEqual(["drillThroughSet", "measures"]);

    await act(async () => {
      await view.result.current.history.undo();
    });
    expect(parametersApiMock.update).toHaveBeenCalledWith(
      "p1",
      "m1",
      "param-1",
      { display_name: "Area" },
    );
  });

  it("Bug-9395 F-026-10 routes parent-scoped undo commands without leaking metadata", async () => {
    const { view } = setup();
    act(() => useModelEditorStore.getState().markDirty());
    emitCommand({
      type: "command",
      entity: "userDefinedAttribute",
      redo: {
        kind: "create",
        data: {
          name: "net",
          expression: "amount",
          output_data_type: "numeric",
          __table_id: "table-1",
        },
      },
      undo: {
        kind: "delete",
        id: "uda-1",
        data: { __table_id: "table-1" },
      },
    });
    await act(async () => { await view.result.current.history.undo(); });
    expect(userDefinedAttributesApiMock.delete).toHaveBeenCalledWith("p1", "m1", "table-1", "uda-1");

    emitCommand({
      type: "command",
      entity: "attributeRelationship",
      redo: {
        kind: "update",
        id: "rel-1",
        data: { enabled: false, __dimension_id: "dim-1" },
      },
      undo: {
        kind: "update",
        id: "rel-1",
        data: { enabled: true, __dimension_id: "dim-1" },
      },
    });
    await act(async () => { await view.result.current.history.undo(); });
    expect(attributeRelationshipsApiMock.update).toHaveBeenCalledWith(
      "p1",
      "m1",
      "dim-1",
      "rel-1",
      { enabled: true },
    );
  });

  it("L13-R1-F6 replays a conditional pocket edit through one atomic revision", async () => {
    const { view } = setup();
    act(() => useModelEditorStore.getState().markDirty());
    emitCommand({
      type: "command",
      entity: "pocket",
      redo: {
        kind: "update",
        id: "pocket-1",
        data: {
          defining_sql: "SELECT 2",
          __policy: { cron_expression: "0 3 * * *", is_enabled: true },
        },
      },
      undo: {
        kind: "update",
        id: "pocket-1",
        data: {
          defining_sql: "SELECT 1",
          __policy: { cron_expression: "0 2 * * *", is_enabled: false },
        },
      },
    });
    await act(async () => { await view.result.current.history.undo(); });
    expect(pocketsApiMock.updateCompound).toHaveBeenCalledWith("p1", "m1", "pocket-1", {
      definition: { defining_sql: "SELECT 1" },
      policy: { cron_expression: "0 2 * * *", is_enabled: false },
    });
    expect(pocketsApiMock.setPolicy).not.toHaveBeenCalledWith(
      "p1",
      "m1",
      "pocket-1",
      { cron_expression: "0 2 * * *", is_enabled: false },
    );
    expect(useModelEditorStore.getState()).toMatchObject({
      currentRevision: 0,
      savedRevision: 0,
      isDirty: false,
    });
    expect(pocketsApiMock.updateCompound).toHaveBeenCalledTimes(1);
  });

  it("L13-R1-F4 undoes and redoes an auto-created calendar with generated aliases", async () => {
    const { view } = setup();
    act(() => useModelEditorStore.getState().markDirty());
    emitCommand({
      type: "command",
      entity: "calendar",
      redo: { kind: "create", data: { __source_id: "source-1", __calendar_flow: "auto-create", __history_provenance: "token-1", table_name: "calendar", dialect: "postgresql", date_column: "date_key", year_column: "year_no", calendar_type: "standard" } },
      undo: { kind: "delete", id: "calendar-1", data: { __source_id: "source-1", __calendar_flow: "auto-create", __history_provenance: "token-1" } },
    });
    await act(async () => { await view.result.current.history.undo(); });
    expect(calendarApiMock.undoAutoCreate).toHaveBeenCalledWith("p1", "m1", "source-1", "calendar-1", "token-1");
    await act(async () => { await view.result.current.history.redo(); });
    expect(calendarApiMock.bind).toHaveBeenCalledWith("p1", "m1", "source-1", { table_name: "calendar", dialect: "postgresql", date_column: "date_key", year_column: "year_no", calendar_type: "standard", history_provenance: "token-1" });
    expect(calendarApiMock.autoCreate).not.toHaveBeenCalled();
  });

  it("in a mixed canvas+drawer sequence, undo restores the most consequential drawer edit (S5)", async () => {
    const { view } = setup();
    // move (layout) — not a content write
    drag(view, "A", { x: 50, y: 60 });
    // measure formula change (content) — the consequential edit
    act(() => useModelEditorStore.getState().markDirty());
    emitCommand({
      type: "command",
      entity: "measure",
      redo: { kind: "update", id: "rev", data: { expression: "SUM(net_amount)" } },
      undo: { kind: "update", id: "rev", data: { expression: "SUM(amount)" } },
    });
    // join delete recorded on top
    emitCommand({ type: "deleteLink", joinId: "j1", createData: { left_table_id: "A", right_table_id: "B", join_type: "inner", left_column_name: "a", right_column_name: "b" } });

    // Undo the join (deleteLink inverse re-creates it)
    await act(async () => { await view.result.current.history.undo(); });
    expect(joinsCreateMock).toHaveBeenCalledTimes(1);
    // Undo the measure formula change — the consequential edit is restored
    await act(async () => { await view.result.current.history.undo(); });
    expect(measuresApiMock.update).toHaveBeenCalledWith("p1", "m1", "rev", { expression: "SUM(amount)" });
    // Undo the move — layout restored
    await act(async () => { await view.result.current.history.undo(); });
    expect(positions(view.result.current.nodes).A).toEqual({ x: 0, y: 0 });
  });

  it("a create command's undo deletes the created row; redo re-creates and rebinds the new id", async () => {
    const { view } = setup();
    act(() => useModelEditorStore.getState().markDirty());
    emitCommand({
      type: "command",
      entity: "dimension",
      redo: { kind: "create", data: { name: "d" } },
      undo: { kind: "delete", id: "dim-created" },
    });
    await act(async () => { await view.result.current.history.undo(); });
    expect(dimensionsApiMock.delete).toHaveBeenCalledWith("p1", "m1", "dim-created");
    // redo re-creates (server returns { id: "new-id" }); undo op must rebind.
    await act(async () => { await view.result.current.history.redo(); });
    expect(dimensionsApiMock.create).toHaveBeenCalledWith("p1", "m1", { name: "d" });
    await act(async () => { await view.result.current.history.undo(); });
    expect(dimensionsApiMock.delete).toHaveBeenLastCalledWith("p1", "m1", "new-id");
  });
});

describe("useModelEditorStore dirty reconciliation via undo (F-026-02)", () => {
  beforeEach(() => {
    for (const m of [measuresApiMock]) {
      m.update.mockClear();
    }
    useModelEditorStore.setState({
      modelId: "m1",
      isDirty: false,
      currentRevision: 0,
      savedRevision: 0,
      lastSavedVersion: 1,
      deployedVersion: 1,
      lastDeployedAt: null,
    });
  });

  it("edit -> undo returns the model to a clean state", async () => {
    const { view } = setup();
    // A content edit marks dirty and bumps the revision.
    act(() => useModelEditorStore.getState().markDirty());
    expect(useModelEditorStore.getState().isDirty).toBe(true);
    emitCommand({
      type: "command",
      entity: "measure",
      redo: { kind: "update", id: "m", data: { expression: "SUM(b)" } },
      undo: { kind: "update", id: "m", data: { expression: "SUM(a)" } },
    });
    await act(async () => { await view.result.current.history.undo(); });
    // Undoing back to the saved baseline clears dirty.
    expect(useModelEditorStore.getState().isDirty).toBe(false);
  });

  it("edit -> save -> edit -> undo returns to clean at the saved baseline", async () => {
    const { view } = setup();
    act(() => useModelEditorStore.getState().markDirty()); // first edit
    emitCommand({
      type: "command",
      entity: "measure",
      redo: { kind: "update", id: "m", data: { expression: "SUM(b)" } },
      undo: { kind: "update", id: "m", data: { expression: "SUM(a)" } },
    });
    act(() => useModelEditorStore.getState().markClean({ lastSavedVersion: 2 }));
    expect(useModelEditorStore.getState().isDirty).toBe(false);

    act(() => useModelEditorStore.getState().markDirty()); // second edit after save
    emitCommand({
      type: "command",
      entity: "measure",
      redo: { kind: "update", id: "m", data: { expression: "SUM(c)" } },
      undo: { kind: "update", id: "m", data: { expression: "SUM(b)" } },
    });
    expect(useModelEditorStore.getState().isDirty).toBe(true);

    await act(async () => { await view.result.current.history.undo(); });
    expect(useModelEditorStore.getState().isDirty).toBe(false);
  });

  it("undoing one edit does NOT clear dirt from an interleaved out-of-band write", async () => {
    const { view } = setup();
    // History edit A (recorded).
    act(() => useModelEditorStore.getState().markDirty());
    emitCommand({
      type: "command",
      entity: "measure",
      redo: { kind: "update", id: "m", data: { expression: "SUM(b)" } },
      undo: { kind: "update", id: "m", data: { expression: "SUM(a)" } },
    });
    // A separate, non-history content write (e.g. a table PATCH) bumps the
    // revision with no history entry to reconcile it.
    act(() => useModelEditorStore.getState().markDirty());
    expect(useModelEditorStore.getState().isDirty).toBe(true);

    // Undoing edit A must leave the model dirty because the out-of-band write
    // is still unsaved — delta-based reconciliation preserves it.
    await act(async () => { await view.result.current.history.undo(); });
    expect(useModelEditorStore.getState().isDirty).toBe(true);
  });
});

describe("F-026-05: undo/redo invalidation keys stay aligned with the panels that read them", () => {
  it("namedSet invalidation key matches NamedSetsPanel's query prefix", () => {
    // Regression guard: the history registry previously invalidated
    // ["named-sets"] (the panel ID), while the query cache is keyed on
    // "namedSets" — so an undo left the open drawer showing pre-undo rows.
    expect(ENTITY_QUERY_KEYS.namedSet).toEqual([NAMED_SETS_QUERY_KEY_PREFIX]);
  });

  it("namedQuery invalidation key matches NamedQueryEditor's query prefix", () => {
    expect(ENTITY_QUERY_KEYS.namedQuery).toEqual([NAMED_QUERIES_QUERY_KEY_PREFIX]);
  });
});

describe("useCanvasHistory — read-only gating (F-026-04)", () => {
  function setupRO(initialReadOnly: boolean) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const onApplyMove = vi.fn();
    const view = renderHook(
      ({ readOnly }: { readOnly: boolean }) => {
        const [nodes, setNodes] = useState<Node[]>(makeNodes());
        const history = useCanvasHistory(
          nodes,
          setNodes,
          "p1",
          "m1",
          qc,
          onApplyMove,
          undefined,
          undefined,
          readOnly,
        );
        return { nodes, setNodes, history };
      },
      { initialProps: { readOnly: initialReadOnly } },
    );
    return { view, onApplyMove };
  }

  it("flipping to read-only clears a leftover author-session stack", () => {
    const { view } = setupRO(false);
    drag(view, "A", { x: 50, y: 60 });
    expect(view.result.current.history.canUndo).toBe(true);

    // Role flip (viewer / ?readonly=1): the stack must be dropped so it cannot
    // be replayed.
    view.rerender({ readOnly: true });
    expect(view.result.current.history.canUndo).toBe(false);
    expect(view.result.current.history.canRedo).toBe(false);
  });

  it("undo/redo refuse to replay writes while read-only", async () => {
    const { view, onApplyMove } = setupRO(false);
    drag(view, "A", { x: 50, y: 60 });
    view.rerender({ readOnly: true });
    onApplyMove.mockClear();

    await act(async () => { await view.result.current.history.undo(); });
    await act(async () => { await view.result.current.history.redo(); });

    // Nothing was replayed and no layout write was persisted.
    expect(onApplyMove).not.toHaveBeenCalled();
    expect(positions(view.result.current.nodes)).toEqual({
      A: { x: 50, y: 60 },
      B: { x: 100, y: 100 },
    });
  });
});
