/**
 * Real-engine layout tests.
 *
 * These run the actual `computeLayout` pipeline with the installed `elkjs` and
 * `libavoid-js` — no engine mocks — so they prove the placement, coordinated
 * routing and geometry validation agree with each other and with the renderer's
 * docking contract. Route *quality* is asserted as measurable metrics, not as a
 * shape assertion.
 */
import { describe, expect, it } from "vitest";
import { computeLayout } from "./layout.worker";
import { toElkGraph } from "./elkLayout";
import { resolveMarkerKinds, markerExtent } from "./docking";
import { layoutRadial, rectsOverlap, segmentIntersectsRect, routeTouchesUnrelatedNode } from "./geometry";
import type {
  LayoutEdgeSnapshot,
  LayoutNodeSnapshot,
  LayoutSnapshot,
} from "./types";

const NOTATION = "crowsfoot" as const;

function node(
  id: string,
  x: number,
  y: number,
  width: number,
  height: number,
  overrides: Partial<LayoutNodeSnapshot> = {},
): LayoutNodeSnapshot {
  return { id, x, y, width, height, tableType: "dimension", pinned: false, fixed: false, selected: false, measured: true, ...overrides };
}

/** Build a relationship with the marker extents the renderer would draw. */
function edge(
  id: string,
  source: LayoutNodeSnapshot,
  target: LayoutNodeSnapshot,
  overrides: Partial<LayoutEdgeSnapshot> = {},
): LayoutEdgeSnapshot {
  const kinds = resolveMarkerKinds({
    sourceIsFact: source.tableType === "fact",
    targetIsFact: target.tableType === "fact",
    sourceIsDim: source.tableType !== "fact",
    targetIsDim: target.tableType !== "fact",
  });
  return {
    id,
    source: source.id,
    target: target.id,
    pathMode: "orthogonal",
    routeMode: "auto",
    waypoints: [],
    locked: false,
    sourceMarkerExtent: markerExtent(kinds.source, NOTATION),
    targetMarkerExtent: markerExtent(kinds.target, NOTATION),
    ...overrides,
  };
}

function snapshot(
  nodes: LayoutNodeSnapshot[],
  edges: LayoutEdgeSnapshot[],
  overrides: Partial<LayoutSnapshot> = {},
): LayoutSnapshot {
  return {
    scope: { projectId: "p1", modelId: "m1" },
    revision: 1,
    nodes,
    edges,
    options: { preset: "hierarchical", direction: "DOWN", spacing: "normal" },
    ...overrides,
  };
}

/** Single star: one fact plus eight dimensions with variable card sizes. */
function starFixture() {
  const fact = node("fact_sales", 0, 0, 320, 420, { tableType: "fact" });
  const dims = Array.from({ length: 8 }, (_, index) =>
    node(`dim_${index}`, 0, 0, 180 + index * 12, 160 + index * 20),
  );
  const edges = dims.map((dim, index) => edge(`join_${index}`, fact, dim));
  return snapshot([fact, ...dims], edges);
}

