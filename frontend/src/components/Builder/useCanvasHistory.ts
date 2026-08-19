import { useCallback, useEffect, useRef, useState } from "react";
import type { Node } from "reactflow";
import type { QueryClient } from "@tanstack/react-query";
import type { CanvasLayout, JoinCreate } from "../../api/types";
import {
  dimensionsApi,
  hierarchiesApi,
  joinsApi,
  kpisApi,
  measuresApi,
  modelTablesApi,
  namedQueriesApi,
  namedSetsApi,
  personasApi,
} from "../../api/client";
import { isEditableTarget } from "../../hooks/useGlobalShortcuts";
import {
  useModelEditorStore,
  beginHistoryApply,
  endHistoryApply,
} from "../../store/useModelEditorStore";

export type NodePositions = Record<string, { x: number; y: number }>;

/** Per-edge layout entries (waypoints, anchor sides/ratios, pathing). */
export type EdgeLayouts = NonNullable<CanvasLayout["edges"]>;

// ---------------------------------------------------------------------------
// Drawer-edit undo/redo (Bug-8227 / G-026-01)
//
// The canvas history originally covered only layout/join/rename actions. A
// modeller editing a measure formula, dimension, hierarchy, persona, KPI or
// named set in a drawer had no way to undo it. To fix this at the root without
// per-panel bespoke logic, every drawer-authored mutation records a generic
// `command` action carrying a forward op and its inverse op. Because all six
// entity APIs share the uniform create(p,m,data) / update(p,m,id,data) /
// delete(p,m,id) contract, one entity->API registry replays either direction.
// ---------------------------------------------------------------------------

/** Drawer entities whose create/update/delete can be undone/redone. */
export type DrawerEntity =
  | "measure"
  | "dimension"
  | "hierarchy"
  | "persona"
  | "kpi"
  | "namedSet"
  | "namedQuery";

/**
 * A single reversible model-content write. `kind` names the API call to make;
 * `id` targets an existing row (update/delete); `data` carries the create/
 * update payload. A create records the id the server assigns back into the
 * paired delete op (see applyAction) so a later undo/redo can target it.
 */
export interface CommandOp {
  kind: "create" | "update" | "delete";
  id?: string;
  data?: Record<string, unknown>;
}

export type HistoryAction =
  | {
      type: "command";
      entity: DrawerEntity;
      /** Applied on redo (re-do the original edit). */
      redo: CommandOp;
      /** Applied on undo (reverse the original edit). */
      undo: CommandOp;
    }
  | {
      type: "move";
      /** Positions of ONLY the nodes that changed in this gesture. */
      before: NodePositions;
      after: NodePositions;
      /** Full edge-layout snapshot before/after — populated for layout-preset
       *  redraws so undo restores manual edge routing (waypoints), not just
       *  table positions. Omitted for plain drags, which never touch edges. */
      beforeEdges?: EdgeLayouts;
      afterEdges?: EdgeLayouts;
    }
  | {
      type: "rename";
      tableId: string;
      sourceId: string;
      oldDisplayName: string;
      newDisplayName: string;
    }
  | {
      type: "addLink";
      joinId: string;
      createData: JoinCreate;
    }
  | {
      type: "deleteLink";
      joinId: string;
      createData: JoinCreate;
    };

export interface CanvasActionEvent {
  action: Omit<HistoryAction, "before" | "after">;
}

/** Uniform CRUD surface every drawer entity API already exposes. */
interface EntityApi {
  create: (p: string, m: string, data: Record<string, unknown>) => Promise<unknown>;
  update: (p: string, m: string, id: string, data: Record<string, unknown>) => Promise<unknown>;
  delete: (p: string, m: string, id: string) => Promise<unknown>;
}

/**
 * Entity -> API registry for command replay. Each client API is cast to the
 * uniform EntityApi shape (they all share create(p,m,data) / update(p,m,id,data)
 * / delete(p,m,id) — the create/update payloads are the entity's *Create /
 * *Update types, which a CommandOp.data carries structurally).
 */
const DRAWER_ENTITY_APIS: Record<DrawerEntity, EntityApi> = {
  measure: measuresApi as unknown as EntityApi,
  dimension: dimensionsApi as unknown as EntityApi,
  hierarchy: hierarchiesApi as unknown as EntityApi,
  persona: personasApi as unknown as EntityApi,
  kpi: kpisApi as unknown as EntityApi,
  namedSet: namedSetsApi as unknown as EntityApi,
  namedQuery: namedQueriesApi as unknown as EntityApi,
};

