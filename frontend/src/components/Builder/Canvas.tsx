/**
 * Canvas — ReactFlow ERD graph for model tables and joins.
 *
 * Layout:  fact tables in the centre, dimensions arranged radially around them.
 * Nodes:   ERDTableNode — shows header, scrollable column list, perimeter handles.
 * Edges:   CrowsFootEdge — IDEF1X / UML / diamond markers, with draggable
 *          midpoint and side anchors for manual routing.
 */
import { useEffect, useCallback, useMemo, useRef, useState } from "react";
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
import SearchIcon from "@mui/icons-material/Search";
import UndoIcon from "@mui/icons-material/Undo";
import RedoIcon from "@mui/icons-material/Redo";
import type { CanvasLayout, ModelTable, Join } from "../../api/types";
import { modelsApi } from "../../api/client";
import { extractApiError } from "../../utils/extractApiError";
import { useBuilderStore } from "../../store/builderStore";
import { useModelEditorStore } from "../../store/useModelEditorStore";
import { useConfirm } from "../Confirm/useConfirm";
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
import { partitionJoinsByEndpoints } from "./joinFilter";
import { nodeHeader, nodeHeaderFallback, palette } from "../../theme/tokens";
import { classifyJoinEndpoints, sameTypeWarningText } from "../../lib/joinRules";
import { computeDimmedTableIds } from "./personaOverlay";
import { positionsFromNodes, useCanvasHistory, type NodePositions } from "./useCanvasHistory";
import dagre from "@dagrejs/dagre";
import {
  forceSimulation, forceLink, forceManyBody, forceCollide,
  forceRadial, forceX, forceY,
  type SimulationNodeDatum,
} from "d3-force";

interface ForceNode extends SimulationNodeDatum {
  id: string;
  width: number;
  height: number;
  fact: boolean;
}

// Minimap hides on narrow viewports (mobile/small tablet).
const MINIMAP_MIN_VW = 900;

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

// ---------------------------------------------------------------------------
// Layout engine — d3-force (collision-aware radial) + dagre (hierarchical).
// ---------------------------------------------------------------------------

function nodeDim(n: Node): { w: number; h: number } {
  return { w: (n as any).width || 260, h: (n as any).height || 200 };
}

type AnchorSide = "left" | "right" | "top" | "bottom";

interface LayoutAnchor {
  sourceSide: AnchorSide;
  targetSide: AnchorSide;
  sourceRatio: number;
  targetRatio: number;
}

interface SideEndpoint {
  edgeId: string;
  endpoint: "source" | "target";
  sortCoord: number;
}

function clampAnchorRatio(value: number): number {
  return Math.max(0.08, Math.min(0.92, value));
}

function sideRatioToward(
  pos: { x: number; y: number },
  dim: { w: number; h: number },
  side: AnchorSide,
  towardCenter: { x: number; y: number },
): number {
  if (side === "left" || side === "right") {
    return clampAnchorRatio((towardCenter.y - pos.y) / dim.h);
  }
  return clampAnchorRatio((towardCenter.x - pos.x) / dim.w);
}

