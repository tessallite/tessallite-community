import { describe, it, expect } from "vitest";
import {
  computeDimmedTableIds,
  computeClsRestrictedObjectIds,
  summarizeDefaultFilters,
} from "./personaOverlay";

// F-026-06 — the persona overlay must dim a table only when NONE of its objects
// are queryable under the persona, mirroring the backend gate where a per-type
// EMPTY allow list means "unrestricted for that type". The review's trace:
//
//   persona P: included_measure_ids = [m1], dimensions [], hierarchies []
//   FactSales (m1, m2)  -> m1 queryable  -> bright (correct)
//   DimCity   (d1)      -> dims unrestricted -> queryable -> bright (was WRONG)
//   DimNote   (no objs) -> bright (correct)
describe("computeDimmedTableIds (F-026-06)", () => {
  it("does NOT dim a dimension-only table under a measures-only persona", () => {
    const dimmed = computeDimmedTableIds(
      [
        { tableId: "FactSales", measureIds: ["m1", "m2"], dimensionIds: [], hierarchyIds: [] },
        { tableId: "DimCity", measureIds: [], dimensionIds: ["d1"], hierarchyIds: [] },
        { tableId: "DimNote", measureIds: [], dimensionIds: [], hierarchyIds: [] },
      ],
      {
        measureIds: new Set(["m1"]),
        dimensionIds: new Set(),
        hierarchyIds: new Set(),
      },
    );
    expect(dimmed.has("FactSales")).toBe(false);
    expect(dimmed.has("DimCity")).toBe(false); // the bug: was dimmed
    expect(dimmed.has("DimNote")).toBe(false);
  });

  it("dims a table whose only restricted-type object is excluded", () => {
    // Persona allows m1 only. FactOther carries m2 (a measure, restricted type)
    // and nothing else -> not queryable -> dimmed.
    const dimmed = computeDimmedTableIds(
      [
        { tableId: "FactSales", measureIds: ["m1"], dimensionIds: [], hierarchyIds: [] },
        { tableId: "FactOther", measureIds: ["m2"], dimensionIds: [], hierarchyIds: [] },
      ],
      { measureIds: new Set(["m1"]), dimensionIds: new Set(), hierarchyIds: new Set() },
    );
    expect(dimmed.has("FactSales")).toBe(false);
    expect(dimmed.has("FactOther")).toBe(true);
  });

  it("dims nothing when the persona is fully unrestricted (all allow lists empty)", () => {
    const dimmed = computeDimmedTableIds(
      [{ tableId: "T", measureIds: ["m1"], dimensionIds: ["d1"], hierarchyIds: [] }],
      { measureIds: new Set(), dimensionIds: new Set(), hierarchyIds: new Set() },
    );
    expect(dimmed.size).toBe(0);
  });

  it("respects an explicit dimension allow list (mixed restriction)", () => {
    // Measures restricted to m1; dimensions restricted to d1.
    const dimmed = computeDimmedTableIds(
      [
        { tableId: "FactSales", measureIds: ["m1"], dimensionIds: [], hierarchyIds: [] },
        { tableId: "DimCity", measureIds: [], dimensionIds: ["d1"], hierarchyIds: [] },
        { tableId: "DimRegion", measureIds: [], dimensionIds: ["d2"], hierarchyIds: [] },
      ],
      { measureIds: new Set(["m1"]), dimensionIds: new Set(["d1"]), hierarchyIds: new Set() },
    );
    expect(dimmed.has("FactSales")).toBe(false);
    expect(dimmed.has("DimCity")).toBe(false);
    expect(dimmed.has("DimRegion")).toBe(true); // d2 not in allow list
  });

  it("never dims a table with no semantic objects", () => {
    const dimmed = computeDimmedTableIds(
      [{ tableId: "Empty", measureIds: [], dimensionIds: [], hierarchyIds: [] }],
      { measureIds: new Set(["m1"]), dimensionIds: new Set(), hierarchyIds: new Set() },
    );
    expect(dimmed.has("Empty")).toBe(false);
  });

  // F-008-03 — the preview must reflect COLUMN-LEVEL security: a field whose
  // backing column the persona restricts is NOT queryable, so a table whose
  // only otherwise-allowed field is CLS-restricted must dim.
  it("dims a table whose only field is CLS-restricted (allow lists empty)", () => {
    const dimmed = computeDimmedTableIds(
      [{ tableId: "DimEmp", measureIds: [], dimensionIds: ["dSalary"], hierarchyIds: [] }],
      { measureIds: new Set(), dimensionIds: new Set(), hierarchyIds: new Set() },
      {
        restrictedColumnIds: new Set(["colSalary"]),
        sources: {
          measureSourceColumnId: {},
          dimensionSourceColumnId: { dSalary: "colSalary" },
        },
      },
    );
    expect(dimmed.has("DimEmp")).toBe(true);
  });

  it("keeps a table lit when a non-restricted field remains queryable", () => {
    const dimmed = computeDimmedTableIds(
      [
        {
          tableId: "DimEmp",
          measureIds: [],
          dimensionIds: ["dSalary", "dRegion"],
          hierarchyIds: [],
        },
      ],
      { measureIds: new Set(), dimensionIds: new Set(), hierarchyIds: new Set() },
      {
        restrictedColumnIds: new Set(["colSalary"]),
        sources: {
          measureSourceColumnId: {},
          dimensionSourceColumnId: { dSalary: "colSalary", dRegion: "colRegion" },
        },
      },
    );
    expect(dimmed.has("DimEmp")).toBe(false);
  });
});

