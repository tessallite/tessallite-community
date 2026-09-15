import { describe, expect, it } from "vitest";
import { applyRouteLock, type PersistedEdgeLayout } from "./routeLock";
import type { DisplayedRoute } from "../edgeGeometry";

const capture: DisplayedRoute = {
  sourceSide: "right",
  targetSide: "left",
  sourceRatio: 0.4,
  targetRatio: 0.6,
  waypoints: [{ x: 300, y: 50 }, { x: 300, y: 20 }],
  heelSource: { x: 210, y: 50 },
  heelTarget: { x: 490, y: 20 },
};

describe("applyRouteLock", () => {
  it("writes the displayed geometry down when locking", () => {
    // An automatic route has no stored path, so without this the lock would
    // freeze nothing and the next reload would recompute a different one.
    const entry = applyRouteLock({ locked: true, capture, routeMode: "auto" });

    expect(entry.locked).toBe(true);
    expect(entry.sourceSide).toBe("right");
    expect(entry.targetSide).toBe("left");
    expect(entry.sourceRatio).toBeCloseTo(0.4, 6);
    expect(entry.targetRatio).toBeCloseTo(0.6, 6);
    expect(entry.waypoints).toEqual(capture.waypoints);
  });

  it("preserves provenance rather than claiming the engine's route as a manual edit", () => {
    expect(applyRouteLock({ locked: true, capture, routeMode: "auto" }).routeMode).toBe("auto");
    expect(applyRouteLock({ locked: true, capture, routeMode: "manual" }).routeMode).toBe("manual");
  });

  it("copies the captured bends instead of aliasing them", () => {
    const entry = applyRouteLock({ locked: true, capture, routeMode: "auto" });
    expect(entry.waypoints?.[0]).not.toBe(capture.waypoints[0]);
  });

  it("supersedes a legacy single waypoint with the captured array", () => {
    // Leaving both would give the reload path two conflicting paths for one
    // frozen route.
    const current: PersistedEdgeLayout = { waypoint: { x: 1, y: 2 } };
    const entry = applyRouteLock({ current, locked: true, capture, routeMode: "auto" });
    expect(entry.waypoint).toBeUndefined();
    expect(entry.waypoints).toEqual(capture.waypoints);
  });

  it("keeps unrelated saved fields", () => {
    const current: PersistedEdgeLayout = { pathing: "straight" };
    expect(applyRouteLock({ current, locked: true, capture, routeMode: "manual" }).pathing).toBe("straight");
  });

  it("clears only the lock when unlocking, never the path", () => {
    // Spec: no action discards a locked path.
    const locked = applyRouteLock({ locked: true, capture, routeMode: "auto" });
    const released = applyRouteLock({ current: locked, locked: false, capture: null, routeMode: "auto" });

    expect(released.locked).toBe(false);
    expect(released.waypoints).toEqual(capture.waypoints);
    expect(released.sourceSide).toBe("right");
    expect(released.sourceRatio).toBeCloseTo(0.4, 6);
    expect(released.routeMode).toBe("auto");
  });

  it("unlocks a relationship that has no saved entry at all", () => {
    expect(applyRouteLock({ locked: false, capture: null, routeMode: "auto" })).toEqual({ locked: false });
  });

  it("refuses to lock without a resolved route", () => {
    // Fail closed: a lock with nothing captured is the silent no-op that makes
    // a frozen route drift on the next reload.
    expect(() => applyRouteLock({ locked: true, capture: null, routeMode: "auto" })).toThrow(
      /could not be resolved/,
    );
  });

  it("does not mutate the entry it was given", () => {
    const current: PersistedEdgeLayout = { waypoint: { x: 1, y: 2 }, locked: false };
    applyRouteLock({ current, locked: true, capture, routeMode: "auto" });
    expect(current.waypoint).toEqual({ x: 1, y: 2 });
    expect(current.locked).toBe(false);
  });
});

