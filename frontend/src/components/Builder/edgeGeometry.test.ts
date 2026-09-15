/**
 * The drawn-route resolution the edge renderer and the route lock share.
 *
 * These cover decisions that used to live inside `CrowsFootEdge.tsx` with no
 * tests at all. They matter beyond the renderer now: locking a relationship
 * freezes what `resolveDisplayedRoute` returns, so a drift here silently
 * freezes a path the user never saw.
 */
import { describe, expect, it } from "vitest";
import { Position } from "reactflow";
import type { Pt } from "./edgeRouting";
import { PARALLEL_EDGE_SPACING } from "./layout/docking";
import {
  type Rect,
  type DisplayedRoute,
  isDrawableRoute,
  isFreezableRoute,
  resolveDisplayedDocking,
  resolveDisplayedRoute,
  resolveDisplayedWaypoints,
  routeEntersCard,
} from "./edgeGeometry";

const left: Rect = { x: 0, y: 0, width: 200, height: 100 };
const right: Rect = { x: 500, y: 0, width: 200, height: 100 };
const below: Rect = { x: 0, y: 400, width: 200, height: 100 };

describe("resolveDisplayedDocking", () => {
  it("derives opposing sides from the cards' relative positions", () => {
    const docking = resolveDisplayedDocking({ source: left, target: right });
    expect(docking.sourceSide).toBe("right");
    expect(docking.targetSide).toBe("left");

    const vertical = resolveDisplayedDocking({ source: left, target: below });
    expect(vertical.sourceSide).toBe("bottom");
    expect(vertical.targetSide).toBe("top");
  });

  it("lets an explicitly stored side and ratio win over the automatic choice", () => {
    const docking = resolveDisplayedDocking({
      source: left,
      target: right,
      sourceSide: "top",
      targetSide: "bottom",
      sourceRatio: 0.25,
      targetRatio: 0.75,
    });
    expect(docking.sourceSide).toBe("top");
    expect(docking.targetSide).toBe("bottom");
    expect(docking.sourceRatio).toBeCloseTo(0.25, 6);
    expect(docking.targetRatio).toBeCloseTo(0.75, 6);
  });

  it("ignores a stored side that is not a side", () => {
    // A hand-edited or newer-build value must not put the edge on a side that
    // does not exist; it falls back to the automatic choice.
    const docking = resolveDisplayedDocking({ source: left, target: right, sourceSide: "sideways" });
    expect(docking.sourceSide).toBe("right");
  });

  it("aligns both docks on the overlap so the connector is a straight line", () => {
    // Equal-height cards facing each other overlap over their whole height, so
    // both docks sit at the centre of that overlap.
    const docking = resolveDisplayedDocking({ source: left, target: right });
    expect(docking.sourceRatio).toBeCloseTo(0.5, 6);
    expect(docking.targetRatio).toBeCloseTo(0.5, 6);
  });

  it("clamps a stored ratio into the drawable band", () => {
    const docking = resolveDisplayedDocking({ source: left, target: right, sourceRatio: 5, targetRatio: -5 });
    expect(docking.sourceRatio).toBeCloseTo(0.92, 6);
    expect(docking.targetRatio).toBeCloseTo(0.08, 6);
  });
});

