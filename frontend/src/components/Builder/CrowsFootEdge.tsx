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
import { isOuterJoinType } from "../../lib/joinRules";
import {
  type Pt,
  awayVec,
  autoOrthogonalRoute,
  polylinePath,
  closestSegIdx,
  applyOrthoSegmentDrag,
  applyFreeSegmentDrag,
  midpoints,
} from "./edgeRouting";

// ---------------------------------------------------------------------------
// Public edge-data contract
// ---------------------------------------------------------------------------

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
  /** Legacy single waypoint — converted to array on first read. */
  waypoint?: { x: number; y: number };
  waypoints?: Pt[];
  pathing?: "orthogonal" | "straight";
  sourceSide?: string;
  targetSide?: string;
  sourceRatio?: number;
  targetRatio?: number;
  onWaypointsChange?: (wps: Pt[]) => void;
  onWaypointReset?: () => void;
  onPathingChange?: (p: "orthogonal" | "straight") => void;
  onSourceSideChange?: (side: string, ratio: number) => void;
  onTargetSideChange?: (side: string, ratio: number) => void;
}

// ---------------------------------------------------------------------------
// Live node geometry from the ReactFlow store
// ---------------------------------------------------------------------------

interface Dims { x: number; y: number; w: number; h: number }

type AnchorSide = "left" | "right" | "top" | "bottom";

const ROUTE_MARGIN = 28;
const ROUTE_STUB = 34;

const SIDE_TO_POSITION: Record<AnchorSide, Position> = {
  left: Position.Left,
  right: Position.Right,
  top: Position.Top,
  bottom: Position.Bottom,
};

function clamp(n: number, min = 0, max = 1): number {
  return Math.max(min, Math.min(max, n));
}

function asAnchorSide(value?: string): AnchorSide | null {
  return value === "left" || value === "right" || value === "top" || value === "bottom"
    ? value
    : null;
}

function selectDimsString(id: string) {
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

function parseDims(str: string | null): Dims | null {
  if (!str) return null;
  const [x, y, w, h] = str.split(",").map(Number);
  return { x, y, w, h };
}

function selectObstacleDimsString(sourceId: string, targetId: string) {
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

function parseObstacleDims(str: string): Dims[] {
  if (!str) return [];
  return str.split("|").map((part) => {
    const pieces = part.split(",");
    const x = Number(pieces[1]);
    const y = Number(pieces[2]);
    const w = Number(pieces[3]);
    const h = Number(pieces[4]);
    return { x, y, w, h };
  }).filter((d) => Number.isFinite(d.x) && Number.isFinite(d.y) && d.w > 0 && d.h > 0);
}

function center(d: Dims): Pt {
  return { x: d.x + d.w / 2, y: d.y + d.h / 2 };
}

function autoSide(from: Dims, to: Dims, endpoint: "source" | "target"): AnchorSide {
  const fc = center(from);
  const tc = center(to);
  const dx = tc.x - fc.x;
  const dy = tc.y - fc.y;
  const horizontal = Math.abs(dx) >= Math.abs(dy);

  if (endpoint === "source") {
    return horizontal ? (dx >= 0 ? "right" : "left") : (dy >= 0 ? "bottom" : "top");
  }
  return horizontal ? (dx >= 0 ? "left" : "right") : (dy >= 0 ? "top" : "bottom");
}

function ratioToward(dims: Dims, side: AnchorSide, toward: Pt, parallelOffset: number): number {
  if (side === "left" || side === "right") {
    return clamp((toward.y + parallelOffset - dims.y) / dims.h, 0.08, 0.92);
  }
  return clamp((toward.x + parallelOffset - dims.x) / dims.w, 0.08, 0.92);
}

function ratioWithParallelOffset(
  dims: Dims,
  side: AnchorSide,
  ratio: number,
  parallelOffset: number,
): number {
  const span = side === "left" || side === "right" ? dims.h : dims.w;
  return clamp(ratio + (span ? parallelOffset / span : 0), 0.08, 0.92);
}

function pointOnSide(dims: Dims, side: AnchorSide, ratio: number): Pt {
  const r = clamp(ratio);
  if (side === "left") return { x: dims.x, y: dims.y + dims.h * r };
  if (side === "right") return { x: dims.x + dims.w, y: dims.y + dims.h * r };
  if (side === "top") return { x: dims.x + dims.w * r, y: dims.y };
  return { x: dims.x + dims.w * r, y: dims.y + dims.h };
}

function inflateRect(rect: Dims, margin: number): Dims {
  return {
    x: rect.x - margin,
    y: rect.y - margin,
    w: rect.w + margin * 2,
    h: rect.h + margin * 2,
  };
}

function segmentIntersectsRect(a: Pt, b: Pt, rect: Dims): boolean {
  const minX = Math.min(a.x, b.x);
  const maxX = Math.max(a.x, b.x);
  const minY = Math.min(a.y, b.y);
  const maxY = Math.max(a.y, b.y);
  const rx2 = rect.x + rect.w;
  const ry2 = rect.y + rect.h;

  if (Math.abs(a.y - b.y) < 0.01) {
    return a.y >= rect.y && a.y <= ry2 && maxX >= rect.x && minX <= rx2;
  }
  if (Math.abs(a.x - b.x) < 0.01) {
    return a.x >= rect.x && a.x <= rx2 && maxY >= rect.y && minY <= ry2;
  }
  return maxX >= rect.x && minX <= rx2 && maxY >= rect.y && minY <= ry2;
}

function compactWaypoints(points: Pt[]): Pt[] {
  const out: Pt[] = [];
  for (const p of points) {
    const prev = out[out.length - 1];
    if (!prev || Math.abs(prev.x - p.x) > 0.5 || Math.abs(prev.y - p.y) > 0.5) {
      out.push(p);
    }
  }
  return out;
}

function routeScore(start: Pt, waypoints: Pt[], end: Pt, obstacles: Dims[]): number {
  const pts = [start, ...waypoints, end];
  let length = 0;
  let intersections = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i];
    const b = pts[i + 1];
    length += Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
    for (const obs of obstacles) {
      if (segmentIntersectsRect(a, b, inflateRect(obs, 8))) intersections += 1;
    }
  }
  return intersections * 100000 + length + waypoints.length * 20;
}

