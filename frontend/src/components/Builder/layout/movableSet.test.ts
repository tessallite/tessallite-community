/**
 * The movable-table rule is shared by the worker and the canvas control, so the
 * cases that matter here are the ones where a table is selected but still must
 * not move — those are what keep "Arrange selected" from offering a batch the
 * worker refuses.
 */
import { describe, expect, it } from "vitest";
import { movableIdsFor, type MovableEdgeInput, type MovableNodeInput } from "./movableSet";

function node(id: string, overrides: Partial<MovableNodeInput> = {}): MovableNodeInput {
  return { id, pinned: false, fixed: false, selected: false, ...overrides };
}

function edge(source: string, target: string, locked = false): MovableEdgeInput {
  return { source, target, locked };
}

describe("movableIdsFor", () => {
  it("takes every unprotected table for arrange-all", () => {
    const nodes = [node("a"), node("b"), node("c")];
    expect([...movableIdsFor(nodes, [], "arrange-all")].sort()).toEqual(["a", "b", "c"]);
  });

  it("narrows to the selection for arrange-selected", () => {
    const nodes = [node("a", { selected: true }), node("b"), node("c", { selected: true })];
    expect([...movableIdsFor(nodes, [], "arrange-selected")].sort()).toEqual(["a", "c"]);
  });

  it("never moves a pinned table, selected or not", () => {
    const nodes = [node("a", { selected: true, pinned: true }), node("b", { selected: true })];
    expect([...movableIdsFor(nodes, [], "arrange-selected")]).toEqual(["b"]);
    expect([...movableIdsFor(nodes, [], "arrange-all")]).toEqual(["b"]);
  });

  it("never moves either endpoint of a locked relationship", () => {
    // A lock freezes the docking as well as the path, so both cards the route
    // docks to have to stay put for the frozen path to remain the path drawn.
    const nodes = [node("a", { selected: true }), node("b", { selected: true }), node("c", { selected: true })];
    const edges = [edge("a", "b", true), edge("b", "c", false)];
    expect([...movableIdsFor(nodes, edges, "arrange-selected")]).toEqual(["c"]);
  });

  it("reports an empty set when the whole selection is protected", () => {
    // This is the case the control must not offer: something IS selected, so a
    // plain selected-count would enable the button, and the worker would then
    // refuse the batch.
    const nodes = [node("a", { selected: true, pinned: true }), node("b")];
    expect(movableIdsFor(nodes, [], "arrange-selected").size).toBe(0);
  });

  it("ignores a lock whose endpoints are not on this canvas", () => {
    const nodes = [node("a", { selected: true })];
    expect([...movableIdsFor(nodes, [edge("x", "y", true)], "arrange-selected")]).toEqual(["a"]);
  });
});
