/**
 * CrowsFootEdge — polyline / orthogonal connector with Visio-style segment dragging.
 *
 * Two pathing modes:
 *   "orthogonal"  — auto-routed H/V segments; drag a middle segment to reposition.
 *   "straight"    — free-angle polyline; click a segment midpoint to add a bend.
 *
 * Markers (crow's-foot, one-bar, etc.) are drawn at the card border in IDEF1X
 * orientation. The polyline starts/ends at the marker heel so the edge line
 * connects cleanly.
 */
import {
  memo,
  useCallback,
  useState,
  useEffect,
  useRef,
  type ReactNode,
  MouseEvent as ReactMouseEvent,
} from "react";
import {
  useReactFlow,
  useStore,
  Position,
  type EdgeProps,
  type ReactFlowState,
} from "reactflow";
import { useBuilderStore } from "../../store/builderStore";
import { useT } from "../../i18n";
import { isOuterJoinType } from "../../lib/joinRules";
import {
  EdgeLabelRenderer,
} from "reactflow";
import {
  type Pt,
  awayVec,
  polylinePath,
  closestSegIdx,
  applyOrthoSegmentDrag,
  applyFreeSegmentDrag,
  midpoints,
} from "./edgeRouting";
import {
  SIDE_TO_POSITION,
  isDrawableRoute,
  resolveDisplayedDocking,
  resolveDisplayedWaypoints,
  routeEntersCard,
} from "./edgeGeometry";
// Docking maths comes from the module the worker uses, so a drawn dock and a
// routed dock cannot drift apart.
import { pointOnSide, ratioWithParallelOffset } from "./layout/docking";
import type { Rect } from "./layout/types";
import { applyOrthoBendDrag } from "./edgeOrthogonal";

// ---------------------------------------------------------------------------
// Public edge-data contract
// ---------------------------------------------------------------------------

/** Shared with `ERDTableNode`: one colour means "locked" across the canvas. */
const LOCKED_ACCENT = "#C2185B";

export interface CrowsFootEdgeData {
  joinType: string;
  sourceIsFact: boolean;
  targetIsFact: boolean;
  /** Whether each endpoint is a dimension table. Used to give dimension-to-
   *  dimension (snowflake / outrigger) edges a default one/many cardinality
   *  pair, which the fact-asymmetry rule alone leaves unmarked (F-026-21). */
  sourceIsDim?: boolean;
  targetIsDim?: boolean;
  offsetIndex?: number;
  totalEdges?: number;
  /** Fan-out frozen at lock time; overrides the recomputed one while locked. */
  lockedParallelOffset?: number;
  /** Legacy single waypoint — converted to array on first read. */
  waypoint?: { x: number; y: number };
  waypoints?: Pt[];
  pathing?: "orthogonal" | "straight";
  /** Provenance of the stored bends: "auto" when the layout engine computed
   *  them, "manual" when the user edited the route. Absent is resolved on read
   *  (waypoints present => manual, none => auto), which is how layouts written
   *  before this field existed stay correct. */
  routeMode?: "auto" | "manual";
  sourceSide?: string;
  targetSide?: string;
  sourceRatio?: number;
  targetRatio?: number;
  /** The complete displayed route and docking are frozen. Independent of a
   *  table pin; unlocking never unpins. Absent means false, so a layout
   *  written before route locking existed stays valid. */
  locked?: boolean;
  /** The columns this relationship joins on, shown when it is selected (R09). */
  sourceColumn?: string | null;
  targetColumn?: string | null;

  /** Read-only share-link mode (Bug-7636). When true, the edge suppresses
   *  waypoint, anchor, and pathing mutations so viewers cannot accidentally
   *  persist layout changes through the edge's own DOM handlers. */
  readOnly?: boolean;
  onWaypointsChange?: (wps: Pt[]) => void;
  onWaypointReset?: () => void;
  onPathingChange?: (p: "orthogonal" | "straight") => void;
  onSourceSideChange?: (side: string, ratio: number) => void;
  onTargetSideChange?: (side: string, ratio: number) => void;
}