function distributeLayoutAnchors(
  edges: Edge[],
  tableMap: Record<string, { x: number; y: number }>,
  dims: Map<string, { w: number; h: number }>,
): Map<string, LayoutAnchor> {
  const anchors = new Map<string, LayoutAnchor>();
  const sideGroups = new Map<string, SideEndpoint[]>();

  const addEndpoint = (
    edgeId: string,
    endpoint: "source" | "target",
    nodeId: string,
    side: AnchorSide,
    sortCoord: number,
  ) => {
    const key = `${nodeId}:${side}`;
    const group = sideGroups.get(key) ?? [];
    group.push({ edgeId, endpoint, sortCoord });
    sideGroups.set(key, group);
  };

  for (const edge of edges) {
    const srcPos = tableMap[edge.source];
    const tgtPos = tableMap[edge.target];
    const srcD = dims.get(edge.source);
    const tgtD = dims.get(edge.target);
    if (!srcPos || !tgtPos || !srcD || !tgtD) continue;

    const scx = srcPos.x + srcD.w / 2;
    const scy = srcPos.y + srcD.h / 2;
    const tcx = tgtPos.x + tgtD.w / 2;
    const tcy = tgtPos.y + tgtD.h / 2;
    const dx = tcx - scx;
    const dy = tcy - scy;

    const sourceSide: AnchorSide = Math.abs(dx) >= Math.abs(dy)
      ? (dx > 0 ? "right" : "left")
      : (dy > 0 ? "bottom" : "top");
    const targetSide: AnchorSide = Math.abs(dx) >= Math.abs(dy)
      ? (dx < 0 ? "right" : "left")
      : (dy < 0 ? "bottom" : "top");

    anchors.set(edge.id, {
      sourceSide,
      targetSide,
      sourceRatio: sideRatioToward(srcPos, srcD, sourceSide, { x: tcx, y: tcy }),
      targetRatio: sideRatioToward(tgtPos, tgtD, targetSide, { x: scx, y: scy }),
    });

    const sourceSort = sourceSide === "left" || sourceSide === "right" ? tcy : tcx;
    const targetSort = targetSide === "left" || targetSide === "right" ? scy : scx;
    addEndpoint(edge.id, "source", edge.source, sourceSide, sourceSort);
    addEndpoint(edge.id, "target", edge.target, targetSide, targetSort);
  }

  for (const group of sideGroups.values()) {
    group.sort((a, b) => a.sortCoord - b.sortCoord || a.edgeId.localeCompare(b.edgeId));
    const count = group.length;
    group.forEach((endpoint, index) => {
      const anchor = anchors.get(endpoint.edgeId);
      if (!anchor) return;
      const ratio = count === 1 ? 0.5 : (index + 1) / (count + 1);
      if (endpoint.endpoint === "source") anchor.sourceRatio = ratio;
      else anchor.targetRatio = ratio;
    });
  }

  return anchors;
}

/**
 * d3-force radial layout: facts cluster in the centre, dims orbit around.
 *
 * `pinnedIds` (F-026-03): nodes that already have a user-placed / persisted
 * position are pinned with `fx`/`fy` so the simulation does not move them.
 * Only unpinned nodes (genuinely new / unpositioned tables) settle. When
 * `pinnedIds` is empty (first-ever layout of a model), every node settles as
 * before. This stops a hand-arranged diagram from being scrambled the moment
 * one new table is added.
 */
function layoutForceRadial(nodes: Node[], edges: Edge[], pinnedIds?: Set<string>): Node[] {
  const factIds = new Set(
    nodes.filter((n) => (n.data as ERDNodeData).table?.table_type === "fact").map((n) => n.id),
  );
  const isFact = (id: string) => factIds.has(id);
  const pinned = pinnedIds ?? new Set<string>();

  const fnodes: ForceNode[] = nodes.map((n) => {
    const { w, h } = nodeDim(n);
    const fnode: ForceNode = { id: n.id, width: w, height: h, fact: isFact(n.id), x: n.position.x, y: n.position.y };
    if (pinned.has(n.id)) {
      // Pin to the current position so existing tables stay put.
      fnode.fx = n.position.x;
      fnode.fy = n.position.y;
    }
    return fnode;
  });

  const links = edges.map((e) => ({
    source: fnodes.find((f) => f.id === e.source)!,
    target: fnodes.find((f) => f.id === e.target)!,
  }));
  const dimensionCount = fnodes.length - factIds.size;
  const ringRadius = Math.max(320, (dimensionCount * 320) / (2 * Math.PI));

  const sim = forceSimulation<ForceNode>(fnodes)
    .force("link", forceLink<ForceNode, { source: ForceNode; target: ForceNode }>(links)
      .distance(250)
      .strength(0.4))
    .force("charge", forceManyBody().strength(-1200))
    .force("collide", forceCollide<ForceNode>()
      .radius((d) => Math.max(d.width, d.height) / 2 + 30)
      .strength(1))
    .force("factX", forceX<ForceNode>(0).strength((d) => d.fact ? 0.25 : 0))
    .force("factY", forceY<ForceNode>(0).strength((d) => d.fact ? 0.25 : 0))
    .force("dimensionRing", forceRadial<ForceNode>(ringRadius, 0, 0)
      .strength((d) => d.fact ? 0 : 0.2))
    .stop();

  for (let i = 0; i < 500; i++) sim.tick();

  return nodes.map((n) => {
    const f = fnodes.find((d) => d.id === n.id);
    if (!f || f.x == null || f.y == null) return n;
    return { ...n, position: { x: f.x, y: f.y } };
  });
}