/** Cache-key family each entity's consumers subscribe to, invalidated after a
 *  command replay so canvas/panels refetch. F-026-05: the named-set query cache
 *  is keyed on `namedSets` (camelCase) — both NamedSetsPanel
 *  (NAMED_SETS_QUERY_KEY_PREFIX) and `useNamedSets` subscribe to that. The
 *  earlier `named-sets` value here was the PANEL ID, not the query key, so an
 *  undo/redo of a named-set edit invalidated a family nobody reads and left the
 *  open drawer showing pre-undo rows until a manual refresh. */
export const ENTITY_QUERY_KEYS: Record<DrawerEntity, string[]> = {
  measure: ["measures"],
  dimension: ["dimensions"],
  hierarchy: ["hierarchies"],
  persona: ["personas"],
  kpi: ["kpis"],
  namedSet: ["namedSets"],
  namedQuery: ["namedQueries"],
};

/**
 * A stored history entry. `revisionBefore`/`revisionAfter` snapshot the editor
 * store's content revision on either side of the recorded edit so undo/redo can
 * reconcile the dirty flag against the saved baseline (F-026-02):
 *   - undo of an entry restores `currentRevision = revisionBefore`
 *   - redo of an entry restores `currentRevision = revisionAfter`
 * Layout-only moves leave the revision unchanged (before === after), so undoing
 * a move never affects dirty — only content writes do.
 */
type HistoryEntry = HistoryAction & {
  revisionBefore?: number;
  revisionAfter?: number;
};

const MAX_STACK = 50;

export function positionsFromNodes(nodes: Node[]): NodePositions {
  const positions: NodePositions = {};
  for (const n of nodes) {
    positions[n.id] = { x: n.position.x, y: n.position.y };
  }
  return positions;
}

/** Restrict `before`/`after` to the nodes whose position actually changed. */
function diffPositions(
  before: NodePositions,
  after: NodePositions,
): { before: NodePositions; after: NodePositions } | null {
  const changedBefore: NodePositions = {};
  const changedAfter: NodePositions = {};
  let changed = false;
  for (const id of Object.keys(after)) {
    const b = before[id];
    const a = after[id];
    if (b && (b.x !== a.x || b.y !== a.y)) {
      changedBefore[id] = b;
      changedAfter[id] = a;
      changed = true;
    }
  }
  return changed ? { before: changedBefore, after: changedAfter } : null;
}

/**
 * Canvas undo/redo history.
 *
 * Semantics (F-026-02):
 * - One user gesture = one undo entry. Moves are captured per drag gesture
 *   (`beginMove` at drag start, `endMove` at drag end); renames and join
 *   add/delete arrive as discrete actions via the `canvas-history-action`
 *   window event.
 * - Undo applies the inverse of the most recent action; redo re-applies it.
 * - The history stack itself is session-scoped (cleared on reload / model
 *   change), but the *positions* an undo/redo produces are persisted to the
 *   model's canvas_layout via the `onApplyMove` callback, so an undone move
 *   survives a reload exactly like a normal drag.
 */
