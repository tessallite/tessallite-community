import type {
  AnchorSide,
  LayoutEdgeSnapshot,
  LayoutMetrics,
  LayoutNodeSnapshot,
  LayoutOptions,
  LayoutResult,
  LayoutRoute,
  LayoutSnapshot,
  Point,
  Rect,
} from "./types";
import { oppositeSide, outwardVector, parallelOffsetFor, pointOnSide, ratioWithParallelOffset, sideFromCenters } from "./docking";
import { invalidInput, geometryInvalid, noRoute } from "./layoutErrors";

// One home for the docking primitives: the worker, this validator and the
// `CrowsFootEdge` renderer all resolve heels through `./docking`, so a route
// accepted here is a route the renderer can draw at the same anchor.
export { oppositeSide, outwardVector, pointOnSide, sideFromCenters };

const EPSILON = 0.5;

export function clonePoint(point: Point): Point {
  return { x: point.x, y: point.y };
}

export function clonePoints(points: Point[]): Point[] {
  return points.map(clonePoint);
}

export function isFinitePoint(point: Point): boolean {
  return Number.isFinite(point.x) && Number.isFinite(point.y);
}

export function rectForNode(node: LayoutNodeSnapshot, position?: Point): Rect {
  return {
    x: position?.x ?? node.x,
    y: position?.y ?? node.y,
    width: node.width,
    height: node.height,
  };
}

export function rectsOverlap(a: Rect, b: Rect, gap = 0): boolean {
  return (
    a.x < b.x + b.width + gap - EPSILON &&
    a.x + a.width + gap - EPSILON > b.x &&
    a.y < b.y + b.height + gap - EPSILON &&
    a.y + a.height + gap - EPSILON > b.y
  );
}

export function terminalPoint(
  node: LayoutNodeSnapshot,
  position: Point,
  side: AnchorSide,
  ratio: number,
  extent: number,
): Point {
  const border = pointOnSide(rectForNode(node, position), side, ratio);
  const vector = outwardVector(side);
  return { x: border.x + vector.x * extent, y: border.y + vector.y * extent };
}

function pointEquals(a: Point, b: Point): boolean {
  return Math.abs(a.x - b.x) <= EPSILON && Math.abs(a.y - b.y) <= EPSILON;
}

/**
 * Collapse duplicate/near-duplicate points. Fail closed on a non-finite
 * coordinate: silently dropping it would turn "the engine returned garbage"
 * into an apparently valid shorter route (spec: no best-effort success).
 */
export function compactPoints(points: Point[]): Point[] {
  const compacted: Point[] = [];
  for (const point of points) {
    if (!isFinitePoint(point)) throw geometryInvalid("route contains a non-finite coordinate");
    if (!compacted.length || !pointEquals(compacted[compacted.length - 1], point)) {
      compacted.push(clonePoint(point));
    }
  }
  return compacted;
}

export function isOrthogonalSegment(a: Point, b: Point): boolean {
  return Math.abs(a.x - b.x) <= EPSILON || Math.abs(a.y - b.y) <= EPSILON;
}

export function segmentLength(a: Point, b: Point): number {
  return Math.hypot(b.x - a.x, b.y - a.y);
}

/**
 * Does a segment meet a rectangle?
 *
 * ONE clipping implementation, not a pile of orientation cases. The segment is
 * clipped parametrically against the rectangle's four slabs (Liang-Barsky) and
 * the surviving interval decides the answer. `includeBoundary` then chooses
 * between two genuinely different questions:
 *
 *   `true`  — any contact at all, a single corner or a collinear run included.
 *   `false` — the segment crosses a POSITIVE LENGTH of the OPEN interior.
 *
 * The previous version answered these with separate axis-aligned branches plus
 * a per-edge crossing test, and got the interior question wrong at both ends.
 * A diagonal entering and leaving through opposite CORNERS — straight through
 * the middle of a card — touched each rectangle edge only at that edge's own
 * endpoint, which a proper-crossing test excludes by definition, so the route
 * validator accepted a connector drawn through a table. In the other direction,
 * a segment merely ending on a border was reported as interior, rejecting a
 * legitimate route. Clipping has no corner case because it never asks about
 * edges at all: it asks how much of the segment survives inside the slabs.
 */
