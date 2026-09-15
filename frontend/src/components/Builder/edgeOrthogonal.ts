/**
 * Manual orthogonal editing of a relationship's bends.
 *
 * Spec section 4: "For orthogonal paths, drag segments only perpendicular to
 * themselves. Moving a bend adjusts neighbouring bends or inserts valid doglegs
 * so every segment stays horizontal/vertical and terminals exit away from the
 * card. Delete/add bend must not create diagonal segments."
 *
 * The rule is enforced by construction rather than by rejecting the gesture:
 * the user drags where they like and the route is repaired to the nearest legal
 * shape, so editing stays direct and a diagonal can never be persisted.
 */
import { Position } from "reactflow";
import { type Pt, awayVec, sameX, sameY } from "./edgeRouting";

/** Length of the stub inserted to make a terminal leave along its own side. */
const TERMINAL_STUB = 20;

/** The axis a route must run along as it leaves a card through `position`. */
function exitAxis(position: Position): "h" | "v" {
  return position === Position.Left || position === Position.Right ? "h" : "v";
}

/** Drop repeated points and merge collinear runs, so bends mean something. */
function compactOrthogonal(points: Pt[]): Pt[] {
  const out: Pt[] = [];
  for (const point of points) {
    const last = out[out.length - 1];
    if (last && sameX(last, point) && sameY(last, point)) continue;
    out.push({ x: point.x, y: point.y });
  }
  for (let index = 1; index < out.length - 1; ) {
    const before = out[index - 1]!;
    const here = out[index]!;
    const after = out[index + 1]!;
    if ((sameX(before, here) && sameX(here, after)) || (sameY(before, here) && sameY(here, after))) {
      out.splice(index, 1);
      continue;
    }
    index += 1;
  }
  return out;
}

/**
 * Repair a heel-to-heel polyline into a valid orthogonal route.
 *
 * Three things are guaranteed on the way out: every segment is horizontal or
 * vertical, the route leaves the source along the source's own side, and it
 * arrives at the target along the target's own side. Both heels are fixed —
 * they are where the markers are drawn — so the repair only ever adds or moves
 * interior points.
 *
 * Done in two passes, because the two problems are different. Diagonals are
 * resolved first, by inserting one corner each, alternating orientation so the
 * result is a staircase rather than a zigzag that doubles back. Only then are
 * the terminals corrected: after the first pass every leg is axis-aligned, so a
 * terminal on the wrong axis is always the case where the route runs along the
 * card's border, and the fix is always the same three-point dogleg that pushes
 * it clear of the card first.
 */
