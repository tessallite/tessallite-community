/**
 * What locking or unlocking a relationship writes into the saved layout (R06).
 *
 * Kept as a pure rule rather than inline in the Canvas handler because the
 * interesting part is not the toggle, it is what has to be captured alongside
 * it: an automatic route has no stored path of its own, so without writing the
 * displayed geometry down a lock would freeze nothing and the next reload would
 * recompute a different path.
 */
import type { DisplayedRoute } from "../edgeGeometry";
import type { Point, RouteMode } from "./types";

/** The saved presentation entry for one relationship (`CanvasLayout.edges[id]`). */
export interface PersistedEdgeLayout {
  waypoint?: Point;
  waypoints?: Point[];
  pathing?: "orthogonal" | "straight";
  sourceSide?: string;
  targetSide?: string;
  sourceRatio?: number;
  targetRatio?: number;
  locked?: boolean;
  routeMode?: RouteMode;
  /**
   * The parallel fan-out offset in force when this route was frozen.
   *
   * The renderer normally recomputes the fan-out from the CURRENT set of
   * relationships between the two cards, then adds it to the stored base ratio.
   * So adding a second relationship moved an existing LOCKED attachment — by
   * 7.5px in the reviewer's reproduction — while its frozen bends stayed put,
   * which can turn a frozen orthogonal terminal into a diagonal. A locked route
   * draws with the offset it was locked with.
   */
  lockedParallelOffset?: number;
  /**
   * True when `pathing` was written by the lock rather than chosen by the user.
   *
   * A locked route has to own its resolved path mode: an automatic orthogonal
   * route with no explicit `pathing` inherits the model-wide setting, so
   * switching that setting to Straight made the renderer discard the very bends
   * the lock had frozen. Recording that the override is the lock's own doing is
   * what lets Unlock hand the relationship back to the model setting instead of
   * leaving a permanent style override the user never asked for.
   */
  pathingFrozenByLock?: boolean;
}

export interface RouteLockInput {
  /** The relationship's current saved entry, if it has one. */
  current?: PersistedEdgeLayout;
  /** The state being moved to. */
  locked: boolean;
  /** The geometry as drawn. Required to lock; ignored when unlocking. */
  capture: DisplayedRoute | null;
  /**
   * Provenance of the captured bends, resolved by the caller. Capturing an
   * engine route does not turn it into the user's manual edit, so this is
   * carried through rather than rewritten to "manual".
   */
  routeMode: RouteMode;
  /** The path mode the relationship is currently DRAWN with. Required to lock. */
  resolvedPathing?: "orthogonal" | "straight";
  /** The fan-out offset the relationship is currently drawn with. */
  parallelOffset?: number;
}

/**
 * Locking freezes the displayed path, docking sides and anchor ratios.
 * Unlocking clears the lock and nothing else: no action discards a locked path,
 * and a table pin is independent of a route lock.
 */
export function applyRouteLock(input: RouteLockInput): PersistedEdgeLayout {
  const next: PersistedEdgeLayout = { ...(input.current ?? {}) };

  if (!input.locked) {
    next.locked = false;
    // Hand an inherited path mode back to the model-wide setting. Leaving the
    // override behind would make Unlock a one-way door: the relationship would
    // keep whatever mode happened to be in force when it was locked, forever,
    // and the user never chose that.
    if (next.pathingFrozenByLock) {
      delete next.pathing;
      delete next.pathingFrozenByLock;
    }
    delete next.lockedParallelOffset;
    return next;
  }
  if (!input.capture) {
    throw new Error("cannot lock a relationship whose displayed route could not be resolved");
  }

  next.locked = true;
  next.sourceSide = input.capture.sourceSide;
  next.targetSide = input.capture.targetSide;
  next.sourceRatio = input.capture.sourceRatio;
  next.targetRatio = input.capture.targetRatio;
  next.waypoints = input.capture.waypoints.map((point) => ({ x: point.x, y: point.y }));
  next.routeMode = input.routeMode;

  // Freeze the resolved path mode, remembering whether it was the user's
  // explicit choice or inherited from the model setting.
  if (next.pathing === undefined && input.resolvedPathing !== undefined) {
    next.pathing = input.resolvedPathing;
    next.pathingFrozenByLock = true;
  }

  // Freeze the fan-out this route is drawn with, so a relationship added later
  // between the same two cards cannot move a frozen attachment.
  if (input.parallelOffset !== undefined) {
    next.lockedParallelOffset = input.parallelOffset;
  }

  // The legacy single waypoint is superseded by the captured array. Leaving it
  // would give the reload path two conflicting paths for one frozen route.
  delete next.waypoint;
  return next;
}
