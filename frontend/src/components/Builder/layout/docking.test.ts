import { describe, expect, it } from "vitest";
import { ANCHOR_RATIO_MAX, ANCHOR_RATIO_MIN, alignedSideRatios, pointOnSide,
  baseRatioForDroppedPoint,
  ratioWithParallelOffset,
  parallelOffsetFor,
  centreLineDocking,
  sideFromCenters,
} from "./docking";
import type { Rect } from "./types";

function rect(x: number, y: number, width: number, height: number): Rect {
  return { x, y, width, height };
}

describe("alignedSideRatios", () => {
  it("aligns both docks to the overlap when two cards' ranges overlap (straight connector)", () => {
    // Side-by-side cards that also overlap vertically: source right side and
    // target left side should both dock at the same y so the connector is a
    // straight horizontal line.
    const source = rect(0, 0, 200, 200);
    const target = rect(400, 0, 200, 200);
    const { source: s, target: t } = alignedSideRatios(source, "right", target, "left");
    // Same aligned y on both cards: source centre y=100 -> ratio 0.5.
    expect(s).toBeCloseTo(0.5, 5);
    expect(t).toBeCloseTo(0.5, 5);
  });

  it("aligns to the shared overlap band when one card is taller", () => {
    // Target is much taller and its vertical range fully covers the source's,
    // so the overlap is the source's own range: both docks align to y=100.
    const source = rect(0, 0, 200, 200);
    const target = rect(400, -100, 200, 500);
    const { source: s, target: t } = alignedSideRatios(source, "right", target, "left");
    expect(s).toBeCloseTo(0.5, 5);
    // Target top is -100, aligned y=100 -> ratio (100 - (-100))/500 = 0.4.
    expect(t).toBeCloseTo(0.4, 5);
  });

  it("docks where the centre line crosses the border when ranges do not overlap", () => {
    // No vertical overlap, so there is no band both cards can reach. The dock
    // goes where the line between the two card CENTRES crosses this card's
    // border — beside the other card, at the height the line between them is
    // already travelling at.
    //
    // The previous rule aimed each dock AT the other card's centre, which is a
    // different point and frequently not on the edge at all: here it asked for
    // ratio 2.0 and was clamped to the anchor band's limit, parking the
    // connector at the very END of the edge. On a tall card that reads as the
    // relationship leaving from a corner rather than from beside its partner,
    // and it is what made unnecessary crossings look unavoidable.
    const source = rect(0, 0, 200, 200);
    const target = rect(400, 300, 200, 200);
    const { source: s, target: t } = alignedSideRatios(source, "right", target, "left");

    // Centre (100,100) -> (500,400): dx=400, dy=300, so the line leaves the
    // right edge at y = 100 + 300 * (100/400) = 175, i.e. ratio 0.875.
    expect(s).toBeCloseTo(0.875, 3);
    // And reaches the target's left edge at y = 400 - 75 = 325, ratio 0.125.
    expect(t).toBeCloseTo(0.125, 3);

    // Both are strictly inside the edge rather than pinned to its ends, which
    // is the behaviour this rule exists to produce.
    expect(s).toBeLessThan(ANCHOR_RATIO_MAX);
    expect(t).toBeGreaterThan(ANCHOR_RATIO_MIN);
  });

  it("aligns horizontally for vertical docks", () => {
    const source = rect(0, 0, 200, 200);
    const target = rect(0, 400, 200, 200);
    const { source: s, target: t } = alignedSideRatios(source, "bottom", target, "top");
    expect(s).toBeCloseTo(0.5, 5);
    expect(t).toBeCloseTo(0.5, 5);
  });
});