describe("resolveDisplayedWaypoints", () => {
  const heels = {
    heelSource: { x: 200, y: 50 } as Pt,
    heelTarget: { x: 500, y: 50 } as Pt,
    sourcePosition: Position.Right,
    targetPosition: Position.Left,
    obstacles: [] as Rect[],
  };

  it("keeps stored bends for an orthogonal route", () => {
    const stored = [{ x: 300, y: 50 }, { x: 300, y: 20 }];
    expect(resolveDisplayedWaypoints({ ...heels, pathMode: "orthogonal", stored })).toEqual(stored);
  });

  it("falls back to the automatic route when an orthogonal route has no bends", () => {
    const points = resolveDisplayedWaypoints({ ...heels, pathMode: "orthogonal", stored: [] });
    expect(points.length).toBeGreaterThan(0);
    // Every leg of the drawn polyline stays horizontal or vertical.
    const all = [heels.heelSource, ...points, heels.heelTarget];
    for (let i = 0; i < all.length - 1; i++) {
      const a = all[i]!;
      const b = all[i + 1]!;
      expect(Math.abs(a.x - b.x) < 0.01 || Math.abs(a.y - b.y) < 0.01).toBe(true);
    }
  });

  it("draws a straight auto route as the direct heel-to-heel line", () => {
    // Engine-generated orthogonal bends are not valid straight geometry, so a
    // straight auto route carries none (F08).
    const stored = [{ x: 300, y: 50 }, { x: 300, y: 20 }];
    expect(resolveDisplayedWaypoints({ ...heels, pathMode: "straight", routeMode: "auto", stored })).toEqual([]);
  });

  it("keeps a straight route's manual free-angle bends", () => {
    const stored = [{ x: 310, y: 33 }];
    expect(resolveDisplayedWaypoints({ ...heels, pathMode: "straight", routeMode: "manual", stored })).toEqual(stored);
  });

  it("resolves absent provenance from whether bends are stored", () => {
    // Legacy layouts have no routeMode; stored bends mean the user drew them.
    const stored = [{ x: 310, y: 33 }];
    expect(resolveDisplayedWaypoints({ ...heels, pathMode: "straight", stored })).toEqual(stored);
    expect(resolveDisplayedWaypoints({ ...heels, pathMode: "straight", stored: [] })).toEqual([]);
  });

  it("routes around a card standing between the two endpoints", () => {
    const blocker: Rect = { x: 300, y: 20, width: 100, height: 60 };
    const points = resolveDisplayedWaypoints({ ...heels, pathMode: "orthogonal", stored: [], obstacles: [blocker] });
    const all = [heels.heelSource, ...points, heels.heelTarget];
    // No leg of the route passes through the blocking card.
    for (let i = 0; i < all.length - 1; i++) {
      const a = all[i]!;
      const b = all[i + 1]!;
      const crossesX = Math.max(a.x, b.x) > blocker.x && Math.min(a.x, b.x) < blocker.x + blocker.width;
      const crossesY = Math.max(a.y, b.y) > blocker.y && Math.min(a.y, b.y) < blocker.y + blocker.height;
      expect(crossesX && crossesY).toBe(false);
    }
  });
});

describe("resolveDisplayedRoute", () => {
  const base = {
    source: left,
    target: right,
    pathMode: "orthogonal" as const,
    sourceMarkerExtent: 10,
    targetMarkerExtent: 10,
    obstacles: [] as Rect[],
  };

  it("returns base ratios with the parallel offset left out", () => {
    // The renderer re-applies the fan-out on read. Baking it into the persisted
    // ratio would fan the relationship out twice on the next reload.
    const single = resolveDisplayedRoute(base);
    const third = resolveDisplayedRoute({ ...base, offsetIndex: 2, totalEdges: 3 });
    expect(third.sourceRatio).toBeCloseTo(single.sourceRatio, 6);
    expect(third.targetRatio).toBeCloseTo(single.targetRatio, 6);
  });

  it("still draws the parallel relationships apart", () => {
    // The offset is absent from the ratios but present in the geometry: the
    // third of three relationships routes one spacing step below the centre.
    const middle = resolveDisplayedRoute({ ...base, offsetIndex: 1, totalEdges: 3 });
    const last = resolveDisplayedRoute({ ...base, offsetIndex: 2, totalEdges: 3 });
    const middleY = middle.waypoints[0]?.y ?? 0;
    const lastY = last.waypoints[0]?.y ?? 0;
    expect(lastY - middleY).toBeCloseTo(PARALLEL_EDGE_SPACING, 6);
  });

  it("measures its bends from the marker heels, not the card border", () => {
    // Two cards facing each other across a gap dock at the same height, so the
    // automatic route is a single straight run and its bends sit on the heel
    // line — 10 px outside each border here.
    const route = resolveDisplayedRoute(base);
    const heelY = left.y + left.height * route.sourceRatio;
    for (const point of route.waypoints) {
      expect(point.y).toBeCloseTo(heelY, 6);
      expect(point.x).toBeGreaterThanOrEqual(left.x + left.width + base.sourceMarkerExtent);
      expect(point.x).toBeLessThanOrEqual(right.x - base.targetMarkerExtent);
    }
  });

  it("carries the resolved docking through unchanged", () => {
    const route = resolveDisplayedRoute({ ...base, sourceSide: "top", sourceRatio: 0.3 });
    const docking = resolveDisplayedDocking({ ...base, sourceSide: "top", sourceRatio: 0.3 });
    expect(route.sourceSide).toBe(docking.sourceSide);
    expect(route.targetSide).toBe(docking.targetSide);
    expect(route.sourceRatio).toBeCloseTo(docking.sourceRatio, 6);
    expect(route.targetRatio).toBeCloseTo(docking.targetRatio, 6);
  });

  it("copies the bends rather than aliasing the stored array", () => {
    // The lock persists what this returns; handing back the caller's own array
    // would let a later edit mutate the frozen path.
    const stored = [{ x: 300, y: 50 }];
    const route = resolveDisplayedRoute({ ...base, waypoints: stored });
    expect(route.waypoints).toEqual(stored);
    expect(route.waypoints[0]).not.toBe(stored[0]);
  });
});

