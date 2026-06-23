import { Position } from "reactflow";

export interface Pt {
  x: number;
  y: number;
}

export function awayVec(pos: Position): [number, number] {
  switch (pos) {
    case Position.Left:   return [-1, 0];
    case Position.Right:  return [1, 0];
    case Position.Top:    return [0, -1];
    case Position.Bottom: return [0, 1];
  }
}

// ---------------------------------------------------------------------------
// Orthogonal auto-router — generates H/V waypoints (2–4 bends)
// ---------------------------------------------------------------------------

const STUB = 30;

export function autoOrthogonalRoute(
  sx: number, sy: number, sp: Position,
  tx: number, ty: number, tp: Position,
): Pt[] {
  const [sdx, sdy] = awayVec(sp);
  const [tdx, tdy] = awayVec(tp);

  const sHoriz = sdy === 0;
  const tHoriz = tdy === 0;

  if (sHoriz && tHoriz) {
    if (sdx !== tdx) {
      const toward = (sdx > 0 && tx >= sx) || (sdx < 0 && tx <= sx);
      if (toward && Math.abs(tx - sx) > STUB * 2) {
        const mid = (sx + tx) / 2;
        return [{ x: mid, y: sy }, { x: mid, y: ty }];
      }
    }
    const ext = sdx > 0 ? Math.max(sx, tx) + STUB : Math.min(sx, tx) - STUB;
    return [{ x: ext, y: sy }, { x: ext, y: ty }];
  }

  if (!sHoriz && !tHoriz) {
    if (sdy !== tdy) {
      const toward = (sdy > 0 && ty >= sy) || (sdy < 0 && ty <= sy);
      if (toward && Math.abs(ty - sy) > STUB * 2) {
        const mid = (sy + ty) / 2;
        return [{ x: sx, y: mid }, { x: tx, y: mid }];
      }
    }
    const ext = sdy > 0 ? Math.max(sy, ty) + STUB : Math.min(sy, ty) - STUB;
    return [{ x: sx, y: ext }, { x: tx, y: ext }];
  }

  if (sHoriz) {
    const fwdOK = (sdx > 0 && tx >= sx + STUB) || (sdx < 0 && tx <= sx - STUB);
    const tOK   = (tdy > 0 && sy >= ty + STUB) || (tdy < 0 && sy <= ty - STUB);
    if (fwdOK && tOK) return [{ x: tx, y: sy }];
    const escX = sx + sdx * STUB;
    const escY = ty + tdy * STUB;
    return [{ x: escX, y: sy }, { x: escX, y: escY }, { x: tx, y: escY }];
  }

  const fwdOK = (sdy > 0 && ty >= sy + STUB) || (sdy < 0 && ty <= sy - STUB);
  const tOK   = (tdx > 0 && sx >= tx + STUB) || (tdx < 0 && sx <= tx - STUB);
  if (fwdOK && tOK) return [{ x: sx, y: ty }];
  const escY = sy + sdy * STUB;
  const escX = tx + tdx * STUB;
  return [{ x: sx, y: escY }, { x: escX, y: escY }, { x: escX, y: ty }];
}

// ---------------------------------------------------------------------------
// Polyline SVG path — straight line segments with rounded corners at bends
// ---------------------------------------------------------------------------

export function polylinePath(points: Pt[], radius: number = 6): string {
  if (points.length < 2) return "";
  if (points.length === 2) {
    return `M ${points[0].x},${points[0].y} L ${points[1].x},${points[1].y}`;
  }

  let d = `M ${points[0].x},${points[0].y}`;

  for (let i = 1; i < points.length - 1; i++) {
    const prev = points[i - 1];
    const curr = points[i];
    const next = points[i + 1];

    const dx1 = prev.x - curr.x, dy1 = prev.y - curr.y;
    const dx2 = next.x - curr.x, dy2 = next.y - curr.y;
    const len1 = Math.sqrt(dx1 * dx1 + dy1 * dy1);
    const len2 = Math.sqrt(dx2 * dx2 + dy2 * dy2);

    const r = Math.min(radius, len1 / 2, len2 / 2);
    if (r < 1) { d += ` L ${curr.x},${curr.y}`; continue; }

    const p1x = curr.x + (dx1 / len1) * r;
    const p1y = curr.y + (dy1 / len1) * r;
    const p2x = curr.x + (dx2 / len2) * r;
    const p2y = curr.y + (dy2 / len2) * r;

    d += ` L ${p1x},${p1y} Q ${curr.x},${curr.y} ${p2x},${p2y}`;
  }

  d += ` L ${points[points.length - 1].x},${points[points.length - 1].y}`;
  return d;
}