describe("a lock owns the geometry it promises to freeze", () => {
  // Reported by the external review (C01). Locking withdrew the connector's
  // drag handles, but it did not own two things the drawn path depends on, so
  // ordinary controls elsewhere still changed a frozen route.
  const capture: DisplayedRoute = {
    sourceSide: "right", targetSide: "left",
    sourceRatio: 0.5, targetRatio: 0.5,
    waypoints: [{ x: 350, y: 100 }],
    heelSource: { x: 214, y: 100 }, heelTarget: { x: 486, y: 100 },
    sourceMarkerExtent: 14, targetMarkerExtent: 14,
  } as unknown as DisplayedRoute;

  it("freezes the inherited path mode, and records that it did", () => {
    // An automatic orthogonal route with no explicit `pathing` inherits the
    // model-wide setting. Switching that setting to Straight made the renderer
    // discard the very bends the lock had frozen.
    const entry = applyRouteLock({
      current: {}, locked: true, capture, routeMode: "auto",
      resolvedPathing: "orthogonal", parallelOffset: 0,
    });
    expect(entry.pathing).toBe("orthogonal");
    expect(entry.pathingFrozenByLock).toBe(true);
  });

  it("does not claim an explicit choice as its own", () => {
    // The user chose Straight for this relationship. Unlock must leave that
    // alone — only the lock's own override is the lock's to remove.
    const entry = applyRouteLock({
      current: { pathing: "straight" }, locked: true, capture, routeMode: "auto",
      resolvedPathing: "straight", parallelOffset: 0,
    });
    expect(entry.pathing).toBe("straight");
    expect(entry.pathingFrozenByLock).toBeUndefined();
  });

  it("hands an inherited mode back to the model setting on unlock", () => {
    // Otherwise Unlock is a one-way door: the relationship keeps whatever mode
    // was in force when it was locked, forever, and nobody chose that.
    const locked = applyRouteLock({
      current: {}, locked: true, capture, routeMode: "auto",
      resolvedPathing: "orthogonal", parallelOffset: 0,
    });
    const unlocked = applyRouteLock({ current: locked, locked: false, capture: null, routeMode: "auto" });
    expect(unlocked.pathing, "back to inheriting the model setting").toBeUndefined();
    expect(unlocked.pathingFrozenByLock).toBeUndefined();
  });

  it("keeps an explicit mode through unlock", () => {
    const locked = applyRouteLock({
      current: { pathing: "straight" }, locked: true, capture, routeMode: "auto",
      resolvedPathing: "straight", parallelOffset: 0,
    });
    const unlocked = applyRouteLock({ current: locked, locked: false, capture: null, routeMode: "auto" });
    expect(unlocked.pathing).toBe("straight");
  });

  it("freezes the parallel fan-out in force", () => {
    // The fan-out is normally derived from the CURRENT relationship set, so
    // adding a second relationship between the same two cards moved an existing
    // frozen attachment by 7.5px while its bends stayed absolute — enough to
    // turn a frozen orthogonal terminal into a diagonal.
    const entry = applyRouteLock({
      current: {}, locked: true, capture, routeMode: "auto",
      resolvedPathing: "orthogonal", parallelOffset: -7.5,
    });
    expect(entry.lockedParallelOffset).toBe(-7.5);
  });

  it("releases the frozen fan-out on unlock", () => {
    const locked = applyRouteLock({
      current: {}, locked: true, capture, routeMode: "auto",
      resolvedPathing: "orthogonal", parallelOffset: -7.5,
    });
    const unlocked = applyRouteLock({ current: locked, locked: false, capture: null, routeMode: "auto" });
    expect(unlocked.lockedParallelOffset).toBeUndefined();
  });

  it("leaves a layout saved before locks carried these fields alone", () => {
    // A legacy locked entry has neither field. Unlocking must not invent one or
    // strip a pathing value it did not write.
    const legacy = { locked: true, pathing: "orthogonal" as const, waypoints: [{ x: 1, y: 2 }] };
    const unlocked = applyRouteLock({ current: legacy, locked: false, capture: null, routeMode: "auto" });
    expect(unlocked.pathing, "a legacy override is not the lock's to remove").toBe("orthogonal");
    expect(unlocked.waypoints).toEqual([{ x: 1, y: 2 }]);
  });
});
