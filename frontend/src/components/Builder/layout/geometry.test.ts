/**
 * Geometry predicates that decide whether a layout is accepted.
 *
 * These are the fail-closed checks: if one of them is wrong, either a broken
 * diagram ships (false negative) or a correct one is rejected and the user gets
 * no layout at all (false positive). Both have happened, so both directions are
 * asserted here, and the expected values are specified independently rather
 * than read back from the implementation.
 */
import { describe, expect, it } from "vitest";
import {
  layoutRadial,
  rectsOverlap,
  routeQualityFaults,
  routeTouchesUnrelatedNode,
  segmentIntersectsRect,
  separateRectangles,
} from "./geometry";
import type { LayoutEdgeSnapshot, LayoutNodeSnapshot, Rect } from "./types";

const RECT: Rect = { x: 0, y: 0, width: 100, height: 100 };

function node(id: string, width: number, height: number, tableType = "dimension"): LayoutNodeSnapshot {
  return { id, x: 0, y: 0, width, height, tableType, pinned: false, fixed: false, selected: false, measured: true };
}

function edge(id: string, source: string, target: string): LayoutEdgeSnapshot {
  return {
    id, source, target,
    pathMode: "orthogonal", routeMode: "auto", waypoints: [], locked: false,
    sourceMarkerExtent: 0, targetMarkerExtent: 0,
  };
}

describe("segmentIntersectsRect", () => {
  // `includeBoundary: true` asks "does this segment touch the card at all",
  // `false` asks "does it cross a positive length of the card's interior".
  // They are different questions and the validator relies on the second.
  const cases: Array<{ name: string; a: { x: number; y: number }; b: { x: number; y: number }; contact: boolean; interior: boolean }> = [
    {
      // The escape that let a connector be drawn straight through a table: the
      // diagonal meets each rectangle edge only at that edge's own endpoint, so
      // a proper-crossing test excludes both corners and reports nothing.
      name: "opposite-corner traversal, through the centre",
      a: { x: -10, y: -10 }, b: { x: 110, y: 110 }, contact: true, interior: true,
    },
    {
      name: "single-corner tangency, no interior",
      a: { x: -10, y: 10 }, b: { x: 10, y: -10 }, contact: true, interior: false,
    },
    {
      name: "bounding boxes overlap but the line misses",
      a: { x: -50, y: 150 }, b: { x: 150, y: 120 }, contact: false, interior: false,
    },
    {
      name: "collinear along the top border",
      a: { x: -10, y: 0 }, b: { x: 110, y: 0 }, contact: true, interior: false,
    },
    {
      name: "endpoint inside the card",
      a: { x: 50, y: 50 }, b: { x: 500, y: 50 }, contact: true, interior: true,
    },
    {
      // The false positive in the other direction: a route that stops exactly
      // on the border was rejected as if it had entered.
      name: "grazes an edge and stops, never enters",
      a: { x: -10, y: 50 }, b: { x: 0, y: 50 }, contact: true, interior: false,
    },
    {
      name: "clear miss",
      a: { x: 200, y: 200 }, b: { x: 300, y: 300 }, contact: false, interior: false,
    },
  ];

  for (const { name, a, b, contact, interior } of cases) {
    it(`${name}: contact=${contact}, interior=${interior}`, () => {
      expect(segmentIntersectsRect(a, b, RECT, true), "contact").toBe(contact);
      expect(segmentIntersectsRect(a, b, RECT, false), "interior").toBe(interior);
    });
  }

  it("is symmetric in the segment's direction", () => {
    for (const { a, b } of cases) {
      expect(segmentIntersectsRect(b, a, RECT, true)).toBe(segmentIntersectsRect(a, b, RECT, true));
      expect(segmentIntersectsRect(b, a, RECT, false)).toBe(segmentIntersectsRect(a, b, RECT, false));
    }
  });
});

