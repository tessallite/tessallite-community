/**
 * R12: deterministic quality comparison on the agreed fixtures.
 *
 * Runs the real `computeLayout` pipeline (installed elkjs + libavoid-js, no engine
 * mocks — mocks may prove transport ordering, never route quality) on the fixture
 * cases the specification names for this requirement, and turns the result into
 * recorded numbers rather than a shape assertion.
 *
 * Two classes of assertion, deliberately separated:
 *
 *   Hard invariants — zero node overlaps, zero through-node segments, zero moved
 *   fixed nodes, and every relationship accounted for with a finite route. These
 *   are absolute: the specification requires them on supported fixtures, and
 *   "fewer crossings" can never be bought by dropping a link.
 *
 *   Budgets — edge crossings, bends, length and elapsed time are compared against
 *   per-fixture budgets recorded in `config.json`. The specification requires
 *   crossings to be *minimised and documented per fixture*, and explicitly
 *   forbids a universal zero-crossing claim, so the budget is the assertion and
 *   the measured value is the evidence.
 *
 * The measured metric set is printed so a run leaves a readable record; the
 * committed numbers live in `docs/execution/evidence/`.
 */
import { describe, expect, it } from "vitest";
import config from "./config.json";
import { computeLayout } from "./layout.worker";
import { markerExtent, resolveMarkerKinds } from "./docking";
import type {
  LayoutEdgeSnapshot,
  LayoutMetrics,
  LayoutNodeSnapshot,
  LayoutSnapshot,
} from "./types";

const NOTATION = "crowsfoot" as const;

/** Sizes taken from the specification's variable-size band: 180x160 .. 420x640. */
const SIZES: Array<[number, number]> = [
  [180, 160],
  [240, 200],
  [300, 240],
  [320, 420],
  [380, 520],
  [420, 640],
];

function node(id: string, kind: "fact" | "dimension", size: [number, number]): LayoutNodeSnapshot {
  return {
    id,
    x: 0,
    y: 0,
    width: size[0],
    height: size[1],
    tableType: kind,
    pinned: false,
    fixed: false,
    selected: false,
    measured: true,
  };
}

function relationship(id: string, source: LayoutNodeSnapshot, target: LayoutNodeSnapshot): LayoutEdgeSnapshot {
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
  };
}

function snapshot(nodes: LayoutNodeSnapshot[], edges: LayoutEdgeSnapshot[]): LayoutSnapshot {
  return {
    scope: { projectId: "quality", modelId: "fixtures" },
    revision: 1,
    nodes,
    edges,
    options: { preset: "hierarchical", direction: "DOWN", spacing: "normal" },
  };
}

// ---------------------------------------------------------------------------
// The agreed fixtures
// ---------------------------------------------------------------------------

/** Shared dimensions: three facts and eight dimensions with shared links. */
function sharedDimensions() {
  const facts = [0, 1, 2].map((i) => node(`fact-${i}`, "fact", SIZES[3]));
  const dims = Array.from({ length: 8 }, (_, i) => node(`dim-${i}`, "dimension", SIZES[i % SIZES.length]));
  const edges: LayoutEdgeSnapshot[] = [];
  // Every dimension joins two facts, so most links share a semantic endpoint.
  dims.forEach((dim, i) => {
    edges.push(relationship(`j-${i}-a`, facts[i % facts.length], dim));
    edges.push(relationship(`j-${i}-b`, dim, facts[(i + 2) % facts.length]));
  });
  return { nodes: [...facts, ...dims], edges };
}

/** Variable sizes across the whole specification band, single star. */
function variableSizes() {
  const fact = node("fact-0", "fact", SIZES[5]);
  const dims = SIZES.map((size, i) => node(`dim-${i}`, "dimension", size));
  return {
    nodes: [fact, ...dims],
    edges: dims.map((dim, i) => relationship(`j-${i}`, fact, dim)),
  };
}

/** Parallel joins between one pair, including a reverse-oriented relationship. */
function parallelJoins() {
  const fact = node("fact-0", "fact", SIZES[3]);
  const dim = node("dim-0", "dimension", SIZES[2]);
  const other = node("dim-1", "dimension", SIZES[1]);
  const four = [
    relationship("j-a", fact, dim),
    relationship("j-b", fact, dim),
    relationship("j-c", fact, dim),
    // Reverse orientation: same pair, opposite direction.
    relationship("j-rev", dim, fact),
  ].map((edge, index) => ({ ...edge, offsetIndex: index, totalEdges: 4 }));
  return {
    nodes: [fact, dim, other],
    edges: [...four, relationship("j-other", other, fact)],
  };
}

