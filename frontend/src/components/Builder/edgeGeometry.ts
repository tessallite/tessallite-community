/**
 * Card geometry and the route a relationship is actually drawn with.
 *
 * Relocated out of `CrowsFootEdge.tsx` (spec section 7: this layer owns the
 * shared route results and the edge carries "no per-edge full-node scan/
 * router"). Three callers depend on it: the edge renderer draws from it, the
 * route lock freezes what it returns, and the locked-route guard tests moved
 * cards against it.
 *
 * The docking maths is NOT here. `layout/docking.ts` owns it for the worker,
 * and this module used to carry a second copy of `alignedSideRatios`,
 * `pointOnSide`, `ratioWithParallelOffset`, the automatic side rule, the anchor
 * clamp and the parallel spacing — one contract with two implementations kept
 * in step by a comment. They are imported from that module now, which is the
 * single authority for where a relationship docks, and `Rect` from
 * `layout/types` is the one rectangle shape the whole canvas uses.
 */
import { Position } from "reactflow";
import {
  alignedSideRatios,
  clampAnchorRatio,
  oppositeSide,
  parallelOffsetFor,
  pointOnSide,
  ratioWithParallelOffset,
  sideFromCenters,
} from "./layout/docking";
import type { AnchorSide, Rect } from "./layout/types";
import { type Pt, autoOrthogonalRoute, awayVec, sameX, sameY } from "./edgeRouting";

export type { AnchorSide, Rect };

/** Anchor sides map onto React Flow's own positions for the marker geometry. */
const ROUTE_MARGIN = 28;
const ROUTE_STUB = 34;

export const SIDE_TO_POSITION: Record<AnchorSide, Position> = {
  left: Position.Left,
  right: Position.Right,
  top: Position.Top,
  bottom: Position.Bottom,
};

export function asAnchorSide(value?: string): AnchorSide | null {
  return value === "left" || value === "right" || value === "top" || value === "bottom"
    ? value
    : null;
}

function inflateRect(rect: Rect, margin: number): Rect {
  return {
    x: rect.x - margin,
    y: rect.y - margin,
    width: rect.width + margin * 2,
    height: rect.height + margin * 2,
  };
}

function segmentIntersectsRect(a: Pt, b: Pt, rect: Rect): boolean {
  const minX = Math.min(a.x, b.x);
  const maxX = Math.max(a.x, b.x);
  const minY = Math.min(a.y, b.y);
  const maxY = Math.max(a.y, b.y);
  const rx2 = rect.x + rect.width;
  const ry2 = rect.y + rect.height;

  if (Math.abs(a.y - b.y) < 0.01) {
    return a.y >= rect.y && a.y <= ry2 && maxX >= rect.x && minX <= rx2;
  }
  if (Math.abs(a.x - b.x) < 0.01) {
    return a.x >= rect.x && a.x <= rx2 && maxY >= rect.y && minY <= ry2;
  }
  return maxX >= rect.x && minX <= rx2 && maxY >= rect.y && minY <= ry2;
}

function compactWaypoints(points: Pt[]): Pt[] {
  const out: Pt[] = [];
  for (const p of points) {
    const prev = out[out.length - 1];
    if (!prev || Math.abs(prev.x - p.x) > 0.5 || Math.abs(prev.y - p.y) > 0.5) {
      out.push(p);
    }
  }
  return out;
}

function routeScore(start: Pt, waypoints: Pt[], end: Pt, obstacles: Rect[]): number {
  const pts = [start, ...waypoints, end];
  let length = 0;
  let intersections = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i];
    const b = pts[i + 1];
    length += Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
    for (const obs of obstacles) {
      if (segmentIntersectsRect(a, b, inflateRect(obs, 8))) intersections += 1;
    }
  }
  return intersections * 100000 + length + waypoints.length * 20;
}