export function segmentIntersectsRect(a: Point, b: Point, rect: Rect, includeBoundary = true): boolean {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  let enter = 0;
  let exit = 1;

  // One slab boundary. `p` is the rate of approach, `q` the distance to it.
  // `p === 0` means the segment is parallel to this pair of edges, so it is
  // either wholly inside the slab (q >= 0) or wholly outside.
  const clip = (p: number, q: number): boolean => {
    if (Math.abs(p) < 1e-12) return q >= 0;
    const t = q / p;
    if (p < 0) {
      if (t > exit) return false;
      if (t > enter) enter = t;
    } else {
      if (t < enter) return false;
      if (t < exit) exit = t;
    }
    return true;
  };

  if (!clip(-dx, a.x - rect.x)) return false;
  if (!clip(dx, rect.x + rect.width - a.x)) return false;
  if (!clip(-dy, a.y - rect.y)) return false;
  if (!clip(dy, rect.y + rect.height - a.y)) return false;
  if (enter > exit) return false;
  if (includeBoundary) return true;

  // Interior: the surviving piece must have real length, and its midpoint must
  // be strictly inside. The midpoint is the reliable witness — an endpoint of
  // the clipped piece always lies ON a boundary by construction.
  if ((exit - enter) * Math.hypot(dx, dy) <= EPSILON) return false;
  const mid = { x: a.x + dx * ((enter + exit) / 2), y: a.y + dy * ((enter + exit) / 2) };
  return (
    mid.x > rect.x + EPSILON &&
    mid.x < rect.x + rect.width - EPSILON &&
    mid.y > rect.y + EPSILON &&
    mid.y < rect.y + rect.height - EPSILON
  );
}

export function routeHasDiagonal(points: Point[]): boolean {
  for (let i = 1; i < points.length; i++) {
    if (!isOrthogonalSegment(points[i - 1], points[i])) return true;
  }
  return false;
}

export function routeLength(points: Point[]): number {
  let total = 0;
  for (let i = 1; i < points.length; i++) total += segmentLength(points[i - 1], points[i]);
  return total;
}

export function routeTouchesUnrelatedNode(
  points: Point[],
  edge: LayoutEdgeSnapshot,
  nodes: LayoutNodeSnapshot[],
  positions: Record<string, Point>,
): boolean {
  for (const node of nodes) {
    const rect = rectForNode(node, positions[node.id]);
    for (let i = 1; i < points.length; i++) {
      // The only legitimate contact with an endpoint card is its own docking
      // stub: the first segment leaving the source, or the last segment
      // arriving at the target. Every other segment must not enter either
      // endpoint's interior — a route that leaves then loops back through its
      // own source/target card is invalid (F07).
      if (node.id === edge.source && i === 1) continue;
      if (node.id === edge.target && i === points.length - 1) continue;
      if (segmentIntersectsRect(points[i - 1], points[i], rect, false)) return true;
    }
  }
  return false;
}

/**
 * Quality faults in a route that the best-effort policy TOLERATES.
 *
 * Deliberately a different question from `routeTouchesUnrelatedNode`, which
 * answers hard validity and therefore excuses a relationship's own endpoint
 * cards on its first and last segments — those are its docking stubs.
 *
 * Reporting reused that excuse, and so could not see the faults the relaxed
 * policy lets through. A route that leaves the source's LEFT heel and travels
 * right, straight through the whole source card, really does cross a table; the
 * count came back zero, so the canvas never raised its crowded-diagram warning
 * and a degraded arrangement looked like a clean one. Accepting an imperfect
 * diagram was the owner's decision; not saying so was not.
 *
 * A well-formed stub points AWAY from its card and never enters the interior,
 * so counting these segments costs a correct route nothing.
 */
