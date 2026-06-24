import { useCallback, useEffect, useRef, useState } from "react";
import type { Node } from "reactflow";
import type { QueryClient } from "@tanstack/react-query";
import type { CanvasLayout, JoinCreate } from "../../api/types";
import { joinsApi, modelTablesApi } from "../../api/client";

export type NodePositions = Record<string, { x: number; y: number }>;

/** Per-edge layout entries (waypoints, anchor sides/ratios, pathing). */
export type EdgeLayouts = NonNullable<CanvasLayout["edges"]>;

export type HistoryAction =
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
) {
  const undoStack = useRef<HistoryAction[]>([]);
  const redoStack = useRef<HistoryAction[]>([]);
  /** Positions captured at the start of an in-flight drag gesture. */
  const pendingBefore = useRef<NodePositions | null>(null);
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;
  const busyRef = useRef(false);

  const [canUndo, setCanUndo] = useState(false);
  const [canRedo, setCanRedo] = useState(false);

  const syncCan = useCallback(() => {
    setCanUndo(undoStack.current.length > 0);
    setCanRedo(redoStack.current.length > 0);
  }, []);

  const pushAction = useCallback(
    (action: HistoryAction) => {
      undoStack.current.push(action);
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
    async (action: HistoryAction, reverse: boolean) => {
      switch (action.type) {
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
    [projectId, modelId, setNodes, invalidateJoins, invalidateTables, onApplyMove, onApplyEdges],
  );

  const undo = useCallback(async () => {
    if (undoStack.current.length === 0 || busyRef.current) return;
    busyRef.current = true;
    const action = undoStack.current.pop()!;
    try {
      await applyAction(action, true);
      redoStack.current.push(action);
      syncCan();
    } catch (err) {
      // Apply failed (network / 4xx). Restore the action to the undo stack so
      // it is not silently dropped, and surface the failure (F-026-19).
      undoStack.current.push(action);
      syncCan();
      onError?.(err);
    } finally {
      busyRef.current = false;
    }
  }, [applyAction, syncCan, onError]);

  const redo = useCallback(async () => {
    if (redoStack.current.length === 0 || busyRef.current) return;
    busyRef.current = true;
    const action = redoStack.current.pop()!;
    try {
      await applyAction(action, false);
      undoStack.current.push(action);
      syncCan();
    } catch (err) {
      // Apply failed. Restore the action to the redo stack and surface the
      // failure rather than letting the rejection escape (F-026-19).
      redoStack.current.push(action);
      syncCan();
      onError?.(err);
    } finally {
      busyRef.current = false;
    }
  }, [applyAction, syncCan, onError]);

  // Listen for actions dispatched from other components (rename, link add/delete)
  useEffect(() => {
    function handleCanvasAction(e: Event) {
      const detail = (e as CustomEvent<CanvasActionEvent>).detail;
      if (detail?.action) {
        pushAction(detail.action as HistoryAction);
      }
    }
    window.addEventListener("canvas-history-action", handleCanvasAction);
    return () =>
      window.removeEventListener("canvas-history-action", handleCanvasAction);
  }, [pushAction]);

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (!(e.ctrlKey || e.metaKey)) return;
      if (
        (e.target as HTMLElement)?.tagName === "INPUT" ||
        (e.target as HTMLElement)?.tagName === "TEXTAREA"
      )
        return;
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
  }, [undo, redo]);

  return { beginMove, endMove, recordMove, undo, redo, canUndo, canRedo };
}