export function autoOrthogonalRouteAvoiding(
  sx: number, sy: number, sp: Position,
  tx: number, ty: number, tp: Position,
  obstacles: Rect[],
): Pt[] {
  const base = autoOrthogonalRoute(sx, sy, sp, tx, ty, tp);
  if (obstacles.length === 0) return base;

  const start = { x: sx, y: sy };
  const end = { x: tx, y: ty };
  const [sdx, sdy] = awayVec(sp);
  const [tdx, tdy] = awayVec(tp);
  const sEsc = { x: sx + sdx * ROUTE_STUB, y: sy + sdy * ROUTE_STUB };
  const tEsc = { x: tx + tdx * ROUTE_STUB, y: ty + tdy * ROUTE_STUB };
  const candidates: Pt[][] = [base];

  for (const obs of obstacles) {
    const expanded = inflateRect(obs, ROUTE_MARGIN);
    const viaXs = [expanded.x, expanded.x + expanded.width];
    const viaYs = [expanded.y, expanded.y + expanded.height];

    for (const x of viaXs) {
      candidates.push(compactWaypoints([
        sEsc,
        { x, y: sEsc.y },
        { x, y: tEsc.y },
        tEsc,
      ]));
    }
    for (const y of viaYs) {
      candidates.push(compactWaypoints([
        sEsc,
        { x: sEsc.x, y },
        { x: tEsc.x, y },
        tEsc,
      ]));
    }
  }

  return candidates.reduce((best, candidate) => (
    routeScore(start, candidate, end, obstacles) < routeScore(start, best, end, obstacles)
      ? candidate
      : best
  ), base);
}


// ---------------------------------------------------------------------------
// The route as actually drawn
// ---------------------------------------------------------------------------
//
// Two steps, because the edge component resolves them at two different points
// in its render (docking before it knows its marker extents, bends after), and
// `resolveDisplayedRoute` composes the same two for callers that just want the
// finished geometry — notably "lock this relationship", which must freeze the
// path the user can see. One implementation per decision, called twice.

export interface DisplayedDocking {
  sourceSide: AnchorSide;
  targetSide: AnchorSide;
  /**
   * BASE ratios: the parallel offset is deliberately NOT baked in, matching the
   * persisted convention (`LayoutRoute.sourceRatio`) — the renderer re-applies
   * the offset on read, so baking it in here would fan the route out twice on
   * the next reload.
   */
  sourceRatio: number;
  targetRatio: number;
}

/**
 * Where a relationship docks: an explicit stored side wins over the automatic
 * side, and an explicit stored ratio wins over the aligned ratio.
 */
export function resolveDisplayedDocking(input: {
  source: Rect;
  target: Rect;
  sourceSide?: string;
  targetSide?: string;
  sourceRatio?: number;
  targetRatio?: number;
}): DisplayedDocking {
  // One rule for the automatic side, shared with the worker: the source
  // faces the target, and the target faces back.
  const facing = sideFromCenters(input.source, input.target);
  const sourceSide = asAnchorSide(input.sourceSide) ?? facing;
  const targetSide = asAnchorSide(input.targetSide) ?? oppositeSide(facing);
  // Both endpoints use the shared aligned docking rule, so the pre-arrange
  // fallback docks exactly where the worker would assign.
  const aligned = alignedSideRatios(input.source, sourceSide, input.target, targetSide);
  return {
    sourceSide,
    targetSide,
    sourceRatio: input.sourceRatio !== undefined ? clampAnchorRatio(input.sourceRatio) : aligned.source,
    targetRatio: input.targetRatio !== undefined ? clampAnchorRatio(input.targetRatio) : aligned.target,
  };
}

/**
 * The bends actually drawn between the two marker heels.
 *
 * Stored bends win. With none, an orthogonal route falls back to the
 * obstacle-aware auto route; a straight route is the direct heel-to-heel line,
 * because engine-generated orthogonal bends are not valid straight geometry and
 * only manual free-angle bends survive a switch to Straight (F08).
 */
export function resolveDisplayedWaypoints(input: {
  heelSource: Pt;
  heelTarget: Pt;
  sourcePosition: Position;
  targetPosition: Position;
  pathMode: "orthogonal" | "straight";
  routeMode?: "auto" | "manual";
  stored: Pt[];
  obstacles: Rect[];
}): Pt[] {
  const routeMode = input.routeMode ?? (input.stored.length ? "manual" : "auto");
  if (input.pathMode === "orthogonal") {
    return input.stored.length === 0
      ? autoOrthogonalRouteAvoiding(
          input.heelSource.x, input.heelSource.y, input.sourcePosition,
          input.heelTarget.x, input.heelTarget.y, input.targetPosition,
          input.obstacles,
        )
      : input.stored;
  }
  return routeMode === "manual" ? input.stored : [];
}