/** dagre hierarchical — rankdir TB, configurable spacing. */
function dagreLayout(nodes: Node[], edges: Edge[], opts: { rankdir: string; nodesep: number; ranksep: number }): Node[] {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: opts.rankdir, nodesep: opts.nodesep, ranksep: opts.ranksep, marginx: 40, marginy: 40 });

  for (const n of nodes) {
    const { w, h } = nodeDim(n);
    g.setNode(n.id, { width: w, height: h });
  }
  for (const e of edges) {
    g.setEdge(e.source, e.target);
  }

  dagre.layout(g);

  return nodes.map((n) => {
    const pos = g.node(n.id);
    if (!pos) return n;
    return {
      ...n,
      position: {
        x: pos.x - pos.width! / 2,
        y: pos.y - pos.height! / 2,
      },
    };
  });
}

function layoutHierarchical(nodes: Node[], edges: Edge[]): Node[] {
  return dagreLayout(nodes, edges, { rankdir: "TB", nodesep: 100, ranksep: 120 });
}

function layoutCompact(nodes: Node[], edges: Edge[]): Node[] {
  return dagreLayout(nodes, edges, { rankdir: "TB", nodesep: 60, ranksep: 80 });
}

type LayoutPreset = "radial" | "hierarchical" | "compact";

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
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const liveNodesRef = useRef<Node[]>([]);
  useEffect(() => {
    liveNodesRef.current = nodes;
  }, [nodes]);

  // Drop the published view controls when this canvas unmounts so the page
  // shell doesn't drive a stale ReactFlow instance (F-026-08).
  useEffect(() => {
    return () => onViewControlsReady?.(null);
  }, [onViewControlsReady]);
  const queryClient = useQueryClient();

  const isConnectingMode = useBuilderStore((s) => s.isConnectingMode);

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
  useEffect(() => {
    if (userDirtyRef.current) return;
    layoutRef.current = canvasLayout ?? {};
  }, [canvasLayout]);

  const flushLayout = useCallback(() => {
    userDirtyRef.current = true;
    if (flushTimer.current !== null) {
      window.clearTimeout(flushTimer.current);
    }
    flushTimer.current = window.setTimeout(() => {
      flushTimer.current = null;
      modelsApi
        .update(projectId, modelId, { canvas_layout: layoutRef.current })
        .catch(() => {
          // swallow — next drag stop will retry
        });
    }, 600);
  }, [projectId, modelId]);

  useEffect(() => {
    // Flush any pending layout update if the component unmounts mid-debounce.
    return () => {
      if (flushTimer.current !== null) {
        window.clearTimeout(flushTimer.current);
        modelsApi
          .update(projectId, modelId, { canvas_layout: layoutRef.current })
          .catch((e: unknown) => console.warn("Canvas layout save failed:", e));
      }
    };
  }, [projectId, modelId]);

  const [layoutMenuOpen, setLayoutMenuOpen] = useState(false);

  // Undo/redo writes positions through here so they hit the same
  // canvas_layout persistence path as a normal drag (F-026-02).
  const applyMovePositions = useCallback(
    (positions: NodePositions) => {
      const tableMap = { ...(layoutRef.current.tables ?? {}) };
      for (const [id, p] of Object.entries(positions)) {
        const prev = tableMap[id] ?? {};
        tableMap[id] = { ...prev, x: p.x, y: p.y };
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
            },
          };
        }),
      );
    },
    [flushLayout, setEdges],
  );

  const { beginMove, endMove, recordMove, undo, redo, canUndo, canRedo } =
    useCanvasHistory(
      nodes,
      setNodes,
      projectId,
      modelId,
      queryClient,
      applyMovePositions,
      applyEdgeLayouts,
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
    );
  const isDraggingRef = useRef(false);

  const handleRedrawLayout = useCallback((preset: LayoutPreset = "radial") => {
    const beforePositions = positionsFromNodes(nodes);
    // Snapshot the edge layout BEFORE the redraw clears waypoints, so undo can
    // restore manual edge routing (review finding 2 / LOW-2). Deep-cloned so
    // the history entry is immune to later mutation of layoutRef.
    const beforeEdges = JSON.parse(
      JSON.stringify(layoutRef.current.edges ?? {}),
    ) as NonNullable<CanvasLayout["edges"]>;
    let newNodes: Node[];
    if (preset === "hierarchical") {
      newNodes = layoutHierarchical(nodes, edges);
    } else if (preset === "compact") {
      newNodes = layoutCompact(nodes, edges);
    } else {
      newNodes = layoutForceRadial(nodes, edges);
    }
    setNodes(newNodes.map((n) => ({ ...n })));
    const tableMap = { ...(layoutRef.current.tables ?? {}) };
    newNodes.forEach((n) => {
      tableMap[n.id] = { x: n.position.x, y: n.position.y };
    });

    const dims = new Map<string, { w: number; h: number }>();
    for (const n of newNodes) dims.set(n.id, nodeDim(n));
    const anchors = distributeLayoutAnchors(edges, tableMap, dims);

    const next = { ...(layoutRef.current.edges ?? {}) };
    for (const eid of Object.keys(next)) {
      const entry = { ...next[eid] };
      delete entry.waypoint;
      delete entry.waypoints;
      if (Object.keys(entry).length === 0) delete next[eid];
      else next[eid] = entry;
    }

    for (const e of edges) {
      const anchor = anchors.get(e.id);
      if (!anchor) continue;

      const existing = next[e.id] ?? {};
      next[e.id] = {
        ...existing,
        ...anchor,
      };
    }
    layoutRef.current = { ...layoutRef.current, tables: tableMap, edges: next };
    // A layout redraw is one user action — record it as a single undo entry
    // carrying BOTH table positions and the full before/after edge layout, so
    // undo restores manual edge waypoints discarded by the redraw (LOW-2).
    const afterEdges = JSON.parse(JSON.stringify(next)) as NonNullable<CanvasLayout["edges"]>;
    recordMove(beforePositions, positionsFromNodes(newNodes), {
      before: beforeEdges,
      after: afterEdges,
    });
    setEdges((es) =>
      es.map((e) => {
        const edgeEntry = next[e.id];
        return {
          ...e,
          data: {
            ...e.data,
            waypoint: undefined,
            waypoints: undefined,
            sourceSide: edgeEntry?.sourceSide,
            targetSide: edgeEntry?.targetSide,
            sourceRatio: edgeEntry?.sourceRatio,
            targetRatio: edgeEntry?.targetRatio,
            pathing: undefined,
          },
        };
      }),
    );
    flushLayout();
    setTimeout(() => {
      reactFlowInstance.current?.fitView({ padding: 0.2, duration: 800 });
    }, 50);
    setLayoutMenuOpen(false);
  }, [nodes, edges, setNodes, flushLayout, recordMove]);

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

  const [notesOpen, setNotesOpen] = useState(false);
  const [notesText, setNotesText] = useState<string>(() => {
    return canvasLayout?.notes ?? "";
  });
  const handleSaveNotes = useCallback(() => {
    layoutRef.current = { ...layoutRef.current, notes: notesText };
    flushLayout();
    setNotesOpen(false);
  }, [notesText, flushLayout]);

  // ---- Window event channel for cross-component edge controls ------------
  // JoinsPanel buttons fire these events; Canvas owns layoutRef so it must
  // be the consumer. Keeps the edge component pure (no store coupling).
  useEffect(() => {
    const handleResetEdge = (e: Event) => {
      const joinId = (e as CustomEvent<string>).detail;
      const next = { ...(layoutRef.current.edges ?? {}) };
      if (next[joinId]) {
        delete next[joinId].waypoint;
        delete next[joinId].waypoints;
        if (Object.keys(next[joinId]).length === 0) delete next[joinId];
      }
      layoutRef.current = { ...layoutRef.current, edges: next };
      flushLayout();
      setEdges((es) =>
        es.map((edge) =>
          edge.id === joinId
            ? { ...edge, data: { ...edge.data, waypoint: undefined, waypoints: undefined } }
            : edge,
        ),
      );
    };

    const handleTogglePathingAuto = (e: Event) => {
      const joinId = (e as CustomEvent<string>).detail;
      const next = { ...(layoutRef.current.edges ?? {}) };
      const current = next[joinId] ?? {};
      const currentPathing = current.pathing ?? useBuilderStore.getState().relationPathing;
      const newPathing: "orthogonal" | "straight" =
        currentPathing === "straight" ? "orthogonal" : "straight";
      next[joinId] = { ...current, pathing: newPathing, waypoint: undefined, waypoints: undefined };
      layoutRef.current = { ...layoutRef.current, edges: next };
      flushLayout();
      setEdges((es) =>
        es.map((edge) =>
          edge.id === joinId
            ? { ...edge, data: { ...edge.data, pathing: newPathing, waypoint: undefined, waypoints: undefined } }
            : edge,
        ),
      );
    };

    const handleNodeResize = (e: Event) => {
      const { id, w, h } = (e as CustomEvent<{ id: string; w: number; h: number }>).detail;
      const tableMap = { ...(layoutRef.current.tables ?? {}) };
      const prev = tableMap[id] ?? { x: 0, y: 0 };
      tableMap[id] = { ...prev, w, h };
      layoutRef.current = { ...layoutRef.current, tables: tableMap };
      flushLayout();
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
    window.addEventListener("toggle-edge-pathing-auto", handleTogglePathingAuto);
    window.addEventListener("node-resize-end", handleNodeResize);
    window.addEventListener("canvas-center-node", handleCenterNode);
    return () => {
      window.removeEventListener("reset-edge-path", handleResetEdge);
      window.removeEventListener("toggle-edge-pathing-auto", handleTogglePathingAuto);
      window.removeEventListener("node-resize-end", handleNodeResize);
      window.removeEventListener("canvas-center-node", handleCenterNode);
    };
  }, [flushLayout, setEdges]);

  const handleNodesChange = useCallback(
    (changes: NodeChange[]) => {
      // Capture the pre-gesture positions at drag START — the live nodes
      // have not received this batch of changes yet, so they still hold the
      // positions the gesture began from.
      for (const ch of changes) {
        if (ch.type === "position" && ch.dragging === true && !isDraggingRef.current) {
          isDraggingRef.current = true;
          beginMove();
          break;
        }
      }
      onNodesChange(changes);
      let dirty = false;
      let dragEnded = false;
      for (const ch of changes) {
        if (ch.type === "position" && ch.position) {
          const tableMap = { ...(layoutRef.current.tables ?? {}) };
          const prev = tableMap[ch.id] ?? { x: 0, y: 0 };
          tableMap[ch.id] = {
            x: ch.position.x,
            y: ch.position.y,
            ...(prev.w !== undefined ? { w: prev.w } : {}),
            ...(prev.h !== undefined ? { h: prev.h } : {}),
          };
          layoutRef.current = { ...layoutRef.current, tables: tableMap };
          dirty = true;
        }
        if (ch.type === "position" && ch.dragging === false) {
          dragEnded = true;
        }
      }
      if (dirty) flushLayout();
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
        endMove(after);
      }
    },
    [onNodesChange, flushLayout, beginMove, endMove],
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
    // bound to specific fact tables via source_table_id. Count each aggregate
    // against the fact table(s) whose dimensions appear in its grain.
    const factTableIds = new Set(
      tables.filter((t) => t.table_type === "fact").map((t) => t.id),
    );
    // Build dimension-name → fact-table-id map from the dimensions data
    const dimNameToFactTableId = new Map<string, string>();
    for (const d of dimensions.data ?? []) {
      if (d.source_table_id && factTableIds.has(d.source_table_id)) {
        dimNameToFactTableId.set(d.name, d.source_table_id);
      }
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
        const fid = dimNameToFactTableId.get(grainDim);
        if (fid) matched.add(fid);
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
  }, [tables, aggregates.data, pockets.data, dimensions.data]);

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
    for (const m of measures.data ?? []) {
      if (!m.source_table_id) continue;
      if (!measuresByTable.has(m.source_table_id)) measuresByTable.set(m.source_table_id, []);
      measuresByTable.get(m.source_table_id)!.push(m.id);
    }
    const dimsByTable = new Map<string, string[]>();
    for (const d of dimensions.data ?? []) {
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
    );
  }, [
    personaId,
    persona.data,
    tables,
    measures.data,
    dimensions.data,
    hierarchies.data,
  ]);

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
    if (dropped.length > 0) {
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
      const sourceIsDim = tableTypeMap.get(j.left_table_id) === "dimension";
      const targetIsDim = tableTypeMap.get(j.right_table_id) === "dimension";
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
        sourceSide: savedEdge?.sourceSide,
        targetSide: savedEdge?.targetSide,
        sourceRatio: savedEdge?.sourceRatio,
        targetRatio: savedEdge?.targetRatio,
        onWaypointsChange: (wps) => {
          const next = { ...(layoutRef.current.edges ?? {}) };
          next[j.id] = { ...next[j.id], waypoints: wps, waypoint: undefined };
          layoutRef.current = { ...layoutRef.current, edges: next };
          flushLayout();
          setEdges((es) =>
            es.map((e) =>
              e.id === j.id
                ? { ...e, data: { ...e.data, waypoints: wps, waypoint: undefined } }
                : e,
            ),
          );
        },
        onWaypointReset: () => {
          const next = { ...(layoutRef.current.edges ?? {}) };
          if (next[j.id]) {
            delete next[j.id].waypoint;
            delete next[j.id].waypoints;
            if (Object.keys(next[j.id]).length === 0) delete next[j.id];
          }
          layoutRef.current = { ...layoutRef.current, edges: next };
          flushLayout();
          setEdges((es) =>
            es.map((e) =>
              e.id === j.id
                ? { ...e, data: { ...e.data, waypoint: undefined, waypoints: undefined } }
                : e,
            ),
          );
        },
        onSourceSideChange: (side, ratio) => {
          const next = { ...(layoutRef.current.edges ?? {}) };
          next[j.id] = { ...next[j.id], sourceSide: side, sourceRatio: ratio };
          layoutRef.current = { ...layoutRef.current, edges: next };
          flushLayout();
          setEdges((es) =>
            es.map((e) =>
              e.id === j.id
                ? { ...e, data: { ...e.data, sourceSide: side, sourceRatio: ratio } }
                : e,
            ),
          );
        },
        onTargetSideChange: (side, ratio) => {
          const next = { ...(layoutRef.current.edges ?? {}) };
          next[j.id] = { ...next[j.id], targetSide: side, targetRatio: ratio };
          layoutRef.current = { ...layoutRef.current, edges: next };
          flushLayout();
          setEdges((es) =>
            es.map((e) =>
              e.id === j.id
                ? { ...e, data: { ...e.data, targetSide: side, targetRatio: ratio } }
                : e,
            ),
          );
        },
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
      const style: any = { ...(live?.style ?? n.style) };
      if (saved) {
        if (saved.w !== undefined && Number.isFinite(saved.w) && saved.w > 0) {
          style.width = saved.w;
        }
        // Ignore artificially crushed heights from the earlier resizer bug.
        if (saved.h !== undefined && Number.isFinite(saved.h) && saved.h > 160) {
          style.height = saved.h;
        }
      }
      return { ...n, position: pos, style };
    });
    // A node "needs placement" when it has no persisted or live position — a
    // genuinely new table. Only those should be moved by auto-layout; every
    // already-positioned node is pinned so the user's manual arrangement is
    // preserved when a single table is added (F-026-03).
    const needsPlacement = (n: Node) =>
      n.position.x === 0 && n.position.y === 0 && !persisted[n.id] && !liveById.has(n.id);
    const unplaced = hydrated.filter(needsPlacement);
    const needsLayout = unplaced.length > 0;
    const pinnedIds = new Set(
      hydrated.filter((n) => !needsPlacement(n)).map((n) => n.id),
    );
    const finalNodes = needsLayout
      ? layoutForceRadial(hydrated, rfEdges, pinnedIds)
      : hydrated;

    if (needsLayout) {
      const tableMap = { ...(layoutRef.current.tables ?? {}) };
      for (const n of finalNodes) {
        const prev = tableMap[n.id] ?? {};
        tableMap[n.id] = {
          ...prev,
          x: n.position.x,
          y: n.position.y,
        };
      }
      layoutRef.current = { ...layoutRef.current, tables: tableMap };
    }

    setNodes(finalNodes);
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
  ]);

  const onEdgeClick: EdgeMouseHandler = useCallback(
    (_event, edge) => {
      if (
        (edge.source && dimmedTableIds.has(edge.source)) ||
        (edge.target && dimmedTableIds.has(edge.target))
      ) {
        return;
      }
      selectObject(edge.id, "join");
      openPanel("joins");
    },
    [selectObject, openPanel, dimmedTableIds],
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
          // (+ / - / 0) can drive this canvas (F-026-08).
          onViewControlsReady?.({
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
        </Controls>
        {notesOpen && (
          <Panel position="bottom-left">
            <div style={{ background: "#fff", border: "1px solid #cfd8dc", borderRadius: 4, padding: 8, width: 280 }}>
              <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>{t("canvas.modelAnnotations")}</div>
              <textarea
                value={notesText}
                onChange={(e) => setNotesText(e.target.value)}
                placeholder={t("canvas.annotationsPlaceholder")}
                rows={5}
                style={{ width: "100%", border: "1px solid #ccc", borderRadius: 3, padding: 4, fontSize: 12, resize: "vertical" }}
              />
              <div style={{ display: "flex", justifyContent: "flex-end", gap: 4, marginTop: 4 }}>
                <button onClick={() => setNotesOpen(false)} style={{ fontSize: 11, cursor: "pointer" }}>{t("common.cancel")}</button>
                <button onClick={handleSaveNotes} style={{ fontSize: 11, cursor: "pointer", fontWeight: 600 }}>{t("canvas.saveButton")}</button>
              </div>
            </div>
          </Panel>
        )}
        {layoutMenuOpen && (
          <Panel position="top-left">
            <div style={{ background: "#fff", border: "1px solid #cfd8dc", borderRadius: 4, padding: 6, display: "flex", flexDirection: "column", gap: 2 }}>
              {([["radial", t("canvas.layoutRadial")], ["hierarchical", t("canvas.layoutHierarchical")], ["compact", t("canvas.layoutCompact")]] as [LayoutPreset, string][]).map(([preset, label]) => (
                <button
                  key={preset}
                  onClick={() => handleRedrawLayout(preset)}
                  style={{ fontSize: 12, cursor: "pointer", padding: "4px 10px", textAlign: "left", border: "none", background: "transparent", borderRadius: 3 }}
                  onMouseEnter={(e) => { (e.target as HTMLButtonElement).style.background = "#e3f2fd"; }}
                  onMouseLeave={(e) => { (e.target as HTMLButtonElement).style.background = "transparent"; }}
                >
                  {label}
                </button>
              ))}
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
            </div>
          </Panel>
        )}
        {showMinimap && (
          <MiniMap
            nodeColor={minimapNodeColor}
            nodeStrokeWidth={2}
            zoomable
            pannable
            ariaLabel={t("canvas.minimap")}
          />
        )}
        <Background gap={20} color={palette.canvasDot} />
      </ReactFlow>
    </div>
  );
}