export function routeQualityFaults(
  route: LayoutRoute,
  edge: LayoutEdgeSnapshot,
  nodes: LayoutNodeSnapshot[],
  positions: Record<string, Point>,
): number {
  const points = route.points;
  if (points.length < 2) return 0;
  let faults = 0;

  for (const node of nodes) {
    const rect = rectForNode(node, positions[node.id]);
    for (let i = 1; i < points.length; i++) {
      if (segmentIntersectsRect(points[i - 1], points[i], rect, false)) {
        faults++;
        break; // one fault per card, not one per segment
      }
    }
  }

  // The terminal-direction rule the relaxed validator skips: a first or last
  // segment running back towards its own card rather than away from it.
  const sourceNode = nodes.find((node) => node.id === edge.source);
  const targetNode = nodes.find((node) => node.id === edge.target);
  if (sourceNode && targetNode) {
    const sourceOut = outwardVector(route.sourceSide);
    const targetOut = outwardVector(route.targetSide);
    const heelSource = points[0];
    const heelTarget = points[points.length - 1];
    const first = points[1];
    const last = points[points.length - 2];
    if ((first.x - heelSource.x) * sourceOut.x + (first.y - heelSource.y) * sourceOut.y < -EPSILON) faults++;
    if ((last.x - heelTarget.x) * targetOut.x + (last.y - heelTarget.y) * targetOut.y < -EPSILON) faults++;
  }

  return faults;
}

export function validateSnapshot(snapshot: LayoutSnapshot): void {
  if (!snapshot.scope.projectId || !snapshot.scope.modelId) throw invalidInput("layout scope is required");
  if (!Number.isInteger(snapshot.revision) || snapshot.revision < 0) throw invalidInput("layout revision is invalid");
  const ids = new Set<string>();
  for (const node of snapshot.nodes) {
    if (!node.id || ids.has(node.id)) throw invalidInput(`duplicate table id: ${node.id}`);
    ids.add(node.id);
    if (!Number.isFinite(node.x) || !Number.isFinite(node.y) || node.width <= 0 || node.height <= 0) {
      throw invalidInput(`invalid table geometry: ${node.id}`);
    }
  }
  const edgeIds = new Set<string>();
  for (const edge of snapshot.edges) {
    if (!edge.id || edgeIds.has(edge.id)) throw invalidInput(`duplicate relationship id: ${edge.id}`);
    edgeIds.add(edge.id);
    if (!ids.has(edge.source) || !ids.has(edge.target)) throw invalidInput(`relationship endpoint is not visible: ${edge.id}`);
    if (edge.waypoints.some((point) => !isFinitePoint(point))) throw invalidInput(`invalid relationship geometry: ${edge.id}`);
  }
}

function countNodeOverlaps(nodes: LayoutNodeSnapshot[], positions: Record<string, Point>, gap: number): number {
  let count = 0;
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      if (rectsOverlap(rectForNode(nodes[i], positions[nodes[i].id]), rectForNode(nodes[j], positions[nodes[j].id]), gap)) count++;
    }
  }
  return count;
}

function segments(points: Point[]): Array<[Point, Point]> {
  const result: Array<[Point, Point]> = [];
  for (let i = 1; i < points.length; i++) result.push([points[i - 1], points[i]]);
  return result;
}

function properSegmentCrossing(a: Point, b: Point, c: Point, d: Point): boolean {
  const aHorizontal = Math.abs(a.y - b.y) <= EPSILON;
  const cHorizontal = Math.abs(c.y - d.y) <= EPSILON;
  if (aHorizontal === cHorizontal) return false;
  const h = aHorizontal ? [a, b] : [c, d];
  const v = aHorizontal ? [c, d] : [a, b];
  return (
    v[0].x > Math.min(h[0].x, h[1].x) + EPSILON &&
    v[0].x < Math.max(h[0].x, h[1].x) - EPSILON &&
    h[0].y > Math.min(v[0].y, v[1].y) + EPSILON &&
    h[0].y < Math.max(v[0].y, v[1].y) - EPSILON
  );
}

function countEdgeCrossings(routes: LayoutRoute[]): number {
  let count = 0;
  for (let i = 0; i < routes.length; i++) {
    for (let j = i + 1; j < routes.length; j++) {
      const a = routes[i];
      const b = routes[j];
      if (a.edgeId === b.edgeId) continue;
      const aSegments = segments(a.points);
      const bSegments = segments(b.points);
      if (aSegments.some(([a1, a2]) => bSegments.some(([b1, b2]) => properSegmentCrossing(a1, a2, b1, b2)))) count++;
    }
  }
  return count;
}

/**
 * Heel the renderer will draw for this route. `route.sourceRatio` is the
 * *persisted base* ratio, so the parallel offset is re-applied here exactly as
 * `CrowsFootEdge` re-applies it at render time.
 */