export function orthogonaliseRoute(
  points: Pt[],
  sourcePosition: Position,
  targetPosition: Position,
): Pt[] {
  if (points.length < 2) return points.map((point) => ({ x: point.x, y: point.y }));

  const sourceAxis = exitAxis(sourcePosition);
  const targetAxis = exitAxis(targetPosition);
  const [sdx, sdy] = awayVec(sourcePosition);
  const [tdx, tdy] = awayVec(targetPosition);

  // Pass one: every leg axis-aligned.
  const straightened: Pt[] = [{ x: points[0]!.x, y: points[0]!.y }];
  let previousAxis: "h" | "v" | null = null;
  for (let index = 1; index < points.length; index++) {
    const a = straightened[straightened.length - 1]!;
    const b = { x: points[index]!.x, y: points[index]!.y };
    if (sameX(a, b) || sameY(a, b)) {
      straightened.push(b);
      previousAxis = sameY(a, b) ? "h" : "v";
      continue;
    }
    // The corner's first leg: forced on the way out of the source, alternating
    // after that so consecutive turns step rather than double back.
    const firstLegAxis: "h" | "v" =
      straightened.length === 1 ? sourceAxis : previousAxis === "h" ? "v" : "h";
    straightened.push(firstLegAxis === "h" ? { x: b.x, y: a.y } : { x: a.x, y: b.y });
    straightened.push(b);
    previousAxis = firstLegAxis === "h" ? "v" : "h";
  }

  // Pass two: make both terminals leave through their own side. A leg on the
  // wrong axis here runs along the card's border, so the route is pushed clear
  // of the card and brought back — always three points, never two.
  const withTerminals = [...straightened];
  const heelSource = withTerminals[0]!;
  const afterSource = withTerminals[1];
  if (afterSource && legAxis(heelSource, afterSource) !== sourceAxis) {
    withTerminals.splice(
      1, 0,
      sourceAxis === "h"
        ? { x: heelSource.x + sdx * TERMINAL_STUB, y: heelSource.y }
        : { x: heelSource.x, y: heelSource.y + sdy * TERMINAL_STUB },
      sourceAxis === "h"
        ? { x: heelSource.x + sdx * TERMINAL_STUB, y: afterSource.y }
        : { x: afterSource.x, y: heelSource.y + sdy * TERMINAL_STUB },
    );
  }

  const heelTarget = withTerminals[withTerminals.length - 1]!;
  const beforeTarget = withTerminals[withTerminals.length - 2];
  if (beforeTarget && legAxis(beforeTarget, heelTarget) !== targetAxis) {
    withTerminals.splice(
      withTerminals.length - 1, 0,
      targetAxis === "h"
        ? { x: heelTarget.x + tdx * TERMINAL_STUB, y: beforeTarget.y }
        : { x: beforeTarget.x, y: heelTarget.y + tdy * TERMINAL_STUB },
      targetAxis === "h"
        ? { x: heelTarget.x + tdx * TERMINAL_STUB, y: heelTarget.y }
        : { x: heelTarget.x, y: heelTarget.y + tdy * TERMINAL_STUB },
    );
  }

  return compactOrthogonal(withTerminals);
}

/** Which axis an axis-aligned leg runs along; `null` if it is diagonal. */
function legAxis(a: Pt, b: Pt): "h" | "v" | null {
  if (sameX(a, b) && sameY(a, b)) return null;
  if (sameY(a, b)) return "h";
  if (sameX(a, b)) return "v";
  return null;
}

/**
 * Move one bend of an orthogonal route and keep the route legal.
 *
 * Returns the interior waypoints, ready to persist. The bend goes where the
 * pointer is; its neighbours follow on the coordinate they shared with it, and
 * where a neighbour is a fixed heel a dogleg is inserted instead.
 */
export function applyOrthoBendDrag(
  bendIndex: number,
  position: Pt,
  waypoints: Pt[],
  heelSource: Pt,
  heelTarget: Pt,
  sourcePosition: Position,
  targetPosition: Position,
): Pt[] {
  if (bendIndex < 0 || bendIndex >= waypoints.length) return waypoints.map((point) => ({ x: point.x, y: point.y }));

  const next = waypoints.map((point) => ({ x: point.x, y: point.y }));
  const moved = { x: position.x, y: position.y };
  const before = bendIndex === 0 ? heelSource : next[bendIndex - 1]!;
  const after = bendIndex === waypoints.length - 1 ? heelTarget : next[bendIndex + 1]!;
  const original = next[bendIndex]!;

  // A neighbouring BEND follows on the coordinate it shared with this one, so
  // the segment between them keeps its orientation. A neighbouring HEEL cannot
  // move, so `orthogonaliseRoute` inserts the dogleg below instead.
  if (bendIndex > 0) {
    if (sameX(before, original)) next[bendIndex - 1]!.x = moved.x;
    else if (sameY(before, original)) next[bendIndex - 1]!.y = moved.y;
  }
  if (bendIndex < waypoints.length - 1) {
    if (sameX(after, original)) next[bendIndex + 1]!.x = moved.x;
    else if (sameY(after, original)) next[bendIndex + 1]!.y = moved.y;
  }
  next[bendIndex] = moved;

  const repaired = orthogonaliseRoute([heelSource, ...next, heelTarget], sourcePosition, targetPosition);
  return repaired.slice(1, -1);
}