// ---------------------------------------------------------------------------
// Live node geometry from the ReactFlow store
// ---------------------------------------------------------------------------

function selectRectString(id: string) {
  return (s: ReactFlowState): string | null => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const n = s.nodeInternals.get(id) as any;
    if (!n) return null;
    const pos = n.positionAbsolute ?? n.position;
    const x = pos?.x ?? 0;
    const y = pos?.y ?? 0;
    const w = n.width ?? 220;
    const h = n.height ?? 160;
    return `${x},${y},${w},${h}`;
  };
}

function parseRect(str: string | null): Rect | null {
  if (!str) return null;
  const [x, y, w, h] = str.split(",").map(Number);
  return { x, y, width: w, height: h };
}

function selectObstacleRectString(sourceId: string, targetId: string) {
  return (s: ReactFlowState): string => {
    const parts: string[] = [];
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    s.nodeInternals.forEach((n: any, nodeId: string) => {
      if (nodeId === sourceId || nodeId === targetId) return;
      const pos = n.positionAbsolute ?? n.position;
      const x = pos?.x ?? 0;
      const y = pos?.y ?? 0;
      const w = n.width ?? 220;
      const h = n.height ?? 160;
      parts.push(`${nodeId},${x},${y},${w},${h}`);
    });
    return parts.join("|");
  };
}

function parseObstacleRects(str: string): Rect[] {
  if (!str) return [];
  return str.split("|").map((part) => {
    const pieces = part.split(",");
    const x = Number(pieces[1]);
    const y = Number(pieces[2]);
    const w = Number(pieces[3]);
    const h = Number(pieces[4]);
    return { x, y, width: w, height: h };
  }).filter((d) => Number.isFinite(d.x) && Number.isFinite(d.y) && d.width > 0 && d.height > 0);
}

// ---------------------------------------------------------------------------
// IDEF1X / UML marker drawing (unchanged)
// ---------------------------------------------------------------------------

type MarkerKind = "many" | "one" | "optional" | "none";

function crowsFoot(ex: number, ey: number, pos: Position): ReactNode {
  const [dx, dy] = awayVec(pos);
  const [px, py] = [-dy, dx];
  const STEP = 14, SPREAD = 10;
  const bx = ex + dx * STEP, by = ey + dy * STEP;
  return (
    <g>
      <line x1={ex} y1={ey} x2={bx} y2={by} stroke="currentColor" strokeWidth={1.5} />
      <line x1={ex + px * SPREAD} y1={ey + py * SPREAD} x2={bx} y2={by} stroke="currentColor" strokeWidth={1.5} />
      <line x1={ex - px * SPREAD} y1={ey - py * SPREAD} x2={bx} y2={by} stroke="currentColor" strokeWidth={1.5} />
    </g>
  );
}

function umlArrow(ex: number, ey: number, pos: Position): ReactNode {
  const [dx, dy] = awayVec(pos);
  const [px, py] = [-dy, dx];
  const STEP = 12, SPREAD = 7;
  const bx = ex + dx * STEP, by = ey + dy * STEP;
  return <polygon points={`${ex},${ey} ${bx + px * SPREAD},${by + py * SPREAD} ${bx - px * SPREAD},${by - py * SPREAD}`} fill="currentColor" stroke="currentColor" strokeWidth={1.5} />;
}

function diamond(ex: number, ey: number, pos: Position): ReactNode {
  const [dx, dy] = awayVec(pos);
  const [px, py] = [-dy, dx];
  const STEP = 14, SPREAD = 8;
  const bx = ex + dx * STEP, by = ey + dy * STEP;
  const mx = ex + dx * (STEP / 2), my = ey + dy * (STEP / 2);
  return <polygon points={`${ex},${ey} ${mx + px * SPREAD},${my + py * SPREAD} ${bx},${by} ${mx - px * SPREAD},${my - py * SPREAD}`} fill="#ffffff" stroke="currentColor" strokeWidth={1.5} />;
}

