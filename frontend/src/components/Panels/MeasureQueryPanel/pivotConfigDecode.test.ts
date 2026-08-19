/**
 * Defensive config decoder (Bug-8161, review B2). Proves a malformed HISTORICAL
 * config is coerced to a safe shape on READ so it can never crash the loader,
 * while a well-formed config passes through untouched.
 */
import { describe, expect, it } from "vitest";
import {
  decodeSlicers,
  decodeConditionalFormat,
  decodeMeasureSelections,
  decodeEmptyCellMode,
  decodeBool,
  decodePersonaId,
} from "./pivotConfigDecode";

describe("decodeSlicers", () => {
  it("drops malformed entries and reports ok=false (the SlicerBar crash vectors)", () => {
    const { value, ok } = decodeSlicers([
      null,
      3,
      { op: "eq", values: [] }, // missing dimensionId
      { dimensionId: "d1", op: "nope", values: [] }, // unknown op
      { dimensionId: "d2", op: "eq", values: ["x", 5, null] }, // non-string values dropped
    ]);
    expect(ok).toBe(false);
    expect(value).toEqual([{ dimensionId: "d2", op: "eq", values: ["x"] }]);
  });

  it("passes a well-formed list through with ok=true", () => {
    const { value, ok } = decodeSlicers([{ dimensionId: "d", op: "in", values: ["a", "b"] }]);
    expect(ok).toBe(true);
    expect(value).toEqual([{ dimensionId: "d", op: "in", values: ["a", "b"] }]);
  });

  it("treats an absent field as empty and ok, but a non-array as not-ok", () => {
    expect(decodeSlicers(undefined)).toEqual({ value: [], ok: true });
    expect(decodeSlicers("oops")).toEqual({ value: [], ok: false });
    expect(decodeSlicers(null)).toEqual({ value: [], ok: false });
  });
});

describe("decodeConditionalFormat", () => {
  it("coerces null / scalar / unknown-kind to {kind:none} with ok=false (PivotGrid crash vector)", () => {
    expect(decodeConditionalFormat(null)).toEqual({ value: { kind: "none" }, ok: false });
    expect(decodeConditionalFormat("oops")).toEqual({ value: { kind: "none" }, ok: false });
    expect(decodeConditionalFormat({ kind: "mystery" })).toEqual({ value: { kind: "none" }, ok: false });
    expect(decodeConditionalFormat({ kind: "color-scale", low: "#fff" })).toEqual({
      value: { kind: "none" },
      ok: false,
    });
  });

  it("passes each valid variant through", () => {
    expect(decodeConditionalFormat({ kind: "none" }).ok).toBe(true);
    expect(decodeConditionalFormat({ kind: "color-scale", low: "#fff", high: "#000" })).toEqual({
      value: { kind: "color-scale", low: "#fff", high: "#000" },
      ok: true,
    });
    expect(decodeConditionalFormat({ kind: "data-bars", color: "#abc" }).ok).toBe(true);
    expect(
      decodeConditionalFormat({ kind: "threshold", below: "#a", above: "#b", threshold: 5 }).ok,
    ).toBe(true);
  });
});

describe("decodeMeasureSelections", () => {
  it("drops null / malformed entries (the buildColumnMeasures crash vector)", () => {
    const { value, ok } = decodeMeasureSelections([
      null,
      { agg: "SUM" }, // no measureId
      { measureId: "m1", agg: "AVG" },
      { measureId: "m2" }, // agg defaulted
    ]);
    expect(ok).toBe(false);
    expect(value).toEqual([
      { measureId: "m1", agg: "AVG" },
      { measureId: "m2", agg: "" },
    ]);
  });
});

describe("scalar decoders", () => {
  it("emptyCellMode falls back to blank on anything unexpected", () => {
    expect(decodeEmptyCellMode("zero")).toBe("zero");
    expect(decodeEmptyCellMode("dash")).toBe("dash");
    expect(decodeEmptyCellMode("purple")).toBe("blank");
    expect(decodeEmptyCellMode(null)).toBe("blank");
  });

  it("decodeBool only accepts a real boolean", () => {
    expect(decodeBool(true, false)).toBe(true);
    expect(decodeBool("yes", false)).toBe(false); // truthy string is NOT a boolean
    expect(decodeBool(undefined, true)).toBe(true);
  });

  it("decodePersonaId only accepts a non-empty string", () => {
    expect(decodePersonaId("p1")).toBe("p1");
    expect(decodePersonaId("")).toBeNull();
    expect(decodePersonaId(5)).toBeNull();
    expect(decodePersonaId(undefined)).toBeNull();
  });
});
