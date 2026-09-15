/**
 * Turning a saved table entry back into the card the user sees.
 *
 * Three copies of this decision disagreed, each in a way the user could see.
 * These fix the agreed answer so the copies cannot drift apart again.
 */
import { describe, expect, it } from "vitest";
import {
  MIN_TABLE_HEIGHT,
  MIN_TABLE_WIDTH,
  isSupportedTableSize,
  styleForTableEntry,
} from "./tablePresentation";

describe("styleForTableEntry", () => {
  it("applies a saved size to the card", () => {
    expect(styleForTableEntry({}, { w: 320, h: 240 })).toMatchObject({ width: 320, height: 240 });
  });

  it("removes a size the entry does not carry", () => {
    // The first-resize rollback hole. The before-state of a content-sized card
    // has no explicit width or height; restoring it has to give the card back
    // to its content, not keep the size the resize introduced.
    const afterFirstResize = { width: 320, height: 240, background: "#fff" };
    const restored = styleForTableEntry(afterFirstResize, {});
    expect(restored.width, "the introduced width is gone").toBeUndefined();
    expect(restored.height, "the introduced height is gone").toBeUndefined();
    expect(restored.background, "unrelated style is untouched").toBe("#fff");
  });

  it("keeps the smallest size the resizer can actually produce", () => {
    // Hydration used to ignore any saved height of 160 or less while the
    // resizer's minimum is 150, so a card the user was allowed to make 150
    // tall reopened at a different height and the edit looked lost.
    const style = styleForTableEntry({}, { w: MIN_TABLE_WIDTH, h: MIN_TABLE_HEIGHT });
    expect(style.height).toBe(150);
    expect(style.width).toBe(200);
  });

  it("rejects a size below the resizer's minimum as corrupt", () => {
    // An earlier resizer defect wrote crushed values. Those are not sizes any
    // user produced, so they do not come back on reopen.
    const style = styleForTableEntry({}, { w: 12, h: 3 });
    expect(style.width).toBeUndefined();
    expect(style.height).toBeUndefined();
  });

  it("rejects a non-finite size", () => {
    const style = styleForTableEntry({}, { w: Number.NaN, h: Number.POSITIVE_INFINITY });
    expect(style.width).toBeUndefined();
    expect(style.height).toBeUndefined();
  });

  it("does not mutate the style it was given", () => {
    const original = { width: 320, height: 240 };
    styleForTableEntry(original, {});
    expect(original).toEqual({ width: 320, height: 240 });
  });
});

describe("isSupportedTableSize", () => {
  it("accepts the minimum itself, not merely values above it", () => {
    // `> 160` versus `>= 150` is the exact shape of the defect.
    expect(isSupportedTableSize(MIN_TABLE_HEIGHT, MIN_TABLE_HEIGHT)).toBe(true);
  });

  it("rejects one pixel below the minimum", () => {
    expect(isSupportedTableSize(MIN_TABLE_HEIGHT - 1, MIN_TABLE_HEIGHT)).toBe(false);
  });

  it("rejects a missing value", () => {
    expect(isSupportedTableSize(undefined, MIN_TABLE_WIDTH)).toBe(false);
  });
});