function oneBar(ex: number, ey: number, pos: Position): ReactNode {
  const [dx, dy] = awayVec(pos);
  const [px, py] = [-dy, dx];
  const SETBACK = 8, HALF = 8;
  const bx = ex + dx * SETBACK, by = ey + dy * SETBACK;
  return <line x1={bx + px * HALF} y1={by + py * HALF} x2={bx - px * HALF} y2={by - py * HALF} stroke="currentColor" strokeWidth={1.5} />;
}

function optionalRing(ex: number, ey: number, pos: Position): ReactNode {
  const [dx, dy] = awayVec(pos);
  const SETBACK = 10;
  const bx = ex + dx * SETBACK, by = ey + dy * SETBACK;
  return <circle cx={bx} cy={by} r={4} fill="#ffffff" stroke="currentColor" strokeWidth={1.5} />;
}

function markerExtent(kind: MarkerKind, notation: "crowsfoot" | "uml" | "diamond"): number {
  if (kind === "many") {
    if (notation === "uml") return 12;
    if (notation === "diamond") return 14;
    return 14;
  }
  if (kind === "one") return 8;
  if (kind === "optional") return 14;
  return 0;
}

function drawMarker(ex: number, ey: number, pos: Position, kind: MarkerKind, notation: "crowsfoot" | "uml" | "diamond"): ReactNode {
  if (kind === "none") return null;
  if (kind === "many") {
    if (notation === "uml") return umlArrow(ex, ey, pos);
    if (notation === "diamond") return diamond(ex, ey, pos);
    return crowsFoot(ex, ey, pos);
  }
  if (kind === "optional") return optionalRing(ex, ey, pos);
  return oneBar(ex, ey, pos);
}

// ---------------------------------------------------------------------------
// Drag-mode discriminated union
// ---------------------------------------------------------------------------

type DragMode =
  | null
  | { type: "segment"; segIdx: number; origin: Pt; initialWps: Pt[] }
  | { type: "waypoint"; wpIdx: number }
  | { type: "source" }
  | { type: "target" };

// ---------------------------------------------------------------------------
// Edge component
// ---------------------------------------------------------------------------

