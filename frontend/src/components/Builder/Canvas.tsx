/**
 * Canvas — ReactFlow ERD graph for model tables and joins.
 *
 * Layout:  fact tables in the centre, dimensions arranged radially around them.
 * Nodes:   ERDTableNode — shows header, scrollable column list, perimeter handles.
 * Edges:   CrowsFootEdge — IDEF1X / UML / diamond markers, with draggable
 *          midpoint and side anchors for manual routing.
 */
import { useEffect, useCallback, useMemo, useRef, useState } from "react";
import { Alert } from "@mui/material";
import { useT } from "../../i18n";
import ReactFlow, {
  Background,
  ConnectionLineType,
  Controls,
  ControlButton,
  ConnectionMode,
  MiniMap,
  Panel,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type EdgeMouseHandler,
  type Node,
  type NodeChange,
  type OnConnectStartParams,
} from "reactflow";
import "reactflow/dist/style.css";
import { useQueryClient } from "@tanstack/react-query";
import AccountTreeIcon from "@mui/icons-material/AccountTree";
import CameraAltIcon from "@mui/icons-material/CameraAlt";
import LinkIcon from "@mui/icons-material/Link";
import NoteIcon from "@mui/icons-material/StickyNote2Outlined";
import MapIcon from "@mui/icons-material/Map";
import CloseIcon from "@mui/icons-material/Close";
import CableIcon from "@mui/icons-material/Cable";
import SearchIcon from "@mui/icons-material/Search";
import UndoIcon from "@mui/icons-material/Undo";
import RedoIcon from "@mui/icons-material/Redo";
import type { CanvasLayout, ModelTable, Join } from "../../api/types";
import { modelsApi } from "../../api/client";
import { extractApiError } from "../../utils/extractApiError";
import { useBuilderStore } from "../../store/builderStore";
import { useModelEditorStore } from "../../store/useModelEditorStore";
import { useConfirm } from "../Confirm/useConfirm";
import { buildLayoutSnapshot } from "./layout/layoutSnapshot";
import { useCanvasLayout, type LayoutRunContext } from "./layout/useCanvasLayout";
import CanvasLayoutPanel, { type CanvasLayoutPreference } from "./layout/CanvasLayoutPanel";
import { movableIdsFor } from "./layout/movableSet";
// The same validator the worker applies to a snapshot's options, reused so a
// persisted preference is read back under exactly one rule.
import { defaultOptions } from "./layout/geometry";
import { dispositionFor } from "./layout/failurePolicy";
import { applyRouteLock } from "./layout/routeLock";
import { baseRatioForDroppedPoint, parallelOffsetFor } from "./layout/docking";
import { mergeEdgeEntry, mergeTableEntry } from "./layout/presentationEntries";
import { styleForTableEntry } from "./layout/tablePresentation";
import { displayedRouteFor, routePolyline } from "./layout/displayedRoutes";
import { changedCards, lockedEndpointIds, lockedRouteIntrusions, type LockedRouteGeometry } from "./layout/lockedRoutes";
import { type DisplayedRoute, isFreezableRoute } from "./edgeGeometry";
import type { AnchorSide, Rect } from "./layout/types";
import type { LayoutOperation, LayoutOptions as CanvasLayoutOptions, LayoutResult } from "./layout/types";
import {
  useAggregates,
  useDimensions,
  useHierarchiesWithLevels,
  useMeasures,
  usePersona,
  usePersonas,
  usePockets,
} from "../../api/hooks";
import PersonaPicker from "../Persona/PersonaPicker";
import ERDTableNode, {
  type ERDNodeData,
  type HierarchyGroupOnTable,
  type OverlayCounts,
  type SegmentationRefs,
} from "./ERDTableNode";
import CrowsFootEdge, { type CrowsFootEdgeData } from "./CrowsFootEdge";
import { countDroppedJoins, partitionJoinsByEndpoints } from "./joinFilter";
import { nodeHeader, nodeHeaderFallback, palette } from "../../theme/tokens";
import { classifyJoinEndpoints, isDimTableType, sameTypeWarningText } from "../../lib/joinRules";
import {
  computeDimmedTableIds,
  computeClsRestrictedObjectIds,
  summarizeDefaultFilters,
} from "./personaOverlay";
import { positionsFromNodes, useCanvasHistory, type EdgeLayouts, type NodePositions } from "./useCanvasHistory";

// Minimap hides on narrow viewports (mobile/small tablet).
const MINIMAP_MIN_VW = 900;

// Bug-7401: the model-annotations notes field has no backend length
// constraint (a plain Postgres TEXT column in the canvas layout blob) — this
// is a client-side UX guard against an unbounded, ever-growing layout JSON,
// not a mirror of a server-enforced limit.
const NOTES_MAX_LENGTH = 2000;

function minimapNodeColor(node: Node): string {
  const data = node.data as ERDNodeData | undefined;
  const ttype = data?.table?.table_type ?? "";
  const style = nodeHeader[ttype] ?? nodeHeaderFallback;
  return style.bg;
}

// ---------------------------------------------------------------------------
// Module-level constants — must not be recreated inside the component or
// ReactFlow will unmount/remount every node on every render.
// ---------------------------------------------------------------------------
const NODE_TYPES = { erdTable: ERDTableNode };
const EDGE_TYPES = { crowsFoot: CrowsFootEdge };

/**
 * Bug-8762: remove an edge's waypoint fields copy-on-write.
 *
 * `layoutRef` aliases the React Query cache object, so the two deletion paths
 * (handleResetEdge and onWaypointReset) must never delete keys from a cached
 * nested entry in place — a failed persistence PATCH would then leave the
 * client cache inconsistent with the server until reload. Both paths share
 * this primitive so the invariant lives in one place; the input `edges` object
 * and its entries are never mutated.
 */
export function clearEdgeWaypoints(
  edges: EdgeLayouts | undefined,
  joinId: string,
): EdgeLayouts {
  const next = { ...(edges ?? {}) };
  const entry = next[joinId];
  if (entry) {
    next[joinId] = { ...entry };
    delete next[joinId].waypoint;
    delete next[joinId].waypoints;
    if (Object.keys(next[joinId]).length === 0) delete next[joinId];
  }
  return next;
}

// ---------------------------------------------------------------------------

interface SideEndpoint {
  edgeId: string;
  endpoint: "source" | "target";
  sortCoord: number;
}

type LayoutPreset = "radial" | "hierarchical" | "compact";

/**
 * Initial placement of genuinely new tables waits for React Flow to measure the
 * cards, because placing from placeholder sizes is exactly what the layout spec
 * forbids. The wait is bounded: if measurement never arrives the tables stay
 * where they are and the user can press an Arrange action, rather than the
 * canvas inventing positions from placeholder geometry.
 */
const INITIAL_PLACEMENT_RETRY_MS = 150;
const INITIAL_PLACEMENT_MAX_ATTEMPTS = 20;

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------
interface Props {
  projectId: string;
  modelId: string;
  tenantSlug?: string;
  projectSlug?: string;
  modelSlug?: string;
  versionNumber?: number | null;
  tables: ModelTable[];
  joins: Join[];
  canvasLayout?: CanvasLayout;
  readOnly?: boolean;
  /**
   * Hands the page shell the canvas zoom/fit controls once the ReactFlow
   * instance is ready, so the global keyboard shortcuts (+ / - / 0) can drive
   * the canvas the user is looking at (F-026-08). Passing null on unmount lets
   * the caller drop the stale handlers.
   */
  onViewControlsReady?: (
    controls: { zoomIn: () => void; zoomOut: () => void; fitView: () => void } | null,
  ) => void;
}

