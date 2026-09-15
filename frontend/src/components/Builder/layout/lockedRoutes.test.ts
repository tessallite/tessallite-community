import { describe, expect, it } from "vitest";
import { lockedEndpointIds, lockedRouteIntrusions, type LockedRouteGeometry, changedCards } from "./lockedRoutes";
import type { Rect } from "./types";

describe("lockedEndpointIds", () => {
  it("holds both cards a locked relationship docks to", () => {
    const ids = lockedEndpointIds([
      { id: "j1", source: "fact", target: "dim_a", locked: true },
      { id: "j2", source: "fact", target: "dim_b", locked: false },
    ]);
    expect([...ids].sort()).toEqual(["dim_a", "fact"]);
  });

  it("holds nothing when no relationship is locked", () => {
    expect(lockedEndpointIds([{ id: "j1", source: "a", target: "b", locked: false }]).size).toBe(0);
  });

  it("counts a card once even when several locked relationships dock to it", () => {
    const ids = lockedEndpointIds([
      { id: "j1", source: "fact", target: "dim_a", locked: true },
      { id: "j2", source: "fact", target: "dim_b", locked: true },
    ]);
    expect(ids.size).toBe(3);
  });
});

describe("lockedRouteIntrusions", () => {
  // A frozen horizontal run at y = 100, from x = 200 to x = 600.
  const route: LockedRouteGeometry = {
    id: "j1",
    source: "fact",
    target: "dim",
    points: [{ x: 200, y: 100 }, { x: 600, y: 100 }],
  };

  it("reports a card dropped across the frozen path", () => {
    const hits = lockedRouteIntrusions([route], [
      { id: "other", rect: { x: 350, y: 60, width: 120, height: 90 } },
    ]);
    expect(hits).toEqual(["j1"]);
  });

  it("ignores a card that was moved clear of it", () => {
    const hits = lockedRouteIntrusions([route], [
      { id: "other", rect: { x: 350, y: 300, width: 120, height: 90 } },
    ]);
    expect(hits).toEqual([]);
  });

  it("ignores the route's own endpoint cards", () => {
    // A route is supposed to touch the cards it connects, and an endpoint
    // cannot move anyway — reporting it would refuse every legitimate gesture.
    const hits = lockedRouteIntrusions([route], [
      { id: "fact", rect: { x: 150, y: 60, width: 120, height: 90 } },
      { id: "dim", rect: { x: 560, y: 60, width: 120, height: 90 } },
    ]);
    expect(hits).toEqual([]);
  });

  it("does not treat a card that only touches the line as an intrusion", () => {
    // A route legitimately runs along a card edge; refusing that would make a
    // lock reject ordinary tidy-up moves.
    const hits = lockedRouteIntrusions([route], [
      { id: "other", rect: { x: 350, y: 100, width: 120, height: 90 } },
    ]);
    expect(hits).toEqual([]);
  });

  it("reports a card across a vertical leg of a multi-bend route", () => {
    const bent: LockedRouteGeometry = {
      id: "j2",
      source: "a",
      target: "b",
      points: [{ x: 200, y: 100 }, { x: 400, y: 100 }, { x: 400, y: 500 }, { x: 700, y: 500 }],
    };
    const hits = lockedRouteIntrusions([bent], [
      { id: "other", rect: { x: 360, y: 250, width: 90, height: 80 } },
    ]);
    expect(hits).toEqual(["j2"]);
  });

  it("reports a card across a diagonal straight-mode route", () => {
    const diagonal: LockedRouteGeometry = {
      id: "j3",
      source: "a",
      target: "b",
      points: [{ x: 0, y: 0 }, { x: 400, y: 400 }],
    };
    expect(
      lockedRouteIntrusions([diagonal], [{ id: "other", rect: { x: 180, y: 180, width: 60, height: 60 } }]),
    ).toEqual(["j3"]);
    // A card in the diagonal's bounding box but off the line is not an
    // intrusion — a bounding-box test would wrongly refuse this gesture.
    expect(
      lockedRouteIntrusions([diagonal], [{ id: "other", rect: { x: 20, y: 300, width: 60, height: 60 } }]),
    ).toEqual([]);
  });

  it("reports every locked route a single move intrudes on", () => {
    const second: LockedRouteGeometry = { ...route, id: "j2", points: [{ x: 200, y: 140 }, { x: 600, y: 140 }] };
    const hits = lockedRouteIntrusions([route, second], [
      { id: "other", rect: { x: 350, y: 60, width: 120, height: 200 } },
    ]);
    expect(hits.sort()).toEqual(["j1", "j2"]);
  });
});

describe("changedCards", () => {
  // Reported by the external review (C02). The obstruction guard picked its
  // candidate cards by comparing x and y, which cannot see a resize: growing a
  // card from its bottom or right edge leaves both coordinates untouched. The
  // card that grew across a frozen connector was never offered to the geometry
  // check, and a locked route is precisely the route that cannot move aside.
  const rect = (x: number, y: number, width: number, height: number): Rect => ({ x, y, width, height });

  it("sees a card that grew downward without moving", () => {
    const before = new Map([["t", rect(250, 0, 200, 100)]]);
    const after = new Map([["t", rect(250, 0, 200, 250)]]);
    expect(changedCards(before, after).map((c) => c.id)).toEqual(["t"]);
  });

  it("sees a card that grew to the right without moving", () => {
    const before = new Map([["t", rect(0, 0, 200, 100)]]);
    const after = new Map([["t", rect(0, 0, 420, 100)]]);
    expect(changedCards(before, after).map((c) => c.id)).toEqual(["t"]);
  });

  it("sees a card that moved", () => {
    const before = new Map([["t", rect(0, 0, 200, 100)]]);
    const after = new Map([["t", rect(60, 0, 200, 100)]]);
    expect(changedCards(before, after).map((c) => c.id)).toEqual(["t"]);
  });

  it("treats a card that has just appeared as changed", () => {
    // It may have appeared on top of a frozen line.
    const after = new Map([["new", rect(0, 0, 200, 100)]]);
    expect(changedCards(new Map(), after).map((c) => c.id)).toEqual(["new"]);
  });

  it("ignores a card that did not change", () => {
    const before = new Map([["t", rect(10, 20, 200, 100)]]);
    const after = new Map([["t", rect(10, 20, 200, 100)]]);
    expect(changedCards(before, after)).toEqual([]);
  });

  it("ignores sub-pixel measurement noise", () => {
    // React Flow republishes measured sizes; a fraction of a pixel is not an
    // edit, and treating it as one would refuse harmless gestures.
    const before = new Map([["t", rect(10, 20, 200, 100)]]);
    const after = new Map([["t", rect(10.2, 20.1, 200.3, 100.4)]]);
    expect(changedCards(before, after)).toEqual([]);
  });

  it("reports the resized card's NEW rectangle, so the check tests what is on screen", () => {
    const before = new Map([["t", rect(250, 0, 200, 100)]]);
    const after = new Map([["t", rect(250, 0, 200, 250)]]);
    expect(changedCards(before, after)[0].rect).toEqual(rect(250, 0, 200, 250));
  });
});