function expectedTerminal(
  node: LayoutNodeSnapshot,
  position: Point,
  side: AnchorSide,
  baseRatio: number,
  edge: LayoutEdgeSnapshot,
  endpoint: "source" | "target",
): Point {
  const rect = rectForNode(node, position);
  const offset = parallelOffsetFor(edge.offsetIndex ?? 0, edge.totalEdges ?? 1);
  const ratio = ratioWithParallelOffset(rect, side, baseRatio, offset);
  const extent = (endpoint === "source" ? edge.sourceMarkerExtent : edge.targetMarkerExtent) ?? 0;
  return terminalPoint(node, position, side, ratio, extent);
}

export function validateRoutes(
  nodes: LayoutNodeSnapshot[],
  edges: LayoutEdgeSnapshot[],
  positions: Record<string, Point>,
  routes: Record<string, LayoutRoute>,
  nodeGap = 0,
  /**
   * `false` for operations that do not own table coordinates (reroute links):
   * a pre-existing overlap in a hand-arranged model must not make rerouting
   * fail, because rerouting is forbidden from moving the cards anyway.
   */
  requireNoOverlap = true,
  /**
   * Whether a route crossing an unrelated card fails the whole batch.
   *
   * `false` is the product decision for dense diagrams: arrange to the best
   * reasonable effort rather than refusing to draw anything.
   *
   * The line this draws is between a route that is UGLY and a route that is
   * UNDRAWABLE, and only the second is worth refusing a whole diagram for:
   *
   *   Tolerated when `false` — a route crossing an unrelated card, and a route
   *   whose terminal segment runs back into its own end card. Both render as a
   *   line over a table. A modeller can see them and move the card.
   *
   *   Always refused — a missing position, a non-finite coordinate, a missing
   *   route, a diagonal in an orthogonal route, redundant bends, an
   *   inconsistent lock, and terminals that do not match the docking they claim.
   *   These are incoherent rather than untidy: the route would not render, or
   *   would render somewhere other than where the model says it attaches.
   *
   * Tolerated faults are counted in `metrics.throughNodeSegmentCount`, which
   * the canvas surfaces, so a degraded diagram is visible rather than silent.
   */
  rejectRouteQualityFaults = true,
): void {
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  for (const node of nodes) {
    const position = positions[node.id];
    if (!position || !isFinitePoint(position)) throw geometryInvalid(`missing position for table: ${node.id}`);
  }
  if (requireNoOverlap && countNodeOverlaps(nodes, positions, nodeGap) > 0) {
    throw geometryInvalid("layout contains overlapping table cards");
  }
  for (const edge of edges) {
    const route = routes[edge.id];
    if (!route || route.points.length < 2) throw noRoute(`no route for relationship: ${edge.id}`);
    if (routeHasDiagonal(route.points) && edge.pathMode === "orthogonal") throw geometryInvalid(`relationship is not orthogonal: ${edge.id}`);
    if (route.points.some((point) => !isFinitePoint(point))) throw geometryInvalid(`relationship has invalid points: ${edge.id}`);
    if (route.points.some((point, index) => index > 0 && pointEquals(point, route.points[index - 1]))) throw geometryInvalid(`relationship has redundant bends: ${edge.id}`);
    if (rejectRouteQualityFaults && routeTouchesUnrelatedNode(route.points, edge, nodes, positions)) {
      throw geometryInvalid(`relationship crosses a table: ${edge.id}`);
    }
    const sourceNode = nodeById.get(edge.source);
    const targetNode = nodeById.get(edge.target);
    if (!sourceNode || !targetNode) throw geometryInvalid(`relationship endpoint disappeared: ${edge.id}`);
    if (route.locked !== edge.locked) throw geometryInvalid(`relationship lock state is inconsistent: ${edge.id}`);
    const sourceHeel = expectedTerminal(sourceNode, positions[edge.source], route.sourceSide, route.sourceRatio, edge, "source");
    const targetHeel = expectedTerminal(targetNode, positions[edge.target], route.targetSide, route.targetRatio, edge, "target");
    if (!pointEquals(route.points[0], sourceHeel) || !pointEquals(route.points[route.points.length - 1], targetHeel)) {
      throw geometryInvalid(`relationship terminals do not match docking: ${edge.id}`);
    }
    const sourceOut = outwardVector(route.sourceSide);
    const targetOut = outwardVector(route.targetSide);
    // The segment leaving the source and the segment arriving at the target
    // must both point away from the card: the neighbour point has to sit at or
    // beyond the heel along the outward normal. (The target comparison is
    // outward-positive for the same reason as the source one.)
    const first = route.points[1];
    const last = route.points[route.points.length - 2];
    if (rejectRouteQualityFaults) {
      if ((first.x - sourceHeel.x) * sourceOut.x + (first.y - sourceHeel.y) * sourceOut.y < -EPSILON) {
        throw geometryInvalid(`relationship enters source card: ${edge.id}`);
      }
      if ((last.x - targetHeel.x) * targetOut.x + (last.y - targetHeel.y) * targetOut.y < -EPSILON) {
        throw geometryInvalid(`relationship enters target card: ${edge.id}`);
      }
    }
  }
}