export default function Canvas({ projectId, modelId, tenantSlug, projectSlug, modelSlug, versionNumber, tables, joins, canvasLayout, readOnly = false, onViewControlsReady }: Props) {
  const t = useT();
  // useT() returns a fresh closure on every render (Bug-1010). Keep the latest
  // translator in a ref so the debounced/unmount layout-save callbacks can read
  // it WITHOUT listing `t` in their dependency arrays. Listing `t` gave
  // `flushLayout` a new identity every render, which re-ran the node/edge
  // hydrate effect (it depends on `flushLayout`) unconditionally — an unbounded
  // render loop that, after any layout edit, degenerated into a continuous
  // PATCH storm (Bug-6373 / F-026-01).
  const tRef = useRef(t);
  useEffect(() => {
    tRef.current = t;
  }, [t]);
  // Bug-8504: `flushLayout` is the single primitive every canvas-layout write
  // funnels through (node moves, resizes, notes, edge waypoint reset, edge
  // pathing toggle, the Save dialog's layout-only flush, and the unmount
  // flush). Read-only was previously enforced per call site, so the two
  // JoinsPanel-dispatched edge controls persisted layout for a read-only share
  // link. The guard belongs on the primitive so every present and future
  // caller inherits it. It is held in a ref, not a dependency, because listing
  // it on `flushLayout` would churn that callback's identity — the exact
  // PATCH-storm regression Bug-6373 fixed.
  const readOnlyRef = useRef(readOnly);
  readOnlyRef.current = readOnly;
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const liveNodesRef = useRef<Node[]>([]);
  useEffect(() => {
    liveNodesRef.current = nodes;
  }, [nodes]);
  // The locked-route guard runs in a post-commit effect, after the gesture, so
  // it reads the edges through a ref rather than closing over a render's copy.
  const liveEdgesRef = useRef<Edge[]>([]);
  useEffect(() => {
    liveEdgesRef.current = edges;
  }, [edges]);

  // Hold the publish callback in a ref so the "drop controls on unmount" effect
  // below is NOT keyed on the callback's identity. The parent passes an inline
  // arrow, so its identity changes on every parent re-render; keying the
  // cleanup effect on it re-ran the cleanup after the first re-render and
  // nulled the live controls — which are only re-published in ReactFlow's
  // one-shot onInit — leaving the +/-/0 keyboard shortcuts dead (Bug-6375).
  const onViewControlsReadyRef = useRef(onViewControlsReady);
  useEffect(() => {
    onViewControlsReadyRef.current = onViewControlsReady;
  }, [onViewControlsReady]);

  // Drop the published view controls when this canvas unmounts so the page
  // shell doesn't drive a stale ReactFlow instance (F-026-08). Runs only on
  // real mount/unmount now that it no longer depends on the callback identity.
  useEffect(() => {
    return () => onViewControlsReadyRef.current?.(null);
  }, []);
  const queryClient = useQueryClient();
  const hiddenJoinCount = useMemo(
    () => countDroppedJoins(joins, new Set(tables.map((table) => table.id))),
    [joins, tables],
  );

  const isConnectingMode = useBuilderStore((s) => s.isConnectingMode);
  const relationPathing = useBuilderStore((s) => s.relationPathing);
  // Read reactively (not through getState) because the lock state is resolved
  // during render: the marker extents decide where the frozen heels sit.
  const relationNotation = useBuilderStore((s) => s.relationNotation);
  const relationTerminalOverrides = useBuilderStore((s) => s.relationTerminalOverrides);

  // ---- 8.B.7 Persona preview overlay state --------------------------
  // Canvas-local selection — not persisted.  Picker auto-hides when no
  // personas exist for the current audience role.
  const [personaId, setPersonaId] = useState<string | null>(null);
  const personas = usePersonas(projectId, modelId);
  const persona = usePersona(projectId, modelId, personaId ?? "");

  // Viewport-width-driven minimap visibility.
  const [showMinimap, setShowMinimap] = useState(
    typeof window === "undefined" ? true : window.innerWidth >= MINIMAP_MIN_VW,
  );
  useEffect(() => {
    if (typeof window === "undefined") return;
    const handle = () => setShowMinimap(window.innerWidth >= MINIMAP_MIN_VW);
    window.addEventListener("resize", handle);
    return () => window.removeEventListener("resize", handle);
  }, []);

  /**
   * Two canvas view controls, both deliberately session-only.
   *
   * Neither is persisted and neither is seeded from the saved layout: opening a
   * model always starts with the Joins drawer enabled and the minimap shown.
   * They exist to get something out of the way for a minute, not to express a
   * preference — a hidden setting that survived a reload would leave the
   * modeller wondering why the canvas behaves differently from a colleague's.
   */
  const [joinsDrawerSuppressed, setJoinsDrawerSuppressed] = useState(false);
  const [minimapDismissed, setMinimapDismissed] = useState(false);
  // Every model change returns both to their defaults.
  useEffect(() => {
    setJoinsDrawerSuppressed(false);
    setMinimapDismissed(false);
  }, [projectId, modelId]);
  const minimapVisible = showMinimap && !minimapDismissed;
  const selectObject      = useBuilderStore((s) => s.selectObject);
  const openPanel         = useBuilderStore((s) => s.openPanel);
  const closePanel        = useBuilderStore((s) => s.closePanel);
  const activePanel       = useBuilderStore((s) => s.activePanel);
  const setPendingJoin    = useBuilderStore((s) => s.setPendingJoin);
  const setGlobalMessage  = useBuilderStore((s) => s.setGlobalMessage);
  const isDirty            = useModelEditorStore((s) => s.isDirty);
  const confirm            = useConfirm();

  // Mirror of the persisted canvas layout. Drag stops and edge edits merge
  // into this and are debounced-flushed to the backend so we don't spam
  // PATCH requests while the user is dragging.
  const layoutRef = useRef<CanvasLayout>(canvasLayout ?? {});
  const flushTimer = useRef<number | null>(null);
  const reactFlowInstance = useRef<any>(null);

  // Once the user has made any local edit, stop syncing layoutRef from the
  // server-side canvasLayout prop. Background React Query refetches would
  // otherwise pull a stale snapshot before our debounced flush completes,
  // reverting the user's drag-in-progress.
  const userDirtyRef = useRef(false);

  // Bug-7634: track the previous modelId so we can reset per-model canvas
  // state (layoutRef, userDirty, notes, pending flush) on in-place model
  // navigation. The actual reset effect is below, after the notesText state
  // declaration, so it can call setNotesText.
  const prevModelIdRef = useRef(modelId);

  useEffect(() => {
    if (userDirtyRef.current) return;
    layoutRef.current = canvasLayout ?? {};
  }, [canvasLayout]);

  /**
   * Bug-8504: THE single funnel for every canvas-layout write — the debounced
   * flush, the unmount drain, and the Save dialog's immediate flush all go
   * through here. Read-only used to be enforced per call site, which is how
   * the two JoinsPanel edge controls (reset waypoints / toggle pathing) kept
   * persisting layout for a read-only share-link session while the node-resize
   * path was guarded. A funnel means a caller cannot forget the check, and any
   * future write path inherits it.
   *
   * WHY THE FUNNEL IS NOT SUFFICIENT ON ITS OWN
   * -------------------------------------------
   * It stops the PATCH, not the EDIT. Every writer below merges into
   * `layoutRef.current` BEFORE reaching here, and `layoutRef` outlives the
   * read-only state: `readOnly` is reactive (`ModelBuilder` derives it from
   * `caller_can_author` and from `?readonly=1`), so it can flip on the SAME
   * Canvas mount and an unauthorised edit then rides the next legitimate
   * flush. Each writer therefore takes its own `readOnlyRef` entry guard.
   *
   * ENUMERATION OF EVERY `layoutRef.current =` WRITE SITE — keep this table in
   * step with `grep -n "layoutRef.current *=" Canvas.tsx`. Two review rounds
   * derived the guard set from the reported symptom instead of from this grep
   * and both times missed a writer (`handleSaveNotes`, `handleRedrawLayout` —
   * the second one destructive), which is exactly the enumeration blind spot
   * CLAUDE.md names as a first-class finding category. Diff the table, do not
   * re-derive it from whatever the current bug report happens to mention.
   *
   *   server hydrate / model-change reset  no guard — not a user edit
   *   applyMovePositions                   guarded
   *   applyEdgeLayouts                     guarded
   *   applyTableLayouts                    guarded
   *   restoreLayoutState                   guarded — the gesture rollback
   *   handleRepairRoutes (keep-geometry)   guarded via handleRepairRoutes'
   *     own read-only entry, taken when the engine never rendered a verdict
   *   handleTogglePin                      guarded
   *   toggleRouteLockFor                   guarded — takes the relationship
   *     explicitly, so both the layout panel and the Joins panel reach one writer
   *   persistLayoutPreferences             guarded
   *   handleRedrawLayout                   guarded
   *   handleApplyLayout                    guarded — Reroute Links returns
   *     overridden relationships to the model-wide Edge Pathing setting
   *   handleSaveNotes                      guarded
   *   commitRouteEdit                      guarded — the single lock-aware,
   *     history-recording writer for every route-only edit: Reset Path and
   *     Toggle Path Style from the Joins panel, and bend edit/reset from the
   *     connector itself. Two of those four used to bypass the route lock
   *   handleNodeResize                     guarded (Bug-7636)
   *   handleNodesChange                    guarded
   *   commitAttachmentChange                   guarded — the single writer both
   *     attachment-drag callbacks delegate to, so the effective-to-base ratio
   *     conversion and the route repair happen once rather than in two copies
   *   hydrate auto-placement of unplaced nodes — NO LONGER A WRITE SITE. The
   *     canvas stopped computing initial placement on the render thread: the
   *     spec requires it to wait for measured card geometry and to run off the
   *     UI thread, so hydration only records which tables are new and the
   *     positions are written by the guarded `handleRedrawLayout` writer below
   *     when the worker batch is applied. The row is kept as a pointer because a
   *     reader diffing this table against history would otherwise conclude the
   *     site was lost rather than relocated.
   */
  /**
   * A save may not carry geometry the engine has not accepted.
   *
   * A gesture writes its result into `layoutRef.current` immediately, because
   * the canvas has to draw it. But that same object is what every save path
   * sends to the server, so between the gesture and the end of route repair the
   * canvas was one debounce away from persisting CANDIDATE tables next to the
   * OLD routes — a saved diagram whose cards and connectors disagree. Not
   * scheduling a new save is not the same as isolating an uncommitted edit: a
   * debounce from the previous action, an explicit layout-only Save, or the
   * unmount flush could all still fire.
   *
   * So a save that lands mid-transaction is DEFERRED, not downgraded. Sending
   * the last accepted geometry instead would be safe but would quietly drop the
   * user's newest edit; waiting sends the whole, coherent state a moment later.
   * The wait is bounded because every exit from a repair settles the candidate,
   * and the worker itself has a hard timeout.
   *
   * `candidateHeldRef` false means the live object is accepted and savable.
   */
  const candidateHeldRef = useRef(false);
  const deferredSaveRef = useRef<Array<() => void>>([]);

  const persistLayoutNow = useCallback((): Promise<void> => {
    return modelsApi
      .update(projectId, modelId, { canvas_layout: layoutRef.current })
      .then(() => {});
  }, [projectId, modelId]);
  const persistLayoutNowRef = useRef(persistLayoutNow);
  persistLayoutNowRef.current = persistLayoutNow;

  /** Hold saves: the live geometry is a candidate the engine has not judged. */
  const beginCandidateGeometry = useCallback(() => {
    candidateHeldRef.current = true;
  }, []);

  /**
   * The live geometry is accepted. Release any save that arrived while it was
   * held, so a deferred Save Now is honoured rather than silently dropped.
   *
   * Called on EVERY exit from a repair transaction — committed, restored, kept
   * because the engine never answered, abandoned, or refused before it started.
   * An exit that forgets to settle would stop the canvas saving for the rest of
   * the session, which is worse than the unsafe save this prevents.
   */
  const acceptGeometry = useCallback(() => {
    candidateHeldRef.current = false;
    const waiting = deferredSaveRef.current;
    if (!waiting.length) return;
    deferredSaveRef.current = [];
    void persistLayoutNowRef.current()
      .catch(() => {})
      .finally(() => {
        for (const resolve of waiting) resolve();
      });
  }, []);

  const persistLayout = useCallback((): Promise<void> => {
    if (readOnlyRef.current) return Promise.resolve();
    userDirtyRef.current = true;
    if (candidateHeldRef.current) {
      // Wait for the transaction to settle, then save the complete state.
      return new Promise<void>((resolve) => {
        deferredSaveRef.current.push(resolve);
      });
    }
    return persistLayoutNow();
  }, [persistLayoutNow]);

  const flushLayout = useCallback(() => {
    // Read-only sessions also skip marking the layout dirty — a dirty flag
    // would suppress rehydration from the server copy for no benefit, since
    // nothing will ever be written.
    if (readOnlyRef.current) return;
    userDirtyRef.current = true;
    if (flushTimer.current !== null) {
      window.clearTimeout(flushTimer.current);
    }
    flushTimer.current = window.setTimeout(() => {
      flushTimer.current = null;
      persistLayout().catch(() => {
        setGlobalMessage(
          tRef.current("builder.layoutSaveFailed"),
          "warning",
        );
      });
    }, 600);
  }, [persistLayout, setGlobalMessage]);

  useEffect(() => {
    // Flush any pending layout update if the component unmounts mid-debounce.
    return () => {
      if (flushTimer.current !== null) {
        window.clearTimeout(flushTimer.current);
        // Null the handle after clearing so a re-fired cleanup cannot re-PATCH
        // the same pending layout again (Bug-6373).
        flushTimer.current = null;
        persistLayout().catch(() => {
          setGlobalMessage(tRef.current("builder.layoutSaveFailed"), "warning");
        });
      }
    };
  }, [persistLayout, setGlobalMessage]);

  // Register an immediate (non-debounced) layout flush in the builderStore
  // so the Save dialog's "layout only" mode can persist without waiting for
  // the 600ms debounce or creating a model version.
  const setFlushCanvasLayoutNow = useBuilderStore((s) => s.setFlushCanvasLayoutNow);
  useEffect(() => {
    const flushNow = (): Promise<void> => {
      if (flushTimer.current !== null) {
        window.clearTimeout(flushTimer.current);
        flushTimer.current = null;
      }
      return persistLayout();
    };
    setFlushCanvasLayoutNow(flushNow);
    return () => setFlushCanvasLayoutNow(null);
  }, [persistLayout, setFlushCanvasLayoutNow]);

  const [layoutMenuOpen, setLayoutMenuOpen] = useState(false);

  // Undo/redo writes positions through here so they hit the same
  // canvas_layout persistence path as a normal drag (F-026-02).
  const applyMovePositions = useCallback(
    (positions: NodePositions) => {
      // R1 review: undo/redo is a second caller of this layout write path and
      // its keyboard shortcut is registered unconditionally, so it survives a
      // false -> true read-only flip on the same Canvas mount. `flushLayout`
      // already refuses to persist, but the guard belongs at the entry so no
      // orphaned edit is left in `layoutRef` either.
      if (readOnlyRef.current) return;
      const tableMap = { ...(layoutRef.current.tables ?? {}) };
      for (const [id, p] of Object.entries(positions)) {
        tableMap[id] = mergeTableEntry(tableMap[id], { x: p.x, y: p.y });
      }
      layoutRef.current = { ...layoutRef.current, tables: tableMap };
      flushLayout();
    },
    [flushLayout],
  );

  // Undo/redo of a layout-preset redraw restores the full edge-layout
  // snapshot (waypoints, anchor sides/ratios, pathing) through here so manual
  // edge routing survives an undo, not just table positions (F-026-11 round 2).
  const applyEdgeLayouts = useCallback(
    (edges: NonNullable<CanvasLayout["edges"]>) => {
      if (readOnlyRef.current) return; // same undo/redo entry point as above
      const next = JSON.parse(JSON.stringify(edges)) as NonNullable<CanvasLayout["edges"]>;
      layoutRef.current = { ...layoutRef.current, edges: next };
      flushLayout();
      setEdges((es) =>
        es.map((e) => {
          const entry = next[e.id];
          return {
            ...e,
            data: {
              ...e.data,
              waypoint: entry?.waypoint,
              waypoints: entry?.waypoints,
              sourceSide: entry?.sourceSide,
              targetSide: entry?.targetSide,
              sourceRatio: entry?.sourceRatio,
              targetRatio: entry?.targetRatio,
              pathing: entry?.pathing,
              routeMode: entry?.routeMode,
              // The lock is part of the edge layout: an undo that restored the
              // path but not the lock would leave a frozen route unfrozen, or
              // freeze one the user had released.
              locked: entry?.locked === true,
              lockedParallelOffset: entry?.lockedParallelOffset,
            },
          };
        }),
      );
    },
    [flushLayout, setEdges],
  );

  /**
   * Undo/redo of a change that alters a table's presentation without moving it
   * — pinning — restores the full table-layout snapshot through here, so the
   * pin comes back with the same persistence path a drag uses.
   */
  const applyTableLayouts = useCallback(
    (tables: NonNullable<CanvasLayout["tables"]>) => {
      if (readOnlyRef.current) return; // same undo/redo entry point as above
      const next = JSON.parse(JSON.stringify(tables)) as NonNullable<CanvasLayout["tables"]>;
      layoutRef.current = { ...layoutRef.current, tables: next };
      flushLayout();
      setNodes((ns) =>
        ns.map((node) => {
          const entry = next[node.id];
          // Previously this compared the pin and returned the node UNCHANGED
          // when the pin had not moved — so undoing a resize wrote the old
          // height into the saved layout while the card stayed at its new size
          // on screen, with the restored connectors drawn around the wrong
          // rectangle. The card's geometry is part of the state being restored.
          return {
            ...node,
            style: styleForTableEntry(node.style as Record<string, unknown> | undefined, entry),
            data: { ...node.data, pinned: entry?.pinned === true },
          };
        }),
      );
    },
    [flushLayout, setNodes],
  );

  const { recordMove, undo, redo, canUndo, canRedo } =
    useCanvasHistory(
      nodes,
      setNodes,
      projectId,
      modelId,
      queryClient,
      applyMovePositions,
      applyEdgeLayouts,
      applyTableLayouts,
      // F-026-19: an undo/redo whose join create/delete fails (network/4xx)
      // restores the action to its stack; surface the cause instead of letting
      // the rejection escape the onClick handler.
      useCallback(
        (err: unknown) =>
          setGlobalMessage(
            extractApiError(err, t("modelBuilder.undoFailed")),
            "error",
          ),
        [setGlobalMessage, t],
      ),
      // F-026-04: gate the history boundary on the same read-only authority the
      // canvas write paths already honour.
      readOnly,
    );
  const isDraggingRef = useRef(false);
  /** One refusal message per gesture, not one per pointer-move frame (R06). */
  const frozenMoveNoticeRef = useRef(false);

  // ---- Coordinated placement and routing (P1) ------------------------------
  //
  // Placement and coordinated orthogonal routing run in a module worker. The
  // canvas only decides *when* to ask and whether a returned result is still
  // current: `useCanvasLayout` refuses any result computed before a
  // layout-relevant edit (drag, resize, join change, manual bend, undo, model
  // switch) and refuses everything in a read-only session.
  /**
   * Everything one gesture or action owns, captured before it starts.
   *
   * `tables` is here as well as `positions` because a resize changes width and
   * height, which a position map cannot carry: an undo that restored the old
   * connector while leaving the new card size recreated the very attachment
   * mismatch the repair had just fixed. Pins live in the same map, so they are
   * restored by the same snapshot.
   */
  type LayoutTransactionState = {
    positions: Record<string, { x: number; y: number }>;
    /**
     * Measured card rectangles at the start of the gesture.
     *
     * Positions alone cannot describe a resize: growing a card from its bottom
     * or right edge leaves x and y untouched. The locked-route guard compared
     * positions, so a card that grew straight across a frozen connector was not
     * even considered a changed card — the one obstruction a locked route
     * cannot route around, because it is not allowed to move.
     */
    rects: Map<string, Rect>;
    tables: NonNullable<CanvasLayout["tables"]>;
    edges: NonNullable<CanvasLayout["edges"]>;
  };
  const pendingLayoutBeforeRef = useRef<LayoutTransactionState | null>(null);
  /**
   * Pre/post-gesture state captured at drag start/end so the deferred repair
   * can record ONE undo entry carrying the position change, the card sizes and
   * the repaired routes together (F05). The repair itself runs in a post-commit
   * effect because the synchronous gesture handler still holds pre-commit nodes.
   *
   * The transaction carries an IDENTITY. Without one, a gesture finishing after
   * a newer gesture started would clear the newer one's captured state, and the
   * newer gesture would then apply with no history record of its own. Only the
   * transaction that is still live may commit, restore or clear.
   */
  const gestureSeqRef = useRef(0);
  const liveGestureRef = useRef(0);

  /**
   * Ownership of an asynchronous canvas operation.
   *
   * A gesture counter alone is not ownership. The counter says "no newer
   * gesture started"; it says nothing about WHICH MODEL is on the canvas. The
   * builder renders Canvas without a model-specific key, so navigating to an
   * already-cached model reuses this mount: a repair started on model A could
   * finish after the shared refs had been refilled with model B, then write
   * B's presentation to A's endpoint and push an A-before/B-after entry into
   * B's history.
   *
   * So an operation owns the canvas only while all four still hold: the same
   * project, the same model, the same mount (epoch), and the same gesture. An
   * operation that has lost ownership must do NOTHING — not apply, not
   * restore, not record history, not persist, not raise a message.
   */
  const epochRef = useRef(0);
  const ownershipRef = useRef({ projectId, modelId });
  if (ownershipRef.current.projectId !== projectId || ownershipRef.current.modelId !== modelId) {
    ownershipRef.current = { projectId, modelId };
    epochRef.current += 1;
    // Any capture belonging to the previous model is now unreachable state.
    liveGestureRef.current = ++gestureSeqRef.current;
    // Including any candidate hold: it belongs to the previous model, and
    // leaving it set would stop the NEW model saving at all.
    candidateHeldRef.current = false;
    deferredSaveRef.current = [];
  }
  useEffect(() => () => {
    // Unmount retires every pending operation the same way a model change does.
    epochRef.current += 1;
  }, []);

  type CanvasOperationToken = { projectId: string; modelId: string; epoch: number; gesture: number };
  const claimCanvas = useCallback(
    (): CanvasOperationToken => ({ projectId, modelId, epoch: epochRef.current, gesture: liveGestureRef.current }),
    [projectId, modelId],
  );
  const stillOwnsCanvas = useCallback(
    (token: CanvasOperationToken): boolean =>
      token.projectId === ownershipRef.current.projectId &&
      token.modelId === ownershipRef.current.modelId &&
      token.epoch === epochRef.current &&
      token.gesture === liveGestureRef.current,
    [],
  );
  const pendingRepairBeforeRef = useRef<LayoutTransactionState | null>(null);
  const pendingRepairAfterRef = useRef<Record<string, { x: number; y: number }> | null>(null);
  const repairPendingRef = useRef(false);
  const [repairTick, setRepairTick] = useState(0);
  // `tableTypeMap` is derived later in this component. The snapshot builder only
  // runs on an explicit layout action, so it reads the latest map through this
  // ref instead of depending on a value declared further down.
  const tableTypeMapRef = useRef<Map<string, string>>(new Map());
  /** Tables hydration found genuinely new, awaiting a measured worker placement. */
  /**
   * Panel preferences, seeded from the saved layout (spec §5 `layoutOptions`).
   *
   * `defaultOptions` is the worker's own validator, reused rather than
   * reimplemented: an absent field, an unknown preset from a newer build, or a
   * hand-edited value all fall back to the same default the engine would apply,
   * so the panel can never show a preference the engine would not honour.
   */
  const [layoutPreferences, setLayoutPreferences] = useState<CanvasLayoutPreference>(
    () => defaultOptions(canvasLayout?.layoutOptions),
  );
  /**
   * The user has chosen a preference in this session, so a late-arriving server
   * payload must not overwrite it. Same hazard the notes text guards against:
   * a background refetch resolving after the choice would otherwise revert it.
   */
  const layoutPreferencesDirtyRef = useRef(false);

  useEffect(() => {
    if (layoutPreferencesDirtyRef.current) return;
    const saved = defaultOptions(canvasLayout?.layoutOptions);
    // Bail out on an unchanged value rather than setting a fresh object every
    // time the query cache hands back a new `canvasLayout` identity — this
    // canvas has a history of render loops fed by exactly that (Bug-6373).
    setLayoutPreferences((current) =>
      current.preset === saved.preset &&
      current.direction === saved.direction &&
      current.spacing === saved.spacing
        ? current
        : saved,
    );
  }, [canvasLayout]);

  /**
   * Persist the preferences an arrangement actually succeeded with (spec §5:
   * "last successfully applied panel options").
   *
   * Deliberately not called when a preference is merely chosen: a preset the
   * engine refused, or a direction picked and never used, is not an applied
   * option, and persisting it would make the next reload open with an
   * arrangement that was never drawn.
   */
  const persistLayoutPreferences = useCallback(
    (options: CanvasLayoutPreference) => {
      if (readOnlyRef.current) return;
      layoutRef.current = { ...layoutRef.current, layoutOptions: { ...options } };
      flushLayout();
    },
    [flushLayout],
  );
  /**
   * How many selected tables an `arrange-selected` batch would actually move.
   *
   * Resolved through the worker's own movable-table rule rather than by counting
   * selected nodes, because a pinned table and the endpoints of a locked
   * relationship are protected: counting the selection alone would enable the
   * control for a batch the worker then refuses with "select at least one
   * movable table".
   */
  const movableSelectedCount = useMemo(
    () =>
      movableIdsFor(
        nodes.map((node) => ({
          id: node.id,
          pinned: (node.data as { pinned?: boolean } | undefined)?.pinned === true,
          fixed: false,
          selected: node.selected === true,
        })),
        edges.map((edge) => ({
          source: edge.source,
          target: edge.target,
          locked: (edge.data as CrowsFootEdgeData | undefined)?.locked === true,
        })),
        "arrange-selected",
      ).size,
    [nodes, edges],
  );
  /**
   * The selected relationship's route, and whether a lock could freeze it (R06).
   *
   * A lock persists the exact displayed polyline and stops anything from
   * recomputing it, so it is only offered for a route that is actually
   * drawable. The geometry comes from `resolveDisplayedRoute` — the same
   * resolution the edge renderer uses — so what a lock freezes is what the user
   * sees, not a second opinion about where the route ought to go.
   */
  const routeContext = useMemo(
    () => ({
      globalPathing: relationPathing,
      notation: relationNotation,
      terminalOverrides: relationTerminalOverrides,
    }),
    [relationPathing, relationNotation, relationTerminalOverrides],
  );
  const routeContextRef = useRef(routeContext);
  routeContextRef.current = routeContext;

  /**
   * Live card rectangles: positions from the node state, sizes from React
   * Flow's measurement. An unmeasured card is absent rather than guessed, the
   * same split `buildWorkerSnapshot` uses.
   */
  const liveCardRects = useCallback((): Map<string, Rect> => {
    const measured = new Map<string, { w: number; h: number }>();
    for (const node of reactFlowInstance.current?.getNodes() ?? []) {
      const w = typeof node.width === "number" ? node.width : undefined;
      const h = typeof node.height === "number" ? node.height : undefined;
      if (w !== undefined && h !== undefined && w > 0 && h > 0) measured.set(node.id, { w, h });
    }
    const rects = new Map<string, Rect>();
    for (const node of liveNodesRef.current) {
      const size = measured.get(node.id);
      if (!size) continue;
      rects.set(node.id, { x: node.position.x, y: node.position.y, width: size.w, height: size.h });
    }
    return rects;
  }, []);

  /** Cards a locked relationship docks to. They must not move at all (R06). */
  const lockedEndpoints = useMemo(
    () =>
      lockedEndpointIds(
        edges.map((edge) => ({
          id: edge.id,
          source: edge.source,
          target: edge.target,
          locked: (edge.data as CrowsFootEdgeData | undefined)?.locked === true,
        })),
      ),
    [edges],
  );
  const lockedEndpointsRef = useRef(lockedEndpoints);
  lockedEndpointsRef.current = lockedEndpoints;

  /** Both end cards of the selected relationship, highlighted together (R09). */
  const joinHighlightIds = useMemo(() => {
    const ids = new Set<string>();
    for (const edge of edges) {
      if (edge.selected !== true) continue;
      ids.add(edge.source);
      ids.add(edge.target);
    }
    return ids;
  }, [edges]);

  // Both per-card flags are written by one pass: whether a locked relationship
  // is holding the card (so it withdraws its resize handles rather than resize
  // and snap back), and whether it is an endpoint of the selected relationship.
  // The updater returns the same array when nothing changed, so React Flow
  // bails out and this cannot become a render loop (Bug-6373).
  useEffect(() => {
    setNodes((ns) => {
      let changed = false;
      const next = ns.map((node) => {
        const frozen = lockedEndpoints.has(node.id);
        const highlighted = joinHighlightIds.has(node.id);
        const data = node.data as { lockedByRoute?: boolean; joinHighlighted?: boolean } | undefined;
        if ((data?.lockedByRoute === true) === frozen && (data?.joinHighlighted === true) === highlighted) {
          return node;
        }
        changed = true;
        return { ...node, data: { ...node.data, lockedByRoute: frozen, joinHighlighted: highlighted } };
      });
      return changed ? next : ns;
    });
  }, [lockedEndpoints, joinHighlightIds, setNodes]);

  type RouteLockInfo = {
    id: string;
    locked: boolean;
    capture: DisplayedRoute | null;
    routeMode: "auto" | "manual";
    /** The path mode this relationship is currently DRAWN with. */
    resolvedPathing: "orthogonal" | "straight";
    /** The parallel fan-out it is currently drawn with. */
    parallelOffset: number;
  };

  /**
   * Everything locking or unlocking one relationship needs to know.
   *
   * Taken for an explicit relationship rather than "whichever is selected", so
   * the Joins panel can offer the same action. Selecting a relationship opens
   * that drawer over the layout panel, which left Lock Route reachable only by
   * pressing Escape first (Bug-10034).
   */
  const routeLockInfoFor = useCallback((edge: Edge): RouteLockInfo => {
    const data = (edge.data ?? {}) as CrowsFootEdgeData;

    const live = liveCardRects();
    const locked = data.locked === true;

    // The fan-out this relationship is drawn with right now. Freezing it is
    // what stops a relationship added later between the same two cards from
    // moving a frozen attachment.
    const parallelOffset =
      data.lockedParallelOffset ?? parallelOffsetFor(data.offsetIndex ?? 0, data.totalEdges ?? 1);

    const resolved = displayedRouteFor(edge, live, routeContext);
    if (!resolved) {
      return {
        id: edge.id, locked, capture: null, routeMode: "auto",
        resolvedPathing: data.pathing ?? routeContext.globalPathing, parallelOffset,
      };
    }

    return {
      id: edge.id,
      locked,
      capture: isFreezableRoute(resolved.route, resolved.pathMode) ? resolved.route : null,
      routeMode: resolved.routeMode,
      // The RESOLVED mode, not the stored one: a route with no explicit
      // `pathing` inherits the model setting, and that inherited mode is what
      // the lock has to freeze — otherwise changing the model setting later
      // discards the bends the lock just froze.
      resolvedPathing: resolved.pathMode,
      parallelOffset,
    };
  }, [liveCardRects, routeContext]);

  const selectedRouteLock = useMemo((): RouteLockInfo | null => {
    const selected = edges.filter((edge) => edge.selected === true);
    // One relationship, or the lock state would be ambiguous.
    if (selected.length !== 1) return null;
    return routeLockInfoFor(selected[0]!);
  }, [edges, nodes, routeLockInfoFor]);

  const routeLockState: "none" | "invalid" | "locked" | "unlocked" =
    !selectedRouteLock
      ? "none"
      : selectedRouteLock.locked
        ? "locked"
        : selectedRouteLock.capture
          ? "unlocked"
          : "invalid";

  /**
   * Freeze or release the selected relationship's route (R06).
   *
   * Locking writes the displayed geometry down — sides, base ratios and bends —
   * because an automatic route has no stored path of its own: without the
   * capture, reopening the model would recompute a different one and the lock
   * would have frozen nothing. Provenance is preserved rather than rewritten:
   * capturing an engine route does not make it the user's manual edit.
   *
   * Unlocking clears only the lock. It never discards the path (spec: no action
   * discards a locked path) and never unpins a table — lock and pin are
   * independent.
   *
   * Both directions are one undo entry carrying the complete before/after edge
   * layout, the same entry shape a redraw records.
   */
  /**
   * Lock or unlock ONE relationship's route.
   *
   * Takes the relationship explicitly. An earlier version made the argument
   * optional and fell back to the selection — which quietly broke the panel
   * button, because `onClick={handler}` hands React's MouseEvent to the first
   * parameter. The event is truthy, so it was treated as the relationship, had
   * no `capture`, and the handler returned without locking anything. Nothing
   * failed; the button simply stopped working. An optional parameter on
   * anything wired to an event handler is a trap.
   */
  const toggleRouteLockFor = useCallback((selection: RouteLockInfo | null) => {
    if (readOnlyRef.current) return;
    if (!selection) return;
    const nextLocked = !selection.locked;
    if (nextLocked && !selection.capture) return;

    const before = JSON.parse(JSON.stringify(layoutRef.current.edges ?? {})) as NonNullable<CanvasLayout["edges"]>;
    const entry = applyRouteLock({
      current: layoutRef.current.edges?.[selection.id],
      locked: nextLocked,
      capture: selection.capture,
      routeMode: selection.routeMode,
      resolvedPathing: selection.resolvedPathing,
      parallelOffset: selection.parallelOffset,
    });
    const nextEdges = { ...(layoutRef.current.edges ?? {}), [selection.id]: entry };
    layoutRef.current = { ...layoutRef.current, edges: nextEdges };
    flushLayout();

    setEdges((es) =>
      es.map((edge) =>
        edge.id === selection.id
          ? {
              ...edge,
              data: {
                ...edge.data,
                // The rendered edge reads the same entry the layout persists,
                // so the drawn route and the saved route cannot diverge.
                locked: entry.locked === true,
                sourceSide: entry.sourceSide,
                targetSide: entry.targetSide,
                sourceRatio: entry.sourceRatio,
                targetRatio: entry.targetRatio,
                waypoint: entry.waypoint,
                waypoints: entry.waypoints,
                routeMode: entry.routeMode,
              },
            }
          : edge,
      ),
    );

    const positions = positionsFromNodes(nodes);
    recordMove(positions, positions, {
      before,
      after: JSON.parse(JSON.stringify(nextEdges)) as NonNullable<CanvasLayout["edges"]>,
    });
  }, [flushLayout, setEdges, recordMove, nodes]);

  /** The layout panel's control, which acts on the selected relationship. */
  const handleToggleRouteLock = useCallback(() => {
    toggleRouteLockFor(selectedRouteLock);
  }, [toggleRouteLockFor, selectedRouteLock]);

  const toggleRouteLockForRef = useRef(toggleRouteLockFor);
  toggleRouteLockForRef.current = toggleRouteLockFor;
  const routeLockInfoForRef = useRef(routeLockInfoFor);
  routeLockInfoForRef.current = routeLockInfoFor;

  /**
   * Pin state of the current table selection (R06).
   *
   * A pin protects a table from automatic placement, including the placement of
   * newly added tables. It says nothing about the model — a pinned table is not
   * semantically special — and it is independent of a route lock.
   *
   * The control acts on the whole selection, so the action is Unpin only when
   * every selected table is already pinned; a mixed selection pins the rest,
   * which is the outcome a user asking to "pin these" expects.
   */
  const selectedTablePins = useMemo(() => {
    const selected = nodes.filter((node) => node.selected === true);
    const pinnedCount = selected.filter(
      (node) => (node.data as { pinned?: boolean } | undefined)?.pinned === true,
    ).length;
    return { ids: selected.map((node) => node.id), count: selected.length, pinnedCount };
  }, [nodes]);

  const pinState: "none" | "pinned" | "unpinned" =
    selectedTablePins.count === 0
      ? "none"
      : selectedTablePins.pinnedCount === selectedTablePins.count
        ? "pinned"
        : "unpinned";

  /**
   * Pin or unpin every selected table, as one undo entry.
   *
   * The pin lives in the table layout rather than in the position map, so the
   * history entry carries a table snapshot: a position diff cannot see it, and
   * without the snapshot an undo would silently leave the pin behind.
   */
  const handleTogglePin = useCallback(() => {
    if (readOnlyRef.current) return;
    if (!selectedTablePins.ids.length) return;
    const nextPinned = pinState !== "pinned";

    const before = JSON.parse(JSON.stringify(layoutRef.current.tables ?? {})) as NonNullable<CanvasLayout["tables"]>;
    const tableMap = { ...(layoutRef.current.tables ?? {}) };
    for (const id of selectedTablePins.ids) {
      const live = liveNodesRef.current.find((node) => node.id === id);
      tableMap[id] = mergeTableEntry(
        tableMap[id] ?? { x: live?.position.x ?? 0, y: live?.position.y ?? 0 },
        { pinned: nextPinned },
      );
    }
    layoutRef.current = { ...layoutRef.current, tables: tableMap };
    flushLayout();

    const pinnedIds = new Set(selectedTablePins.ids);
    setNodes((ns) =>
      ns.map((node) =>
        pinnedIds.has(node.id) ? { ...node, data: { ...node.data, pinned: nextPinned } } : node,
      ),
    );

    const positions = positionsFromNodes(nodes);
    recordMove(positions, positions, undefined, {
      before,
      after: JSON.parse(JSON.stringify(tableMap)) as NonNullable<CanvasLayout["tables"]>,
    });
  }, [selectedTablePins, pinState, flushLayout, setNodes, recordMove, nodes]);

  const initialPlacementRef = useRef<string[] | null>(null);
  const initialPlacementAttemptsRef = useRef(0);
  const [initialPlacementRetry, setInitialPlacementRetry] = useState(0);

  const buildWorkerSnapshot = useCallback(
    (revision: number, options: Partial<CanvasLayoutOptions>, context: LayoutRunContext) => {
      // Measured card sizes supersede persisted ones: the engine must never
      // place variable-height cards using identical placeholder dimensions.
      const measured = new Map<string, { w: number; h: number }>();
      for (const node of reactFlowInstance.current?.getNodes() ?? []) {
        const w = typeof node.width === "number" ? node.width : undefined;
        const h = typeof node.height === "number" ? node.height : undefined;
        if (w !== undefined && h !== undefined && w > 0 && h > 0) measured.set(node.id, { w, h });
      }
      const persisted = layoutRef.current.tables ?? {};
      const { relationTerminalOverrides, relationNotation, relationPathing } = useBuilderStore.getState();
      return buildLayoutSnapshot({
        projectId,
        modelId,
        revision,
        globalPathing: relationPathing,
        nodes: nodes.map((node) => ({
          id: node.id,
          position: { x: node.position.x, y: node.position.y },
          measuredWidth: measured.get(node.id)?.w,
          measuredHeight: measured.get(node.id)?.h,
          provisionalWidth: persisted[node.id]?.w,
          provisionalHeight: persisted[node.id]?.h,
          tableType: tableTypeMapRef.current.get(node.id) ?? "",
          // A transient movable set is expressed as "everything else is
          // pinned", so initial placement moves only the new tables and no pin
          // is persisted.
          pinned: context.movableIds
            ? !context.movableIds.has(node.id)
            : (node.data as { pinned?: boolean } | undefined)?.pinned === true,
          selected: node.selected === true,
        })),
        edges: edges.map((edge) => {
          const data = (edge.data ?? {}) as CrowsFootEdgeData;
          return {
            id: edge.id,
            source: edge.source,
            target: edge.target,
            sourceSide: data.sourceSide,
            targetSide: data.targetSide,
            sourceRatio: data.sourceRatio,
            targetRatio: data.targetRatio,
            offsetIndex: data.offsetIndex,
            totalEdges: data.totalEdges,
            pathing: data.pathing,
            waypoint: data.waypoint,
            waypoints: data.waypoints,
            routeMode: data.routeMode,
            locked: data.locked === true,
            sourceIsFact: data.sourceIsFact === true,
            targetIsFact: data.targetIsFact === true,
            sourceIsDim: data.sourceIsDim === true,
            targetIsDim: data.targetIsDim === true,
            terminalOverride: relationTerminalOverrides?.[edge.id],
            notation: relationNotation,
          };
        }),
        options,
      });
    },
    [projectId, modelId, nodes, edges],
  );

  /**
   * Apply one coordinated layout result.
   *
   * Named `handleRedrawLayout` because it is the guarded writer the Canvas
   * write-site enumeration and the Bug-8763 guard audit track for the layout
   * redraw; it now applies a worker result instead of computing one locally.
   *
   * The read-only guard below is defence in depth for a *queued* worker result:
   * the orchestrator already refuses to apply one in a read-only session, and
   * this boundary refuses again rather than trusting that caller.
   */
  const handleRedrawLayout = useCallback(
    (result: LayoutResult) => {
      if (readOnlyRef.current) return;
      const before = pendingLayoutBeforeRef.current;
      pendingLayoutBeforeRef.current = null;

      const tableMap = { ...(layoutRef.current.tables ?? {}) };
      for (const [id, point] of Object.entries(result.positions)) {
        tableMap[id] = mergeTableEntry(tableMap[id], { x: point.x, y: point.y });
      }
      const edgeMap = { ...(layoutRef.current.edges ?? {}) };
      for (const route of Object.values(result.routes)) {
        const previous = edgeMap[route.edgeId] ?? {};
        // Preserve the explicit-vs-inherited distinction (F08): an edge with no
        // stored `pathing` inherits the global preference, so the apply path
        // must not write the resolved mode back as an explicit override — that
        // would freeze the inherited mode and stop later global changes from
        // applying to it.
        const hadExplicitPathing = previous.pathing !== undefined;
        edgeMap[route.edgeId] = mergeEdgeEntry(previous, {
          // Named explicitly with `undefined`: the captured array supersedes the
          // legacy single waypoint, and naming it is the only way a field is
          // ever removed.
          waypoint: undefined,
          waypoints: route.waypoints.length ? route.waypoints : undefined,
          sourceSide: route.sourceSide,
          targetSide: route.targetSide,
          sourceRatio: route.sourceRatio,
          targetRatio: route.targetRatio,
          pathing: hadExplicitPathing ? route.pathMode : undefined,
          routeMode: route.routeMode,
        });
      }
      layoutRef.current = { ...layoutRef.current, tables: tableMap, edges: edgeMap };

      setNodes((current) =>
        current.map((node) => {
          const point = result.positions[node.id];
          return point ? { ...node, position: { x: point.x, y: point.y } } : node;
        }),
      );
      setEdges((current) =>
        current.map((edge) => {
          const route = result.routes[edge.id];
          if (!route) return edge;
          const data = (edge.data ?? {}) as CrowsFootEdgeData;
          const hadExplicitPathing = data.pathing !== undefined;
          return {
            ...edge,
            data: {
              ...edge.data,
              waypoint: undefined,
              waypoints: route.waypoints.length ? route.waypoints : undefined,
              sourceSide: route.sourceSide,
              targetSide: route.targetSide,
              sourceRatio: route.sourceRatio,
              targetRatio: route.targetRatio,
              pathing: hadExplicitPathing ? route.pathMode : undefined,
              routeMode: route.routeMode,
            },
          };
        }),
      );
      // One user action is one undo entry carrying the table positions, the
      // table layout (sizes and pins) and the complete before/after edge layout,
      // so undo restores the manual edge routing the redraw replaced
      // (F-026-11 / LOW-2) AND the card geometry the gesture changed.
      if (before) {
        recordMove(
          before.positions,
          result.positions,
          { before: before.edges, after: edgeMap },
          { before: before.tables, after: JSON.parse(JSON.stringify(tableMap)) as NonNullable<CanvasLayout["tables"]> },
        );
      }
      // The single flush for the whole transaction: geometry, history and
      // persistence commit together, after the routes were repaired against
      // the final geometry.
      flushLayout();

      // The engine arranges to the best reasonable effort rather than refusing
      // to draw a dense diagram (Bug-10032). When it could not steer every
      // relationship clear of every card, say so: an unannounced degrade is
      // worse than the refusal it replaced, because the modeller cannot tell a
      // crowded arrangement from a correct one.
      if (result.metrics.throughNodeSegmentCount > 0) {
        setGlobalMessage(
          tRef.current("canvas.layoutCrowded", {
            count: String(result.metrics.throughNodeSegmentCount),
          }),
          "info",
        );
      }
    },
    [flushLayout, recordMove, setNodes, setEdges, setGlobalMessage],
  );

  const layoutController = useCanvasLayout({
    projectId,
    modelId,
    nodes,
    edges,
    relationPathing,
    readOnly,
    buildSnapshot: buildWorkerSnapshot,
    applyResult: handleRedrawLayout,
    onError: useCallback((message: string) => setGlobalMessage(message, "error"), [setGlobalMessage]),
  });

  /**
   * Run one coordinated layout batch.
   *
   * `reroute-links` never changes a table coordinate; `arrange-all` and
   * `arrange-selected` move only their movable set, leaving pinned cards and the
   * endpoints of locked relationships in place.
   */
  const handleApplyLayout = useCallback(
    async (
      operation: LayoutOperation,
      options: Partial<CanvasLayoutOptions> = {},
      context: LayoutRunContext = {},
    ): Promise<boolean> => {
      if (readOnlyRef.current) return false;
      // Captured before the batch starts so undo restores exactly this state.
      const token = claimCanvas();
      pendingLayoutBeforeRef.current = captureLayoutState();
      setLayoutMenuOpen(false);

      // Reroute Links returns every relationship to the model-wide Edge Pathing
      // setting.
      //
      // A per-relationship `pathing` value overrides that setting, correctly —
      // but an earlier build wrote one onto EVERY relationship each time the
      // diagram was arranged, so whole models ended up with the setting
      // silently overridden everywhere and changing it appeared to do nothing.
      // Stopping that write fixes new models; it does nothing for the ones
      // already carrying the overrides, and there was no action that cleared
      // them. Reroute Links is that action: it is the command that already
      // means "redraw every connector the way the model says".
      //
      // A LOCKED route keeps its override: the user froze that path, and its
      // stored mode is part of what was frozen.
      if (operation === "reroute-links") {
        const edges = layoutRef.current.edges ?? {};
        let cleared = false;
        const next: NonNullable<CanvasLayout["edges"]> = {};
        for (const [id, entry] of Object.entries(edges)) {
          if (entry?.pathing !== undefined && entry.locked !== true) {
            const { pathing: _dropped, ...rest } = entry;
            next[id] = rest;
            cleared = true;
          } else {
            next[id] = entry;
          }
        }
        if (cleared) {
          layoutRef.current = { ...layoutRef.current, edges: next };
          setEdges((es) =>
            es.map((e) =>
              (e.data as CrowsFootEdgeData | undefined)?.locked === true
                ? e
                : { ...e, data: { ...e.data, pathing: undefined } },
            ),
          );
        }
      }
      const outcome = await layoutController.run(operation, options, context);
      // The batch may have outlived the model it was computed for. Clearing the
      // pending capture or refitting the viewport now would act on whatever is
      // on the canvas instead.
      if (!stillOwnsCanvas(token)) return false;
      if (!outcome.applied) {
        pendingLayoutBeforeRef.current = null;
        return false;
      }
      setTimeout(() => {
        // Re-checked inside the timer too: 850ms is long enough for a model
        // switch, and fitting the view is a visible action on the new model.
        if (!stillOwnsCanvas(token)) return;
        reactFlowInstance.current?.fitView({ padding: 0.2, duration: 800 });
      }, 50);
      return true;
    },
    [nodes, layoutController, claimCanvas, stillOwnsCanvas],
  );

  /** Snapshot everything a gesture can change, before it changes it. */
  const captureLayoutState = useCallback((): LayoutTransactionState => {
    const layout = layoutRef.current;
    return {
      positions: positionsFromNodes(liveNodesRef.current),
      rects: new Map(liveCardRects()),
      tables: JSON.parse(JSON.stringify(layout.tables ?? {})) as NonNullable<CanvasLayout["tables"]>,
      edges: JSON.parse(JSON.stringify(layout.edges ?? {})) as NonNullable<CanvasLayout["edges"]>,
    };
  }, [liveCardRects]);

  /**
   * Put the canvas back exactly as the gesture found it.
   *
   * Used when a gesture's routes cannot be repaired: spec §4 requires the
   * pre-gesture layout to be restored rather than a broken connector persisted.
   * Only this transaction's owned state is restored — notes and viewport are
   * deliberately untouched, because they are not part of the gesture.
   */
  const restoreLayoutState = useCallback(
    (state: LayoutTransactionState) => {
      if (readOnlyRef.current) return;
      layoutRef.current = {
        ...layoutRef.current,
        tables: JSON.parse(JSON.stringify(state.tables)) as NonNullable<CanvasLayout["tables"]>,
        edges: JSON.parse(JSON.stringify(state.edges)) as NonNullable<CanvasLayout["edges"]>,
      };
      flushLayout();
      setNodes((ns) =>
        ns.map((node) => {
          const saved = state.tables[node.id];
          const position = state.positions[node.id];
          if (!saved && !position) return node;
          return {
            ...node,
            position: position ? { x: position.x, y: position.y } : node.position,
            style: styleForTableEntry(node.style as Record<string, unknown> | undefined, saved),
            data: { ...node.data, pinned: saved?.pinned === true },
          };
        }),
      );
      setEdges((es) =>
        es.map((edge) => {
          const entry = state.edges[edge.id];
          return {
            ...edge,
            data: {
              ...edge.data,
              waypoint: entry?.waypoint,
              waypoints: entry?.waypoints,
              sourceSide: entry?.sourceSide,
              targetSide: entry?.targetSide,
              sourceRatio: entry?.sourceRatio,
              targetRatio: entry?.targetRatio,
              pathing: entry?.pathing,
              routeMode: entry?.routeMode,
              locked: entry?.locked === true,
              lockedParallelOffset: entry?.lockedParallelOffset,
            },
          };
        }),
      );
    },
    [flushLayout, setNodes, setEdges],
  );

  /**
   * Locked relationships whose frozen path a card has been moved across.
   *
   * The frozen polyline is resolved from the same displayed-route rule the lock
   * captured it with; its endpoint cards cannot move, so the path is stable and
   * only the intruding card is new. `previous` bounds the check to cards this
   * gesture actually moved — a card that was already sitting on a locked route
   * when it was locked is not this gesture's doing and refusing it would make
   * the canvas unusable.
   */
  const lockedRouteIntrusionsNow = useCallback(
    (previous: Map<string, Rect>): string[] => {
      const cards = liveCardRects();
      const locked: LockedRouteGeometry[] = [];
      for (const edge of liveEdgesRef.current) {
        if ((edge.data as CrowsFootEdgeData | undefined)?.locked !== true) continue;
        const resolved = displayedRouteFor(edge, cards, routeContextRef.current);
        if (!resolved) continue;
        locked.push({
          id: edge.id,
          source: edge.source,
          target: edge.target,
          points: routePolyline(resolved.route),
        });
      }
      if (!locked.length) return [];

      const moved = changedCards(previous, cards);
      if (!moved.length) return [];

      return lockedRouteIntrusions(locked, moved);
    },
    [liveCardRects],
  );

  /**
   * The one boundary every route-only edit goes through.
   *
   * Two defects shared a cause: these edits were written inline, four times
   * over, each copy responsible for remembering the rules.
   *
   * A locked route was not actually protected. Locking withdraws the edge's
   * drag handles, but Reset Path and Toggle Path Style in the Joins panel stay
   * available for any writable session, and their handlers checked read-only
   * and nothing else. Reset deleted a frozen path's bends while leaving it
   * marked locked — the lock was a label on geometry anyone could still change.
   *
   * And none of them recorded history, so Undo stepped straight past a manual
   * connector edit to whatever happened before it, while the help page promises
   * these edits are undoable.
   *
   * Returns false when the edit was refused, so the caller does not report
   * success for something that did not happen.
   */
  const commitRouteEdit = useCallback(
    (
      edgeId: string,
      nextEdgesFor: (edges: NonNullable<CanvasLayout["edges"]>) => NonNullable<CanvasLayout["edges"]>,
      dataPatch: Record<string, unknown>,
    ): boolean => {
      if (readOnlyRef.current) return false;

      if (layoutRef.current.edges?.[edgeId]?.locked === true) {
        setGlobalMessage(tRef.current("canvas.lockedRouteEditRefused"), "info");
        return false;
      }

      const beforeEdges = JSON.parse(
        JSON.stringify(layoutRef.current.edges ?? {}),
      ) as NonNullable<CanvasLayout["edges"]>;

      const next = nextEdgesFor(layoutRef.current.edges ?? {});
      layoutRef.current = { ...layoutRef.current, edges: next };
      flushLayout();
      setEdges((es) =>
        es.map((e) => (e.id === edgeId ? { ...e, data: { ...e.data, ...dataPatch } } : e)),
      );

      // A route-only change moves no card, so the position maps are identical
      // and the entry carries the edge snapshots alone. recordMove already
      // accepts that shape; nothing here needs a second history stack.
      const positions = positionsFromNodes(liveNodesRef.current);
      recordMove(positions, positions, {
        before: beforeEdges,
        after: JSON.parse(JSON.stringify(next)) as NonNullable<CanvasLayout["edges"]>,
      });
      return true;
    },
    [flushLayout, recordMove, setEdges, setGlobalMessage],
  );
  // The Joins panel reaches the canvas through window events, whose listeners
  // are registered once. They read the current committer through this ref
  // rather than closing over a stale one.
  const commitRouteEditRef = useRef(commitRouteEdit);
  commitRouteEditRef.current = commitRouteEdit;

  /**
   * Commit an attachment the user dragged to a new point on a card.
   *
   * Three things were wrong with doing this inline in the edge callbacks.
   *
   * The pointer calculation produces an EFFECTIVE ratio — where on the border
   * the heel sits — and it was stored as the BASE ratio, to which the renderer
   * then adds this edge's parallel fan-out offset. The attachment landed beside
   * where the user dropped it whenever the relationship had a parallel sibling.
   *
   * The existing bends were left untouched. Moving a right-side attachment on a
   * 200-tall card from ratio .5 to .7 moves its heel from y=100 to y=140 while
   * the first bend stays at y=100, so the first leg becomes DIAGONAL in a route
   * the model says is orthogonal.
   *
   * And it wrote and flushed directly, so the edit never entered history and
   * Undo stepped straight past it.
   *
   * All three are answered by committing through the same owned transaction a
   * move or a resize uses: write the docking, then let the deferred repair fix
   * the adjacent bends, validate the whole route, and record one history entry.
   * A route that cannot be repaired is restored, exactly as a bad drag is.
   */
  const commitAttachmentChange = useCallback(
    (
      edgeId: string,
      tableId: string,
      end: "source" | "target",
      side: string,
      effectiveRatio: number,
      offsetIndex: number,
      totalEdges: number,
    ) => {
      if (readOnlyRef.current) return;
      const rect = liveCardRects().get(tableId);
      // Without a measured card there is no border to convert against, and
      // storing the raw value is the defect this exists to prevent.
      if (!rect) return;

      const base = baseRatioForDroppedPoint(
        rect,
        side as AnchorSide,
        effectiveRatio,
        offsetIndex,
        totalEdges,
      );

      const before = captureLayoutState();
      const next = { ...(layoutRef.current.edges ?? {}) };
      next[edgeId] = mergeEdgeEntry(next[edgeId], {
        ...(end === "source"
          ? { sourceSide: side, sourceRatio: base }
          : { targetSide: side, targetRatio: base }),
        routeMode: "manual",
      });
      layoutRef.current = { ...layoutRef.current, edges: next };
      setEdges((es) =>
        es.map((e) =>
          e.id === edgeId
            ? {
                ...e,
                data: {
                  ...e.data,
                  ...(end === "source"
                    ? { sourceSide: side, sourceRatio: base }
                    : { targetSide: side, targetRatio: base }),
                  routeMode: "manual",
                },
              }
            : e,
        ),
      );

      // Not flushed here: the new docking is candidate geometry until the bends
      // around it have been repaired and the route validated.
      pendingRepairBeforeRef.current = before;
      pendingRepairAfterRef.current = before.positions;
      liveGestureRef.current = ++gestureSeqRef.current;
      beginCandidateGeometry();
      repairPendingRef.current = true;
      setRepairTick((tick) => tick + 1);
    },
    [captureLayoutState, liveCardRects, setEdges, beginCandidateGeometry],
  );

  /**
   * Repair engine-generated routes after a card move or resize (F05).
   *
   * Runs the worker `repair-routes` batch, which never moves a card and
   * recomputes only `auto` routes; manual and locked geometry stays. It is the
   * gesture's route-repair transaction, not a new placement action, so it does
   * not close the panel or refit the viewport.
   *
   * Invoked from a post-commit effect (see below): the synchronous gesture
   * handler still holds pre-commit `nodes`, so starting the batch there would
   * build a snapshot of the old geometry that the stability guard then refuses
   * — a silent no-op. The captured before/after state lets the apply path
   * record ONE undo entry carrying positions and repaired routes together.
   */
  const handleRepairRoutes = useCallback(async () => {
    // Every exit from here has to settle the candidate. A transaction that
    // returns early without accepting leaves saves pinned to an old snapshot
    // for the rest of the session, which is a silent stop-saving bug — worse
    // than the unsafe save it was added to prevent.
    if (readOnlyRef.current) {
      acceptGeometry();
      return;
    }
    const before = pendingRepairBeforeRef.current;
    pendingRepairBeforeRef.current = null;
    pendingRepairAfterRef.current = null;
    if (!before) {
      acceptGeometry();
      return;
    }

    // R06: a card that the finished gesture has left lying across a locked
    // relationship is refused here rather than up front, because most moves do
    // not touch a locked path and refusing them all would make locking one
    // relationship quietly freeze the whole diagram. The previous geometry is
    // restored, so the canvas returns to a state where the frozen path is still
    // the path drawn, and no undo entry is recorded — the gesture did not
    // happen.
    const intruded = lockedRouteIntrusionsNow(before.rects);
    if (intruded.length) {
      restoreLayoutState(before);
      acceptGeometry(); // the restored geometry is the accepted geometry
      setGlobalMessage(tRef.current("canvas.lockedRouteBlocked"), "warning");
      return;
    }

    // This transaction's identity. A newer gesture starting while the repair is
    // in flight makes this one stale, and a stale transaction may not commit,
    // restore or clear — the newer gesture owns the canvas now.
    const token = claimCanvas();
    pendingLayoutBeforeRef.current = before;
    const outcome = await layoutController.run("repair-routes", { preset: "hierarchical" });
    if (outcome.applied) {
      // handleRedrawLayout committed geometry, history and persistence together.
      acceptGeometry();
      return;
    }

    if (!stillOwnsCanvas(token)) {
      // A newer gesture, a different model, or an unmount. The newer owner
      // captured its own before-state and will commit or restore on its own;
      // anything written here would land on top of it — or, after a model
      // change, on a different model entirely.
      //
      // Releasing the candidate hold is not "writing": the newer owner has
      // either set its own hold or owns accepted geometry already, and leaving
      // this one set would block its saves.
      acceptGeometry();
      return;
    }
    pendingLayoutBeforeRef.current = null;

    // What happens next is decided by ONE policy, in layout/failurePolicy.ts,
    // rather than by a list of codes spelled out here. The previous list read
    // "restore for geometry-invalid or no-route, otherwise keep" — and every
    // untyped rejection reached the worker as `unknown`, so a genuine geometry
    // rejection fell through to the keep-and-save branch.
    const disposition = dispositionFor(outcome.failure ?? "unknown");

    // A newer gesture or a different model owns the canvas now. Anything this
    // continuation writes would be written over someone else's state.
    if (disposition === "abandon") return;

    // The engine judged this geometry undrawable: restore the pre-gesture
    // layout rather than persist a broken connector, and record NO history
    // entry, because the gesture did not happen.
    if (disposition === "discard") {
      restoreLayoutState(before);
      acceptGeometry(); // the pre-gesture geometry is accepted again
      setGlobalMessage(tRef.current("canvas.layoutRepairRestored"), "warning");
      return;
    }

    // The engine never rendered a verdict — unavailable or timed out. There is
    // no evidence the user's edit is bad, and discarding their work for an
    // infrastructure failure would be worse than leaving the routes
    // unrepaired. Keep the geometry, persist it, and record the gesture without
    // the route repair.
    layoutRef.current = {
      ...layoutRef.current,
      tables: { ...(layoutRef.current.tables ?? {}) },
    };
    // Deliberate: the engine never judged this geometry, and the product
    // decision is to keep the user's edit rather than discard it for an
    // infrastructure failure. Accepting it here is what makes it savable —
    // leaving it as a candidate would silently stop persisting the canvas.
    acceptGeometry();
    flushLayout();
    const after = positionsFromNodes(liveNodesRef.current);
    recordMove(before.positions, after, undefined, {
      before: before.tables,
      after: JSON.parse(JSON.stringify(layoutRef.current.tables ?? {})) as NonNullable<CanvasLayout["tables"]>,
    });
  }, [
    flushLayout,
    recordMove,
    layoutController,
    lockedRouteIntrusionsNow,
    restoreLayoutState,
    setGlobalMessage,
    acceptGeometry,
    claimCanvas,
    stillOwnsCanvas,
  ]);
  const handleRepairRoutesRef = useRef(handleRepairRoutes);
  handleRepairRoutesRef.current = handleRepairRoutes;

  // Run a pending repair after the gesture's geometry change has committed, so
  // the snapshot and the stability signature both describe the final cards.
  useEffect(() => {
    if (!repairPendingRef.current) return;
    repairPendingRef.current = false;
    void handleRepairRoutesRef.current();
  }, [repairTick, nodes, edges]);

  // ---- Initial placement of genuinely new tables ---------------------------
  useEffect(() => {
    const pending = initialPlacementRef.current;
    if (!pending || readOnly) return;

    const measured = new Map<string, { width?: number | null; height?: number | null }>();
    for (const node of reactFlowInstance.current?.getNodes() ?? []) {
      measured.set(node.id, node);
    }
    const ready = pending.every((id) => {
      const node = measured.get(id);
      return (
        !!node &&
        typeof node.width === "number" && node.width > 0 &&
        typeof node.height === "number" && node.height > 0
      );
    });

    if (!ready) {
      if (initialPlacementAttemptsRef.current >= INITIAL_PLACEMENT_MAX_ATTEMPTS) {
        // Give up quietly: no placeholder-based positions are ever applied, and
        // the user can still run an Arrange action explicitly.
        initialPlacementRef.current = null;
        initialPlacementAttemptsRef.current = 0;
        return;
      }
      initialPlacementAttemptsRef.current += 1;
      const timer = setTimeout(
        () => setInitialPlacementRetry((tick) => tick + 1),
        INITIAL_PLACEMENT_RETRY_MS,
      );
      return () => clearTimeout(timer);
    }

    // Claim the batch before starting so it cannot be started twice.
    initialPlacementRef.current = null;
    initialPlacementAttemptsRef.current = 0;
    void handleApplyLayout(
      "arrange-all",
      { preset: "hierarchical" },
      { movableIds: new Set(pending) },
    );
  }, [initialPlacementRetry, readOnly, nodes, handleApplyLayout]);

  // ---- Canvas export to PNG -----------------------------------------------
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const [exporting, setExporting] = useState(false);

  const [exportFormat, setExportFormat] = useState<"png" | "svg">("png");
  const handleExportCanvas = useCallback(async () => {
    if (isDirty) {
      const proceed = await confirm({
        title: t("canvas.unsavedChanges"),
        message: t("canvas.exportVersionWarning"),
        confirmLabel: t("canvas.exportAnyway"),
        cancelLabel: t("common.cancel"),
      });
      if (!proceed) return;
    }
    const rf = reactFlowInstance.current;
    if (!rf) return;
    setExporting(true);

    const savedViewport = rf.getViewport();
    rf.fitView({ padding: 0.2, duration: 0 });

    // Wait one animation frame for the DOM to reflect the new transform.
    requestAnimationFrame(() => {
      setTimeout(() => {
        const viewport = document.querySelector(".react-flow__viewport") as HTMLElement | null;
        if (!viewport) { setExporting(false); return; }
        import("html-to-image").then((mod) => {
          const converter = exportFormat === "svg" ? mod.toSvg : mod.toPng;
          const opts: any = { backgroundColor: "#ffffff" };
          if (exportFormat === "png") opts.pixelRatio = 2;
          converter(viewport, opts).then((dataUrl) => {
            const slug = [tenantSlug, projectSlug, modelSlug].filter(Boolean).join("-") || "canvas";
            const ver = versionNumber != null ? `-v${versionNumber}` : "";
            const a = document.createElement("a");
            a.href = dataUrl;
            a.download = `${slug}${ver}.${exportFormat}`;
            a.click();
          }).catch(() => {
            // F-026-15: the image conversion can fail (large diagram, browser
            // limits). Surface a user-appropriate message instead of silently
            // swallowing the error.
            setGlobalMessage(t("canvas.exportFailed"), "error");
          }).finally(() => {
            rf.setViewport(savedViewport, { duration: 0 });
            setExporting(false);
          });
        }).catch(() => {
          // F-026-15: the dynamic import of the export library failed (chunk
          // load / offline). "Install html-to-image" was developer-speak no
          // end user could act on — show a plain, actionable message.
          setGlobalMessage(t("canvas.exportFailed"), "error");
          rf.setViewport(savedViewport, { duration: 0 });
          setExporting(false);
        });
      }, 50);
    });
  }, [confirm, setGlobalMessage, exportFormat, tenantSlug, projectSlug, modelSlug, versionNumber, isDirty]);

  const handleSearch = useCallback((term: string) => {
    if (!term.trim()) return;
    const lc = term.toLowerCase();
    const match = nodes.find((n) => {
      const d = n.data as ERDNodeData | undefined;
      if (!d?.table) return false;
      const t = d.table;
      return (
        (t.display_name ?? "").toLowerCase().includes(lc) ||
        (t.alias ?? "").toLowerCase().includes(lc) ||
        (t.physical_name ?? "").toLowerCase().includes(lc)
      );
    });
    if (match && reactFlowInstance.current) {
      reactFlowInstance.current.setCenter(
        match.position.x + 100,
        match.position.y + 50,
        { zoom: 1.2, duration: 600 },
      );
    }
  }, [nodes]);

  const handleCopyShareLink = useCallback(() => {
    const url = new URL(window.location.href);
    url.searchParams.set("readonly", "1");
    navigator.clipboard.writeText(url.toString()).then(() => {
      setGlobalMessage(t("canvas.shareLinkCopied"), "success");
    }).catch(() => {});
  }, [setGlobalMessage]);

  // Bug (user-reported 2026-08-24): the hidden-joins warning had no way to
  // dismiss it and stayed on screen permanently, permanently covering canvas
  // space even when the modeller had already seen it and chose not to add the
  // missing tables. Track the count it was dismissed AT — if the situation
  // changes (a different set of joins becomes hidden, or the count changes),
  // the warning reappears, so a genuinely new problem is never silently
  // suppressed by an old dismissal.
  const [dismissedHiddenJoinsCount, setDismissedHiddenJoinsCount] = useState<number | null>(null);
  const [notesOpen, setNotesOpen] = useState(false);
  const [notesText, setNotesText] = useState<string>(() => {
    return canvasLayout?.notes ?? "";
  });
  // Bug-7634 (finding 2): track whether the user has locally edited notes
  // so we can distinguish "user typed new notes" from "waiting for server
  // data". When false, late-arriving canvasLayout updates sync notes text.
  const notesDirtyRef = useRef(false);
  const handleSaveNotes = useCallback(() => {
    // R2 review: the Notes BUTTON is hidden read-only, but the Notes PANEL
    // renders on `notesOpen` alone, so a panel already open when the session
    // flips keeps a live Save. Refuse the write AND say so — closing the panel
    // with `notesDirtyRef = false` would be a success-shaped response to a
    // discarded edit, which is the silent-refusal failure this lane rejects.
    if (readOnlyRef.current) {
      setNotesOpen(false);
      setGlobalMessage(tRef.current("canvas.readOnlyRefused"), "info");
      return;
    }
    layoutRef.current = { ...layoutRef.current, notes: notesText };
    flushLayout();
    setNotesOpen(false);
    notesDirtyRef.current = false; // saved — no longer locally dirty
  }, [notesText, flushLayout, setGlobalMessage]);

  /**
   * R3 review: withdraw open authoring surfaces on the READ-ONLY TRANSITION,
   * and say why.
   *
   * The render gates on the notes panel and the preset menu are the fail-closed
   * backstop, but on their own they make an open panel — possibly holding
   * unsaved text — vanish mid-keystroke with no explanation. That is the same
   * silent refusal this lane rejects one layer down in `handleSaveNotes`, and
   * the sibling JoinsPanel dialog already handles it correctly. Keyed on the
   * false -> true edge rather than on the current value so it fires once, and
   * it clears the open flags so a later re-grant cannot make the panels
   * reappear unbidden.
   *
   * A same-mount transition is reachable when React Query refreshes the cached
   * model in the background and returns a changed `caller_can_author` value.
   * Background refetches keep the existing Canvas mounted while the new
   * authority value changes `readOnly`.
   */
  const prevReadOnlyRef = useRef(readOnly);
  useEffect(() => {
    const becameReadOnly = !prevReadOnlyRef.current && readOnly;
    prevReadOnlyRef.current = readOnly;
    if (!becameReadOnly) return;
    if (!notesOpen && !layoutMenuOpen) return;
    setNotesOpen(false);
    setLayoutMenuOpen(false);
    setGlobalMessage(tRef.current("canvas.readOnlyRefused"), "info");
  }, [readOnly, notesOpen, layoutMenuOpen, setGlobalMessage]);

  // Bug-7634: reset all per-model canvas state when the user navigates
  // between models in-place (without a full remount). Without this,
  // model A's layout, notes, and pending flush timer bleed into model B
  // and get persisted under B — corrupting B's canvas_layout.
  useEffect(() => {
    if (prevModelIdRef.current !== modelId) {
      prevModelIdRef.current = modelId;
      // Cancel any pending flush — it carries the old model's data and
      // would write model A's layout onto model B's record.
      if (flushTimer.current !== null) {
        window.clearTimeout(flushTimer.current);
        flushTimer.current = null;
      }
      // Allow the new model's server canvasLayout to sync in.
      userDirtyRef.current = false;
      layoutRef.current = canvasLayout ?? {};
      // Reset the notes text so model A's annotations don't appear and
      // potentially get saved to model B.
      setNotesText(canvasLayout?.notes ?? "");
      notesDirtyRef.current = false;
      setNotesOpen(false);
      // Same for the layout preferences: a choice made on model A is not a
      // choice about model B, and leaving the dirty flag set would block model
      // B's own saved options from seeding the panel.
      layoutPreferencesDirtyRef.current = false;
      setLayoutPreferences(defaultOptions(canvasLayout?.layoutOptions));
    }
  }, [modelId, canvasLayout]);

  // Bug-7634 (finding 2): when canvasLayout arrives late (e.g. the server
  // data for model B arrives after the model-change effect already ran with
  // undefined canvasLayout), sync notes text so saving notes does not
  // overwrite model B's persisted notes with a stale/blank value.
  useEffect(() => {
    if (userDirtyRef.current || notesDirtyRef.current) return;
    setNotesText(canvasLayout?.notes ?? "");
  }, [canvasLayout]);

  // ---- Window event channel for cross-component edge controls ------------
  // JoinsPanel buttons fire these events; Canvas owns layoutRef so it must
  // be the consumer. Keeps the edge component pure (no store coupling).
  useEffect(() => {
    // Bug-8504 (round 2): `persistLayout` already refuses to WRITE in a
    // read-only session, but these two handlers still mutated `layoutRef` and
    // the live ReactFlow edges before reaching it. Two consequences, both real:
    // a viewer saw an edge redraw that no one had authorised and that snaps
    // back on the next refetch, and — because `layoutRef` outlives the
    // read-only state — the orphaned mutation would have ridden along after a
    // background model refetch granted authoring on the same Canvas mount.
    // Guard at the entry of each handler, exactly as the node-resize handler
    // already does, so read-only leaves no state behind at all.
    // Reset Path, from the Joins panel. This is one of the two alternate
    // writers that made a route lock a label rather than a guarantee: it stayed
    // available for any writable session and deleted a frozen path's bends
    // while leaving `locked: true` in place. It goes through the same boundary
    // as the edge gestures now, so the lock refuses it and Undo can reverse it.
    const handleResetEdge = (e: Event) => {
      const joinId = (e as CustomEvent<string>).detail;
      commitRouteEditRef.current(
        joinId,
        (edges: NonNullable<CanvasLayout["edges"]>) => {
          // Bug-8762: copy-on-write via the shared primitive — never mutate the
          // cached nested entry in place (layoutRef aliases the query cache).
          const cleared = clearEdgeWaypoints(edges, joinId);
          // Do not invent an entry for an edge that had none: absence plus no
          // waypoints already resolves to "auto".
          return cleared[joinId]
            ? { ...cleared, [joinId]: { ...cleared[joinId], routeMode: undefined } }
            : cleared;
        },
        {
          waypoint: undefined,
          waypoints: undefined,
          // Bends cleared: the relationship is engine-routed again, so
          // provenance must not keep claiming a manual edit.
          routeMode: undefined,
        },
      );
    };

    // Lock/Unlock Route, from the Joins panel. Same action and same writer as
    // the canvas control; only the way in is different.
    const handleToggleEdgeRouteLock = (e: Event) => {
      const joinId = (e as CustomEvent<string>).detail;
      const edge = liveEdgesRef.current.find((candidate) => candidate.id === joinId);
      if (!edge) return;
      toggleRouteLockForRef.current(routeLockInfoForRef.current(edge));
    };

    // Toggle Path Style, from the Joins panel: the other alternate writer, and
    // the only thing in the product that creates a per-relationship `pathing`
    // override. It cycles through THREE states rather than two, because an
    // override that can be created but never removed makes the model-wide Edge
    // Pathing setting look broken for that relationship forever:
    //
    //   inherit the model setting -> orthogonal -> straight -> inherit ...
    const handleTogglePathingAuto = (e: Event) => {
      const joinId = (e as CustomEvent<string>).detail;
      const current = layoutRef.current.edges?.[joinId] ?? {};
      const nextPathing: "orthogonal" | "straight" | undefined =
        current.pathing === undefined
          ? "orthogonal"
          : current.pathing === "orthogonal"
            ? "straight"
            : undefined;

      commitRouteEditRef.current(
        joinId,
        (edges: NonNullable<CanvasLayout["edges"]>) => ({
          ...edges,
          [joinId]: mergeEdgeEntry(edges[joinId], {
            pathing: nextPathing,
            waypoint: undefined,
            waypoints: undefined,
            routeMode: undefined,
          }),
        }),
        {
          pathing: nextPathing,
          waypoint: undefined,
          waypoints: undefined,
          routeMode: undefined,
        },
      );
    };

    const handleNodeResize = (e: Event) => {
      if (readOnly) return; // Bug-7636: do not persist resizes in read-only mode
      const { id, w, h } = (e as CustomEvent<{ id: string; w: number; h: number }>).detail;
      // R06: resizing a locked route's endpoint moves its docked heels, which
      // is the frozen geometry. The card withdraws its resize handles while
      // locked; this refuses the event too, so the persisted size cannot change
      // through any other caller of the same channel.
      if (lockedEndpointsRef.current.has(id)) {
        setGlobalMessage(tRef.current("canvas.lockedEndpointRefused"), "info");
        return;
      }
      // Captured BEFORE the new size is written. Capturing afterwards recorded
      // the NEW width and height as the "before" state, so an undo restored the
      // old connector while leaving the card resized — the attachment mismatch
      // the repair had just corrected.
      const before = captureLayoutState();
      const tableMap = { ...(layoutRef.current.tables ?? {}) };
      tableMap[id] = mergeTableEntry(tableMap[id], { w, h });
      layoutRef.current = { ...layoutRef.current, tables: tableMap };
      // Not flushed here: the new size is candidate geometry until the routes
      // have been repaired against it. The commit or the restore does the flush.
      // F05: a resized card changes the docked heels, so its automatic routes
      // must be repaired against the new dimensions. Positions are unchanged by
      // a resize, so the before/after position maps are identical and the undo
      // entry carries the size change and the route repair.
      pendingRepairBeforeRef.current = before;
      pendingRepairAfterRef.current = before.positions;
      liveGestureRef.current = ++gestureSeqRef.current;
      // The new size is on the canvas but its routes have not been repaired
      // yet, so no save may carry it until the repair settles.
      beginCandidateGeometry();
      repairPendingRef.current = true;
      setRepairTick((tick) => tick + 1);
    };

    // Validation tray click-to-navigate: centre the canvas on a table node.
    const handleCenterNode = (e: Event) => {
      const tableId = (e as CustomEvent<string>).detail;
      const match = liveNodesRef.current.find((n) => n.id === tableId);
      if (match && reactFlowInstance.current) {
        reactFlowInstance.current.setCenter(
          match.position.x + 100,
          match.position.y + 50,
          { zoom: 1.2, duration: 600 },
        );
      }
    };

    window.addEventListener("reset-edge-path", handleResetEdge);
    window.addEventListener("toggle-edge-route-lock", handleToggleEdgeRouteLock);
    window.addEventListener("toggle-edge-pathing-auto", handleTogglePathingAuto);
    window.addEventListener("node-resize-end", handleNodeResize);
    window.addEventListener("canvas-center-node", handleCenterNode);
    return () => {
      window.removeEventListener("reset-edge-path", handleResetEdge);
      window.removeEventListener("toggle-edge-route-lock", handleToggleEdgeRouteLock);
      window.removeEventListener("toggle-edge-pathing-auto", handleTogglePathingAuto);
      window.removeEventListener("node-resize-end", handleNodeResize);
      window.removeEventListener("canvas-center-node", handleCenterNode);
    };
  }, [flushLayout, setEdges, readOnly, setGlobalMessage]);

  const handleNodesChange = useCallback(
    (changes: NodeChange[]) => {
      // Unreachable read-only (`onNodesChange` is undefined and
      // `nodesDraggable` is false), guarded anyway so the invariant below is
      // uniform across all eleven layout writers rather than true for the
      // subset that had a reported defect.
      if (readOnlyRef.current) return;

      // R06: a locked relationship freezes its path, so the cards it docks to
      // must not move at all. The position change is dropped before React Flow
      // ever sees it, so the card does not travel and snap back — it simply
      // does not move — and the user is told which action would release it.
      // Everything else in the same gesture still applies: selecting a locked
      // endpoint, or dragging other cards alongside it, keeps working.
      const frozen = lockedEndpointsRef.current;
      let refusedFrozenMove = false;
      if (frozen.size) {
        const kept: NodeChange[] = [];
        for (const ch of changes) {
          if (ch.type === "position" && frozen.has(ch.id)) {
            refusedFrozenMove = true;
            continue;
          }
          kept.push(ch);
        }
        if (refusedFrozenMove) {
          changes = kept;
          // Announce once per gesture, not once per pointer-move frame.
          if (!frozenMoveNoticeRef.current) {
            frozenMoveNoticeRef.current = true;
            setGlobalMessage(tRef.current("canvas.lockedEndpointRefused"), "info");
          }
        }
      }
      if (!refusedFrozenMove && !changes.some((ch) => ch.type === "position" && ch.dragging === true)) {
        frozenMoveNoticeRef.current = false;
      }
      if (changes.length === 0) return;

      // Capture the pre-gesture positions and edges at drag START — the live
      // nodes have not received this batch of changes yet, so they still hold
      // the state the gesture began from. The position change and the repaired
      // routes are then recorded as ONE undo entry by the repair apply.
      for (const ch of changes) {
        if (ch.type === "position" && ch.dragging === true && !isDraggingRef.current) {
          isDraggingRef.current = true;
          pendingRepairBeforeRef.current = captureLayoutState();
          liveGestureRef.current = ++gestureSeqRef.current;
          break;
        }
      }
      onNodesChange(changes);
      let dirty = false;
      let dragEnded = false;
      for (const ch of changes) {
        if (ch.type === "position" && ch.position) {
          const tableMap = { ...(layoutRef.current.tables ?? {}) };
          tableMap[ch.id] = mergeTableEntry(tableMap[ch.id], {
            x: ch.position.x,
            y: ch.position.y,
          });
          layoutRef.current = { ...layoutRef.current, tables: tableMap };
          dirty = true;
        }
        if (ch.type === "position" && ch.dragging === false) {
          dragEnded = true;
        }
      }
      // Deliberately NOT flushed per change. The dragged coordinates are
      // candidate geometry until the routes have been repaired against them;
      // persisting here wrote an unvalidated diagram, and on a drag longer than
      // the debounce it also emitted a PATCH mid-gesture, which spec §5 forbids.
      // The commit path flushes once, or the restore path flushes the rollback.
      if (dirty && !isDraggingRef.current) flushLayout();
      // Commit the gesture at drag END — one drag = one undo entry
      // (F-026-02). layoutRef holds the authoritative final positions
      // (merged above), so the entry is exact even if the last change has
      // not rendered yet.
      if (dragEnded && isDraggingRef.current) {
        isDraggingRef.current = false;
        const after: Record<string, { x: number; y: number }> = {};
        for (const n of liveNodesRef.current) {
          const saved = (layoutRef.current.tables ?? {})[n.id];
          after[n.id] =
            saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)
              ? { x: saved.x, y: saved.y }
              : { x: n.position.x, y: n.position.y };
        }
        // F05: the stored automatic waypoints are absolute, so the moved
        // cards' auto routes must be repaired against the final geometry. The
        // position change and the repaired routes become one undo entry via
        // the deferred repair's apply path.
        pendingRepairAfterRef.current = after;
        // Cards have moved but their routes still describe the old positions.
        beginCandidateGeometry();
        repairPendingRef.current = true;
        setRepairTick((tick) => tick + 1);
      }
    },
    [onNodesChange, flushLayout, setGlobalMessage, beginCandidateGeometry],
  );

  // Track the source node of an in-progress connection drag so that
  // onConnectEnd can complete the join even when the user releases over
  // the interior of the target node (not on a handle).
  const connectingNodeId  = useRef<string | null>(null);
  const connectionHandled = useRef(false);

  // table_id → table_type (needed to annotate edges)
  const tableTypeMap = useMemo(() => {
    const m = new Map<string, string>();
    for (const t of tables) m.set(t.id, t.table_type);
    return m;
  }, [tables]);
  tableTypeMapRef.current = tableTypeMap;

  // ---- A1 fact-node column segmentation ---------------------------------
  const measures = useMeasures(projectId, modelId);
  const dimensions = useDimensions(projectId, modelId);
  const hierarchies = useHierarchiesWithLevels(projectId, modelId);

  const segmentationByTableId = useMemo(() => {
    const factIds = new Set(
      tables.filter((t) => t.table_type === "fact").map((t) => t.id),
    );
    const result = new Map<string, SegmentationRefs>();
    if (factIds.size === 0) return result;

    for (const factId of factIds) {
      result.set(factId, {
        measureCols: new Set<string>(),
        dimCols: new Set<string>(),
        keyCols: new Set<string>(),
        levelCols: new Set<string>(),
      });
    }

    const allBuckets = Array.from(result.values());

    for (const m of measures.data ?? []) {
      const name = (m.source_column_name ?? m.user_defined_attribute_name ?? m.name).toLowerCase();
      if (m.source_table_id) {
        const bucket = result.get(m.source_table_id);
        if (bucket) bucket.measureCols.add(name);
      } else {
        for (const b of allBuckets) b.measureCols.add(name);
      }
    }

    for (const d of dimensions.data ?? []) {
      const name = (d.source_column_name ?? d.user_defined_attribute_name ?? d.name).toLowerCase();
      if (d.source_table_id) {
        const bucket = result.get(d.source_table_id);
        if (bucket) bucket.dimCols.add(name);
      } else {
        for (const b of allBuckets) b.dimCols.add(name);
      }
    }

    for (const j of joins) {
      const leftBucket = result.get(j.left_table_id);
      const rightBucket = result.get(j.right_table_id);
      if (leftBucket && j.left_column_name) {
        leftBucket.keyCols.add(j.left_column_name.toLowerCase());
      }
      if (rightBucket && j.right_column_name) {
        rightBucket.keyCols.add(j.right_column_name.toLowerCase());
      }
    }

    for (const h of hierarchies.data) {
      for (const lvl of h.levels ?? []) {
        const keyAttr = lvl.key_attribute;
        if (keyAttr?.table_id) {
          const bucket = result.get(keyAttr.table_id);
          if (bucket && keyAttr.name) {
            bucket.levelCols.add(keyAttr.name.toLowerCase());
          }
        }
        for (const extra of lvl.attributes ?? []) {
          const extraAttr = extra?.attribute;
          if (!extraAttr?.table_id || !extraAttr.name) continue;
          const extraBucket = result.get(extraAttr.table_id);
          if (extraBucket) {
            extraBucket.levelCols.add(extraAttr.name.toLowerCase());
          }
        }
      }
    }

    return result;
  }, [tables, joins, measures.data, dimensions.data, hierarchies.data]);

  // ---- A6 aggregate + pocket overlay chips on fact nodes ---------------
  const aggregates = useAggregates(projectId, modelId);
  const pockets = usePockets(projectId, modelId);

  const overlayByTableId = useMemo(() => {
    const result = new Map<string, OverlayCounts>();
    // Aggregates carry a grain (dimension names), which reference dimensions
    // bound to specific tables via source_table_id. Count each aggregate
    // against the fact table(s) whose dimensions appear in its grain.
    const factTableIds = new Set(
      tables.filter((t) => t.table_type === "fact").map((t) => t.id),
    );

    // Bug-7638: Build a dim-table-id -> fact-table-id(s) map by walking joins.
    // In a star schema, dimensions live on dim tables joined to fact tables.
    // The previous code only mapped dims whose source_table_id was itself a
    // fact, which only works for degenerate dimensions. Now we trace from a
    // dim's source table through joins to find the connected fact table(s).
    const dimTableToFactIds = new Map<string, Set<string>>();
    for (const j of joins) {
      const leftIsFact = factTableIds.has(j.left_table_id);
      const rightIsFact = factTableIds.has(j.right_table_id);
      if (leftIsFact && !rightIsFact) {
        if (!dimTableToFactIds.has(j.right_table_id)) dimTableToFactIds.set(j.right_table_id, new Set());
        dimTableToFactIds.get(j.right_table_id)!.add(j.left_table_id);
      } else if (rightIsFact && !leftIsFact) {
        if (!dimTableToFactIds.has(j.left_table_id)) dimTableToFactIds.set(j.left_table_id, new Set());
        dimTableToFactIds.get(j.left_table_id)!.add(j.right_table_id);
      }
    }

    // Build dimension-name -> fact-table-id(s) map from the dimensions data.
    // A dim on a fact table maps directly; a dim on a non-fact table maps
    // through the join graph above.
    const dimNameToFactTableIds = new Map<string, Set<string>>();
    for (const d of dimensions.data ?? []) {
      if (!d.source_table_id) continue;
      const fids = new Set<string>();
      if (factTableIds.has(d.source_table_id)) {
        // Degenerate dimension: lives directly on the fact table
        fids.add(d.source_table_id);
      } else {
        // Normal star: dim lives on a dim table joined to one or more facts
        const joined = dimTableToFactIds.get(d.source_table_id);
        if (joined) for (const fid of joined) fids.add(fid);
      }
      if (fids.size > 0) dimNameToFactTableIds.set(d.name, fids);
    }

    // Initialise counts for every fact table
    const aggCounts = new Map<string, number>();
    const pocketCounts = new Map<string, number>();
    for (const fid of factTableIds) {
      aggCounts.set(fid, 0);
      pocketCounts.set(fid, 0);
    }
    // Attribute each aggregate to a fact table based on its grain dimensions
    for (const agg of aggregates.data ?? []) {
      const matched = new Set<string>();
      for (const grainDim of agg.grain ?? []) {
        const fids = dimNameToFactTableIds.get(grainDim);
        if (fids) for (const fid of fids) matched.add(fid);
      }
      // Bug-5298: if no grain dimension resolved to a specific fact, do NOT
      // attribute to every fact — show 0 rather than the total. This avoids a
      // fact with no real aggregate showing a non-zero count.
      if (matched.size === 0) {
        // unresolved grain — skip, counts stay at 0
      } else {
        for (const fid of matched) {
          aggCounts.set(fid, (aggCounts.get(fid) ?? 0) + 1);
        }
      }
    }
    // Bug-5298: pockets carry only a defining_sql with no source_table_id.
    // Parsing the SQL client-side to attribute per-fact is not viable.  Rather
    // than attributing every pocket to every fact (which inflates counts and
    // misleads), leave counts at 0.  The total pocket count is visible in the
    // Pocket Tables panel.
    // pocketCounts remain at 0 (initialised above).
    for (const fid of factTableIds) {
      result.set(fid, {
        aggregates: aggCounts.get(fid) ?? 0,
        pockets: pocketCounts.get(fid) ?? 0,
      });
    }
    return result;
  }, [tables, joins, aggregates.data, pockets.data, dimensions.data]);

  // ---- A3 hierarchy grouping overlay for dim tables --------------------
  const hierarchyGroupsByTableId = useMemo(() => {
    const result = new Map<string, HierarchyGroupOnTable[]>();
    for (const h of hierarchies.data) {
      const levelsByTable = new Map<string, HierarchyGroupOnTable["levels"]>();
      for (const lvl of h.levels ?? []) {
        const keyAttr = lvl.key_attribute;
        const tid = keyAttr?.table_id;
        if (!tid || !keyAttr?.name) continue;
        if (!levelsByTable.has(tid)) levelsByTable.set(tid, []);
        levelsByTable.get(tid)!.push({
          name: lvl.name,
          ordinal: lvl.ordinal,
          column_name: keyAttr.name,
        });
      }
      for (const [tid, levels] of levelsByTable) {
        const sorted = [...levels].sort((a, b) => a.ordinal - b.ordinal);
        if (!result.has(tid)) result.set(tid, []);
        result.get(tid)!.push({ name: h.name, levels: sorted });
      }
    }
    return result;
  }, [hierarchies.data]);

  // ---- 8.B.7 Dimmed-table set ------------------------------------------
  // Delegates to computeDimmedTableIds, which mirrors the backend persona gate
  // (per-type empty allow list = unrestricted for that type) — see
  // personaOverlay.ts. Fixes F-026-06, where dimension tables under a
  // measures-only persona were wrongly dimmed.
  const dimmedTableIds = useMemo(() => {
    if (!personaId || !persona.data) return new Set<string>();
    const p = persona.data;

    const measuresByTable = new Map<string, string[]>();
    const measureSourceColumnId: Record<string, string | null> = {};
    for (const m of measures.data ?? []) {
      measureSourceColumnId[m.id] = m.source_column_id ?? null;
      if (!m.source_table_id) continue;
      if (!measuresByTable.has(m.source_table_id)) measuresByTable.set(m.source_table_id, []);
      measuresByTable.get(m.source_table_id)!.push(m.id);
    }
    const dimsByTable = new Map<string, string[]>();
    const dimensionSourceColumnId: Record<string, string | null> = {};
    for (const d of dimensions.data ?? []) {
      dimensionSourceColumnId[d.id] = d.source_column_id ?? null;
      if (!d.source_table_id) continue;
      if (!dimsByTable.has(d.source_table_id)) dimsByTable.set(d.source_table_id, []);
      dimsByTable.get(d.source_table_id)!.push(d.id);
    }
    const hierByTable = new Map<string, string[]>();
    for (const h of hierarchies.data) {
      for (const lvl of h.levels ?? []) {
        const tid = lvl.key_attribute?.table_id;
        if (!tid) continue;
        if (!hierByTable.has(tid)) hierByTable.set(tid, []);
        hierByTable.get(tid)!.push(h.id);
      }
    }

    // F-008-03: reflect column-level security in the preview — a measure /
    // dimension whose backing column the persona restricts is not queryable,
    // so it does not keep its table lit and it can be marked as blocked.
    const restrictedColumnIds = new Set(p.restricted_column_ids ?? []);
    const blockedMeasureIds = new Set(p.cls_blocked_measure_ids ?? []);
    const blockedDimensionIds = new Set(p.cls_blocked_dimension_ids ?? []);

    return computeDimmedTableIds(
      tables.map((t) => ({
        tableId: t.id,
        measureIds: measuresByTable.get(t.id) ?? [],
        dimensionIds: dimsByTable.get(t.id) ?? [],
        hierarchyIds: hierByTable.get(t.id) ?? [],
      })),
      {
        measureIds: new Set(p.included_measure_ids),
        dimensionIds: new Set(p.included_dimension_ids),
        hierarchyIds: new Set(p.included_hierarchy_ids),
      },
      {
        restrictedColumnIds,
        sources: { measureSourceColumnId, dimensionSourceColumnId },
        blockedMeasureIds,
        blockedDimensionIds,
      },
    );
  }, [
    personaId,
    persona.data,
    tables,
    measures.data,
    dimensions.data,
    hierarchies.data,
  ]);

  // F-008-03: object ids the persona CLS-restricts (backing column restricted)
  // and the persona's mandatory default-filter scope, surfaced so the canvas
  // shows what the runtime actually blocks / scopes — not an "all available"
  // preview that diverges from execution.
  const clsRestrictedObjectIds = useMemo(() => {
    if (!personaId || !persona.data) {
      return { measureIds: new Set<string>(), dimensionIds: new Set<string>() };
    }
    const restrictedColumnIds = new Set(persona.data.restricted_column_ids ?? []);
    const measureSourceColumnId: Record<string, string | null> = {};
    for (const m of measures.data ?? []) measureSourceColumnId[m.id] = m.source_column_id ?? null;
    const dimensionSourceColumnId: Record<string, string | null> = {};
    for (const d of dimensions.data ?? []) dimensionSourceColumnId[d.id] = d.source_column_id ?? null;
    return computeClsRestrictedObjectIds(
      { measureSourceColumnId, dimensionSourceColumnId },
      restrictedColumnIds,
      {
        measureIds: persona.data.cls_blocked_measure_ids,
        dimensionIds: persona.data.cls_blocked_dimension_ids,
      },
    );
  }, [personaId, persona.data, measures.data, dimensions.data]);

  const personaDefaultFilterChips = useMemo(
    () => (personaId && persona.data ? summarizeDefaultFilters(persona.data.default_filters) : []),
    [personaId, persona.data],
  );

  useEffect(() => {
    const rfNodes: Node[] = tables.map((t) => {
      const data: ERDNodeData = {
        table: t,
        projectId,
        modelId,
        segmentation: segmentationByTableId.get(t.id),
        hierarchyGroups: hierarchyGroupsByTableId.get(t.id),
        overlay: overlayByTableId.get(t.id),
        dimmed: dimmedTableIds.has(t.id),
        readOnly,
      };
      return {
        id: t.id,
        type: "erdTable",
        data,
        position: { x: 0, y: 0 },
      };
    });

    // Drop joins whose endpoints are not on the canvas. The /tables list
    // endpoint intentionally omits model tables linked to an autocreated
    // calendar table (see model-service tables.list_tables), yet joins to those
    // hidden calendar tables still exist. Rendering an edge to a missing node
    // breaks ReactFlow and feeds `undefined` into the d3-force link force, which
    // throws "node not found: undefined" and white-screens the whole builder.
    // An edge to a table that isn't drawn cannot be drawn either, so skip it.
    const nodeIds = new Set(rfNodes.map((n) => n.id));
    const { linked: linkedJoins, dropped } = partitionJoinsByEndpoints(joins, nodeIds);
    if (countDroppedJoins(dropped, nodeIds) > 0) {
      console.warn(
        `Canvas: skipped ${dropped.length} join(s) referencing tables not on the canvas:`,
        dropped.map((j) => j.id),
      );
    }

    // Parallel-edge offset map — multiple joins between the same pair
    // of tables get fanned out so they don't overlap.
    const pairCounts = new Map<string, number>();
    const edgeOffsets = new Map<string, number>();
    for (const j of linkedJoins) {
      const pair = [j.left_table_id, j.right_table_id].sort().join("-");
      const current = pairCounts.get(pair) ?? 0;
      pairCounts.set(pair, current + 1);
      edgeOffsets.set(j.id, current);
    }

    const rfEdges: Edge[] = linkedJoins.map((j) => {
      const sourceIsFact = tableTypeMap.get(j.left_table_id) === "fact";
      const targetIsFact = tableTypeMap.get(j.right_table_id) === "fact";
      const sourceIsDim = isDimTableType(tableTypeMap.get(j.left_table_id));
      const targetIsDim = isDimTableType(tableTypeMap.get(j.right_table_id));
      const pair = [j.left_table_id, j.right_table_id].sort().join("-");
      const totalEdges = pairCounts.get(pair) ?? 1;

      const savedEdge = (layoutRef.current.edges ?? {})[j.id];

      const edgeData: CrowsFootEdgeData = {
        joinType: j.join_type,
        sourceIsFact,
        targetIsFact,
        sourceIsDim,
        targetIsDim,
        offsetIndex: edgeOffsets.get(j.id) ?? 0,
        totalEdges,
        waypoint: savedEdge?.waypoint,
        waypoints: savedEdge?.waypoints,
        pathing: savedEdge?.pathing,
        routeMode: savedEdge?.routeMode,
        sourceSide: savedEdge?.sourceSide,
        targetSide: savedEdge?.targetSide,
        sourceRatio: savedEdge?.sourceRatio,
        targetRatio: savedEdge?.targetRatio,
        // Without this a locked relationship reopened unlocked: the flag was
        // persisted and read by the worker, but never carried back into the
        // edge the renderer and the panel read.
        locked: savedEdge?.locked === true,
        lockedParallelOffset: savedEdge?.lockedParallelOffset,
        sourceColumn: j.left_column_name ?? null,
        targetColumn: j.right_column_name ?? null,
        readOnly,
        // R1 review: the four edge-layout callbacks below are unreachable in a
        // read-only session today (`onEdgesChange` is undefined and
        // `elementsSelectable` is false, so an edge can never become selected,
        // which every CrowsFootEdge drag entry point requires). They still take
        // the same entry guard as their siblings: the reachability argument
        // depends on three ReactFlow props staying exactly as they are, and
        // "read-only leaves no state behind" should hold for every writer, not
        // for the two that happened to have a reported defect.
        onWaypointsChange: (wps) => {
          commitRouteEdit(
            j.id,
            (edges) => ({
              ...edges,
              [j.id]: mergeEdgeEntry(edges[j.id], {
                waypoints: wps,
                waypoint: undefined,
                routeMode: "manual",
              }),
            }),
            { waypoints: wps, waypoint: undefined, routeMode: "manual" },
          );
        },
        onWaypointReset: () => {
          // Bug-8762: copy-on-write via the shared primitive — never mutate
          // the cached nested entry in place (layoutRef aliases the query
          // cache object, so in-place deletion survives a failed PATCH).
          commitRouteEdit(
            j.id,
            (edges) => clearEdgeWaypoints(edges, j.id),
            { waypoint: undefined, waypoints: undefined },
          );
        },
        onSourceSideChange: (side, ratio) =>
          commitAttachmentChange(j.id, j.left_table_id, "source", side, ratio, edgeOffsets.get(j.id) ?? 0, totalEdges),
        onTargetSideChange: (side, ratio) =>
          commitAttachmentChange(j.id, j.right_table_id, "target", side, ratio, edgeOffsets.get(j.id) ?? 0, totalEdges),
      };

      const dashed =
        dimmedTableIds.has(j.left_table_id) ||
        dimmedTableIds.has(j.right_table_id);
      const baseStroke = sourceIsFact || targetIsFact
        ? palette.joinFactDim
        : palette.joinSameType;

      return {
        id: j.id,
        source: j.left_table_id,
        sourceHandle: j.left_column_name,
        target: j.right_table_id,
        targetHandle: j.right_column_name,
        type: "crowsFoot",
        data: edgeData,
        style: dashed
          ? { stroke: baseStroke, strokeDasharray: "4 4", opacity: 0.4 }
          : { stroke: baseStroke },
      };
    });

    // Apply persisted positions and sizes. When joins change, keep the
    // currently visible positions so adding an edge does not re-run the force
    // layout and move every table. Only genuinely new/unpositioned tables are
    // sent through auto-layout.
    const persisted = layoutRef.current.tables ?? {};
    const liveById = new Map(liveNodesRef.current.map((n) => [n.id, n]));
    const hydrated = rfNodes.map((n) => {
      const saved = persisted[n.id];
      const live = liveById.get(n.id);
      const hasSavedPosition = saved && Number.isFinite(saved.x) && Number.isFinite(saved.y);
      const hasLivePosition = live && Number.isFinite(live.position?.x) && Number.isFinite(live.position?.y);
      const pos = hasSavedPosition
        ? { x: saved!.x, y: saved!.y }
        : hasLivePosition
          ? { x: live!.position.x, y: live!.position.y }
          : n.position;
      // Without this a pinned table reopened movable: the flag was persisted
      // and the worker's snapshot builder reads it from node data, which
      // nothing ever wrote.
      const pinned = saved?.pinned === true;
      // The same rule as rollback and undo. The previous version ignored any
      // saved height of 160 or less while the resizer's own minimum is 150, so
      // a card the user was allowed to make 150 tall reopened at a different
      // height and their edit looked lost.
      const style = styleForTableEntry(
        (live?.style ?? n.style) as Record<string, unknown> | undefined,
        saved,
      );
      return { ...n, position: pos, style, data: { ...n.data, pinned } };
    });
    // A node "needs placement" when it has no persisted or live position — a
    // genuinely new table. Only those should be moved by auto-layout; every
    // already-positioned node is pinned so the user's manual arrangement is
    // preserved when a single table is added (F-026-03).
    const needsPlacement = (n: Node) =>
      n.position.x === 0 && n.position.y === 0 && !persisted[n.id] && !liveById.has(n.id);
    const unplaced = hydrated.filter(needsPlacement);
    // Placement itself is NOT computed here. Hydration only records which
    // tables are genuinely new; the worker places them once React Flow has
    // measured the cards, through the same path every Arrange action uses.
    if (unplaced.length > 0) {
      initialPlacementRef.current = unplaced.map((n) => n.id);
    }

    setNodes(hydrated);
    setEdges(rfEdges);
  }, [
    projectId,
    modelId,
    tables,
    joins,
    tableTypeMap,
    segmentationByTableId,
    hierarchyGroupsByTableId,
    overlayByTableId,
    dimmedTableIds,
    readOnly,
    flushLayout,
    setNodes,
    setEdges,
    selectObject,
    openPanel,
  ]);

  const onEdgeClick: EdgeMouseHandler = useCallback(
    (_event, edge) => {
      if (
        (edge.source && dimmedTableIds.has(edge.source)) ||
        (edge.target && dimmedTableIds.has(edge.target))
      ) {
        return;
      }
      // The relationship is still SELECTED — that is what the layout panel acts
      // on, and what draws the highlight. Only the drawer is withheld.
      //
      // Bug-10034: the Joins drawer opens over the layout panel, so selecting a
      // relationship hid the Lock Route control the user had just gone looking
      // for. Until the panels are rearranged properly, this lets them turn the
      // drawer off for as long as they are working on routes.
      selectObject(edge.id, "join");
      if (joinsDrawerSuppressed) return;
      openPanel("joins");
    },
    [selectObject, openPanel, dimmedTableIds, joinsDrawerSuppressed],
  );

  // Clicking empty canvas clears the current selection and retracts any open
  // context panel — so the canvas background acts as a "dismiss" target
  // (Bug-5331). The drawer is intentionally non-modal (no backdrop) for the
  // joins workflow, so while a join is being drawn (connecting mode) the panel
  // must stay open; we skip the close in that case.
  const onPaneClick = useCallback(() => {
    selectObject(null, null);
    if (isConnectingMode) return;
    if (activePanel) closePanel();
  }, [selectObject, isConnectingMode, activePanel, closePanel]);

  const onConnectStart = useCallback(
    (_event: unknown, params: OnConnectStartParams) => {
      connectingNodeId.current  = params.nodeId ?? null;
      connectionHandled.current = false;
    },
    [],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      connectionHandled.current = true;
      if (connection.source && connection.target && connection.source !== connection.target) {
        const check = classifyJoinEndpoints(
          tableTypeMap.get(connection.source),
          tableTypeMap.get(connection.target),
          t,
        );
        if (check.isSameType && check.sameTypeLabel) {
          setGlobalMessage(sameTypeWarningText(check.sameTypeLabel, t), "warning");
        }
        setPendingJoin({
          leftTableId:  connection.source,
          rightTableId: connection.target,
        });
        openPanel("joins");
      }
    },
    [setPendingJoin, openPanel, tableTypeMap, setGlobalMessage, t],
  );

  const onConnectEnd = useCallback(
    (event: MouseEvent | TouchEvent) => {
      if (connectionHandled.current) {
        connectionHandled.current  = false;
        connectingNodeId.current   = null;
        return;
      }

      const srcId = connectingNodeId.current;
      connectingNodeId.current = null;
      if (!srcId) return;

      const rawTarget = "changedTouches" in event
        ? document.elementFromPoint(
            event.changedTouches[0].clientX,
            event.changedTouches[0].clientY,
          )
        : (event.target as Element);

      let el: Element | null = rawTarget;
      while (el && !el.classList.contains("react-flow__node")) {
        el = el.parentElement;
      }
      if (!el) return;

      const tgtId = (el as HTMLElement).dataset.id;
      if (!tgtId || tgtId === srcId) return;

      const check = classifyJoinEndpoints(
        tableTypeMap.get(srcId),
        tableTypeMap.get(tgtId),
        t,
      );
      if (check.isSameType && check.sameTypeLabel) {
        setGlobalMessage(sameTypeWarningText(check.sameTypeLabel, t), "warning");
      }
      setPendingJoin({ leftTableId: srcId, rightTableId: tgtId });
      openPanel("joins");
    },
    [setPendingJoin, openPanel, tableTypeMap, setGlobalMessage, t],
  );

  return (
    <div data-testid="canvas" style={{ width: "100%", height: "100%", minHeight: 300, backgroundColor: palette.mint }}>
      <style>{`
        .react-flow__node .react-flow__resize-control.handle {
          opacity: 0;
          transition: opacity 150ms;
          background: #90a4ae !important;
          border-radius: 2px !important;
        }
        .react-flow__node:hover .react-flow__resize-control.handle,
        .react-flow__node.selected .react-flow__resize-control.handle {
          opacity: 0.7;
        }
        @keyframes pulse {
          0%, 100% { opacity: 0.4; }
          50% { opacity: 1; }
        }
        .canvas-exporting-icon {
          opacity: 0.4;
          animation: pulse 1s ease-in-out infinite;
        }
      `}</style>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        onNodesChange={readOnly ? undefined : handleNodesChange}
        onEdgesChange={readOnly ? undefined : onEdgesChange}
        onEdgeClick={readOnly ? undefined : onEdgeClick}
        onPaneClick={onPaneClick}
        onConnectStart={readOnly ? undefined : onConnectStart}
        onConnect={readOnly ? undefined : onConnect}
        onConnectEnd={readOnly ? undefined : onConnectEnd}
        onInit={(instance) => {
          reactFlowInstance.current = instance;
          // Publish zoom/fit controls so the page-level keyboard shortcuts
          // (+ / - / 0) can drive this canvas (F-026-08). Read through the ref
          // so the latest callback is used regardless of render timing.
          onViewControlsReadyRef.current?.({
            zoomIn: () => instance.zoomIn?.({ duration: 200 }),
            zoomOut: () => instance.zoomOut?.({ duration: 200 }),
            fitView: () => instance.fitView?.({ padding: 0.2, duration: 300 }),
          });
        }}
        deleteKeyCode={null}
        connectionMode={ConnectionMode.Loose}
        connectionLineType={ConnectionLineType.SmoothStep}
        connectionLineStyle={{
          stroke: palette.joinFactDim,
          strokeWidth: 2,
          strokeDasharray: "6 4",
        }}
        nodesDraggable={!readOnly}
        nodesConnectable={!readOnly}
        elementsSelectable={!readOnly}
        elevateEdgesOnSelect
        fitView
        fitViewOptions={{ padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
      >
        <Controls>
          {!readOnly && (
            <ControlButton onClick={undo} title={t("canvas.undoButton")} disabled={!canUndo}>
              <UndoIcon sx={{ fontSize: 16, opacity: canUndo ? 1 : 0.35 }} />
            </ControlButton>
          )}
          {!readOnly && (
            <ControlButton onClick={redo} title={t("canvas.redoButton")} disabled={!canRedo}>
              <RedoIcon sx={{ fontSize: 16, opacity: canRedo ? 1 : 0.35 }} />
            </ControlButton>
          )}
          {!readOnly && (
            <ControlButton onClick={() => setLayoutMenuOpen(!layoutMenuOpen)} title={t("canvas.layoutPresets")}>
              <AccountTreeIcon sx={{ fontSize: 16 }} />
            </ControlButton>
          )}
          <ControlButton
            onClick={handleExportCanvas}
            onContextMenu={(e) => { e.preventDefault(); setExportFormat((f) => f === "png" ? "svg" : "png"); }}
            title={exporting ? t("canvas.exportingAs", { format: exportFormat.toUpperCase() }) : t("canvas.exportToggleFormat", { format: exportFormat.toUpperCase() })}
            disabled={exporting}
          >
            <CameraAltIcon className={exporting ? "canvas-exporting-icon" : undefined} sx={{ fontSize: 16 }} />
          </ControlButton>
          {/* Bug-7402: a visible format selector so SVG export is discoverable.
              The right-click toggle on the camera button remains as a shortcut. */}
          <ControlButton
            onClick={() => setExportFormat((f) => (f === "png" ? "svg" : "png"))}
            title={t("canvas.exportFormatToggle", { format: exportFormat.toUpperCase() })}
            disabled={exporting}
          >
            <span
              style={{ fontSize: 9, fontWeight: 700, letterSpacing: 0.3, lineHeight: 1 }}
            >
              {exportFormat.toUpperCase()}
            </span>
          </ControlButton>
          <ControlButton onClick={() => setSearchOpen(!searchOpen)} title={t("canvas.searchTables")}>
            <SearchIcon sx={{ fontSize: 16 }} />
          </ControlButton>
          {!readOnly && (
            <ControlButton onClick={handleCopyShareLink} title={t("canvas.copyShareLink")}>
              <LinkIcon sx={{ fontSize: 16 }} />
            </ControlButton>
          )}
          {!readOnly && (
            <ControlButton onClick={() => setNotesOpen(!notesOpen)} title={t("canvas.modelAnnotations")}>
              <NoteIcon sx={{ fontSize: 16 }} />
            </ControlButton>
          )}
          {/* Red while the drawer is being held back, so the canvas always says
              which way the switch is set. A control that changes what a click
              does must not look the same in both states. */}
          <ControlButton
            onClick={() => setJoinsDrawerSuppressed((on) => !on)}
            title={joinsDrawerSuppressed ? t("canvas.joinsDrawerAllow") : t("canvas.joinsDrawerSuppress")}
            aria-pressed={joinsDrawerSuppressed}
            data-testid="toggle-joins-drawer"
          >
            <CableIcon sx={{ fontSize: 16, color: joinsDrawerSuppressed ? "#d32f2f" : undefined }} />
          </ControlButton>
          <ControlButton
            onClick={() => setMinimapDismissed((hidden) => !hidden)}
            title={minimapVisible ? t("canvas.minimapHide") : t("canvas.minimapShow")}
            aria-pressed={minimapVisible}
            data-testid="toggle-minimap"
          >
            <MapIcon sx={{ fontSize: 16, opacity: minimapVisible ? 1 : 0.45 }} />
          </ControlButton>
        </Controls>
        {((hiddenJoinCount > 0 && hiddenJoinCount !== dismissedHiddenJoinsCount) || (notesOpen && !readOnly)) && (
          <Panel position="bottom-center">
            <div style={{ display: "flex", flexDirection: "column", gap: 8, alignItems: "center" }}>
              {hiddenJoinCount > 0 && hiddenJoinCount !== dismissedHiddenJoinsCount && (
                // R3 (alert-mechanism audit, 2026-08-25): this was a hand-rolled
                // <div> with hardcoded hex colors and a raw "&times;" dismiss
                // button — the one message-like element in the app that didn't
                // use MUI's Alert, unlike ValidationTray/every panel-local
                // alert. Same severity vocabulary, same look, still placed
                // inside ReactFlow's <Panel> for canvas-relative positioning.
                <Alert
                  severity="warning"
                  variant="standard"
                  aria-live="polite"
                  onClose={() => setDismissedHiddenJoinsCount(hiddenJoinCount)}
                  sx={{ maxWidth: 320, fontSize: 11, py: 0.5 }}
                >
                  {t("canvas.hiddenJoinsWarning", { count: String(hiddenJoinCount) })}
                </Alert>
              )}
              {notesOpen && !readOnly && (
                <div style={{ background: "#fff", border: "1px solid #cfd8dc", borderRadius: 4, padding: 8, width: 280 }}>
                  <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>{t("canvas.modelAnnotations")}</div>
                  <textarea
                    value={notesText}
                    onChange={(e) => { notesDirtyRef.current = true; setNotesText(e.target.value); }}
                    placeholder={t("canvas.annotationsPlaceholder")}
                    rows={5}
                    maxLength={NOTES_MAX_LENGTH}
                    style={{ width: "100%", border: "1px solid #ccc", borderRadius: 3, padding: 4, fontSize: 12, resize: "vertical" }}
                  />
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 4, marginTop: 4 }}>
                    <span style={{ fontSize: 10, color: "#78909c" }}>
                      {t("canvas.annotationsCharCount", { count: String(notesText.length), max: String(NOTES_MAX_LENGTH) })}
                    </span>
                    <div style={{ display: "flex", gap: 4 }}>
                      <button onClick={() => setNotesOpen(false)} style={{ fontSize: 11, cursor: "pointer" }}>{t("common.cancel")}</button>
                      <button onClick={handleSaveNotes} style={{ fontSize: 11, cursor: "pointer", fontWeight: 600 }}>{t("canvas.saveButton")}</button>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </Panel>
        )}
        {layoutMenuOpen && !readOnly && (
          <Panel position="top-left">
            <CanvasLayoutPanel
              preferences={layoutPreferences}
              busy={layoutController.busy}
              tableCount={nodes.length}
              selectedTableCount={selectedTablePins.count}
              movableSelectedCount={movableSelectedCount}
              onPreferenceChange={(next) => {
                // A choice is the user's, so stop seeding from the server copy;
                // it is not persisted until an arrangement succeeds with it.
                layoutPreferencesDirtyRef.current = true;
                setLayoutPreferences((current) => ({ ...current, ...next }));
              }}
              onArrangeAll={(preset) => {
                // Direction and spacing travel with the action, not with the
                // engine default, so the controls actually steer the arrangement.
                const applying = { ...layoutPreferences, preset };
                layoutPreferencesDirtyRef.current = true;
                setLayoutPreferences(applying);
                void handleApplyLayout("arrange-all", applying).then((applied) => {
                  if (applied) persistLayoutPreferences(applying);
                });
              }}
              onArrangeSelected={() => {
                void handleApplyLayout("arrange-selected", layoutPreferences).then((applied) => {
                  if (applied) persistLayoutPreferences(layoutPreferences);
                });
              }}
              onRerouteLinks={() => { void handleApplyLayout("reroute-links"); }}
              routeLock={routeLockState}
              onToggleRouteLock={handleToggleRouteLock}
              tablePins={pinState}
              onTogglePin={handleTogglePin}
            />
          </Panel>
        )}
        {layoutController.busy && (
          <Panel position="top-center">
            <div style={{ background: "#fff", border: "1px solid #cfd8dc", borderRadius: 4, padding: "4px 10px", display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
              <span>{t("canvas.layoutWorking")}</span>
              <button onClick={() => layoutController.cancel()} style={{ fontSize: 11, cursor: "pointer" }}>{t("common.cancel")}</button>
            </div>
          </Panel>
        )}
        {searchOpen && (
          <Panel position="top-left">
            <div style={{ background: "#fff", border: "1px solid #cfd8dc", borderRadius: 4, padding: 6, display: "flex", gap: 4 }}>
              <input
                type="text"
                placeholder={t("canvas.searchTablesPlaceholder")}
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") handleSearch(searchTerm); if (e.key === "Escape") setSearchOpen(false); }}
                style={{ border: "1px solid #ccc", borderRadius: 3, padding: "2px 6px", fontSize: 13, width: 180 }}
                autoFocus
              />
              <button onClick={() => handleSearch(searchTerm)} style={{ fontSize: 12, cursor: "pointer" }}>{t("canvas.goButton")}</button>
            </div>
          </Panel>
        )}
        {(personas.data ?? []).length > 0 && (
          <Panel position="top-right">
            <div
              style={{
                background: "#ffffff",
                border: "1px solid #cfd8dc",
                borderRadius: 4,
                padding: "4px 6px",
                boxShadow: "0 1px 2px rgba(0,0,0,0.08)",
                display: "flex",
                alignItems: "center",
                gap: 6,
              }}
              aria-label={t("canvas.personaPreview")}
              data-testid="persona-picker"
            >
              <PersonaPicker
                projectId={projectId}
                modelId={modelId}
                value={personaId}
                onChange={setPersonaId}
                label={t("canvas.previewPersona")}
                forAudience={false}
              />
              {personaId && dimmedTableIds.size > 0 && (
                <span
                  style={{
                    fontSize: 11,
                    color: "#607d8b",
                    whiteSpace: "nowrap",
                  }}
                >
                  {t("canvas.hiddenCount", { count: String(dimmedTableIds.size) })}
                </span>
              )}
              {personaId &&
                clsRestrictedObjectIds.measureIds.size +
                  clsRestrictedObjectIds.dimensionIds.size >
                  0 && (
                  <span
                    style={{ fontSize: 11, color: "#b00020", whiteSpace: "nowrap" }}
                  >
                    {t("canvas.clsRestrictedCount", {
                      count: String(
                        clsRestrictedObjectIds.measureIds.size +
                          clsRestrictedObjectIds.dimensionIds.size,
                      ),
                    })}
                  </span>
                )}
              {personaId && personaDefaultFilterChips.length > 0 && (
                <span
                  style={{ fontSize: 11, color: "#607d8b", whiteSpace: "nowrap" }}
                >
                  {t("canvas.defaultFilterScope", {
                    filters: personaDefaultFilterChips.join("; "),
                  })}
                </span>
              )}
            </div>
          </Panel>
        )}
        {minimapVisible && (
          <>
            <MiniMap
              nodeColor={minimapNodeColor}
              nodeStrokeWidth={2}
              zoomable
              pannable
              ariaLabel={t("canvas.minimap")}
            />
            {/* Sits on the minimap's own corner. The toolbelt button brings it
                back, so dismissing it is never a one-way door. */}
            <button
              type="button"
              onClick={() => setMinimapDismissed(true)}
              title={t("canvas.minimapClose")}
              aria-label={t("canvas.minimapClose")}
              data-testid="close-minimap"
              style={{
                position: "absolute",
                right: 18,
                bottom: 138,
                zIndex: 6,
                width: 18,
                height: 18,
                lineHeight: 1,
                padding: 0,
                cursor: "pointer",
                background: "#ffffff",
                border: "1px solid #cfd8dc",
                borderRadius: 3,
                color: "#455a64",
                fontSize: 12,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              <CloseIcon sx={{ fontSize: 12 }} />
            </button>
          </>
        )}
        <Background gap={20} color={palette.canvasDot} />
      </ReactFlow>
    </div>
  );
}
