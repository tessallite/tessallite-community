/**
 * R10 / spec §5: "never silently drop unknown presentation fields".
 *
 * These assert the retention rule directly, because the way it gets broken is
 * always the same and always looks careful: rebuilding an entry and listing the
 * fields you care about.
 */
import { describe, expect, it } from "vitest";
import { mergeEdgeEntry, mergeTableEntry, type EdgeEntry, type TableEntry } from "./presentationEntries";

/** A saved entry written by a build that knew about more than this one does. */
function futureTable(): TableEntry & Record<string, unknown> {
  return { x: 10, y: 20, w: 300, h: 200, pinned: true, collapsed: true, accentColour: "#123456" };
}

describe("mergeTableEntry", () => {
  it("keeps fields this build does not know about", () => {
    const merged = mergeTableEntry(futureTable(), { x: 99, y: 98 }) as Record<string, unknown>;
    expect(merged.collapsed).toBe(true);
    expect(merged.accentColour).toBe("#123456");
  });

  it("keeps the pin through a move", () => {
    // The concrete defect this replaced: the drag write site rebuilt the entry
    // enumerating only w and h, so dragging a pinned table unpinned it.
    const merged = mergeTableEntry(futureTable(), { x: 99, y: 98 });
    expect(merged.pinned).toBe(true);
    expect(merged.x).toBe(99);
    expect(merged.y).toBe(98);
  });

  it("keeps the size through a move, and the position through a resize", () => {
    const moved = mergeTableEntry(futureTable(), { x: 99, y: 98 });
    expect(moved.w).toBe(300);
    expect(moved.h).toBe(200);

    const resized = mergeTableEntry(futureTable(), { w: 400, h: 250 });
    expect(resized.x).toBe(10);
    expect(resized.y).toBe(20);
  });

  it("starts an entry that did not exist at the origin", () => {
    // A table with no saved entry still needs coordinates; the caller supplies
    // the real ones in the change.
    expect(mergeTableEntry(undefined, { pinned: true })).toEqual({ x: 0, y: 0, pinned: true });
  });

  it("removes a field only when it is named with undefined", () => {
    const merged = mergeTableEntry(futureTable(), { w: undefined });
    expect("w" in merged).toBe(true);
    expect(merged.w).toBeUndefined();
    // …and JSON persistence then drops the key, which is how a field is cleared.
    expect(JSON.parse(JSON.stringify(merged))).not.toHaveProperty("w");
  });

  it("does not mutate the entry it was given", () => {
    const previous = futureTable();
    mergeTableEntry(previous, { x: 99, pinned: false });
    expect(previous.x).toBe(10);
    expect(previous.pinned).toBe(true);
  });
});

describe("mergeEdgeEntry", () => {
  function futureEdge(): EdgeEntry & Record<string, unknown> {
    return {
      waypoints: [{ x: 1, y: 2 }],
      pathing: "orthogonal",
      sourceSide: "right",
      locked: true,
      routeMode: "manual",
      annotation: "reviewed",
    };
  }

  it("keeps unknown fields, the lock and the provenance through a reroute", () => {
    const merged = mergeEdgeEntry(futureEdge(), {
      waypoints: [{ x: 5, y: 6 }],
      targetSide: "left",
    }) as Record<string, unknown>;
    expect(merged.annotation).toBe("reviewed");
    expect(merged.locked).toBe(true);
    expect(merged.routeMode).toBe("manual");
    expect(merged.waypoints).toEqual([{ x: 5, y: 6 }]);
    expect(merged.targetSide).toBe("left");
  });

  it("clears the legacy single waypoint when it is named", () => {
    // The redraw path supersedes a legacy `waypoint` with a captured array.
    const merged = mergeEdgeEntry({ waypoint: { x: 1, y: 1 } }, { waypoint: undefined, waypoints: [{ x: 2, y: 2 }] });
    expect(JSON.parse(JSON.stringify(merged))).toEqual({ waypoints: [{ x: 2, y: 2 }] });
  });

  it("starts from nothing for a relationship with no saved entry", () => {
    expect(mergeEdgeEntry(undefined, { locked: true })).toEqual({ locked: true });
  });

  it("does not mutate the entry it was given", () => {
    const previous = futureEdge();
    mergeEdgeEntry(previous, { locked: false });
    expect(previous.locked).toBe(true);
  });
});