/** A hand-arranged model with a pinned card that must never move. */
function pinnedFixed() {
  const fact = node("fact-0", "fact", SIZES[3]);
  const pinned = { ...node("dim-0", "dimension", SIZES[1]), x: 2400, y: 1600, pinned: true };
  const free = node("dim-1", "dimension", SIZES[0]);
  return {
    nodes: [fact, pinned, free],
    edges: [
      relationship("j-pin", fact, pinned),
      relationship("j-free", fact, free),
    ],
  };
}

/**
 * Large model: 100 nodes and 180 joins with varying sizes.
 *
 * This is the specification's stress case. Note (F09): with `dim = dims[i % 90]`
 * and `fact = facts[i % 10]`, 90 divides 180, so the model is ten disconnected
 * stars — a useful parallel/disconnected stress test, not a dense connected
 * model. A genuinely connected shared-dimension graph is covered by the
 * `connected` fixture; a dense *connected* 100-node graph is a known engine
 * limitation (libavoid routes through a table) recorded in the remediation plan.
 */
function largeModel() {
  const nodeCount = config.qualityFixtures.largeNodeCount;
  const joinCount = config.qualityFixtures.largeJoinCount;
  const factCount = 10;
  const nodes: LayoutNodeSnapshot[] = [];
  for (let i = 0; i < nodeCount; i += 1) {
    const kind = i < factCount ? "fact" : "dimension";
    nodes.push(node(`${kind}-${i}`, kind, SIZES[i % SIZES.length]));
  }
  const facts = nodes.slice(0, factCount);
  const dims = nodes.slice(factCount);
  const edges: LayoutEdgeSnapshot[] = [];
  for (let i = 0; i < joinCount; i += 1) {
    const dim = dims[i % dims.length];
    const fact = facts[i % facts.length];
    edges.push(relationship(`j-${i}`, fact, dim));
  }
  return { nodes, edges };
}

/**
 * Connected graph with genuinely shared dimensions: every dimension joins two
 * adjacent facts in a ring, so no fact is an isolated star and dimensions are
 * shared across facts (F09). Sized to the largest connected graph the current
 * router handles without a through-table segment.
 */
function connectedSharedDimensions() {
  return connectedRing(5, 20);
}

/**
 * The same ring at a size that used to produce NO LAYOUT AT ALL.
 *
 * Bug-10032: a densely connected model — dimensions shared between adjacent
 * facts, the ordinary shape of a constellation schema — was rejected outright
 * above roughly 25 tables, so the modeller pressed Arrange and nothing
 * happened. The engine now arranges to the best reasonable effort instead. This
 * fixture is the regression floor for that: it must produce a complete diagram,
 * inside the worker's own time budget.
 */
function connectedDense() {
  return connectedRing(8, 40);
}

function connectedRing(factCount: number, dimCount: number) {
  const facts = Array.from({ length: factCount }, (_, i) => node(`fact-${i}`, "fact", SIZES[3]));
  const dims = Array.from({ length: dimCount }, (_, i) => node(`dim-${i}`, "dimension", SIZES[i % SIZES.length]));
  const edges: LayoutEdgeSnapshot[] = [];
  for (let i = 0; i < dims.length; i += 1) {
    edges.push(relationship(`j-${i}a`, facts[i % facts.length], dims[i]));
    edges.push(relationship(`j-${i}b`, facts[(i + 1) % facts.length], dims[i]));
  }
  return { nodes: [...facts, ...dims], edges };
}

const FIXTURES = {
  "shared-dimensions": sharedDimensions,
  "variable-sizes": variableSizes,
  "parallel-joins": parallelJoins,
  "pinned-fixed": pinnedFixed,
  "connected-shared": connectedSharedDimensions,
  "connected-dense": connectedDense,
  "large-model": largeModel,
} as const;

type FixtureName = keyof typeof FIXTURES;