describe("layoutRadial", () => {
  it("places four cards of very unequal size without overlap", () => {
    // The witness from the external review: the ring used to space centres by
    // half the sum of two angular spans, which is not the span the larger
    // card's chord was computed from, so these sizes overlapped by roughly
    // 95 x 62 px and the block-placement stage rejected the arrangement before
    // routing ever ran.
    const fact = node("fact", 320, 420, "fact");
    const dims = [node("d1", 420, 640), node("d2", 380, 520), node("d3", 180, 160)];
    const nodes = [fact, ...dims];

    const positions = layoutRadial(
      nodes,
      dims.map((dim, index) => edge(`j${index}`, fact.id, dim.id)),
      new Set(nodes.map((n) => n.id)),
      24,
    );

    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = { ...positions[nodes[i].id], width: nodes[i].width, height: nodes[i].height };
        const b = { ...positions[nodes[j].id], width: nodes[j].width, height: nodes[j].height };
        expect(rectsOverlap(a, b), `${nodes[i].id} overlaps ${nodes[j].id}`).toBe(false);
      }
    }
  });
});

describe("separateRectangles with locked cards", () => {
  function card(id: string, width: number, height: number): LayoutNodeSnapshot {
    return { id, x: 0, y: 0, width, height, tableType: "dimension", pinned: false, fixed: false, selected: false, measured: true };
  }
  function overlapping(nodes: LayoutNodeSnapshot[], positions: Record<string, { x: number; y: number }>, gap: number) {
    const bad: string[] = [];
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = { ...positions[nodes[i].id], width: nodes[i].width, height: nodes[i].height };
        const b = { ...positions[nodes[j].id], width: nodes[j].width, height: nodes[j].height };
        if (rectsOverlap(a, b, gap)) bad.push(`${nodes[i].id}~${nodes[j].id}`);
      }
    }
    return bad;
  }

  it("does not fail when movable cards are trapped between two locked cards", () => {
    // Reported from the running application: locking a table made Arrange fail
    // outright with "table cards cannot be separated without moving a fixed
    // card". A boxed-in card is pushed off one neighbour onto the next and
    // oscillates until the iteration budget runs out. Failing there is the
    // worst answer available — the user gets no diagram because they protected
    // one card — so a card that cannot settle is parked outside the cluster.
    const nodes = [card("lockA", 300, 400), card("lockB", 300, 400)];
    const positions: Record<string, { x: number; y: number }> = { lockA: { x: 0, y: 0 }, lockB: { x: 340, y: 0 } };
    for (let i = 0; i < 6; i++) {
      nodes.push(card(`m${i}`, 280, 380));
      positions[`m${i}`] = { x: 150 + i * 8, y: i * 8 };
    }

    const out = separateRectangles(nodes, positions, new Set(["lockA", "lockB"]), 24);

    // The locked cards did not move. That is the invariant this function exists
    // to keep, and parking must not quietly break it.
    expect(out.lockA).toEqual({ x: 0, y: 0 });
    expect(out.lockB).toEqual({ x: 340, y: 0 });
    expect(overlapping(nodes, out, 24), "every card is separated").toEqual([]);
  });

  it("still separates normally when nothing is locked", () => {
    const nodes = [card("a", 200, 200), card("b", 200, 200), card("c", 200, 200)];
    const positions = { a: { x: 0, y: 0 }, b: { x: 10, y: 10 }, c: { x: 20, y: 20 } };
    const out = separateRectangles(nodes, positions, new Set(), 24);
    expect(overlapping(nodes, out, 24)).toEqual([]);
  });

  it("leaves two overlapping locked cards alone rather than failing", () => {
    // The user placed them and asked for them to stay. Counting that overlap
    // would fail an arrangement for geometry nothing is allowed to change.
    const nodes = [card("lockA", 300, 300), card("lockB", 300, 300), card("m", 200, 200)];
    const positions = { lockA: { x: 0, y: 0 }, lockB: { x: 20, y: 20 }, m: { x: 600, y: 600 } };
    const out = separateRectangles(nodes, positions, new Set(["lockA", "lockB"]), 24);
    expect(out.lockA).toEqual({ x: 0, y: 0 });
    expect(out.lockB).toEqual({ x: 20, y: 20 });
  });
});