export function metricsForLayout(
  nodes: LayoutNodeSnapshot[],
  edges: LayoutEdgeSnapshot[],
  positions: Record<string, Point>,
  routes: Record<string, LayoutRoute>,
  elapsedMs: number,
  nodeGap = 0,
): LayoutMetrics {
  let throughNodeSegmentCount = 0;
  let totalBends = 0;
  let totalLength = 0;
  const routeList = edges.map((edge) => routes[edge.id]).filter((route): route is LayoutRoute => !!route);
  for (const edge of edges) {
    const route = routes[edge.id];
    if (!route) continue;
    totalBends += Math.max(0, route.points.length - 2);
    totalLength += routeLength(route.points);
    // Counted with the reporting predicate, not the validator's. The validator
    // excuses a relationship's own endpoint cards on its docking segments;
    // reusing that excuse here made a tolerated fault invisible.
    if (routeQualityFaults(route, edge, nodes, positions) > 0) throughNodeSegmentCount++;
  }
  return {
    nodeOverlapCount: countNodeOverlaps(nodes, positions, nodeGap),
    throughNodeSegmentCount,
    edgeCrossingCount: countEdgeCrossings(routeList),
    totalBends,
    totalLength,
    elapsedMs,
  };
}

export function validateLayoutResult(
  snapshot: LayoutSnapshot,
  positions: Record<string, Point>,
  routes: Record<string, LayoutRoute>,
  nodeGap = 0,
  requireNoOverlap = true,
  rejectRouteQualityFaults = true,
): void {
  validateSnapshot(snapshot);
  validateRoutes(
    snapshot.nodes, snapshot.edges, positions, routes, nodeGap, requireNoOverlap, rejectRouteQualityFaults,
  );
}

function bounds(nodes: LayoutNodeSnapshot[], positions: Record<string, Point>): Rect {
  const left = Math.min(...nodes.map((node) => positions[node.id].x));
  const top = Math.min(...nodes.map((node) => positions[node.id].y));
  const right = Math.max(...nodes.map((node) => positions[node.id].x + node.width));
  const bottom = Math.max(...nodes.map((node) => positions[node.id].y + node.height));
  return { x: left, y: top, width: right - left, height: bottom - top };
}

/**
 * Translate an engine-arranged block to the closest free deterministic location.
 *
 * `padding` is the clearance the block must keep from the *fixed* cards around
 * it. The block's internal spacing is the engine's own output and is not
 * re-judged here: a selection with no internal relationships is laid out at the
 * engine's default inline spacing (20px), so requiring `padding` *inside* the
 * block rejected every candidate and made multi-card selections unplaceable.
 * Intra-block gaps below the configured node gap are resolved afterwards by
 * `separateRectangles`, which may only move the movable cards.
 *
 * Candidate order is displacement, then x/y, which makes selected layout
 * repeatable across browsers.
 */