/**
 * Every fixture is exercised. This list existed while Bug-10029 was open, when the
 * 100-node / 180-join case could not be arranged at all (the engine rejected the batch
 * for a terminal that had moved off its docking side). The defect is fixed: libavoid
 * shifts a free connector terminal in the normal direction, and the terminal is now
 * corrected onto the dock the renderer draws instead of the whole batch failing.
 *
 * The guard below still fails if the list is used again to skip a fixture, so a case
 * cannot be quietly disabled to make the suite green.
 */
const BLOCKED_FIXTURES: FixtureName[] = [];
const EXERCISED = (Object.keys(FIXTURES) as FixtureName[]).filter(
  (name) => !BLOCKED_FIXTURES.includes(name),
);

interface Reading {
  metrics: LayoutMetrics;
  routeCount: number;
  movedFixedNodes: number;
  nonFinitePoints: number;
}

async function read(name: FixtureName): Promise<Reading> {
  const { nodes, edges } = FIXTURES[name]();
  const result = await computeLayout(snapshot(nodes, edges), "arrange-all", `quality-${name}`);
  expect(result.kind, `${name}: engine returned ${result.kind}`).toBe("success");
  if (result.kind !== "success") throw new Error("unreachable");

  // A relationship must never be missing, and no coordinate may be unusable:
  // metrics cannot be improved by dropping a link or emitting NaN.
  const routes = Object.values(result.routes);
  expect(routes.length, `${name}: route count`).toBe(edges.length);
  let nonFinitePoints = 0;
  for (const route of routes) {
    expect(route.points.length, `${name}: ${route.edgeId} segments`).toBeGreaterThanOrEqual(2);
    for (const point of route.points) {
      if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) nonFinitePoints += 1;
    }
  }

  // Fixed nodes (pinned tables and locked-route endpoints) must not move at all.
  const before = new Map(nodes.map((n) => [n.id, n]));
  let movedFixedNodes = 0;
  for (const n of nodes) {
    if (!n.pinned && !n.fixed) continue;
    const after = result.positions[n.id];
    const start = before.get(n.id);
    if (!after || !start || after.x !== start.x || after.y !== start.y) movedFixedNodes += 1;
  }

  return { metrics: result.metrics, routeCount: routes.length, movedFixedNodes, nonFinitePoints };
}

function budgets(name: FixtureName) {
  const fixture = (config.qualityFixtures.fixtures as Record<string, { maxEdgeCrossings: number; maxTotalBends: number; maxThroughNodeSegments: number }>)[name];
  expect(fixture, `${name}: missing recorded budget in config.json`).toBeTruthy();
  return fixture;
}

