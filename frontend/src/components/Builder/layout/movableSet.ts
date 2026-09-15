/**
 * Which tables a layout operation is allowed to move.
 *
 * This is one rule with two readers. The worker needs it to decide what it may
 * place (R04); the canvas needs it to decide whether "Arrange selected" is
 * offered at all, and what to tell the user when it is not. Keeping the rule
 * here rather than duplicating it in the control means the button can never
 * invite an action the worker will refuse.
 *
 * Deliberately pure and engine-free: the renderer imports it, so it must not
 * drag elkjs or libavoid into the main bundle the way `layout.worker.ts` would.
 *
 * The inputs are structural, not the full snapshot types, so both a
 * `LayoutSnapshot` and the canvas' own node/edge data satisfy them directly.
 */
import type { LayoutOperation } from "./types";

export interface MovableNodeInput {
  id: string;
  /** Explicitly pinned by the user. */
  pinned: boolean;
  /** Held in place by the operation itself rather than by a user decision. */
  fixed: boolean;
  selected: boolean;
}

export interface MovableEdgeInput {
  source: string;
  target: string;
  locked: boolean;
}

/**
 * Table ids no operation may move: explicitly pinned or fixed tables, and both
 * endpoints of a locked relationship — a lock freezes docking, so the cards it
 * docks to have to stay put for the frozen path to remain the path drawn.
 */
function protectedNodeIds(
  nodes: readonly MovableNodeInput[],
  edges: readonly MovableEdgeInput[],
): Set<string> {
  const fixed = new Set(nodes.filter((node) => node.fixed || node.pinned).map((node) => node.id));
  for (const edge of edges) {
    if (!edge.locked) continue;
    fixed.add(edge.source);
    fixed.add(edge.target);
  }
  return fixed;
}

/**
 * The tables `operation` owns. `arrange-selected` narrows to the user's
 * selection; every other placing operation takes everything unprotected.
 *
 * `reroute-links` and `repair-routes` never own coordinates at all, so their
 * caller ignores this set — it is computed the same way only so one operation
 * cannot silently acquire a different movable rule from another.
 */
export function movableIdsFor(
  nodes: readonly MovableNodeInput[],
  edges: readonly MovableEdgeInput[],
  operation: LayoutOperation,
): Set<string> {
  const protectedIds = protectedNodeIds(nodes, edges);
  const movable = nodes.filter((node) => !protectedIds.has(node.id));
  if (operation === "arrange-selected") {
    return new Set(movable.filter((node) => node.selected).map((node) => node.id));
  }
  return new Set(movable.map((node) => node.id));
}