describe("alignedSideRatios — usable bands, not full edges", () => {
  /** Where this card's dock actually lands, in absolute coordinates. */
  function dockY(r: Rect, side: "left" | "right", ratio: number): number {
    return pointOnSide(r, side, ratio).y;
  }

  it("puts both docks on one coordinate when the usable bands overlap", () => {
    // External review witness. The old rule intersected the FULL edges, took
    // that midpoint, then clamped each end separately — and with these very
    // unequal heights the two clamps landed 13.7 px apart, producing a dogleg
    // where a straight connector was available.
    const source = rect(0, 0, 420, 640);
    const target = rect(700, 565, 180, 160);

    const ratios = alignedSideRatios(source, "right", target, "left");
    const sourceY = dockY(source, "right", ratios.source);
    const targetY = dockY(target, "left", ratios.target);

    expect(Math.abs(sourceY - targetY), "the two docks must share a coordinate").toBeLessThan(0.5);

    // …and that coordinate must be reachable by BOTH cards, which is the
    // property that stops either clamp from moving it afterwards.
    const band = (r: Rect) => ({
      low: r.y + r.height * ANCHOR_RATIO_MIN,
      high: r.y + r.height * ANCHOR_RATIO_MAX,
    });
    const sourceBand = band(source);
    const targetBand = band(target);
    expect(sourceY).toBeGreaterThanOrEqual(Math.max(sourceBand.low, targetBand.low) - 0.5);
    expect(sourceY).toBeLessThanOrEqual(Math.min(sourceBand.high, targetBand.high) + 0.5);
  });

  it("does the same on the horizontal axis", () => {
    const source = rect(0, 0, 640, 420);
    const target = rect(565, 700, 160, 180);
    const ratios = alignedSideRatios(source, "bottom", target, "top");
    const sourceX = pointOnSide(source, "bottom", ratios.source).x;
    const targetX = pointOnSide(target, "top", ratios.target).x;
    expect(Math.abs(sourceX - targetX)).toBeLessThan(0.5);
  });

  it("falls back to centre-pointing when the usable bands do not overlap", () => {
    // Far apart vertically: no coordinate is reachable by both, so each dock
    // points at the other card instead of being forced onto a shared line.
    const source = rect(0, 0, 200, 100);
    const target = rect(700, 4000, 200, 100);
    const ratios = alignedSideRatios(source, "right", target, "left");
    expect(ratios.source).toBeCloseTo(ANCHOR_RATIO_MAX, 6);
    expect(ratios.target).toBeCloseTo(ANCHOR_RATIO_MIN, 6);
  });

  it("does not align sides that vary along different axes", () => {
    // A vertical source and a horizontal target share no coordinate to agree
    // on. The old rule picked its axis from the source alone and applied it to
    // both, which aligned on an axis only one dock could move along.
    const source = rect(0, 0, 200, 600);
    const target = rect(400, 700, 600, 200);
    const ratios = alignedSideRatios(source, "right", target, "top");
    const towardTarget = alignedSideRatios(source, "right", rect(400, 700, 600, 200), "top");
    expect(ratios).toEqual(towardTarget);
    // Each ratio stays inside the drawable band rather than being clamped to an
    // edge by an axis that does not apply to it.
    for (const value of [ratios.source, ratios.target]) {
      expect(value).toBeGreaterThanOrEqual(ANCHOR_RATIO_MIN);
      expect(value).toBeLessThanOrEqual(ANCHOR_RATIO_MAX);
    }
  });
});

describe("baseRatioForDroppedPoint", () => {
  // Reported by the external review (C08). The pointer calculation produces an
  // EFFECTIVE ratio — where on the card border the heel actually sits — and the
  // commit stored it as the BASE ratio, to which the renderer then adds this
  // edge's parallel fan-out offset. The attachment therefore landed beside
  // where the user dropped it whenever the relationship had a parallel sibling.
  const card: Rect = { x: 0, y: 0, width: 300, height: 200 };

  it("round-trips a dropped point back to itself for a parallel relationship", () => {
    // The witness: the second of two parallel joins. A drop asking for y=120 is
    // ratio 0.6 on a 200-tall card; storing 0.6 as the base rendered at 127.5.
    const requested = 0.6;
    const base = baseRatioForDroppedPoint(card, "right", requested, 1, 2);
    const rendered = ratioWithParallelOffset(card, "right", base, parallelOffsetFor(1, 2));

    expect(rendered).toBeCloseTo(requested, 6);
    expect(base, "the stored value is not the dropped value").not.toBeCloseTo(requested, 6);
    expect(card.y + rendered * card.height, "the heel lands where it was dropped").toBeCloseTo(120, 6);
  });

  it("is the identity when the relationship has no parallel sibling", () => {
    // A single relationship has no fan-out, so nothing should be subtracted.
    expect(baseRatioForDroppedPoint(card, "right", 0.7, 0, 1)).toBeCloseTo(0.7, 6);
  });

  it("round-trips on a horizontal side, where the span is the width", () => {
    const requested = 0.35;
    const base = baseRatioForDroppedPoint(card, "top", requested, 0, 2);
    const rendered = ratioWithParallelOffset(card, "top", base, parallelOffsetFor(0, 2));
    expect(rendered).toBeCloseTo(requested, 6);
  });

  it("clamps to the card rather than refusing a drop near the corner", () => {
    // The user dropped somewhere. If the fan-out puts the exact point off the
    // card, the nearest reachable point is the honest answer — not a refusal,
    // and not a value outside 0..1.
    const base = baseRatioForDroppedPoint(card, "right", 0.99, 1, 2);
    expect(base).toBeGreaterThanOrEqual(0);
    expect(base).toBeLessThanOrEqual(1);
  });
});