export interface DisplayedRouteInput {
  /** Live card geometry, in flow coordinates. */
  source: Rect;
  target: Rect;
  /** Stored docking overrides, if the relationship carries any. */
  sourceSide?: string;
  targetSide?: string;
  sourceRatio?: number;
  targetRatio?: number;
  /** Stored bends between the marker heels. */
  waypoints?: Pt[];
  /** Provenance; absent is resolved the same way the renderer resolves it. */
  routeMode?: "auto" | "manual";
  pathMode: "orthogonal" | "straight";
  /** Drawn marker extents — the heel is border + outward * extent. */
  sourceMarkerExtent: number;
  targetMarkerExtent: number;
  /** Cards that are not this relationship's endpoints. */
  obstacles: Rect[];
  /** Parallel-relationship fan-out, as the renderer applies it. */
  offsetIndex?: number;
  totalEdges?: number;
  /**
   * The fan-out a LOCKED route was frozen with, overriding the recomputed one.
   *
   * Without this a frozen attachment moved whenever a relationship was added or
   * removed between the same two cards, because the offset is derived from the
   * current relationship set while the frozen bends are absolute.
   */
  frozenParallelOffset?: number;
}

export interface DisplayedRoute extends DisplayedDocking {
  /** Bend points between the two marker heels, exactly as drawn. */
  waypoints: Pt[];
  /** The polyline's own endpoints — border + outward * marker extent. */
  heelSource: Pt;
  heelTarget: Pt;
}

/** The complete geometry a relationship is currently drawn with. */
export function resolveDisplayedRoute(input: DisplayedRouteInput): DisplayedRoute {
  const docking = resolveDisplayedDocking(input);
  const offset =
    input.frozenParallelOffset ?? parallelOffsetFor(input.offsetIndex ?? 0, input.totalEdges ?? 1);
  const sourceAnchor = pointOnSide(
    input.source,
    docking.sourceSide,
    ratioWithParallelOffset(input.source, docking.sourceSide, docking.sourceRatio, offset),
  );
  const targetAnchor = pointOnSide(
    input.target,
    docking.targetSide,
    ratioWithParallelOffset(input.target, docking.targetSide, docking.targetRatio, offset),
  );

  const sourcePosition = SIDE_TO_POSITION[docking.sourceSide];
  const targetPosition = SIDE_TO_POSITION[docking.targetSide];
  const [sdx, sdy] = awayVec(sourcePosition);
  const [tdx, tdy] = awayVec(targetPosition);

  const heelSource: Pt = {
    x: sourceAnchor.x + sdx * input.sourceMarkerExtent,
    y: sourceAnchor.y + sdy * input.sourceMarkerExtent,
  };
  const heelTarget: Pt = {
    x: targetAnchor.x + tdx * input.targetMarkerExtent,
    y: targetAnchor.y + tdy * input.targetMarkerExtent,
  };

  const waypoints = resolveDisplayedWaypoints({
    heelSource,
    heelTarget,
    sourcePosition,
    targetPosition,
    pathMode: input.pathMode,
    routeMode: input.routeMode,
    stored: input.waypoints ?? [],
    obstacles: input.obstacles,
  });

  return {
    ...docking,
    waypoints: waypoints.map((point) => ({ x: point.x, y: point.y })),
    heelSource,
    heelTarget,
  };
}

/**
 * Whether a resolved route is geometry a lock can freeze.
 *
 * A lock persists this exact polyline and stops anything from recomputing it,
 * so freezing something undrawable would strand the relationship in a broken
 * shape that no later action is allowed to repair. Freezing is therefore
 * refused for a non-finite coordinate, and — in orthogonal mode — for any leg
 * that is neither horizontal nor vertical.
 */
export function isFreezableRoute(route: DisplayedRoute, pathMode: "orthogonal" | "straight"): boolean {
  return isDrawableRoute([route.heelSource, ...route.waypoints, route.heelTarget], pathMode);
}

/**
 * Whether a heel-to-heel polyline is geometry the canvas can actually draw:
 * finite throughout, and axis-aligned when the relationship is orthogonal.
 *
 * One rule with two callers — the lock refuses to freeze a route that fails it,
 * and a manual edit that produces one is discarded rather than persisted. Spec
 * section 4: restore the pre-gesture layout rather than persisting a broken
 * connector.
 */
export function isDrawableRoute(points: Pt[], pathMode: "orthogonal" | "straight"): boolean {
  for (const point of points) {
    if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) return false;
  }
  if (pathMode !== "orthogonal") return true;
  for (let index = 0; index < points.length - 1; index++) {
    if (!sameX(points[index]!, points[index + 1]!) && !sameY(points[index]!, points[index + 1]!)) return false;
  }
  return true;
}

/**
 * Whether any bend has been dragged inside one of the relationship's own end
 * cards. The route must leave and arrive from outside; a bend in the card
 * interior draws the connector through the table it connects.
 */
export function routeEntersCard(waypoints: Pt[], card: Rect): boolean {
  return waypoints.some(
    (point) =>
      point.x > card.x && point.x < card.x + card.width && point.y > card.y && point.y < card.y + card.height,
  );
}