export function placeBlockAroundOriginal(
  blockNodes: LayoutNodeSnapshot[],
  proposed: Record<string, Point>,
  allNodes: LayoutNodeSnapshot[],
  current: Record<string, Point>,
  padding = 32,
  maxDistance = 2400,
): Record<string, Point> {
  if (!blockNodes.length) return {};
  const sourceBounds = bounds(blockNodes, current);
  const proposedBounds = bounds(blockNodes, proposed);
  const dx0 = sourceBounds.x + sourceBounds.width / 2 - (proposedBounds.x + proposedBounds.width / 2);
  const dy0 = sourceBounds.y + sourceBounds.height / 2 - (proposedBounds.y + proposedBounds.height / 2);
  const obstacles = allNodes.filter((node) => !blockNodes.some((block) => block.id === node.id));
  const candidates: Point[] = [{ x: dx0, y: dy0 }];
  const obstacleRects = obstacles.map((node) => rectForNode(node, current[node.id]));
  const xs = new Set<number>([sourceBounds.x - proposedBounds.x, sourceBounds.x + sourceBounds.width - proposedBounds.x, sourceBounds.x - proposedBounds.width - proposedBounds.x]);
  const ys = new Set<number>([sourceBounds.y - proposedBounds.y, sourceBounds.y + sourceBounds.height - proposedBounds.y, sourceBounds.y - proposedBounds.height - proposedBounds.y]);
  for (const rect of obstacleRects) {
    xs.add(rect.x - proposedBounds.width - padding - proposedBounds.x);
    xs.add(rect.x + rect.width + padding - proposedBounds.x);
    ys.add(rect.y - proposedBounds.height - padding - proposedBounds.y);
    ys.add(rect.y + rect.height + padding - proposedBounds.y);
  }
  for (const x of xs) for (const y of ys) candidates.push({ x, y });
  candidates.sort((a, b) => Math.hypot(a.x - dx0, a.y - dy0) - Math.hypot(b.x - dx0, b.y - dy0) || a.x - b.x || a.y - b.y);
  for (const delta of candidates) {
    if (Math.hypot(delta.x - dx0, delta.y - dy0) > maxDistance) continue;
    const translated: Record<string, Point> = {};
    for (const node of blockNodes) translated[node.id] = { x: proposed[node.id].x + delta.x, y: proposed[node.id].y + delta.y };
    if (blockNodes.some((node) => obstacleRects.some((obstacle) => rectsOverlap(rectForNode(node, translated[node.id]), obstacle, padding)))) continue;
    // A block the engine returned is internally disjoint; only a real overlap is
    // a rejection, because a tighter-than-`padding` internal gap is legitimate.
    if (countNodeOverlaps(blockNodes, translated, 0) > 0) continue;
    return translated;
  }
  throw geometryInvalid("no free position is available for the selected tables");
}

/** Does this card overlap any other, at the requested gap? */
function overlapsAny(
  node: LayoutNodeSnapshot,
  nodes: LayoutNodeSnapshot[],
  positions: Record<string, Point>,
  gap: number,
): boolean {
  const rect = rectForNode(node, positions[node.id]);
  return nodes.some(
    (other) => other.id !== node.id && rectsOverlap(rect, rectForNode(other, positions[other.id]), gap),
  );
}

/** Overlapping pairs that something is actually allowed to move apart. */
function countMovableOverlaps(
  nodes: LayoutNodeSnapshot[],
  positions: Record<string, Point>,
  fixedIds: Set<string>,
  gap: number,
): number {
  let count = 0;
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      if (fixedIds.has(nodes[i].id) && fixedIds.has(nodes[j].id)) continue;
      const a = rectForNode(nodes[i], positions[nodes[i].id]);
      const b = rectForNode(nodes[j], positions[nodes[j].id]);
      if (rectsOverlap(a, b, gap)) count += 1;
    }
  }
  return count;
}

/**
 * Park a card that cannot be separated in place, just outside the cluster.
 *
 * Deterministic and always terminating: the search walks right along the
 * cluster's top edge and then wraps onto successive rows below it, and there is
 * always free space eventually because the cluster is finite. The result is not
 * pretty — a parked card sits apart from the arrangement — but it is readable,
 * which "no diagram at all" is not.
 */
function freeSlotOutside(
  node: LayoutNodeSnapshot,
  nodes: LayoutNodeSnapshot[],
  positions: Record<string, Point>,
  gap: number,
): Point {
  const others = nodes.filter((other) => other.id !== node.id);
  const area = bounds(others, positions);
  const step = node.width + gap;
  const rowHeight = node.height + gap;
  for (let row = 0; row < 64; row++) {
    const y = area.y + row * rowHeight;
    for (let column = 0; column < 64; column++) {
      const candidate = { x: area.x + area.width + gap + column * step, y };
      const rect = rectForNode(node, candidate);
      if (!others.some((other) => rectsOverlap(rect, rectForNode(other, positions[other.id]), gap))) {
        return candidate;
      }
    }
  }
  // Unreachable for any finite cluster, but never return an overlapping point.
  return { x: area.x + area.width + gap, y: area.y + area.height + gap };
}