describe("isFreezableRoute", () => {
  function route(overrides: Partial<DisplayedRoute> = {}): DisplayedRoute {
    return {
      sourceSide: "right",
      targetSide: "left",
      sourceRatio: 0.5,
      targetRatio: 0.5,
      heelSource: { x: 210, y: 50 },
      heelTarget: { x: 490, y: 50 },
      waypoints: [{ x: 350, y: 50 }],
      ...overrides,
    };
  }

  it("accepts an orthogonal route whose every leg is axis-aligned", () => {
    expect(isFreezableRoute(route(), "orthogonal")).toBe(true);
  });

  it("refuses an orthogonal route with a diagonal leg", () => {
    // Freezing this would strand the relationship in a shape no later action is
    // allowed to repair, because a lock stops everything recomputing it.
    expect(isFreezableRoute(route({ waypoints: [{ x: 350, y: 90 }] }), "orthogonal")).toBe(false);
  });

  it("accepts a diagonal in straight mode, where it is the intended shape", () => {
    expect(isFreezableRoute(route({ waypoints: [{ x: 350, y: 90 }] }), "straight")).toBe(true);
  });

  it("refuses a route with a non-finite coordinate in either mode", () => {
    const broken = route({ heelTarget: { x: Number.NaN, y: 50 } });
    expect(isFreezableRoute(broken, "orthogonal")).toBe(false);
    expect(isFreezableRoute(broken, "straight")).toBe(false);
  });

  it("accepts a bendless orthogonal route between aligned heels", () => {
    expect(isFreezableRoute(route({ waypoints: [] }), "orthogonal")).toBe(true);
  });

  it("refuses a bendless orthogonal route between misaligned heels", () => {
    expect(
      isFreezableRoute(route({ waypoints: [], heelTarget: { x: 490, y: 120 } }), "orthogonal"),
    ).toBe(false);
  });
});

describe("isDrawableRoute and routeEntersCard", () => {
  it("rejects a diagonal only in orthogonal mode", () => {
    const diagonal = [{ x: 0, y: 0 }, { x: 100, y: 100 }];
    expect(isDrawableRoute(diagonal, "orthogonal")).toBe(false);
    expect(isDrawableRoute(diagonal, "straight")).toBe(true);
  });

  it("rejects a non-finite coordinate in either mode", () => {
    const broken = [{ x: 0, y: 0 }, { x: Number.POSITIVE_INFINITY, y: 0 }];
    expect(isDrawableRoute(broken, "orthogonal")).toBe(false);
    expect(isDrawableRoute(broken, "straight")).toBe(false);
  });

  it("reports a bend dragged inside the relationship's own card", () => {
    const card: Rect = { x: 0, y: 0, width: 200, height: 100 };
    expect(routeEntersCard([{ x: 100, y: 50 }], card)).toBe(true);
    expect(routeEntersCard([{ x: 300, y: 50 }], card)).toBe(false);
    // A bend exactly on the border is not inside it — the heel itself sits
    // there, so treating the border as inside would refuse every route.
    expect(routeEntersCard([{ x: 200, y: 50 }], card)).toBe(false);
  });
});
