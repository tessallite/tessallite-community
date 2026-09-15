/**
 * What a locked relationship forbids on the canvas (R06, R08).
 *
 * A lock freezes a complete path, so the geometry it depends on has to stop
 * moving too. Two separate rules follow, and the specification treats them
 * differently on purpose:
 *
 * - an ENDPOINT of a locked route must not move at all, so the gesture is
 *   refused outright and the card never leaves its place;
 * - any OTHER card may be moved freely, and is only refused if the completed
 *   gesture has put it across a locked path — at which point the previous
 *   geometry is restored.
 *
 * The asymmetry is deliberate: an endpoint move invalidates the frozen path by
 * definition, while an unrelated move usually does not, and refusing it up
 * front would make locking one relationship quietly freeze the whole diagram.
 */
import type { Point, Rect } from "./types";

export interface LockedEdgeInput {
  id: string;
  source: string;
  target: string;
  locked: boolean;
}

/**
 * Cards held in place because a locked relationship docks to them.
 *
 * This is the same set the worker protects from automatic placement; here it
 * also withdraws manual dragging and resizing.
 */
export function lockedEndpointIds(edges: readonly LockedEdgeInput[]): Set<string> {
  const ids = new Set<string>();
  for (const edge of edges) {
    if (!edge.locked) continue;
    ids.add(edge.source);
    ids.add(edge.target);
  }
  return ids;
}

export interface LockedRouteGeometry {
  id: string;
  source: string;
  target: string;
  /** The frozen polyline, heel to heel. */
  points: Point[];
}

export interface MovedCard {
  id: string;
  rect: Rect;
}

/**
 * Locked routes that a just-moved card now lies across.
 *
 * A card's own locked relationships are skipped: a route is supposed to touch
 * the cards it connects, and an endpoint cannot move anyway.
 *
 * The test is segment-versus-rectangle on the frozen polyline, so it reports
 * the same intrusion the user can see — a card sitting on top of a line that is
 * not allowed to move out of its way.
 */
export function lockedRouteIntrusions(
  routes: readonly LockedRouteGeometry[],
  moved: readonly MovedCard[],
): string[] {
  const hits: string[] = [];
  for (const route of routes) {
    const blocked = moved.some((card) => {
      if (card.id === route.source || card.id === route.target) return false;
      for (let index = 0; index < route.points.length - 1; index++) {
        if (segmentCrossesRect(route.points[index]!, route.points[index + 1]!, card.rect)) return true;
      }
      return false;
    });
    if (blocked) hits.push(route.id);
  }
  return hits;
}

/**
 * Whether a segment enters a rectangle's interior.
 *
 * Touching a border is not an intrusion — a route legitimately runs along a
 * card edge — so the comparison is strict on both axes.
 */
function segmentCrossesRect(a: Point, b: Point, rect: Rect): boolean {
  const left = rect.x;
  const right = rect.x + rect.width;
  const top = rect.y;
  const bottom = rect.y + rect.height;

  // Segment bounding box must overlap the rectangle's interior at all.
  if (Math.max(a.x, b.x) <= left || Math.min(a.x, b.x) >= right) return false;
  if (Math.max(a.y, b.y) <= top || Math.min(a.y, b.y) >= bottom) return false;

  // Axis-aligned segments are fully decided by that overlap.
  if (Math.abs(a.y - b.y) < 0.01 || Math.abs(a.x - b.x) < 0.01) return true;

  // A diagonal only enters if the rectangle's corners fall on both sides of it,
  // or an endpoint is already inside.
  if (pointInside(a, left, right, top, bottom) || pointInside(b, left, right, top, bottom)) return true;
  const corners: Point[] = [
    { x: left, y: top },
    { x: right, y: top },
    { x: right, y: bottom },
    { x: left, y: bottom },
  ];
  let positive = false;
  let negative = false;
  for (const corner of corners) {
    const side = (b.x - a.x) * (corner.y - a.y) - (b.y - a.y) * (corner.x - a.x);
    if (side > 0) positive = true;
    if (side < 0) negative = true;
  }
  return positive && negative;
}

function pointInside(point: Point, left: number, right: number, top: number, bottom: number): boolean {
  return point.x > left && point.x < right && point.y > top && point.y < bottom;
}

/**
 * Cards whose geometry this gesture actually changed.
 *
 * Deliberately compares complete rectangles. Comparing x and y alone looks
 * sufficient — a card that moves changes its position — but growing a card
 * from its bottom or right edge changes neither coordinate. The locked-route
 * obstruction guard used a position-only comparison, so a card that grew
 * straight across a frozen connector was never even offered to the geometry
 * check. A locked route is exactly the route that cannot get out of the way.
 *
 * A card with no previous rectangle counts as changed: it has just appeared,
 * and it may have appeared on top of a frozen line.
 */
export function changedCards(
  previous: Map<string, Rect>,
  current: Map<string, Rect>,
  tolerance = 0.5,
): Array<{ id: string; rect: Rect }> {
  const changed: Array<{ id: string; rect: Rect }> = [];
  for (const [id, rect] of current) {
    const was = previous.get(id);
    if (
      !was ||
      Math.abs(was.x - rect.x) > tolerance ||
      Math.abs(was.y - rect.y) > tolerance ||
      Math.abs(was.width - rect.width) > tolerance ||
      Math.abs(was.height - rect.height) > tolerance
    ) {
      changed.push({ id, rect });
    }
  }
  return changed;
}