export function separateRectangles(
  nodes: LayoutNodeSnapshot[],
  positions: Record<string, Point>,
  fixedIds: Set<string>,
  gap: number,
): Record<string, Point> {
  const next = Object.fromEntries(Object.entries(positions).map(([id, point]) => [id, clonePoint(point)]));
  const movable = nodes.filter((node) => !fixedIds.has(node.id));
  for (let iteration = 0; iteration < 120; iteration++) {
    let changed = false;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i];
        const b = nodes[j];
        if (fixedIds.has(a.id) && fixedIds.has(b.id)) continue;
        const ar = rectForNode(a, next[a.id]);
        const br = rectForNode(b, next[b.id]);
        if (!rectsOverlap(ar, br, gap)) continue;
        const move = fixedIds.has(a.id) ? b : fixedIds.has(b.id) ? a : movable.find((node) => node.id === b.id) ? b : a;
        const anchor = move.id === a.id ? b : a;
        const mr = rectForNode(move, next[move.id]);
        const rr = rectForNode(anchor, next[anchor.id]);
        const overlapX = Math.min(mr.x + mr.width, rr.x + rr.width) - Math.max(mr.x, rr.x) + gap;
        const overlapY = Math.min(mr.y + mr.height, rr.y + rr.height) - Math.max(mr.y, rr.y) + gap;
        if (overlapX <= overlapY) next[move.id].x += mr.x < rr.x ? -overlapX : overlapX;
        else next[move.id].y += mr.y < rr.y ? -overlapY : overlapY;
        changed = true;
      }
    }
    if (!changed) return next;
  }

  // The relaxation above did not settle. That happens when a movable card is
  // boxed in — classically between two locked cards — where every push off one
  // neighbour drives it onto another and it oscillates until the budget runs
  // out. Locking a single table used to make the whole arrangement fail this
  // way, which is the worst possible answer: the user gets no diagram at all
  // because they protected one card.
  //
  // So the cards that could not settle are moved OUT of the cluster instead of
  // being shuffled inside it. Only movable cards are relocated; a locked card
  // never moves, which is the invariant this function exists to keep.
  for (const node of movable) {
    if (!overlapsAny(node, nodes, next, gap)) continue;
    next[node.id] = freeSlotOutside(node, nodes, next, gap);
  }

  // Fixed cards that overlap EACH OTHER are not this function's to fix: the
  // user placed them and asked for them to stay. Counting them here would fail
  // an arrangement for geometry nothing is allowed to change.
  const unresolved = countMovableOverlaps(nodes, next, fixedIds, gap);
  if (unresolved > 0) {
    throw geometryInvalid(`table cards cannot be separated without moving a fixed card (${unresolved} left)`);
  }
  return next;
}

export function defaultOptions(options?: Partial<LayoutOptions>): LayoutOptions {
  const preset = options?.preset === "compact" || options?.preset === "radial" || options?.preset === "hierarchical" ? options.preset : "hierarchical";
  const direction = options?.direction === "RIGHT" ? "RIGHT" : "DOWN";
  const spacing = options?.spacing === "dense" ? "dense" : "normal";
  return { preset, direction, spacing };
}

/**
 * Fact-centred radial placement for the Radial preset (F04).
 *
 * Facts (or, with no fact, a single representative card) sit at the centre;
 * every other movable card is placed on a ring whose radius is derived from the
 * *measured* card dimensions, so variable-height cards cannot overlap. The
 * returned coordinates are top-left positions (the same convention as ELK
 * output); the caller then runs the normal rectangle separation, geometry
 * validation and coordinated routing — radial is one placement stage in the
 * shared pipeline, not a second system.
 *
 * Deterministic: ring order is degree descending (most-connected first) then id.
 */