describe("computeClsRestrictedObjectIds (F-008-03)", () => {
  it("returns the objects whose backing column is restricted", () => {
    const out = computeClsRestrictedObjectIds(
      {
        measureSourceColumnId: { mSalary: "colSalary", mCount: "colCount" },
        dimensionSourceColumnId: { dSalary: "colSalary", dRegion: "colRegion" },
      },
      new Set(["colSalary"]),
    );
    expect(out.measureIds.has("mSalary")).toBe(true);
    expect(out.measureIds.has("mCount")).toBe(false);
    expect(out.dimensionIds.has("dSalary")).toBe(true);
    expect(out.dimensionIds.has("dRegion")).toBe(false);
  });

  it("F-008-06 unions server-provided blocked calculated objects", () => {
    const out = computeClsRestrictedObjectIds(
      {
        measureSourceColumnId: { mSalary: "colSalary", mMargin: null },
        dimensionSourceColumnId: {},
      },
      new Set(["colSalary"]),
      { measureIds: ["mMargin"], dimensionIds: ["dCalc"] },
    );
    expect(out.measureIds.has("mSalary")).toBe(true);
    expect(out.measureIds.has("mMargin")).toBe(true);
    expect(out.dimensionIds.has("dCalc")).toBe(true);
  });

  it("returns nothing when there are no restrictions", () => {
    const out = computeClsRestrictedObjectIds(
      { measureSourceColumnId: { m: "c" }, dimensionSourceColumnId: {} },
      new Set(),
    );
    expect(out.measureIds.size).toBe(0);
    expect(out.dimensionIds.size).toBe(0);
  });
});

describe("summarizeDefaultFilters (F-008-03)", () => {
  it("renders scalar, list, and operator default filters as chips", () => {
    const chips = summarizeDefaultFilters({
      region: "EMEA",
      country: ["FR", "DE"],
      amount: { gte: 100 },
    });
    expect(chips).toContain("region = EMEA");
    expect(chips).toContain("country in [FR, DE]");
    expect(chips).toContain("amount gte 100");
  });

  it("skips @-prefixed parameter overrides and handles empty input", () => {
    expect(summarizeDefaultFilters({ "@param": "x", year: 2026 })).toEqual([
      "year = 2026",
    ]);
    expect(summarizeDefaultFilters(null)).toEqual([]);
    expect(summarizeDefaultFilters(undefined)).toEqual([]);
  });
});
