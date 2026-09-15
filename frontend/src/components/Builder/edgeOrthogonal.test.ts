/**
 * Manual orthogonal editing: the repair that makes a bend drag legal, and the
 * guarantee that a diagonal can never reach the saved layout.
 */
import { describe, expect, it } from "vitest";
import { Position } from "reactflow";
import type { Pt } from "./edgeRouting";
import { isDrawableRoute } from "./edgeGeometry";
import { applyOrthoBendDrag, orthogonaliseRoute } from "./edgeOrthogonal";

describe("orthogonaliseRoute", () => {
  const heelSource: Pt = { x: 200, y: 50 };
  const heelTarget: Pt = { x: 600, y: 300 };

  /** Every leg horizontal or vertical — the property the whole repair exists for. */
  function everyLegAxisAligned(points: Pt[]): boolean {
    for (let i = 0; i < points.length - 1; i++) {
      const a = points[i]!;
      const b = points[i + 1]!;
      if (Math.abs(a.x - b.x) > 0.01 && Math.abs(a.y - b.y) > 0.01) return false;
    }
    return true;
  }

  it("resolves a diagonal into axis-aligned legs", () => {
    const out = orthogonaliseRoute([heelSource, heelTarget], Position.Right, Position.Left);
    expect(everyLegAxisAligned(out)).toBe(true);
    // The heels are where the markers are drawn, so the repair never moves them.
    expect(out[0]).toEqual(heelSource);
    expect(out[out.length - 1]).toEqual(heelTarget);
  });

  it("leaves the source along the source's own side", () => {
    // Source exits Right, so the first leg must run horizontally away from the
    // card rather than along its border.
    const out = orthogonaliseRoute([heelSource, { x: 200, y: 300 }, heelTarget], Position.Right, Position.Left);
    expect(everyLegAxisAligned(out)).toBe(true);
    expect(out[1]!.y).toBeCloseTo(heelSource.y, 6);
    expect(out[1]!.x).toBeGreaterThan(heelSource.x);
  });

  it("arrives at the target along the target's own side", () => {
    const out = orthogonaliseRoute([heelSource, { x: 600, y: 50 }, heelTarget], Position.Right, Position.Left);
    expect(everyLegAxisAligned(out)).toBe(true);
    const last = out[out.length - 1]!;
    const penultimate = out[out.length - 2]!;
    // Target is entered through its Left side, so the final leg is horizontal.
    expect(penultimate.y).toBeCloseTo(last.y, 6);
    expect(penultimate.x).toBeLessThan(last.x);
  });

  it("handles vertical terminals", () => {
    const out = orthogonaliseRoute(
      [{ x: 100, y: 200 }, { x: 400, y: 500 }],
      Position.Bottom,
      Position.Top,
    );
    expect(everyLegAxisAligned(out)).toBe(true);
    // Leaves downwards, arrives from above.
    expect(out[1]!.x).toBeCloseTo(100, 6);
    expect(out[1]!.y).toBeGreaterThan(200);
    expect(out[out.length - 2]!.x).toBeCloseTo(400, 6);
    expect(out[out.length - 2]!.y).toBeLessThan(500);
  });

  it("keeps an already-valid route unchanged apart from redundant points", () => {
    const valid = [heelSource, { x: 400, y: 50 }, { x: 400, y: 300 }, heelTarget];
    expect(orthogonaliseRoute(valid, Position.Right, Position.Left)).toEqual(valid);
  });

  it("merges collinear runs so a bend always means a turn", () => {
    const redundant = [heelSource, { x: 300, y: 50 }, { x: 400, y: 50 }, { x: 400, y: 300 }, heelTarget];
    const out = orthogonaliseRoute(redundant, Position.Right, Position.Left);
    expect(out).toEqual([heelSource, { x: 400, y: 50 }, { x: 400, y: 300 }, heelTarget]);
  });

  it("drops duplicated points", () => {
    const duplicated = [heelSource, { x: 400, y: 50 }, { x: 400, y: 50 }, { x: 400, y: 300 }, heelTarget];
    expect(orthogonaliseRoute(duplicated, Position.Right, Position.Left)).toEqual([
      heelSource, { x: 400, y: 50 }, { x: 400, y: 300 }, heelTarget,
    ]);
  });

  it("returns a degenerate input untouched", () => {
    expect(orthogonaliseRoute([heelSource], Position.Right, Position.Left)).toEqual([heelSource]);
  });
});

describe("applyOrthoBendDrag", () => {
  const heelSource: Pt = { x: 200, y: 50 };
  const heelTarget: Pt = { x: 600, y: 300 };
  // The standard two-bend staircase between those heels.
  const staircase: Pt[] = [{ x: 400, y: 50 }, { x: 400, y: 300 }];

  function polyline(wps: Pt[]): Pt[] {
    return [heelSource, ...wps, heelTarget];
  }

  it("moves a bend and pulls its neighbour along the shared coordinate", () => {
    // Dragging the first bend sideways must take the second with it, because
    // they share the vertical leg between them.
    const out = applyOrthoBendDrag(0, { x: 480, y: 50 }, staircase, heelSource, heelTarget, Position.Right, Position.Left);
    expect(isDrawableRoute(polyline(out), "orthogonal")).toBe(true);
    expect(out[0]!.x).toBeCloseTo(480, 6);
    expect(out[1]!.x).toBeCloseTo(480, 6);
  });

  it("keeps the route legal when a bend is dragged off both axes", () => {
    const out = applyOrthoBendDrag(0, { x: 470, y: 130 }, staircase, heelSource, heelTarget, Position.Right, Position.Left);
    expect(isDrawableRoute(polyline(out), "orthogonal")).toBe(true);
  });

  it("inserts a dogleg rather than dragging a fixed heel", () => {
    // The bend's neighbour here is the source heel, which cannot move: the only
    // legal repair is an extra corner.
    const single: Pt[] = [{ x: 400, y: 50 }];
    const out = applyOrthoBendDrag(0, { x: 400, y: 180 }, single, heelSource, heelTarget, Position.Right, Position.Left);
    expect(isDrawableRoute(polyline(out), "orthogonal")).toBe(true);
    expect(out.length).toBeGreaterThan(single.length);
    // The source still leaves horizontally through its own side.
    expect(out[0]!.y).toBeCloseTo(heelSource.y, 6);
  });

  it("leaves the heels alone", () => {
    const out = applyOrthoBendDrag(1, { x: 520, y: 260 }, staircase, heelSource, heelTarget, Position.Right, Position.Left);
    const points = polyline(out);
    expect(points[0]).toEqual(heelSource);
    expect(points[points.length - 1]).toEqual(heelTarget);
  });

  it("ignores an index that is not a bend", () => {
    expect(applyOrthoBendDrag(7, { x: 1, y: 2 }, staircase, heelSource, heelTarget, Position.Right, Position.Left))
      .toEqual(staircase);
    expect(applyOrthoBendDrag(-1, { x: 1, y: 2 }, staircase, heelSource, heelTarget, Position.Right, Position.Left))
      .toEqual(staircase);
  });

  it("does not mutate the waypoints it was given", () => {
    const original = staircase.map((p) => ({ ...p }));
    applyOrthoBendDrag(0, { x: 480, y: 130 }, staircase, heelSource, heelTarget, Position.Right, Position.Left);
    expect(staircase).toEqual(original);
  });
});
