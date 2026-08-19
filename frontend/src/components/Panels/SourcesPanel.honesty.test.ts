/**
 * F-014-05 / F-014-06 / G-014-02: Sources panel honesty helpers.
 *
 * Truncation is a flag, never a selectable table. Classify-add persists
 * profiled is_nullable instead of hardcoding true.
 */
import { describe, it, expect } from "vitest";
import {
  classifiedColumnsToSyncPayload,
  isTruncationMarker,
  normalizeDiscoverTablesResponse,
  selectableDiscoveredTables,
} from "./SourcesPanel";

describe("F-014-05 catalogue truncation", () => {
  it("strips the sentinel row and surfaces truncated=true", () => {
    const payload = normalizeDiscoverTablesResponse([
      { schema: "public", table: "orders", type: "BASE TABLE" },
      { schema: "__tessallite__", table: "__truncated__", type: "NOTICE" },
    ]);
    expect(payload.truncated).toBe(true);
    expect(payload.tables).toEqual([
      { schema: "public", table: "orders", type: "BASE TABLE" },
    ]);
    expect(payload.tables.some(isTruncationMarker)).toBe(false);
  });

  it("keeps a real public.__truncated__ BASE TABLE selectable (Bug-9290)", () => {
    const payload = normalizeDiscoverTablesResponse([
      { schema: "public", table: "__truncated__", type: "BASE TABLE" },
    ]);
    expect(payload.truncated).toBe(false);
    expect(payload.tables).toEqual([
      { schema: "public", table: "__truncated__", type: "BASE TABLE" },
    ]);
    expect(isTruncationMarker(payload.tables[0])).toBe(false);
    expect(selectableDiscoveredTables(payload.tables)).toHaveLength(1);
  });

  it("honours a structured truncated flag", () => {
    const payload = normalizeDiscoverTablesResponse({
      tables: [{ schema: "sales", table: "invoices", type: "BASE TABLE" }],
      truncated: true,
    });
    expect(payload.truncated).toBe(true);
    expect(selectableDiscoveredTables(payload.tables)).toHaveLength(1);
  });

  it("flags truncation from a sentinel inside the dict payload's tables (Bug-9291/9292)", () => {
    // The dict branch used to read only Boolean(data.truncated), missing a
    // sentinel row carried inside `tables` when the explicit flag was absent.
    const payload = normalizeDiscoverTablesResponse({
      tables: [
        { schema: "sales", table: "invoices", type: "BASE TABLE" },
        { schema: "__tessallite__", table: "__truncated__", type: "NOTICE" },
      ],
    } as never);
    expect(payload.truncated).toBe(true);
    expect(selectableDiscoveredTables(payload.tables)).toHaveLength(1);
  });
});

describe("F-014-06 classify-add nullability", () => {
  it("keeps profiled is_nullable instead of hardcoding true", () => {
    const payload = classifiedColumnsToSyncPayload([
      { column_name: "id", data_type: "bigint", is_nullable: false, is_primary_key: true },
      { column_name: "note", data_type: "text", is_nullable: true },
    ]);
    expect(payload[0].is_nullable).toBe(false);
    expect(payload[1].is_nullable).toBe(true);
    expect(payload.every((c) => c.is_nullable === true)).toBe(false);
  });
});