describe("computeLayout — real ELK + libavoid", () => {
  it("arranges a single star without overlapping cards and routes every join", async () => {
    const input = starFixture();
    const result = await computeLayout(input, "arrange-all", "r1");

    expect(result.kind).toBe("success");
    if (result.kind !== "success") return;

    // Every card received a finite position and no card overlaps another.
    expect(Object.keys(result.positions).sort()).toEqual(input.nodes.map((n) => n.id).sort());
    expect(result.metrics.nodeOverlapCount).toBe(0);
    expect(result.metrics.throughNodeSegmentCount).toBe(0);

    // Every relationship has a route with at least one segment.
    for (const relationship of input.edges) {
      const route = result.routes[relationship.id];
      expect(route, `route for ${relationship.id}`).toBeTruthy();
      expect(route.points.length).toBeGreaterThanOrEqual(2);
      expect(route.locked).toBe(false);
      // Provenance: the engine computed these bends, and that is what the apply
      // path must persist (spec 5).
      expect(route.routeMode).toBe("auto");
    }
    expect(result.metrics.totalBends).toBeGreaterThan(0);
  }, 60000);

  it("produces an identical result on an identical repeat (deterministic)", async () => {
    const first = await computeLayout(starFixture(), "arrange-all", "d1");
    const second = await computeLayout(starFixture(), "arrange-all", "d2");
    expect(first.kind).toBe("success");
    expect(second.kind).toBe("success");
    if (first.kind !== "success" || second.kind !== "success") return;

    expect(second.positions).toEqual(first.positions);
    expect(second.routes).toEqual(first.routes);
  }, 60000);

  it("arranges the star fact-centred and disjointly with the radial preset", async () => {
    const input = { ...starFixture(), options: { preset: "radial", direction: "DOWN", spacing: "normal" } };
    const result = await computeLayout(input, "arrange-all", "r-radial");
    expect(result.kind).toBe("success");
    if (result.kind !== "success") return;
    const positions = Object.values(result.positions);
    const xs = positions.map((p) => p.x);
    const ys = positions.map((p) => p.y);
    const bbox = Math.max(...xs) - Math.min(...xs) + Math.max(...ys) - Math.min(...ys);
    expect(result.metrics.nodeOverlapCount).toBe(0);
    expect(result.metrics.throughNodeSegmentCount).toBe(0);
    expect(bbox).toBeLessThan(2600);

    // F04: the fact card is the centre of the arrangement, not just a label.
    // The fact's centre must lie inside the convex hull of its dimensions'
    // centres and closer to the centroid than any dimension.
    const fact = input.nodes.find((n) => n.tableType === "fact")!;
    const factPos = result.positions[fact.id];
    const factCentre = { x: factPos.x + fact.width / 2, y: factPos.y + fact.height / 2 };
    const dims = input.nodes.filter((n) => n.tableType !== "fact");
    const dimCentres = dims.map((n) => {
      const p = result.positions[n.id];
      return { x: p.x + n.width / 2, y: p.y + n.height / 2 };
    });
    const centroid = {
      x: dimCentres.reduce((s, c) => s + c.x, 0) / dimCentres.length,
      y: dimCentres.reduce((s, c) => s + c.y, 0) / dimCentres.length,
    };
    const factDistance = Math.hypot(factCentre.x - centroid.x, factCentre.y - centroid.y);
    const minDimDistance = Math.min(...dimCentres.map((c) => Math.hypot(c.x - centroid.x, c.y - centroid.y)));
    expect(factDistance).toBeLessThan(minDimDistance);
  });

  it("lays out multiple shared facts on a compact centre with dimensions around them (F04)", async () => {
    const facts = [
      node("fact_orders", 0, 0, 300, 260, { tableType: "fact" }),
      node("fact_invoices", 0, 0, 300, 260, { tableType: "fact" }),
    ];
    const dims = [
      node("dim_customer", 0, 0, 220, 300),
      node("dim_date", 0, 0, 200, 200),
      node("dim_product", 0, 0, 240, 320),
    ];
    const edges = [
      edge("o_c", facts[0], dims[0]),
      edge("o_d", facts[0], dims[1]),
      edge("o_p", facts[0], dims[2]),
      edge("i_c", facts[1], dims[0]),
      edge("i_d", facts[1], dims[1]),
    ];
    const result = await computeLayout(
      snapshot([...facts, ...dims], edges, { options: { preset: "radial", direction: "DOWN", spacing: "normal" } }),
      "arrange-all",
      "r-multi",
    );
    expect(result.kind).toBe("success");
    if (result.kind !== "success") return;
    expect(result.metrics.nodeOverlapCount).toBe(0);

    // Fact-centred rule for the multi-fact model (F04): both facts share the
    // centre cluster, so every dimension's centre is farther from the facts'
    // centroid than either fact is.
    const factsCentroid = {
      x: facts.reduce((s, f) => s + result.positions[f.id].x + f.width / 2, 0) / facts.length,
      y: facts.reduce((s, f) => s + result.positions[f.id].y + f.height / 2, 0) / facts.length,
    };
    const factDistances = facts.map((f) => {
      const p = result.positions[f.id];
      return Math.hypot(p.x + f.width / 2 - factsCentroid.x, p.y + f.height / 2 - factsCentroid.y);
    });
    const dimDistances = dims.map((d) => {
      const p = result.positions[d.id];
      return Math.hypot(p.x + d.width / 2 - factsCentroid.x, p.y + d.height / 2 - factsCentroid.y);
    });
    expect(Math.max(...factDistances)).toBeLessThan(Math.min(...dimDistances));
  }, 60000);

  it("spreads radial ring cards evenly around the centre without overlap (R2-01)", () => {
    // Placement only: `layoutRadial` is asserted directly because the worker's
    // rectangle separation stage would repair an overlapping ring and hide the
    // defect this guards.
    const fact = node("fact_sales", 0, 0, 320, 420, { tableType: "fact" });
    // Two oversized cards followed by four small ones. The previous ring
    // advanced the cursor by each card's own angular span and placed the card
    // at its mid-angle, so two adjacent centres ended up separated by half the
    // sum of two spans instead of by the span the larger card's chord required.
    // Where the extents drop sharply (dim_b to dim_c) that deficit exceeded the
    // half-diagonal margin and the two cards genuinely overlapped.
    const dims = [
      node("dim_a", 0, 0, 700, 700),
      node("dim_b", 0, 0, 700, 700),
      node("dim_c", 0, 0, 120, 80),
      node("dim_d", 0, 0, 120, 80),
      node("dim_e", 0, 0, 120, 80),
      node("dim_f", 0, 0, 120, 80),
    ];
    const nodes = [fact, ...dims];
    const edges = dims.map((dim, index) => edge(`join_${index}`, fact, dim));
    const nodeGap = 40;

    const positions = layoutRadial(nodes, edges, new Set(nodes.map((n) => n.id)), nodeGap);
    expect(Object.keys(positions).sort()).toEqual(nodes.map((n) => n.id).sort());

    // No card overlaps another, at the requested separation.
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = { ...positions[nodes[i].id], width: nodes[i].width, height: nodes[i].height };
        const b = { ...positions[nodes[j].id], width: nodes[j].width, height: nodes[j].height };
        expect(rectsOverlap(a, b), `${nodes[i].id} overlaps ${nodes[j].id}`).toBe(false);
      }
    }

    // The ring surrounds the centre: consecutive ring centres are one equal
    // angular step apart, so a dominating clearance radius cannot leave the
    // unused circumference bunched on one side.
    const angles = dims
      .map((dim) => {
        const p = positions[dim.id];
        return Math.atan2(p.y + dim.height / 2, p.x + dim.width / 2);
      })
      .sort((a, b) => a - b);
    const step = (Math.PI * 2) / dims.length;
    for (let index = 0; index < angles.length; index++) {
      const next = index + 1 === angles.length ? angles[0] + Math.PI * 2 : angles[index + 1];
      expect(next - angles[index]).toBeCloseTo(step, 6);
    }

    // …and the ring's own centroid stays on the fact at the origin.
    const centroid = dims.reduce(
      (sum, dim) => {
        const p = positions[dim.id];
        return { x: sum.x + (p.x + dim.width / 2) / dims.length, y: sum.y + (p.y + dim.height / 2) / dims.length };
      },
      { x: 0, y: 0 },
    );
    const ringRadius = Math.hypot(
      positions[dims[0].id].x + dims[0].width / 2,
      positions[dims[0].id].y + dims[0].height / 2,
    );
    expect(Math.hypot(centroid.x, centroid.y)).toBeLessThan(ringRadius * 0.01);
  });

  it("re-orients auto docking when direction changes after a save (F03)", async () => {
    const input = starFixture();
    const down = await computeLayout(input, "arrange-all", "f3-down");
    expect(down.kind).toBe("success");
    if (down.kind !== "success") return;

    // Persist the DOWN result (auto docking), then arrange the same saved state
    // with direction RIGHT.
    const savedNodes = input.nodes.map((n) => ({ ...n, ...down.positions[n.id] }));
    const savedEdges = input.edges.map((rel) => {
      const route = down.routes[rel.id];
      return {
        ...rel,
        sourceSide: route.sourceSide,
        targetSide: route.targetSide,
        sourceRatio: route.sourceRatio,
        targetRatio: route.targetRatio,
        waypoints: route.waypoints,
      };
    });
    const saved = snapshot(savedNodes, savedEdges, {
      options: { preset: "hierarchical", direction: "RIGHT", spacing: "normal" },
    });
    const fromSaved = await computeLayout(saved, "arrange-all", "f3-right");
    expect(fromSaved.kind).toBe("success");
    if (fromSaved.kind !== "success") return;

    // A fresh RIGHT arrange is the reference: saved auto docking must not bind
    // the router, so the saved-then-RIGHT result must match fresh-RIGHT exactly
    // (same final positions, same docking). If the saved DOWN sides were still
    // treated as constraints, the two would differ.
    const freshRight = await computeLayout(
      { ...input, options: { preset: "hierarchical", direction: "RIGHT", spacing: "normal" } },
      "arrange-all",
      "f3-fresh",
    );
    expect(freshRight.kind).toBe("success");
    if (freshRight.kind !== "success") return;
    // Positions may translate differently (the saved state's "original" block
    // sits elsewhere), but the docking sides are a pure function of the final
    // relative geometry, so they must be identical: saved auto docking did not
    // bind the router.
    for (const rel of input.edges) {
      expect(fromSaved.routes[rel.id].sourceSide).toBe(freshRight.routes[rel.id].sourceSide);
      expect(fromSaved.routes[rel.id].targetSide).toBe(freshRight.routes[rel.id].targetSide);
    }
  }, 60000);

  it("keeps every table coordinate and size unchanged for reroute-links", async () => {
    const input = starFixture();
    // Give the model a real hand-placed layout first.
    const arranged = await computeLayout(input, "arrange-all", "p1");
    expect(arranged.kind).toBe("success");
    if (arranged.kind !== "success") return;

    const placed = snapshot(
      input.nodes.map((n) => ({ ...n, x: arranged.positions[n.id].x, y: arranged.positions[n.id].y })),
      input.edges,
      { revision: 2 },
    );
    const rerouted = await computeLayout(placed, "reroute-links", "p2");
    expect(rerouted.kind).toBe("success");
    if (rerouted.kind !== "success") return;

    for (const original of placed.nodes) {
      expect(rerouted.positions[original.id], original.id).toEqual({ x: original.x, y: original.y });
    }
    // Rerouting still produced a usable route for every relationship.
    for (const relationship of placed.edges) {
      expect(rerouted.routes[relationship.id]?.points.length ?? 0).toBeGreaterThanOrEqual(2);
    }
  }, 60000);

  it("gives parallel joins between the same pair distinct docking points", async () => {
    const fact = node("fact_a", 0, 0, 300, 400, { tableType: "fact" });
    const dim = node("dim_a", 0, 0, 220, 300);
    const edges = [0, 1, 2].map((index) =>
      edge(`p_${index}`, fact, dim, { offsetIndex: index, totalEdges: 3 }),
    );
    const result = await computeLayout(snapshot([fact, dim], edges), "arrange-all", "par");
    expect(result.kind).toBe("success");
    if (result.kind !== "success") return;

    const heels = edges.map((relationship) => {
      const route = result.routes[relationship.id];
      return `${route.points[0].x},${route.points[0].y}`;
    });
    expect(new Set(heels).size).toBe(3);
    // Distinct docking must also mean distinct stored base ratios.
    const ratios = edges.map((relationship) => result.routes[relationship.id].sourceRatio);
    expect(new Set(ratios).size).toBe(3);
  }, 60000);

  it("treats a pinned card as immovable during arrange-all", async () => {
    const input = starFixture();
    const pinnedId = "dim_3";
    const pinned = { ...input.nodes.find((n) => n.id === pinnedId)!, x: 2500, y: 1800, pinned: true };
    const nodes = input.nodes.map((n) => (n.id === pinnedId ? pinned : n));

    const result = await computeLayout(snapshot(nodes, input.edges), "arrange-all", "pin");
    expect(result.kind).toBe("success");
    if (result.kind !== "success") return;
    expect(result.positions[pinnedId]).toEqual({ x: 2500, y: 1800 });
  }, 60000);

  it("maps saved canvas docking sides to valid ELK port sides (F02)", () => {
    const fact = node("fact_sales", 0, 0, 320, 420, { tableType: "fact" });
    const dim = node("dim_0", 0, 0, 180, 160);
    const sides: Array<[LayoutEdgeSnapshot["sourceSide"], LayoutEdgeSnapshot["targetSide"]]> = [
      ["left", "right"],
      ["right", "left"],
      ["top", "bottom"],
      ["bottom", "top"],
    ];
    const edges = sides.map(([sourceSide, targetSide], index) =>
      edge(`join_${index}`, fact, dim, { sourceSide, targetSide, routeMode: "manual" }),
    );
    const graph = toElkGraph(
      snapshot([fact, dim], edges),
      new Set([fact.id, dim.id]),
      { nodeSpacing: 64, layerSpacing: 96, direction: "DOWN" },
    );
    const emitted = new Set<string>();
    for (const child of graph.children ?? []) {
      for (const port of child.ports ?? []) {
        const side = port.layoutOptions?.["elk.port.side"];
        if (side) emitted.add(side);
      }
    }
    // Saved canvas sides must never reach ELK as LEFT/RIGHT/TOP/BOTTOM.
    expect([...emitted].sort()).toEqual(["EAST", "NORTH", "SOUTH", "WEST"]);
  });

  it("arranges a saved diagram a second time across all four sides and both directions (F02 round-trip)", async () => {
    const input = starFixture();
    const first = await computeLayout(input, "arrange-all", "f2a");
    expect(first.kind).toBe("success");
    if (first.kind !== "success") return;

    // Persist the first result the way Canvas does: arranged positions plus the
    // engine's sides/ratios/waypoints, then feed it back in as saved state.
    const savedNodes = input.nodes.map((n) => ({ ...n, ...first.positions[n.id] }));
    const savedEdges = input.edges.map((rel) => {
      const route = first.routes[rel.id];
      return {
        ...rel,
        sourceSide: route.sourceSide,
        targetSide: route.targetSide,
        sourceRatio: route.sourceRatio,
        targetRatio: route.targetRatio,
        waypoints: route.waypoints,
      };
    });

    for (const direction of ["DOWN", "RIGHT"] as const) {
      const again = await computeLayout(
        snapshot(savedNodes, savedEdges, { options: { preset: "hierarchical", direction, spacing: "normal" } }),
        "arrange-all",
        `f2b-${direction}`,
      );
      expect(again.kind, `direction ${direction}`).toBe("success");
    }
  }, 60000);

  it("leaves unselected cards untouched during arrange-selected", async () => {
    const input = starFixture();
    const arranged = await computeLayout(input, "arrange-all", "s0");
    expect(arranged.kind).toBe("success");
    if (arranged.kind !== "success") return;

    // Two selected cards are deliberately stacked on one spot in free space, so
    // the operation must genuinely place them apart to satisfy the contract.
    const selectedIds = ["dim_1", "dim_2"];
    const stackX = -2000;
    const stackY = -2000;
    const nodes = input.nodes.map((n) => ({
      ...n,
      x: selectedIds.includes(n.id) ? stackX : arranged.positions[n.id].x,
      y: selectedIds.includes(n.id) ? stackY : arranged.positions[n.id].y,
      selected: selectedIds.includes(n.id),
    }));
    const result = await computeLayout(snapshot(nodes, input.edges, { revision: 3 }), "arrange-selected", "s1");
    expect(result.kind).toBe("success");
    if (result.kind !== "success") return;

    // Every unselected card keeps bit-for-bit its original coordinates.
    for (const original of nodes) {
      if (selectedIds.includes(original.id)) continue;
      expect(result.positions[original.id], original.id).toEqual({ x: original.x, y: original.y });
    }

    // The stacked selection is now genuinely placed apart.
    const [first, second] = selectedIds.map((id) => {
      const card = nodes.find((n) => n.id === id)!;
      const position = result.positions[id];
      return { x: position.x, y: position.y, width: card.width, height: card.height };
    });
    expect(result.positions[selectedIds[0]]).not.toEqual({ x: stackX, y: stackY });
    expect(rectsOverlap(first, second)).toBe(false);
    expect(result.metrics.nodeOverlapCount).toBe(0);
  }, 60000);

  it("reroutes a valid connector while two unrelated cards overlap (F06)", async () => {
    // Hand-arranged model with two overlapping cards far from the routed pair.
    const a = node("a", 0, 0, 200, 200);
    const b = node("b", 600, 0, 200, 200);
    const c = node("c", -4000, -4000, 300, 300);
    const d = node("d", -3900, -3900, 300, 300); // overlaps c
    const result = await computeLayout(
      snapshot([a, b, c, d], [edge("j", a, b)]),
      "reroute-links",
      "f6",
    );
    expect(result.kind).toBe("success");
    if (result.kind !== "success") return;
    // Reroute never moves a card, so the overlap stays; it must not fail.
    for (const n of [a, b, c, d]) {
      expect(result.positions[n.id]).toEqual({ x: n.x, y: n.y });
    }
    expect(result.routes["j"].points.length).toBeGreaterThanOrEqual(2);
  }, 60000);

  it("repair-routes recomputes auto routes but keeps manual and locked geometry (F05)", async () => {
    const pairs = [
      [node("a1", 0, 0, 200, 200), node("b1", 600, 0, 200, 200)],
      [node("a2", 0, 400, 200, 200), node("b2", 600, 400, 200, 200)],
      [node("a3", 0, 800, 200, 200), node("b3", 600, 800, 200, 200)],
    ] as const;
    const auto = edge("auto", pairs[0][0], pairs[0][1], { routeMode: "auto", waypoints: [] });
    const manual = edge("manual", pairs[1][0], pairs[1][1], {
      routeMode: "manual",
      waypoints: [{ x: 400, y: 500 }],
    });
    const locked = edge("locked", pairs[2][0], pairs[2][1], {
      locked: true,
      routeMode: "manual",
      waypoints: [{ x: 400, y: 900 }],
    });
    const allNodes = pairs.flatMap(([s, t]) => [s, t]);
    const input = snapshot(allNodes, [auto, manual, locked]);
    const result = await computeLayout(input, "repair-routes", "repair");
    expect(result.kind).toBe("success");
    if (result.kind !== "success") return;

    // repair-routes never moves a card.
    for (const n of allNodes) {
      expect(result.positions[n.id]).toEqual({ x: n.x, y: n.y });
    }
    // The auto route was recomputed (auto provenance retained).
    expect(result.routes["auto"].routeMode).toBe("auto");
    // Manual and locked routes keep their stored waypoints bit-for-bit.
    expect(result.routes["manual"].waypoints).toEqual([{ x: 400, y: 500 }]);
    expect(result.routes["locked"].waypoints).toEqual([{ x: 400, y: 900 }]);
    expect(result.routes["locked"].locked).toBe(true);
  }, 60000);

  it("repair-routes re-routes a manual route whose endpoint card moved (no stale absolute bends)", async () => {
    // A valid manual orthogonal route: source right heel at y=100, one bend at
    // (350,100), target left heel at y=100 — all horizontal.
    const a = node("a", 0, 0, 200, 200);
    const b = node("b", 500, 0, 200, 200);
    const manual = edge("j", a, b, {
      routeMode: "manual",
      sourceSide: "right",
      targetSide: "left",
      sourceRatio: 0.5,
      targetRatio: 0.5,
      waypoints: [{ x: 350, y: 100 }],
    });
    // The source card moved down 300px; its heel now sits at y=400, so the
    // stored bend would draw a diagonal from (200,400) to (350,100).
    const moved = node("a", 0, 300, 200, 200);
    const result = await computeLayout(snapshot([moved, b], [manual]), "repair-routes", "repair-moved");
    expect(result.kind).toBe("success");
    if (result.kind !== "success") return;
    // The route was recomputed against the new geometry — no diagonal, and the
    // stale bend was not retained.
    expect(result.routes["j"].points.some((point, index) =>
      index > 0 && Math.abs(point.x - result.routes["j"].points[index - 1].x) > 0.5 &&
        Math.abs(point.y - result.routes["j"].points[index - 1].y) > 0.5,
    )).toBe(false);
  }, 60000);

  it("repair-routes repairs a manual route whose terminal now points into the card", async () => {
    // A stored bend can land in the gap between the target heel and the card
    // border after the card moves: still orthogonal and still outside the card
    // interior, but the arriving segment now points back into the card. The
    // retained route must be repaired, not kept as "enters target card".
    const a = node("a", 0, 0, 200, 200);
    const b = node("b", 500, 0, 200, 200); // card 500..700, left heel at 492
    const manual = edge("j", a, b, {
      routeMode: "manual",
      sourceSide: "right",
      targetSide: "left",
      sourceRatio: 0.5,
      targetRatio: 0.5,
      waypoints: [{ x: 435, y: 100 }],
    });
    // Move the target left by 60: card 440..640, heel at 432, so the stored
    // bend at x=435 sits between the heel and the card border — the arriving
    // segment points into the card.
    const moved = node("b", 440, 0, 200, 200);
    const result = await computeLayout(snapshot([a, moved], [manual]), "repair-routes", "repair-terminal");
    expect(result.kind).toBe("success");
    if (result.kind !== "success") return;
    // The stale bend was dropped and the route was recomputed cleanly.
    expect(result.routes["j"].waypoints).toEqual([]);
    expect(result.routes["j"].points.length).toBe(2);
  }, 60000);

  it("drops engine orthogonal bends from a straight-mode auto route (F08)", async () => {
    // A route previously arranged orthogonally now carries stale orthogonal
    // bends, but the global preference is straight: the retained straight
    // geometry must be the direct heel-to-heel line, not the old bends.
    const a = node("a", 0, 0, 200, 200);
    const b = node("b", 500, 0, 200, 200);
    const straight = edge("j", a, b, {
      pathMode: "straight",
      routeMode: "auto",
      sourceSide: "right",
      targetSide: "left",
      sourceRatio: 0.5,
      targetRatio: 0.5,
      waypoints: [{ x: 350, y: 100 }],
    });
    const result = await computeLayout(snapshot([a, b], [straight]), "arrange-all", "straight-auto");
    expect(result.kind).toBe("success");
    if (result.kind !== "success") return;
    expect(result.routes["j"].pathMode).toBe("straight");
    // No interior bends remain: a straight auto route is exactly two points.
    expect(result.routes["j"].waypoints).toEqual([]);
    expect(result.routes["j"].points.length).toBe(2);
  }, 60000);

  it("rejects a route that leaves then re-enters its source card (F07)", () => {
    const source = node("s", 0, 0, 100, 100);
    const target = node("t", 400, 0, 100, 100);
    // Straight route that exits the source, loops left and comes back through
    // the source interior before heading to the target.
    const points = [
      { x: 50, y: 100 },   // source bottom heel
      { x: 50, y: 200 },
      { x: -200, y: 200 },
      { x: -200, y: 50 },
      { x: 50, y: 50 },    // back inside the source card
      { x: 400, y: 50 },
      { x: 450, y: 0 },    // target left heel
    ];
    const rel = edge("j", source, target, { pathMode: "straight", routeMode: "manual" });
    expect(
      routeTouchesUnrelatedNode(points, rel, [source, target], {
        s: { x: 0, y: 0 },
        t: { x: 400, y: 0 },
      }),
    ).toBe(true);
  });

  it("accepts a straight route whose bounding box merely overlaps an unrelated card (F07)", () => {
    // The diagonal from (0,0) to (100,100) never enters the rect x=0..20,
    // y=70..90, but their bounding boxes overlap — the old predicate rejected
    // this valid straight route.
    expect(
      segmentIntersectsRect(
        { x: 0, y: 0 },
        { x: 100, y: 100 },
        { x: 0, y: 70, width: 20, height: 20 },
        false,
      ),
    ).toBe(false);
    // A genuine diagonal crossing is still detected.
    expect(
      segmentIntersectsRect(
        { x: 0, y: 0 },
        { x: 100, y: 100 },
        { x: 30, y: 45, width: 20, height: 10 },
        false,
      ),
    ).toBe(true);
  });
});