function autoOrthogonalRouteAvoiding(
  sx: number, sy: number, sp: Position,
  tx: number, ty: number, tp: Position,
  obstacles: Dims[],
): Pt[] {
  const base = autoOrthogonalRoute(sx, sy, sp, tx, ty, tp);
  if (obstacles.length === 0) return base;

  const start = { x: sx, y: sy };
  const end = { x: tx, y: ty };
  const [sdx, sdy] = awayVec(sp);
  const [tdx, tdy] = awayVec(tp);
  const sEsc = { x: sx + sdx * ROUTE_STUB, y: sy + sdy * ROUTE_STUB };
  const tEsc = { x: tx + tdx * ROUTE_STUB, y: ty + tdy * ROUTE_STUB };
  const candidates: Pt[][] = [base];

  for (const obs of obstacles) {
    const expanded = inflateRect(obs, ROUTE_MARGIN);
    const viaXs = [expanded.x, expanded.x + expanded.w];
    const viaYs = [expanded.y, expanded.y + expanded.h];

    for (const x of viaXs) {
      candidates.push(compactWaypoints([
        sEsc,
        { x, y: sEsc.y },
        { x, y: tEsc.y },
        tEsc,
      ]));
    }
    for (const y of viaYs) {
      candidates.push(compactWaypoints([
        sEsc,
        { x: sEsc.x, y },
        { x: tEsc.x, y },
        tEsc,
      ]));
    }
  }

  return candidates.reduce((best, candidate) => (
    routeScore(start, candidate, end, obstacles) < routeScore(start, best, end, obstacles)
      ? candidate
      : best
  ), base);
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
  const globalPathing = useBuilderStore((s) => s.relationPathing);
  const notation      = useBuilderStore((s) => s.relationNotation);
  const overrides     = useBuilderStore((s) => s.relationTerminalOverrides[id]);
  const isOrtho       = (data?.pathing ?? globalPathing) === "orthogonal";

  // ---- Live node geometry ----
  const srcSelector = useCallback(selectDimsString(source), [source]);
  const tgtSelector = useCallback(selectDimsString(target), [target]);
  const obstacleSelector = useCallback(selectObstacleDimsString(source, target), [source, target]);
  const srcDims = parseDims(useStore(srcSelector));
  const tgtDims = parseDims(useStore(tgtSelector));
  const obstacleDims = parseObstacleDims(useStore(obstacleSelector));

  // ---- Parallel-edge offset ----
  const idx     = data?.offsetIndex ?? 0;
  const total   = data?.totalEdges ?? 1;
  const spacing = 15;
  const offset  = (idx - (total - 1) / 2) * spacing;

  // ---- Best-side border anchor selection ----
  let sx = sourceX, sy = sourceY + offset;
  let tx = targetX, ty = targetY + offset;
  let sp = sourcePosition, tp = targetPosition;

  if (srcDims && tgtDims) {
    const srcSide = asAnchorSide(data?.sourceSide) ?? autoSide(srcDims, tgtDims, "source");
    const tgtSide = asAnchorSide(data?.targetSide) ?? autoSide(srcDims, tgtDims, "target");
    const srcRatio = data?.sourceRatio !== undefined
      ? ratioWithParallelOffset(srcDims, srcSide, data.sourceRatio, offset)
      : ratioToward(srcDims, srcSide, center(tgtDims), offset);
    const tgtRatio = data?.targetRatio !== undefined
      ? ratioWithParallelOffset(tgtDims, tgtSide, data.targetRatio, offset)
      : ratioToward(tgtDims, tgtSide, center(srcDims), offset);
    const srcPoint = pointOnSide(srcDims, srcSide, srcRatio);
    const tgtPoint = pointOnSide(tgtDims, tgtSide, tgtRatio);

    sx = srcPoint.x;
    sy = srcPoint.y;
    tx = tgtPoint.x;
    ty = tgtPoint.y;
    sp = SIDE_TO_POSITION[srcSide];
    tp = SIDE_TO_POSITION[tgtSide];
  }

  // ---- Drag state ----
  const [dragMode, setDragMode]     = useState<DragMode>(null);
  const [localWps, setLocalWps]     = useState<Pt[] | null>(null);
  const [localSrc, setLocalSrc]     = useState<Pt | null>(null);
  const [localTgt, setLocalTgt]     = useState<Pt | null>(null);
  const dragRef = useRef({ sx, sy, tx, ty, srcDims, tgtDims, data, localWps, localSrc, localTgt, dragMode });
  useEffect(() => { dragRef.current = { sx, sy, tx, ty, srcDims, tgtDims, data, localWps, localSrc, localTgt, dragMode }; });

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
  const wps: Pt[] = localWps ?? storedWps;

  const effectiveWps: Pt[] =
    isOrtho && wps.length === 0
      ? autoOrthogonalRouteAvoiding(psx, psy, sp, ptx, pty, tp, obstacleDims)
      : wps;

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
  const getClosestSide = (pos: Pt, dims: Dims | null) => {
    if (!dims) return null;
    const dL = Math.abs(pos.x - dims.x);
    const dR = Math.abs(pos.x - (dims.x + dims.w));
    const dT = Math.abs(pos.y - dims.y);
    const dB = Math.abs(pos.y - (dims.y + dims.h));
    const min = Math.min(dL, dR, dT, dB);
    let side = "bottom", ratio = 0.5;
    if (min === dL)      { side = "left";   ratio = dims.h ? (pos.y - dims.y) / dims.h : 0.5; }
    else if (min === dR) { side = "right";  ratio = dims.h ? (pos.y - dims.y) / dims.h : 0.5; }
    else if (min === dT) { side = "top";    ratio = dims.w ? (pos.x - dims.x) / dims.w : 0.5; }
    else                 { side = "bottom"; ratio = dims.w ? (pos.x - dims.x) / dims.w : 0.5; }
    return { side, ratio: Math.max(0, Math.min(1, ratio)) };
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
    } else if (!isOrtho) {
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
        if (finalWps && dr.data?.onWaypointsChange) {
          dr.data.onWaypointsChange(finalWps);
        }
        setLocalWps(null);
      } else if (dm.type === "source") {
        if (dr.localSrc && dr.data?.onSourceSideChange) {
          const res = getClosestSide(dr.localSrc, dr.srcDims);
          if (res) dr.data.onSourceSideChange(res.side, res.ratio);
        }
        setLocalSrc(null);
      } else if (dm.type === "target") {
        if (dr.localTgt && dr.data?.onTargetSideChange) {
          const res = getClosestSide(dr.localTgt, dr.tgtDims);
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
  const onDblClick = useCallback((e: ReactMouseEvent) => {
    e.stopPropagation();
    if (data?.onWaypointReset) data.onWaypointReset();
  }, [data]);

  // ---- Visual properties ----
  const baseStroke     = (style.stroke as string) ?? "#90a4ae";
  const stroke         = selected ? "#D4AF37" : baseStroke;
  const isOuterJoin    = isOuterJoinType(data?.joinType);
  const strokeWidth    = selected ? 2.5 : 2;
  const strokeDasharray = isOuterJoin ? "5,5" : undefined;
  const isDragging     = dragMode !== null;

  return (
    <g data-testid={`edge-${id}`} style={{ color: stroke }}>
      {/* Invisible wide hit target for selection and segment drag */}
      <path
        d={edgePath}
        fill="none"
        stroke="transparent"
        strokeWidth={20}
        style={{ cursor: selected ? "grab" : "pointer" }}
        onMouseDown={onPathDown}
        onDoubleClick={onDblClick}
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

      {/* Markers at card borders */}
      {drawMarker(sx, sy, sp, srcMarker, notation)}
      {drawMarker(tx, ty, tp, tgtMarker, notation)}

      {/* Waypoint bend-point handles (squares, visible when selected) */}
      {(selected || isDragging) && effectiveWps.map((wp, i) => {
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

      {/* Source anchor */}
      <g
        style={{ cursor: "grab", pointerEvents: "all", opacity: selected || dragMode?.type === "source" ? 1 : 0 }}
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

      {/* Target anchor */}
      <g
        style={{ cursor: "grab", pointerEvents: "all", opacity: selected || dragMode?.type === "target" ? 1 : 0 }}
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
    </g>
  );
}

export default memo(CrowsFootEdge);
