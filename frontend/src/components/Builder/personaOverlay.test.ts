import { describe, it, expect } from "vitest";
import { computeDimmedTableIds } from "./personaOverlay";

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
});