// ---------------------------------------------------------------------------
// Geometry helpers — segment distance and closest-segment index
// ---------------------------------------------------------------------------

export function distToSeg(
  px: number, py: number,
  ax: number, ay: number,
  bx: number, by: number,
): number {
  const dx = bx - ax, dy = by - ay;
  const lenSq = dx * dx + dy * dy;
  if (lenSq < 0.01) return Math.hypot(px - ax, py - ay);
  const t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / lenSq));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

export function closestSegIdx(mx: number, my: number, pts: Pt[]): number {
  let best = Infinity, idx = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    const d = distToSeg(mx, my, pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y);
    if (d < best) { best = d; idx = i; }
  }
  return idx;
}

export function segIsHorizontal(a: Pt, b: Pt): boolean {
  return Math.abs(b.y - a.y) <= Math.abs(b.x - a.x);
}

// ---------------------------------------------------------------------------
// Segment drag — updates waypoints based on segment translation
// ---------------------------------------------------------------------------

export function applyOrthoSegmentDrag(
  segIdx: number,
  mousePos: Pt,
  origin: Pt,
  initialWps: Pt[],
  startPt: Pt,
  endPt: Pt,
): Pt[] {
  const allPts: Pt[] = [startPt, ...initialWps, endPt];
  const a = allPts[segIdx];
  const b = allPts[segIdx + 1];
  const isH = segIsHorizontal(a, b);

  const newWps = initialWps.map((p) => ({ ...p }));
  const i1 = segIdx - 1;
  const i2 = segIdx;

  if (isH) {
    const dy = mousePos.y - origin.y;
    if (i1 >= 0 && i1 < newWps.length) newWps[i1].y += dy;
    if (i2 >= 0 && i2 < newWps.length) newWps[i2].y += dy;
  } else {
    const dx = mousePos.x - origin.x;
    if (i1 >= 0 && i1 < newWps.length) newWps[i1].x += dx;
    if (i2 >= 0 && i2 < newWps.length) newWps[i2].x += dx;
  }
  return newWps;
}

export function applyFreeSegmentDrag(
  segIdx: number,
  mousePos: Pt,
  origin: Pt,
  initialWps: Pt[],
  startPt: Pt,
  endPt: Pt,
): Pt[] {
  const allPts: Pt[] = [startPt, ...initialWps, endPt];
  const a = allPts[segIdx];
  const b = allPts[segIdx + 1];
  const sdx = b.x - a.x, sdy = b.y - a.y;
  const len = Math.sqrt(sdx * sdx + sdy * sdy);
  if (len < 0.1) return [...initialWps];

  const nx = -sdy / len, ny = sdx / len;
  const dx = mousePos.x - origin.x, dy = mousePos.y - origin.y;
  const perpDist = dx * nx + dy * ny;

  const deltaX = nx * perpDist;
  const deltaY = ny * perpDist;

  const newWps = initialWps.map((p) => ({ ...p }));
  const i1 = segIdx - 1;
  const i2 = segIdx;

  if (i1 >= 0 && i1 < newWps.length) {
    newWps[i1].x += deltaX;
    newWps[i1].y += deltaY;
  }
  if (i2 >= 0 && i2 < newWps.length) {
    newWps[i2].x += deltaX;
    newWps[i2].y += deltaY;
  }
  return newWps;
}

export function midpoints(pts: Pt[]): Pt[] {
  const mids: Pt[] = [];
  for (let i = 0; i < pts.length - 1; i++) {
    mids.push({
      x: (pts[i].x + pts[i + 1].x) / 2,
      y: (pts[i].y + pts[i + 1].y) / 2,
    });
  }
  return mids;
}