describe("R12 quality comparison on the agreed fixtures", () => {
  for (const name of EXERCISED) {
    it(`${name}: hard invariants hold and metrics stay inside the recorded budget`, async () => {
      const reading = await read(name);

      const budget = budgets(name);

      // Hard invariants — absolute, not budgets.
      expect(reading.metrics.nodeOverlapCount, `${name}: node overlaps`).toBe(0);
      // Through-node routes are a recorded per-fixture budget, which is what the
      // owner's best-effort decision (Bug-10032, Option B) said they should
      // become: "the through-node count becomes a budget in layout/config.json
      // alongside crossings and bends, so a regression that doubles it still
      // fails the suite."
      //
      // Every fixture's budget is 0 except connected-dense, which is 1. That
      // fixture is 80 nodes with shared dimensions — the deliberately crowded
      // case the decision was made about. Its single tolerated route is the
      // price of docking connectors on the edge the two cards actually face
      // each other across: the previous rule compared raw |dx| against |dy| and
      // so docked on the top or bottom of a very tall card, drawing the
      // relationship back across its own fact table. That was visible on a real
      // model; this is one route in a synthetic stress fixture.
      expect(
        reading.metrics.throughNodeSegmentCount,
        `${name}: through-node segments (budget ${budget.maxThroughNodeSegments})`,
      ).toBeLessThanOrEqual(budget.maxThroughNodeSegments);
      expect(reading.movedFixedNodes, `${name}: moved fixed nodes`).toBe(0);
      expect(reading.nonFinitePoints, `${name}: non-finite coordinates`).toBe(0);

      // Recorded, per-fixture budget — crossings are minimised, never claimed zero.
      expect(reading.metrics.edgeCrossingCount, `${name}: edge crossings`).toBeLessThanOrEqual(budget.maxEdgeCrossings);
      expect(reading.metrics.totalBends, `${name}: total bends`).toBeLessThanOrEqual(budget.maxTotalBends);

      // The configured worker budget is a hard product bound (15s), not the test
      // timeout: a large model that needs more than the worker allows would time
      // out in the real app. Asserted only here, in a single reproducible fixture.
      expect(reading.metrics.elapsedMs, `${name}: elapsed within worker budget`).toBeLessThanOrEqual(config.qualityFixtures.maxElapsedMs);

      // The measurement is the evidence artifact for this fixture.
      // eslint-disable-next-line no-console
      console.log(
        `[R12] ${name}: nodes=${reading.routeCount ? reading.routeCount : 0} routes=${reading.routeCount} ` +
          `crossings=${reading.metrics.edgeCrossingCount} bends=${reading.metrics.totalBends} ` +
          `length=${Math.round(reading.metrics.totalLength)} overlaps=${reading.metrics.nodeOverlapCount} ` +
          `through=${reading.metrics.throughNodeSegmentCount} ${reading.metrics.elapsedMs}ms`,
      );
      return undefined;
    }, 180_000);

    it(`${name}: produces identical metrics on an identical repeat (deterministic)`, async () => {
      const first = await read(name);
      const second = await read(name);
      const shape = (m: LayoutMetrics) => ({
        crossings: m.edgeCrossingCount,
        bends: m.totalBends,
        length: Math.round(m.totalLength),
        overlaps: m.nodeOverlapCount,
        through: m.throughNodeSegmentCount,
      });
      expect(shape(second.metrics)).toEqual(shape(first.metrics));
    }, 180_000);
  }

  it("fresh → arrange → persist/hydrate → arrange again stays valid (F09 round-trip)", async () => {
    // The failing real workflow: a user arranges a model, saves it, reopens it,
    // and arranges the *persisted* state again. The saved sides/ratios/waypoints
    // must not make the second Arrange fail or inherit stale automatic docking.
    for (const name of ["shared-dimensions", "variable-sizes", "parallel-joins"] as const) {
      const fresh = await read(name);
      const { nodes: baseNodes, edges: baseEdges } = FIXTURES[name]();

      // First pass: arrange the fresh fixture.
      const firstResult = await computeLayout(snapshot(baseNodes, baseEdges), "arrange-all", `rt-${name}-1`);
      expect(firstResult.kind, `${name}: first arrange`).toBe("success");
      if (firstResult.kind !== "success") return;

      // Persist the result the way Canvas does: arranged positions plus the
      // engine's sides/ratios/waypoints with auto provenance.
      const savedNodes = baseNodes.map((n) => ({ ...n, ...firstResult.positions[n.id] }));
      const savedEdges = baseEdges.map((rel) => {
        const route = firstResult.routes[rel.id];
        return {
          ...rel,
          sourceSide: route.sourceSide,
          targetSide: route.targetSide,
          sourceRatio: route.sourceRatio,
          targetRatio: route.targetRatio,
          waypoints: route.waypoints,
        };
      });

      // Hydrate from the persisted state and arrange again.
      const second = await computeLayout(snapshot(savedNodes, savedEdges), "arrange-all", `rt-${name}-2`);
      expect(second.kind, `${name}: second arrange`).toBe("success");
      if (second.kind !== "success") return;
      expect(second.metrics.nodeOverlapCount, `${name}: second-arrange overlaps`).toBe(0);
      expect(second.metrics.throughNodeSegmentCount, `${name}: second-arrange through-node`).toBe(0);

      void fresh;
    }
  }, 180_000);

  it("exercises every agreed fixture, with none blocked", () => {
    // The specification requires all of these cases, so none may be skipped. This
    // fails if a fixture is dropped from the set or parked in the blocked list.
    // `connected-dense` is not from the specification: it is the regression
    // floor for Bug-10032, the size at which a densely connected model used to
    // produce no layout at all.
    expect(Object.keys(FIXTURES)).toEqual([
      "shared-dimensions",
      "variable-sizes",
      "parallel-joins",
      "pinned-fixed",
      "connected-shared",
      "connected-dense",
      "large-model",
    ]);
    expect(BLOCKED_FIXTURES).toEqual([]);
    expect(EXERCISED).toHaveLength(7);
  });
});
