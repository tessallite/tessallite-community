/**
 * What the canvas does with a failed layout operation, and whether a real
 * rejection actually arrives carrying the code that decision depends on.
 *
 * The defect these guard: every rejection in the layout path used to throw a
 * plain `Error`. The worker classified only errors carrying a `.code`, so a
 * genuine geometry rejection ("relationship is not orthogonal") reached the
 * canvas as `unknown` — and `unknown` was the branch that KEPT the candidate,
 * flushed it and recorded it as a successful gesture. The engine could detect
 * an incoherent route and then persist the edit that caused it.
 *
 * Two separate things have to hold, so both are asserted here rather than
 * assumed: the policy has to be right, AND the code has to survive the trip.
 */
import { describe, expect, it } from "vitest";
import { dispositionFor } from "./failurePolicy";
import { LayoutError, failureCodeOf, geometryInvalid } from "./layoutErrors";
import { validateRoutes } from "./geometry";
import type { LayoutEdgeSnapshot, LayoutFailureCode, LayoutNodeSnapshot } from "./types";

describe("dispositionFor", () => {
  const cases: Array<[LayoutFailureCode, string]> = [
    ["geometry-invalid", "discard"],
    ["no-route", "discard"],
    ["invalid-input", "discard"],
    ["engine-unavailable", "keep"],
    ["timeout", "keep"],
    ["cancelled", "abandon"],
    ["superseded", "abandon"],
    ["unknown", "discard"],
  ];

  for (const [code, expected] of cases) {
    it(`${code} -> ${expected}`, () => {
      expect(dispositionFor(code)).toBe(expected);
    });
  }

  it("never keeps an edit the engine did not judge", () => {
    // The asymmetry this whole policy exists for: discarding on an engine fault
    // costs the user one repeated drag, and they can see it happen. Keeping on
    // a geometry fault saves a diagram whose cards and connectors disagree, and
    // they find out when they reopen the model.
    expect(dispositionFor("unknown")).not.toBe("keep");
  });
});

describe("failureCodeOf", () => {
  it("reads the code from a LayoutError", () => {
    expect(failureCodeOf(geometryInvalid("relationship is not orthogonal: j"))).toBe("geometry-invalid");
  });

  it("reads the code after the error has crossed the worker boundary", () => {
    // Structured clone drops the prototype, so `instanceof` is false on the
    // main thread and only the plain shape survives.
    const cloned = JSON.parse(JSON.stringify({ code: "no-route", message: "x" }));
    expect(failureCodeOf(cloned)).toBe("no-route");
  });

  it("does not invent a code for an ordinary Error", () => {
    expect(failureCodeOf(new Error("something else went wrong"))).toBe("unknown");
  });

  it("does not trust an arbitrary code value", () => {
    expect(failureCodeOf({ code: "totally-fine" })).toBe("unknown");
  });
});

describe("a real rejection carries its code", () => {
  function node(id: string, x: number, y: number): LayoutNodeSnapshot {
    return { id, x, y, width: 200, height: 100, tableType: "dimension", pinned: false, fixed: false, selected: false, measured: true };
  }

  it("a diagonal in an orthogonal route is classified geometry-invalid, not unknown", () => {
    // The reviewer's own witness. Before typed errors this threw a plain Error
    // and reached the canvas as `unknown`, which kept and saved it.
    const nodes = [node("a", 0, 0), node("b", 600, 400)];
    const positions = { a: { x: 0, y: 0 }, b: { x: 600, y: 400 } };
    const edges: LayoutEdgeSnapshot[] = [{
      id: "j", source: "a", target: "b",
      pathMode: "orthogonal", routeMode: "auto", waypoints: [], locked: false,
      sourceMarkerExtent: 0, targetMarkerExtent: 0,
    }];
    const routes = {
      j: { edgeId: "j", points: [{ x: 200, y: 50 }, { x: 600, y: 450 }], locked: false,
           sourceSide: "right" as const, targetSide: "left" as const, sourceRatio: 0.5, targetRatio: 0.5,
           waypoints: [], routeMode: "auto" as const, pathMode: "orthogonal" as const },
    };

    let thrown: unknown;
    try {
      validateRoutes(nodes, positions, edges, routes, 24, true, false);
    } catch (error) {
      thrown = error;
    }

    expect(thrown, "the diagonal was rejected").toBeInstanceOf(LayoutError);
    expect(failureCodeOf(thrown)).toBe("geometry-invalid");
    expect(dispositionFor(failureCodeOf(thrown)), "so the canvas discards the edit").toBe("discard");
  });
});