export function layoutRadial(
  nodes: LayoutNodeSnapshot[],
  edges: LayoutEdgeSnapshot[],
  movableIds: Set<string>,
  nodeGap: number,
): Record<string, Point> {
  const movable = nodes.filter((node) => movableIds.has(node.id));
  if (!movable.length) return {};

  const degree = new Map<string, number>();
  for (const edge of edges) {
    if (movableIds.has(edge.source)) degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    if (movableIds.has(edge.target)) degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
  }
  const byDegreeThenId = (a: LayoutNodeSnapshot, b: LayoutNodeSnapshot): number =>
    (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0) || a.id.localeCompare(b.id);

  // The fact-centred arrangement: facts form the centre, everything else the
  // ring. With no fact card, the first card is the centre so the result is
  // still a sensible star rather than an empty canvas.
  const facts = movable.filter((node) => node.tableType === "fact").sort(byDegreeThenId);
  const centreNodes = facts.length ? facts : movable.slice().sort(byDegreeThenId).slice(0, 1);
  const ringNodes = movable.filter((node) => !centreNodes.includes(node)).sort(byDegreeThenId);

  const positions: Record<string, Point> = {};

  // Single centre card sits exactly at the origin; multiple facts share the
  // centre on a compact ring sized from their own measured extents.
  if (centreNodes.length === 1) {
    const node = centreNodes[0];
    positions[node.id] = { x: -node.width / 2, y: -node.height / 2 };
  } else {
    const inner = layoutRadialRing(centreNodes, nodeGap);
    Object.assign(positions, inner);
  }

  if (ringNodes.length) {
    // The centre cluster may be several spread facts, not one card: measure its
    // true farthest corner from the origin, then keep the ring clear of it.
    const centreRadius = Math.max(...centreNodes.map((node) => {
      const p = positions[node.id];
      return Math.max(
        Math.hypot(p.x, p.y),
        Math.hypot(p.x + node.width, p.y),
        Math.hypot(p.x, p.y + node.height),
        Math.hypot(p.x + node.width, p.y + node.height),
      );
    }));
    const ringHalfDiag = Math.max(...ringNodes.map((node) => Math.hypot(node.width, node.height) / 2));
    const ring = layoutRadialRing(ringNodes, nodeGap, centreRadius + ringHalfDiag + nodeGap);
    Object.assign(positions, ring);
  }

  return positions;
}

/**
 * Place a list of measured cards on one ring around the origin.
 *
 * Equal-angular placement: card `i` sits at angle `θ₀ + 2πi/n`, so the centre
 * of every card is a fixed angular step from its neighbours and the ring
 * surrounds the centre instead of clustering to one side. The chord between
 * two centres at angular separation `Δ` is `2r·sin(Δ/2)`, so the radius is
 * chosen as the maximum over *all* card pairs of
 * `(halfDiag_i + halfDiag_j + gap) / (2·sin(Δ(i,j)/2))` — the exact
 * conservative bound for axis-aligned rectangles, using each card's
 * circumscribed radius (half-diagonal) plus the full `nodeGap`. `minRadius`
 * keeps the ring clear of the centre cluster.
 *
 * The previous unequal-span loop placed card `i` at `angle + span_i/2` and
 * then advanced `angle` by `span_i`, so adjacent centres were separated by
 * half the sum of two spans rather than by the span their required chord was
 * computed from — mixed-size cards could overlap, and a dominating
 * `minRadius` left the unused circumference on one side (R2-01).
 */
function layoutRadialRing(
  nodes: LayoutNodeSnapshot[],
  nodeGap: number,
  minRadius = 0,
): Record<string, Point> {
  const positions: Record<string, Point> = {};
  if (nodes.length === 0) return positions;

  const halfDiags = nodes.map((node) => Math.hypot(node.width, node.height) / 2);

  // A single ring card sits on the positive x-axis at the clearance radius.
  if (nodes.length === 1) {
    const node = nodes[0];
    positions[node.id] = { x: minRadius - node.width / 2, y: -node.height / 2 };
    return positions;
  }

  const n = nodes.length;
  const angularStep = (Math.PI * 2) / n;
  let radius = minRadius;
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      const separation = angularStep * Math.min(j - i, n - (j - i));
      const required = (halfDiags[i] + halfDiags[j] + nodeGap) / (2 * Math.sin(separation / 2));
      radius = Math.max(radius, required);
    }
  }

  const theta0 = -Math.PI / 2; // first card at 12 o'clock
  for (let index = 0; index < n; index++) {
    const node = nodes[index];
    const theta = theta0 + angularStep * index;
    positions[node.id] = {
      x: Math.cos(theta) * radius - node.width / 2,
      y: Math.sin(theta) * radius - node.height / 2,
    };
  }
  return positions;
}
