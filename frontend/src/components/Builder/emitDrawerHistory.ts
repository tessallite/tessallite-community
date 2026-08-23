/**
 * Emit a drawer-edit undo/redo entry (Bug-8227 / G-026-01).
 *
 * Drawer panels (measures, dimensions, hierarchies, personas, KPIs, named sets)
 * call this AFTER a mutation succeeds, passing the forward op (what the user
 * just did) and its inverse. useCanvasHistory listens for the same
 * `canvas-history-action` window event the join/rename producers already use
 * and replays either op through its entity->API registry.
 *
 * Record commands only after server success so a failed write never leaves a
 * bogus history entry (mirrors the addLink/deleteLink discipline).
 *
 * Helpers below build the three edit shapes so call sites stay one-liners:
 *
 *   - recordCreate: undo deletes the created row; redo re-creates it.
 *   - recordUpdate: undo/redo PATCH the row back to prior/new field values.
 *   - recordDelete: undo re-creates the row from its prior definition; redo
 *     deletes it again.
 */
import type { CommandOp, DrawerEntity, HistoryAction } from "./useCanvasHistory";

function dispatch(action: HistoryAction): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent("canvas-history-action", { detail: { action } }),
  );
}

function emit(
  entity: DrawerEntity,
  redo: CommandOp,
  undo: CommandOp,
): void {
  dispatch({ type: "command", entity, redo, undo });
}

/** A newly created row: undo removes it, redo re-creates it from `createData`. */
export function recordCreate(
  entity: DrawerEntity,
  createdId: string,
  createData: Record<string, unknown>,
): void {
  emit(
    entity,
    { kind: "create", data: createData },
    // Keep the parent-scope metadata on the delete op. The generic model-
    // scoped APIs ignore it; adapters for UDA/calendar/relationship entities
    // need it to route the inverse call to the correct parent resource.
    { kind: "delete", id: createdId, data: createData },
  );
}

/** An updated row: undo restores `priorData`, redo re-applies `newData`. */
export function recordUpdate(
  entity: DrawerEntity,
  id: string,
  priorData: Record<string, unknown>,
  newData: Record<string, unknown>,
): void {
  emit(
    entity,
    { kind: "update", id, data: newData },
    { kind: "update", id, data: priorData },
  );
}

/** A deleted row: undo re-creates it from `priorData`, redo deletes it again. */
export function recordDelete(
  entity: DrawerEntity,
  id: string,
  priorData: Record<string, unknown>,
): void {
  emit(
    entity,
    // The redo delete also needs parent-scope metadata for non-uniform APIs.
    { kind: "delete", id, data: priorData },
    { kind: "create", data: priorData },
  );
}