describe("routeQualityFaults reports what the relaxed policy tolerates", () => {
  // Reported by the external review (C10). The owner's decision is that a
  // crowded diagram may be imperfect — but the imperfection has to be visible,
  // or the modeller cannot tell a degraded arrangement from a correct one.
  // Reporting reused the VALIDATOR's predicate, which excuses a relationship's
  // own endpoint cards on its first and last segments because those are its
  // docking stubs. So the faults the relaxed policy allows were invisible.
  function card(id: string, x: number, y: number, width = 200, height = 200): LayoutNodeSnapshot {
    return { id, x, y, width, height, tableType: "dimension", pinned: false, fixed: false, selected: false, measured: true };
  }
  function route(points: Array<{ x: number; y: number }>, sourceSide: "left" | "right" | "top" | "bottom" = "right", targetSide: "left" | "right" | "top" | "bottom" = "left") {
    return {
      edgeId: "j", points, locked: false, sourceSide, targetSide,
      sourceRatio: 0.5, targetRatio: 0.5, waypoints: [], routeMode: "auto" as const, pathMode: "orthogonal" as const,
    };
  }
  const j = edge("j", "a", "b");

  it("counts a route that leaves the wrong side and crosses its own source card", () => {
    // The reviewer's witness: out of the source's LEFT heel at (-14,100), then
    // right through the entire source card. The interior crossing is real; the
    // reported count was zero, so no warning was shown.
    const nodes = [card("a", 0, 0), card("b", 600, 0)];
    const positions = { a: { x: 0, y: 0 }, b: { x: 600, y: 0 } };
    const r = route([{ x: -14, y: 100 }, { x: 300, y: 100 }, { x: 586, y: 100 }], "left", "left");

    expect(routeQualityFaults(r, j, nodes, positions)).toBeGreaterThan(0);
    // The hard-validity predicate still excuses it — that is its job, and it is
    // why reporting needed its own answer rather than borrowing this one.
    expect(routeTouchesUnrelatedNode(r.points, j, nodes, positions)).toBe(false);
  });

  it("counts a crossing of an unrelated card", () => {
    const nodes = [card("a", 0, 0), card("b", 800, 0), card("c", 400, 50)];
    const positions = { a: { x: 0, y: 0 }, b: { x: 800, y: 0 }, c: { x: 400, y: 50 } };
    const r = route([{ x: 214, y: 100 }, { x: 786, y: 100 }]);
    expect(routeQualityFaults(r, j, nodes, positions)).toBeGreaterThan(0);
  });

  it("reports nothing for a clean route", () => {
    // The check must cost a correct route nothing, or every diagram warns and
    // the warning stops meaning anything.
    const nodes = [card("a", 0, 0), card("b", 600, 0)];
    const positions = { a: { x: 0, y: 0 }, b: { x: 600, y: 0 } };
    const r = route([{ x: 214, y: 100 }, { x: 586, y: 100 }]);
    expect(routeQualityFaults(r, j, nodes, positions)).toBe(0);
  });

  it("reports nothing for a route that merely grazes a border", () => {
    // Touching is not entering. Counting contact would warn on correct
    // diagrams, which is the false positive that made the validator reject
    // drawable layouts before.
    const nodes = [card("a", 0, 0), card("b", 600, 0), card("c", 300, 100)];
    const positions = { a: { x: 0, y: 0 }, b: { x: 600, y: 0 }, c: { x: 300, y: 100 } };
    const r = route([{ x: 214, y: 100 }, { x: 586, y: 100 }]);
    expect(routeQualityFaults(r, j, nodes, positions)).toBe(0);
  });

  it("counts a terminal segment that runs back into its own card", () => {
    // The relaxed validator skips this check entirely; without it here the
    // fault is tolerated AND silent.
    const nodes = [card("a", 0, 0), card("b", 600, 0)];
    const positions = { a: { x: 0, y: 0 }, b: { x: 600, y: 0 } };
    // Heel on the right of `a`, but the next point is back to the LEFT of it.
    const r = route([{ x: 214, y: 100 }, { x: 150, y: 100 }, { x: 586, y: 100 }]);
    expect(routeQualityFaults(r, j, nodes, positions)).toBeGreaterThan(0);
  });
});