export function useCanvasHistory(
  nodes: Node[],
  setNodes: React.Dispatch<React.SetStateAction<Node[]>>,
  projectId: string,
  modelId: string,
  queryClient: QueryClient,
  /** Persist positions applied by undo/redo (write canvas_layout + flush). */
  onApplyMove?: (positions: NodePositions) => void,
  /** Restore an edge-layout snapshot applied by undo/redo of a layout-preset
   *  redraw (rewrite canvas_layout edges + live ReactFlow edge data + flush). */
  onApplyEdges?: (edges: EdgeLayouts) => void,
  /** Surface an undo/redo apply failure to the user (F-026-19). Receives the
   *  caught error so the caller can extract an API message and toast it. */
  onError?: (err: unknown) => void,
  /** F-026-04: when the builder is read-only (viewer role or ?readonly=1) the
   *  history boundary must not replay writes. The keyboard/event listeners are
   *  not bound, undo/redo refuse, and the stacks are cleared on the rising edge
   *  so a leftover author-session stack cannot be replayed after a role flip. */
  readOnly = false,
) {
  const undoStack = useRef<HistoryEntry[]>([]);
  const redoStack = useRef<HistoryEntry[]>([]);
  const readOnlyRef = useRef(readOnly);
  readOnlyRef.current = readOnly;
  /** Positions captured at the start of an in-flight drag gesture. */
  const pendingBefore = useRef<NodePositions | null>(null);
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;
  const busyRef = useRef(false);

  const [canUndo, setCanUndo] = useState(false);
  const [canRedo, setCanRedo] = useState(false);

  // Bug-7634: clear the undo/redo stacks when the model changes so that
  // model A's history entries (positions, join operations) cannot be
  // applied to model B, corrupting its persisted canvas_layout.
  const prevModelIdRef = useRef(modelId);
  useEffect(() => {
    if (prevModelIdRef.current !== modelId) {
      prevModelIdRef.current = modelId;
      undoStack.current = [];
      redoStack.current = [];
      pendingBefore.current = null;
      busyRef.current = false;
      setCanUndo(false);
      setCanRedo(false);
    }
  }, [modelId]);

  // F-026-04: when the session flips to read-only (viewer role / ?readonly=1),
  // drop any leftover author-session history so it cannot be replayed. Mirrors
  // the model-change reset above.
  const prevReadOnlyRef = useRef(readOnly);
  useEffect(() => {
    if (!prevReadOnlyRef.current && readOnly) {
      undoStack.current = [];
      redoStack.current = [];
      pendingBefore.current = null;
      busyRef.current = false;
      setCanUndo(false);
      setCanRedo(false);
    }
    prevReadOnlyRef.current = readOnly;
  }, [readOnly]);

  const syncCan = useCallback(() => {
    setCanUndo(undoStack.current.length > 0);
    setCanRedo(redoStack.current.length > 0);
  }, []);

  const pushAction = useCallback(
    (action: HistoryAction) => {
      // F-026-02: snapshot the content revision on either side of this edit.
      // The producing content write (measure/dimension/join/rename PATCH/POST)
      // has already fired and been counted by the interceptor's markDirty, so
      // the store's currentRevision now reflects the post-edit state. A pure
      // layout move records no content write, so before === after and undoing
      // it never toggles dirty.
      const revisionAfter = useModelEditorStore.getState().currentRevision;
      // Each recorded content action (command, rename, addLink, deleteLink)
      // corresponds to exactly ONE interceptor-counted content write, so its
      // own revision delta is always 1. Using `revisionAfter - 1` (not the
      // previous entry's revisionAfter) ensures any interleaved non-history
      // content writes (hierarchy levels, time-variant creates, certify/
      // deprecate, etc.) that raised the revision between two recorded entries
      // stay unreconciled — the model correctly stays dirty for them after
      // undoing the recorded entry. A pure layout move has no content write,
      // so its delta is 0 (before === after).
      const revisionBefore =
        action.type === "move"
          ? revisionAfter // layout-only: no content change
          : revisionAfter - 1;
      const entry: HistoryEntry = { ...action, revisionBefore, revisionAfter };
      undoStack.current.push(entry);
      if (undoStack.current.length > MAX_STACK) undoStack.current.shift();
      redoStack.current = [];
      syncCan();
    },
    [syncCan],
  );

  /**
   * Record a completed move given full before/after position maps.
   *
   * `edgeSnapshots` (optional) carries the full edge-layout state on either
   * side of a layout-preset redraw so undo restores manual edge routing
   * (waypoints/anchors), which the redraw otherwise discards. When supplied,
   * the entry is recorded even if no table position changed (a redraw can
   * leave tables put while rewriting every edge anchor).
   */
  const recordMove = useCallback(
    (
      before: NodePositions,
      after: NodePositions,
      edgeSnapshots?: { before: EdgeLayouts; after: EdgeLayouts },
    ) => {
      const diff = diffPositions(before, after);
      if (!diff && !edgeSnapshots) return;
      pushAction({
        type: "move",
        before: diff?.before ?? {},
        after: diff?.after ?? {},
        ...(edgeSnapshots
          ? { beforeEdges: edgeSnapshots.before, afterEdges: edgeSnapshots.after }
          : {}),
      });
    },
    [pushAction],
  );

  /** Call at drag start — captures pre-gesture positions. */
  const beginMove = useCallback(() => {
    pendingBefore.current = positionsFromNodes(nodesRef.current);
  }, []);

  /**
   * Call at drag end. `after` is the authoritative post-gesture position map
   * (the caller owns the layout ref and knows the final positions even when
   * the last render has not committed yet). Falls back to the live nodes.
   */
  const endMove = useCallback(
    (after?: NodePositions) => {
      const before = pendingBefore.current;
      pendingBefore.current = null;
      if (!before) return;
      recordMove(before, after ?? positionsFromNodes(nodesRef.current));
    },
    [recordMove],
  );

  const invalidateEntity = useCallback(
    (entity: DrawerEntity) => {
      for (const key of ENTITY_QUERY_KEYS[entity]) {
        queryClient.invalidateQueries({ queryKey: [key, projectId, modelId] });
      }
      // Measures/dimensions feed the canvas node bodies and the pivot family;
      // hierarchies also feed joins (alias->fact edges). Invalidate the shared
      // canvas-facing families so an undone/redone drawer edit is reflected on
      // the canvas, not just in the owning panel.
      queryClient.invalidateQueries({ queryKey: ["tableAttributes", projectId, modelId] });
    },
    [queryClient, projectId, modelId],
  );

  const invalidateJoins = useCallback(() => {
    queryClient.invalidateQueries({
      queryKey: ["joins", projectId, modelId],
    });
    queryClient.invalidateQueries({
      queryKey: ["tableAttributes", projectId, modelId],
    });
    queryClient.invalidateQueries({
      queryKey: ["dimensions", projectId, modelId],
    });
  }, [queryClient, projectId, modelId]);

  const invalidateTables = useCallback(
    (sourceId: string) => {
      queryClient.invalidateQueries({
        queryKey: ["modelTables", projectId, modelId, sourceId],
      });
      queryClient.invalidateQueries({
        queryKey: ["allModelTables", projectId, modelId],
      });
      queryClient.invalidateQueries({
        queryKey: ["joins", projectId, modelId],
      });
      queryClient.invalidateQueries({
        queryKey: ["dimensions", projectId, modelId],
      });
      queryClient.invalidateQueries({
        queryKey: ["measures", projectId, modelId],
      });
    },
    [queryClient, projectId, modelId],
  );

  const applyAction = useCallback(
    async (action: HistoryEntry, reverse: boolean) => {
      switch (action.type) {
        case "command": {
          // Replay the inverse op (undo) or the forward op (redo).
          const op = reverse ? action.undo : action.redo;
          const paired = reverse ? action.redo : action.undo;
          const registry = DRAWER_ENTITY_APIS[action.entity];
          if (op.kind === "create") {
            const created = await registry.create(projectId, modelId, op.data ?? {});
            const newId = (created as { id?: string })?.id;
            // The row was just re-created with a fresh id. The OPPOSITE op is the
            // one that will later act on this row (delete it, or update it), so
            // point it at the live id — mirrors the addLink id-writeback below.
            if (newId) paired.id = newId;
          } else if (op.kind === "update") {
            if (!op.id) throw new Error("update command missing id");
            await registry.update(projectId, modelId, op.id, op.data ?? {});
          } else {
            if (!op.id) throw new Error("delete command missing id");
            await registry.delete(projectId, modelId, op.id);
          }
          invalidateEntity(action.entity);
          break;
        }
        case "move": {
          const target = reverse ? action.before : action.after;
          setNodes((ns) =>
            ns.map((n) => {
              const p = target[n.id];
              return p ? { ...n, position: { x: p.x, y: p.y } } : n;
            }),
          );
          // Route the applied positions through the layout-persistence path
          // so an undone/redone move survives a reload (F-026-02).
          onApplyMove?.(target);
          // Restore the edge-layout snapshot for layout-preset redraws so
          // undo brings back manual edge routing, not just table positions
          // (F-026-11 round 2 / review finding 2).
          const targetEdges = reverse ? action.beforeEdges : action.afterEdges;
          if (targetEdges) onApplyEdges?.(targetEdges);
          break;
        }
        case "rename": {
          const name = reverse
            ? action.oldDisplayName
            : action.newDisplayName;
          await modelTablesApi.update(
            projectId,
            modelId,
            action.sourceId,
            action.tableId,
            { display_name: name },
          );
          invalidateTables(action.sourceId);
          break;
        }
        case "addLink": {
          if (reverse) {
            await joinsApi.delete(projectId, modelId, action.joinId);
          } else {
            const created = await joinsApi.create(
              projectId,
              modelId,
              action.createData,
            );
            action.joinId = created.id;
          }
          invalidateJoins();
          break;
        }
        case "deleteLink": {
          if (reverse) {
            const created = await joinsApi.create(
              projectId,
              modelId,
              action.createData,
            );
            action.joinId = created.id;
          } else {
            await joinsApi.delete(projectId, modelId, action.joinId);
          }
          invalidateJoins();
          break;
        }
      }
    },
    [projectId, modelId, setNodes, invalidateJoins, invalidateTables, invalidateEntity, onApplyMove, onApplyEdges],
  );

  const undo = useCallback(async () => {
    // F-026-04: refuse to replay writes in a read-only session.
    if (readOnlyRef.current) return;
    if (undoStack.current.length === 0 || busyRef.current) return;
    busyRef.current = true;
    const action = undoStack.current.pop()!;
    // Bug-7634: capture the model ID before the async operation. If the model
    // changes while the operation is in flight (e.g. a join delete is pending
    // on the server), the resolved action must NOT be pushed onto the (now
    // cleared) stack belonging to the new model.
    const startModelId = prevModelIdRef.current;
    // F-026-02: suppress the write interceptor's markDirty for the inverse
    // API call(s) — we reconcile the revision directly below so undoing to the
    // saved baseline clears dirty instead of re-latching it.
    beginHistoryApply();
    try {
      await applyAction(action, true);
      if (prevModelIdRef.current !== startModelId) return; // model changed — discard
      // Undo subtracts this edit's own revision delta from the CURRENT revision
      // (not an absolute jump), so any unrelated out-of-band write that raised
      // the revision after this edit stays counted — the model correctly stays
      // dirty for that write while this specific edit is reconciled away.
      if (action.revisionBefore !== undefined && action.revisionAfter !== undefined) {
        const delta = action.revisionAfter - action.revisionBefore;
        const store = useModelEditorStore.getState();
        store.setRevision(store.currentRevision - delta);
      }
      redoStack.current.push(action);
      syncCan();
    } catch (err) {
      if (prevModelIdRef.current !== startModelId) return; // model changed — discard
      // Apply failed (network / 4xx). Restore the action to the undo stack so
      // it is not silently dropped, and surface the failure (F-026-19).
      undoStack.current.push(action);
      syncCan();
      onError?.(err);
    } finally {
      endHistoryApply();
      // Only clear the busy flag if we are still on the same model. The
      // model-change reset already set busyRef to false for the new model;
      // overwriting it here could allow a second in-flight operation to slip
      // through before the first one's finally block runs.
      if (prevModelIdRef.current === startModelId) {
        busyRef.current = false;
      }
    }
  }, [applyAction, syncCan, onError]);

  const redo = useCallback(async () => {
    // F-026-04: refuse to replay writes in a read-only session.
    if (readOnlyRef.current) return;
    if (redoStack.current.length === 0 || busyRef.current) return;
    busyRef.current = true;
    const action = redoStack.current.pop()!;
    const startModelId = prevModelIdRef.current;
    beginHistoryApply();
    try {
      await applyAction(action, false);
      if (prevModelIdRef.current !== startModelId) return; // model changed — discard
      // Redo re-adds this edit's revision delta to the CURRENT revision (the
      // inverse of undo above), preserving any interleaved out-of-band writes.
      if (action.revisionBefore !== undefined && action.revisionAfter !== undefined) {
        const delta = action.revisionAfter - action.revisionBefore;
        const store = useModelEditorStore.getState();
        store.setRevision(store.currentRevision + delta);
      }
      undoStack.current.push(action);
      syncCan();
    } catch (err) {
      if (prevModelIdRef.current !== startModelId) return; // model changed — discard
      // Apply failed. Restore the action to the redo stack and surface the
      // failure rather than letting the rejection escape (F-026-19).
      redoStack.current.push(action);
      syncCan();
      onError?.(err);
    } finally {
      endHistoryApply();
      if (prevModelIdRef.current === startModelId) {
        busyRef.current = false;
      }
    }
  }, [applyAction, syncCan, onError]);

  // Listen for actions dispatched from other components (rename, link add/delete)
  useEffect(() => {
    // F-026-04: a read-only session records no history — external rename/link
    // actions dispatched by other components must not enter the stack.
    if (readOnly) return;
    function handleCanvasAction(e: Event) {
      const detail = (e as CustomEvent<CanvasActionEvent>).detail;
      if (detail?.action) {
        pushAction(detail.action as HistoryAction);
      }
    }
    window.addEventListener("canvas-history-action", handleCanvasAction);
    return () =>
      window.removeEventListener("canvas-history-action", handleCanvasAction);
  }, [pushAction, readOnly]);

  useEffect(() => {
    // F-026-04: do not bind the Ctrl/Cmd+Z/Y shortcut in a read-only session,
    // so a leftover stack cannot be replayed by keyboard after a role flip.
    if (readOnly) return;
    function handleKeyDown(e: KeyboardEvent) {
      if (!(e.ctrlKey || e.metaKey)) return;
      // Bug-7408: reuse the shared editable-target guard so SELECT and
      // contenteditable targets are excluded too — the previous local guard
      // only covered INPUT/TEXTAREA and let Ctrl+Z hijack those controls.
      if (isEditableTarget(e.target)) return;
      if (e.key === "z" && !e.shiftKey) {
        e.preventDefault();
        undo();
      } else if (e.key === "y" || (e.key === "z" && e.shiftKey)) {
        e.preventDefault();
        redo();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [undo, redo, readOnly]);

  return { beginMove, endMove, recordMove, undo, redo, canUndo, canRedo };
}