function CrowsFootEdge({
  id, source, target,
  sourceX, sourceY, targetX, targetY,
  sourcePosition, targetPosition,
  data, style = {}, selected,
}: EdgeProps<CrowsFootEdgeData>) {
  const t             = useT();
  const globalPathing = useBuilderStore((s) => s.relationPathing);
  const notation      = useBuilderStore((s) => s.relationNotation);
  const overrides     = useBuilderStore((s) => s.relationTerminalOverrides[id]);
  const isOrtho       = (data?.pathing ?? globalPathing) === "orthogonal";

  // ---- Live node geometry ----
  const srcSelector = useCallback(selectRectString(source), [source]);
  const tgtSelector = useCallback(selectRectString(target), [target]);
  const obstacleSelector = useCallback(selectObstacleRectString(source, target), [source, target]);
  const srcRect = parseRect(useStore(srcSelector));
  const tgtRect = parseRect(useStore(tgtSelector));
  const obstacleRects = parseObstacleRects(useStore(obstacleSelector));

  // ---- Parallel-edge offset ----
  // A locked route draws with the fan-out it was frozen with. Recomputing it
  // from the CURRENT relationship set moved a frozen attachment whenever a
  // parallel relationship was added or removed, while its frozen bends stayed
  // absolute — which can turn a frozen orthogonal terminal into a diagonal.
  const idx     = data?.offsetIndex ?? 0;
  const total   = data?.totalEdges ?? 1;
  const spacing = 15;
  const offset  = data?.lockedParallelOffset ?? (idx - (total - 1) / 2) * spacing;

  // ---- Best-side border anchor selection ----
  let sx = sourceX, sy = sourceY + offset;
  let tx = targetX, ty = targetY + offset;
  let sp = sourcePosition, tp = targetPosition;

  if (srcRect && tgtRect) {
    // The shared docking rule — the same call the lock action makes when it
    // freezes this relationship, so the frozen docks are the drawn docks.
    const docking = resolveDisplayedDocking({
      source: srcRect,
      target: tgtRect,
      sourceSide: data?.sourceSide,
      targetSide: data?.targetSide,
      sourceRatio: data?.sourceRatio,
      targetRatio: data?.targetRatio,
    });
    const srcPoint = pointOnSide(
      srcRect,
      docking.sourceSide,
      ratioWithParallelOffset(srcRect, docking.sourceSide, docking.sourceRatio, offset),
    );
    const tgtPoint = pointOnSide(
      tgtRect,
      docking.targetSide,
      ratioWithParallelOffset(tgtRect, docking.targetSide, docking.targetRatio, offset),
    );

    sx = srcPoint.x;
    sy = srcPoint.y;
    tx = tgtPoint.x;
    ty = tgtPoint.y;
    sp = SIDE_TO_POSITION[docking.sourceSide];
    tp = SIDE_TO_POSITION[docking.targetSide];
  }

  // ---- Drag state ----
  const [dragMode, setDragMode]     = useState<DragMode>(null);
  const [localWps, setLocalWps]     = useState<Pt[] | null>(null);
  const [localSrc, setLocalSrc]     = useState<Pt | null>(null);
  const [localTgt, setLocalTgt]     = useState<Pt | null>(null);
  const dragRef = useRef({ sx, sy, tx, ty, srcRect, tgtRect, data, localWps, localSrc, localTgt, dragMode });
  useEffect(() => { dragRef.current = { sx, sy, tx, ty, srcRect, tgtRect, data, localWps, localSrc, localTgt, dragMode }; });

  const { screenToFlowPosition } = useReactFlow();

  // ---- Apply local overrides ----
  if (localSrc) { sx = localSrc.x; sy = localSrc.y; }
  if (localTgt) { tx = localTgt.x; ty = localTgt.y; }

  // ---- Markers ----
  const sourceIsFact = data?.sourceIsFact ?? false;
  const targetIsFact = data?.targetIsFact ?? false;
  const sourceIsDim = data?.sourceIsDim ?? false;
  const targetIsDim = data?.targetIsDim ?? false;
  let srcMarker: MarkerKind = "none", tgtMarker: MarkerKind = "none";
  if (sourceIsFact && !targetIsFact) { srcMarker = "many"; tgtMarker = "one"; }
  else if (!sourceIsFact && targetIsFact) { srcMarker = "one"; tgtMarker = "many"; }
  else if (!sourceIsFact && !targetIsFact && sourceIsDim && targetIsDim) {
    // Dimension-to-dimension (snowflake / outrigger) edge (F-026-21). No
    // fact asymmetry to read, and the model carries no explicit PK/FK roles,
    // so default to the standard outrigger shape: the source (base) dimension
    // references the target (outrigger) dimension's key — the referencing side
    // is "many", the referenced key side is "one", mirroring the fact↔dim rule.
    // A manual JoinsPanel override still wins below.
    srcMarker = "many"; tgtMarker = "one";
  }
  if (overrides) { srcMarker = overrides.source as MarkerKind; tgtMarker = overrides.target as MarkerKind; }

  // ---- Heel-offset start/end for the polyline ----
  const [sdx, sdy] = awayVec(sp);
  const [tdx, tdy] = awayVec(tp);
  const srcOff = markerExtent(srcMarker, notation);
  const tgtOff = markerExtent(tgtMarker, notation);
  const psx = sx + sdx * srcOff, psy = sy + sdy * srcOff;
  const ptx = tx + tdx * tgtOff, pty = ty + tdy * tgtOff;

  // ---- Resolve waypoints ----
  const storedWps: Pt[] =
    data?.waypoints ??
    (data?.waypoint ? [data.waypoint] : []);
  // Provenance resolution matches the worker snapshot: absent routeMode is
  // manual when stored waypoints exist, otherwise auto (legacy layouts stay
  // correct). In straight mode, engine-generated orthogonal bends are not valid
  // straight geometry, so only manual free-angle bends are kept; a straight
  // auto route is the direct heel-to-heel line (F08 / pathing setting). A live
  // drag (`localWps`) always wins, so a straight-mode bend drag previews even
  // before the commit marks the route manual.
  const routeMode: "auto" | "manual" = data?.routeMode ?? (storedWps.length ? "manual" : "auto");

  // A live drag always wins, so a bend drag previews even before the commit
  // marks the route manual. Otherwise the shared resolution decides, which is
  // again the call the lock action makes.
  const effectiveWps: Pt[] = localWps ?? resolveDisplayedWaypoints({
    heelSource: { x: psx, y: psy },
    heelTarget: { x: ptx, y: pty },
    sourcePosition: sp,
    targetPosition: tp,
    pathMode: isOrtho ? "orthogonal" : "straight",
    routeMode,
    stored: storedWps,
    obstacles: obstacleRects,
  });

  const startPt: Pt = { x: psx, y: psy };
  const endPt:   Pt = { x: ptx, y: pty };
  const allPoints: Pt[] = [startPt, ...effectiveWps, endPt];

  const edgePath = polylinePath(allPoints, isOrtho ? 6 : 4);

  // ---- Midpoint of the whole path (for label positioning) ----
  const mids = midpoints(allPoints);
  const centerIdx = Math.floor(mids.length / 2);
  const mx = mids[centerIdx]?.x ?? (psx + ptx) / 2;
  const my = mids[centerIdx]?.y ?? (psy + pty) / 2;

  // ---- Closest-side helper (for anchor drag) ----
  const getClosestSide = (pos: Pt, rect: Rect | null) => {
    if (!rect) return null;
    const dL = Math.abs(pos.x - rect.x);
    const dR = Math.abs(pos.x - (rect.x + rect.width));
    const dT = Math.abs(pos.y - rect.y);
    const dB = Math.abs(pos.y - (rect.y + rect.height));
    const min = Math.min(dL, dR, dT, dB);
    let side = "bottom", ratio = 0.5;
    if (min === dL)      { side = "left";   ratio = rect.height ? (pos.y - rect.y) / rect.height : 0.5; }
    else if (min === dR) { side = "right";  ratio = rect.height ? (pos.y - rect.y) / rect.height : 0.5; }
    else if (min === dT) { side = "top";    ratio = rect.width ? (pos.x - rect.x) / rect.width : 0.5; }
    else                 { side = "bottom"; ratio = rect.width ? (pos.x - rect.x) / rect.width : 0.5; }
    return { side, ratio: Math.max(0, Math.min(1, ratio)) };
  };

  /**
   * Whether a manually edited route may be persisted.
   *
   * Two ways an edit can end up undrawable: a diagonal in orthogonal mode (the
   * repair should prevent it, so this is the backstop), and a bend dragged
   * inside one of the relationship's own cards, which would draw the connector
   * through the table it connects.
   */
  const manualEditIsValid = (wps: Pt[]): boolean => {
    if (!isDrawableRoute([{ x: psx, y: psy }, ...wps, { x: ptx, y: pty }], isOrtho ? "orthogonal" : "straight")) {
      return false;
    }
    // Card rectangles are read at commit time, not captured at render time:
    // the pointer-up handler runs after a gesture that may itself have moved
    // the cards, and the stale rects would test the wrong geometry.
    const live = dragRef.current;
    if (live.srcRect && routeEntersCard(wps, live.srcRect)) return false;
    if (live.tgtRect && routeEntersCard(wps, live.tgtRect)) return false;
    return true;
  };

  // ---- Can a segment be dragged? (both endpoints must be waypoints) ----
  const isDraggableSeg = (segIdx: number) => {
    const i1 = segIdx - 1;
    const i2 = segIdx;
    return i1 >= 0 && i1 < effectiveWps.length && i2 >= 0 && i2 < effectiveWps.length;
  };

  // ---- Path mousedown — segment drag or insert waypoint ----
  const onPathDown = useCallback((e: ReactMouseEvent) => {
    if (!selected) return;
    e.stopPropagation();
    e.preventDefault();
    const flowPos = screenToFlowPosition({ x: e.clientX, y: e.clientY });
    const pts: Pt[] = [{ x: psx, y: psy }, ...effectiveWps, { x: ptx, y: pty }];
    const segIdx = closestSegIdx(flowPos.x, flowPos.y, pts);

    if (isDraggableSeg(segIdx)) {
      setDragMode({ type: "segment", segIdx, origin: flowPos, initialWps: effectiveWps.map((p) => ({ ...p })) });
      setLocalWps(effectiveWps.map((p) => ({ ...p })));
    } else {
      // Adding a bend works in both path modes. In orthogonal mode the new
      // bend is repaired into the route as it is dragged, so the click cannot
      // produce a diagonal (spec §4: add bend must not create diagonals).
      const newWps = [...effectiveWps];
      const insertIdx = Math.max(0, Math.min(segIdx, newWps.length));
      newWps.splice(insertIdx, 0, { ...flowPos });
      setLocalWps(newWps);
      setDragMode({ type: "waypoint", wpIdx: insertIdx });
    }
  }, [selected, screenToFlowPosition, psx, psy, ptx, pty, effectiveWps, isOrtho]);

  // ---- Waypoint handle mousedown ----
  const onWpDown = useCallback((wpIdx: number, e: ReactMouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    setLocalWps(effectiveWps.map((p) => ({ ...p })));
    setDragMode({ type: "waypoint", wpIdx });
  }, [effectiveWps]);

  // ---- Source / target anchor mousedown ----
  const onSrcDown = useCallback((e: ReactMouseEvent) => {
    e.stopPropagation(); e.preventDefault();
    setDragMode({ type: "source" });
    setLocalSrc({ x: sx, y: sy });
  }, [sx, sy]);

  const onTgtDown = useCallback((e: ReactMouseEvent) => {
    e.stopPropagation(); e.preventDefault();
    setDragMode({ type: "target" });
    setLocalTgt({ x: tx, y: ty });
  }, [tx, ty]);

  // ---- Global pointer-move / pointer-up ----
  useEffect(() => {
    if (!dragMode) return;

    function onMove(e: MouseEvent) {
      const pos = screenToFlowPosition({ x: e.clientX, y: e.clientY });
      const dm = dragRef.current.dragMode;
      if (!dm) return;

      if (dm.type === "segment") {
        const fn = isOrtho ? applyOrthoSegmentDrag : applyFreeSegmentDrag;
        const newWps = fn(
          dm.segIdx, pos, dm.origin, dm.initialWps,
          { x: psx, y: psy }, { x: ptx, y: pty },
        );
        setLocalWps(newWps);
      } else if (dm.type === "waypoint") {
        setLocalWps((prev) => {
          if (!prev) return prev;
          // Orthogonal: the bend goes where the pointer is, its neighbouring
          // bends follow on the coordinate they shared with it, and a dogleg is
          // inserted where the neighbour is a fixed heel — so every segment
          // stays axis-aligned and both terminals still leave their own card.
          if (isOrtho) {
            return applyOrthoBendDrag(
              dm.wpIdx, pos, prev,
              { x: psx, y: psy }, { x: ptx, y: pty },
              sp, tp,
            );
          }
          const next = [...prev];
          next[dm.wpIdx] = { ...pos };
          return next;
        });
      } else if (dm.type === "source") {
        setLocalSrc(pos);
      } else if (dm.type === "target") {
        setLocalTgt(pos);
      }
    }

    function onUp() {
      const dr = dragRef.current;
      const dm = dr.dragMode;
      if (!dm) return;

      if (dm.type === "segment" || dm.type === "waypoint") {
        const finalWps = dr.localWps;
        // Spec §4: restore the pre-gesture layout rather than persisting a
        // broken connector. Dropping the local edit without calling back is
        // exactly that restore — the stored route is untouched, so the edge
        // redraws as it was.
        if (finalWps && dr.data?.onWaypointsChange && manualEditIsValid(finalWps)) {
          dr.data.onWaypointsChange(finalWps);
        }
        setLocalWps(null);
      } else if (dm.type === "source") {
        if (dr.localSrc && dr.data?.onSourceSideChange) {
          const res = getClosestSide(dr.localSrc, dr.srcRect);
          if (res) dr.data.onSourceSideChange(res.side, res.ratio);
        }
        setLocalSrc(null);
      } else if (dm.type === "target") {
        if (dr.localTgt && dr.data?.onTargetSideChange) {
          const res = getClosestSide(dr.localTgt, dr.tgtRect);
          if (res) dr.data.onTargetSideChange(res.side, res.ratio);
        }
        setLocalTgt(null);
      }
      setDragMode(null);
    }

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [dragMode, screenToFlowPosition, isOrtho, psx, psy, ptx, pty]);

  // ---- Double-click edge to reset waypoints ----
  const edgeReadOnly = data?.readOnly ?? false;
  /**
   * A locked relationship's path, docking and bends are frozen (R06), so every
   * geometry gesture is withdrawn rather than merely refused on commit: an
   * affordance that visibly moves and then snaps back reads as a bug. The route
   * stays selectable — the user has to select it to unlock it.
   */
  const geometryFrozen = edgeReadOnly || data?.locked === true;
  const onDblClick = useCallback((e: ReactMouseEvent) => {
    e.stopPropagation();
    if (edgeReadOnly) return;
    if (data?.onWaypointReset) data.onWaypointReset();
  }, [data, edgeReadOnly]);

  // ---- Visual properties ----
  const baseStroke     = (style.stroke as string) ?? "#90a4ae";
  // A locked route carries the lock colour, the same one a position-locked card
  // uses. Selection still wins on colour, because a user needs to see what they
  // have selected — the padlock glyph keeps the lock legible either way.
  const stroke         = selected ? "#D4AF37" : data?.locked === true ? LOCKED_ACCENT : baseStroke;
  const isOuterJoin    = isOuterJoinType(data?.joinType);
  const strokeWidth    = selected ? 2.5 : 2;
  const strokeDasharray = isOuterJoin ? "5,5" : undefined;
  const isDragging     = dragMode !== null;

  const locked = data?.locked === true;

  return (
    <g data-testid={`edge-${id}`} data-locked={locked ? "true" : undefined} style={{ color: stroke }}>
      {/* Announced to assistive technology and shown as the native tooltip;
          the glyph below carries the same state visually. */}
      {locked && <title>{t("canvas.routeLockedEdge")}</title>}
      {/* Invisible wide hit target for selection and segment drag */}
      <path
        d={edgePath}
        fill="none"
        stroke="transparent"
        strokeWidth={20}
        style={{ cursor: selected && !geometryFrozen ? "grab" : "pointer" }}
        onMouseDown={geometryFrozen ? undefined : onPathDown}
        onDoubleClick={geometryFrozen ? undefined : onDblClick}
      />

      {/* Visible polyline path */}
      <path
        id={id}
        className="react-flow__edge-path"
        d={edgePath}
        fill="none"
        stroke={stroke}
        strokeWidth={strokeWidth}
        strokeDasharray={strokeDasharray}
        style={{ zIndex: selected ? 100 : 1, pointerEvents: "none" }}
      />

      {/* Lock indicator at the route midpoint: a padlock outline in the edge's
          own colour, so a locked relationship reads as locked without opening
          the panel. */}
      {locked && (
        <g pointerEvents="none" aria-hidden="true">
          <rect
            x={mx - 5} y={my - 3} width={10} height={8} rx={1.5}
            fill="#ffffff" stroke={LOCKED_ACCENT} strokeWidth={1.5}
          />
          <path
            d={`M ${mx - 2.8} ${my - 3} L ${mx - 2.8} ${my - 5.2} A 2.8 2.8 0 0 1 ${mx + 2.8} ${my - 5.2} L ${mx + 2.8} ${my - 3}`}
            fill="none" stroke={LOCKED_ACCENT} strokeWidth={1.5}
          />
        </g>
      )}

      {/* Selected-relationship summary: the columns it joins on (R09).

          A VISUAL surface only, deliberately. React Flow renders edge labels
          inside a container marked `aria-hidden="true"`, so nothing placed here
          reaches assistive technology, and `aria-hidden` is inherited — a
          descendant cannot opt back in. Found by the production-browser gate;
          the jsdom React Flow does not set that attribute, so a control here
          looked correct in unit tests while being invisible to a screen reader.

          There is no "open the join" button here, because selecting the
          relationship has ALREADY opened its detail: `onEdgeClick` selects the
          join and opens the Joins panel. A button whose action has happened by
          the time it appears is not an affordance. The remaining gap — that a
          keyboard user cannot select a relationship on the canvas at all — is
          registered rather than papered over with a control that does nothing. */}
      {selected && (
        <EdgeLabelRenderer>
          <div
            style={{
              position: "absolute",
              transform: `translate(-50%, -50%) translate(${mx}px, ${my}px)`,
              pointerEvents: "none",
              display: "flex",
              alignItems: "center",
              gap: 6,
              background: "#ffffff",
              border: "1px solid #cfd8dc",
              borderRadius: 3,
              padding: "2px 6px",
              fontSize: 10,
              whiteSpace: "nowrap",
              zIndex: 101,
            }}
          >
            <span>
              {data?.sourceColumn && data?.targetColumn
                ? `${data.sourceColumn} = ${data.targetColumn}`
                : t("canvas.joinColumnsUnknown")}
            </span>
          </div>
        </EdgeLabelRenderer>
      )}

      {/* Markers at card borders */}
      {drawMarker(sx, sy, sp, srcMarker, notation)}
      {drawMarker(tx, ty, tp, tgtMarker, notation)}

      {/* Waypoint bend-point handles (squares, visible when selected) */}
      {(selected || isDragging) && !geometryFrozen && effectiveWps.map((wp, i) => {
        const liveWp = localWps ? localWps[i] ?? wp : wp;
        return (
          <g
            key={i}
            style={{ cursor: "grab", pointerEvents: "all" }}
            onMouseDown={(e) => onWpDown(i, e)}
          >
            <rect
              x={liveWp.x - 10} y={liveWp.y - 10}
              width={20} height={20}
              fill="transparent"
            />
            <rect
              x={liveWp.x - 4} y={liveWp.y - 4}
              width={8} height={8}
              rx={1}
              fill={isDragging ? "#D4AF37" : "#ffffff"}
              stroke={stroke}
              strokeWidth={1.5}
            />
          </g>
        );
      })}

      {/* Segment midpoint indicators for polyline mode — click to add waypoint */}
      {selected && !isOrtho && !isDragging && mids.map((m, i) => (
        <g key={`mid-${i}`} style={{ cursor: "crosshair", pointerEvents: "all", opacity: 0.5 }}>
          <circle cx={m.x} cy={m.y} r={12} fill="transparent" />
          <circle cx={m.x} cy={m.y} r={3} fill="#90a4ae" stroke="#ffffff" strokeWidth={1} />
        </g>
      ))}

      {/* Source anchor -- hidden in readOnly mode to prevent layout mutations
          (Bug-7636), and while the route is locked (R06). */}
      {!geometryFrozen && (
      <g
        style={{ cursor: "grab", pointerEvents: selected ? "all" : "none", opacity: selected || dragMode?.type === "source" ? 1 : 0 }}
        onMouseDown={onSrcDown}
      >
        <circle cx={sx} cy={sy} r={16} fill="transparent" />
        <circle
          cx={sx} cy={sy}
          r={dragMode?.type === "source" ? 6 : 4}
          fill={dragMode?.type === "source" ? "#D4AF37" : "#ffffff"}
          stroke={stroke} strokeWidth={2}
        />
      </g>
      )}

      {/* Target anchor -- hidden in readOnly mode to prevent layout mutations
          (Bug-7636), and while the route is locked (R06). */}
      {!geometryFrozen && (
      <g
        style={{ cursor: "grab", pointerEvents: selected ? "all" : "none", opacity: selected || dragMode?.type === "target" ? 1 : 0 }}
        onMouseDown={onTgtDown}
      >
        <circle cx={tx} cy={ty} r={16} fill="transparent" />
        <circle
          cx={tx} cy={ty}
          r={dragMode?.type === "target" ? 6 : 4}
          fill={dragMode?.type === "target" ? "#D4AF37" : "#ffffff"}
          stroke={stroke} strokeWidth={2}
        />
      </g>
      )}
    </g>
  );
}

export default memo(CrowsFootEdge);
