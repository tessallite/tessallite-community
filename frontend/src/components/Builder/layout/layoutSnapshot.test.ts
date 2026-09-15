/**
 * Snapshot producer tests.
 *
 * This is the only place that maps canvas state onto the worker contract, so the
 * cases that matter are the ones the spec calls out: legacy single-waypoint
 * migration, hidden-endpoint filtering, refusing placeholder card sizes, and the
 * marker/parallel-offset inputs the renderer also derives for itself.
 */
import { describe, expect, it } from "vitest";
import { buildLayoutSnapshot, type CanvasSnapshotEdge, type CanvasSnapshotNode } from "./layoutSnapshot";

function node(overrides: Partial<CanvasSnapshotNode> = {}): CanvasSnapshotNode {
  return {
    id: "a",
    position: { x: 10, y: 20 },
    measuredWidth: 200,
    measuredHeight: 150,
    tableType: "dimension",
    ...overrides,
  };
}

function edge(overrides: Partial<CanvasSnapshotEdge> = {}): CanvasSnapshotEdge {
  return {
    id: "j1",
    source: "a",
    target: "b",
    sourceIsFact: true,
    targetIsFact: false,
    sourceIsDim: false,
    targetIsDim: true,
    notation: "crowsfoot",
    ...overrides,
  };
}

const twoNodes = [node(), node({ id: "b", position: { x: 400, y: 20 } })];

function build(nodes: CanvasSnapshotNode[], edges: CanvasSnapshotEdge[], globalPathing?: "orthogonal" | "straight") {
  return buildLayoutSnapshot({ projectId: "p1", modelId: "m1", revision: 4, nodes, edges, options: {}, globalPathing });
}

describe("buildLayoutSnapshot", () => {
  it("carries measured geometry, scope and revision", () => {
    const snapshot = build(twoNodes, [edge()]);
    expect(snapshot.scope).toEqual({ projectId: "p1", modelId: "m1" });
    expect(snapshot.revision).toBe(4);
    expect(snapshot.nodes[0]).toMatchObject({ id: "a", x: 10, y: 20, width: 200, height: 150, measured: true });
  });

  it("prefers the measurement and falls back to the persisted size", () => {
    const snapshot = build(
      [node({ measuredWidth: undefined, measuredHeight: undefined, provisionalWidth: 180, provisionalHeight: 90 }), twoNodes[1]],
      [edge()],
    );
    expect(snapshot.nodes[0]).toMatchObject({ width: 180, height: 90, measured: false });
  });

  it("refuses a card with no usable size instead of guessing one", () => {
    expect(() => build([node({ measuredWidth: undefined, measuredHeight: undefined })], [])).toThrow(/no measured or persisted size/);
  });

  it("drops a relationship whose endpoint is not on the canvas and invents no node", () => {
    const snapshot = build(twoNodes, [edge(), edge({ id: "j2", target: "hidden" })]);
    expect(snapshot.edges.map((e) => e.id)).toEqual(["j1"]);
    expect(snapshot.nodes.map((n) => n.id)).toEqual(["a", "b"]);
  });

  it("migrates the legacy single waypoint into the waypoint list and marks it manual", () => {
    const snapshot = build(twoNodes, [edge({ waypoint: { x: 5, y: 6 } })]);
    expect(snapshot.edges[0].waypoints).toEqual([{ x: 5, y: 6 }]);
    expect(snapshot.edges[0].routeMode).toBe("manual");
  });

  it("treats a relationship with no stored bends as auto", () => {
    const snapshot = build(twoNodes, [edge()]);
    expect(snapshot.edges[0].waypoints).toEqual([]);
    expect(snapshot.edges[0].routeMode).toBe("auto");
  });

  it("keeps a stored auto route auto even though it has bends", () => {
    // Regression guard for the discarding apply path: an engine-routed
    // relationship always has bends, so with provenance thrown away every
    // reload re-read it as a manual edit — crediting the engine's work to the
    // user and, once retention and repair consume the field, treating a
    // machine-computed route as a human decision.
    const snapshot = build(twoNodes, [
      edge({ waypoints: [{ x: 1, y: 2 }], routeMode: "auto" }),
    ]);
    expect(snapshot.edges[0].routeMode).toBe("auto");
    expect(snapshot.edges[0].waypoints).toEqual([{ x: 1, y: 2 }]);
  });

  it("keeps a stored manual route manual", () => {
    const snapshot = build(twoNodes, [
      edge({ waypoints: [{ x: 1, y: 2 }], routeMode: "manual" }),
    ]);
    expect(snapshot.edges[0].routeMode).toBe("manual");
  });

  it("resolves the marker extents the renderer will draw", () => {
    // crowsfoot fact -> dimension: source many (14px), target one (8px).
    const snapshot = build(twoNodes, [edge()]);
    expect(snapshot.edges[0].sourceMarkerExtent).toBe(14);
    expect(snapshot.edges[0].targetMarkerExtent).toBe(8);
  });

  it("carries the parallel-relationship offset the renderer re-applies", () => {
    const snapshot = build(twoNodes, [edge({ offsetIndex: 1, totalEdges: 3 })]);
    expect(snapshot.edges[0]).toMatchObject({ offsetIndex: 1, totalEdges: 3 });
  });

  it("ignores an unknown persisted anchor side", () => {
    const snapshot = build(twoNodes, [edge({ sourceSide: "sideways" })]);
    expect(snapshot.edges[0].sourceSide).toBeUndefined();
  });

  it("defaults the relationship mode to orthogonal and keeps a straight preference", () => {
    expect(build(twoNodes, [edge()]).edges[0].pathMode).toBe("orthogonal");
    expect(build(twoNodes, [edge({ pathing: "straight" })]).edges[0].pathMode).toBe("straight");
  });

  it("resolves an inherited edge using the global pathing preference (F08)", () => {
    // No per-edge override: the global preference must decide, exactly like the
    // renderer's `data.pathing ?? globalPathing`.
    expect(build(twoNodes, [edge()], "straight").edges[0].pathMode).toBe("straight");
    expect(build(twoNodes, [edge()], "orthogonal").edges[0].pathMode).toBe("orthogonal");
    // An explicit per-edge override still wins over the global preference.
    expect(build(twoNodes, [edge({ pathing: "orthogonal" })], "straight").edges[0].pathMode).toBe("orthogonal");
    expect(build(twoNodes, [edge({ pathing: "straight" })], "orthogonal").edges[0].pathMode).toBe("straight");
  });

  it("rejects an invalid position and a duplicate id", () => {
    expect(() => build([node({ position: { x: Number.NaN, y: 0 } })], [])).toThrow(/invalid position/);
    expect(() => build([node(), node()], [])).toThrow(/duplicate table id/);
  });
});