describe("centreLineDocking", () => {
  // The rule: a connector meets a card where the line between the two card
  // centres crosses that card's border — on the edges the cards actually face
  // each other across. Never the far side of a card, never the far end of an
  // edge.
  const tallFact = rect(0, 0, 300, 2000);

  it("leaves a very tall card through the side the other card is on", () => {
    // Reported from a real model. A fifty-column fact table is two thousand
    // pixels tall. Comparing raw |dx| against |dy| made a dimension sitting to
    // its RIGHT but above centre come out as "top", so the connector docked on
    // the top edge and ran diagonally back across the full height of the card
    // it had just left — the relationship drawn through its own fact table.
    const dimensionAboveRight = rect(600, 200, 240, 180);
    expect(centreLineDocking(tallFact, dimensionAboveRight).side).toBe("right");
  });

  it("still uses the top edge for a card genuinely above it", () => {
    // The rule must not simply always answer "right": a card directly above is
    // above, and leaving through the top is correct there.
    const directlyAbove = rect(0, -900, 240, 180);
    expect(centreLineDocking(tallFact, directlyAbove).side).toBe("top");
  });

  it("docks beside the other card, not at the end of the edge", () => {
    const dimensionAboveRight = rect(600, 200, 240, 180);
    const { ratio } = centreLineDocking(tallFact, dimensionAboveRight);
    // The other card's centre is at y=290, which is 14.5% down the fact card.
    // The dock lands near there rather than clamped to the top of the edge.
    expect(ratio).toBeGreaterThan(0.05);
    expect(ratio).toBeLessThan(0.5);
  });

  it("is unchanged for a square card, so ordinary diagrams do not move", () => {
    // The half-extent normalisation cancels when width equals height, which is
    // what makes this safe to apply everywhere.
    const square = rect(0, 0, 200, 200);
    for (const other of [rect(400, 60, 200, 200), rect(-400, 60, 200, 200), rect(60, 400, 200, 200), rect(60, -400, 200, 200)]) {
      expect(centreLineDocking(square, other).side).toBe(sideFromCenters(square, other));
    }
  });

  it("puts the dock at the midpoint for cards directly opposite", () => {
    const square = rect(0, 0, 200, 200);
    const opposite = rect(500, 0, 200, 200);
    const { side, ratio } = centreLineDocking(square, opposite);
    expect(side).toBe("right");
    expect(ratio).toBeCloseTo(0.5, 6);
  });

  it("answers something usable for concentric cards", () => {
    const square = rect(0, 0, 200, 200);
    const { side, ratio } = centreLineDocking(square, rect(0, 0, 200, 200));
    expect(["left", "right", "top", "bottom"]).toContain(side);
    expect(ratio).toBeGreaterThanOrEqual(0);
    expect(ratio).toBeLessThanOrEqual(1);
  });

  it("never returns a ratio outside the card", () => {
    const square = rect(0, 0, 200, 200);
    for (const other of [rect(5000, -5000, 10, 10), rect(-5000, 5000, 10, 10), rect(1, 9999, 10, 10)]) {
      const { ratio } = centreLineDocking(square, other);
      expect(ratio).toBeGreaterThanOrEqual(0);
      expect(ratio).toBeLessThanOrEqual(1);
    }
  });
});
