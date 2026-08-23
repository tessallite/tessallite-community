import { describe, it, expect } from "vitest";
import {
  namedQueryHealth,
  namedQueryHealthColor,
  type NamedQueryHealthSource,
} from "./namedQueryHealth";

const SOURCE = (artifact: NamedQueryHealthSource["artifact"]): NamedQueryHealthSource => ({
  artifact,
});

const ARTIFACT = (overrides: Record<string, unknown> = {}) => ({
  id: "a1",
  target_id: "t1",
  physical_table_name: "nq_table",
  target_schema: null,
  row_count: 12,
  status: "fresh",
  failure_reason: null,
  last_refresh_at: "2026-08-01T00:00:00Z",
  retired_at: null,
  ...overrides,
});

describe("namedQueryHealth", () => {
  it("never materialised -> stale with never-refreshed reason", () => {
    const health = namedQueryHealth(SOURCE(null));
    expect(health.status).toBe("stale");
    expect(health.reasonKey).toBe("namedQueries.healthNeverRefreshed");
    expect(health.detail).toBeNull();
  });

  it("fresh artifact -> fresh, no reason", () => {
    const health = namedQueryHealth(SOURCE(ARTIFACT({ status: "fresh" })));
    expect(health.status).toBe("fresh");
    expect(health.reasonKey).toBeNull();
    expect(health.detail).toBeNull();
  });

  it("failed artifact surfaces the server reason verbatim", () => {
    const health = namedQueryHealth(
      SOURCE(ARTIFACT({ status: "failed", failure_reason: "ROW_CAP_EXCEEDED" })),
    );
    expect(health.status).toBe("failed");
    expect(health.reasonKey).toBeNull();
    expect(health.detail).toBe("ROW_CAP_EXCEEDED");
  });

  it("failed artifact without a reason falls back to the generic key", () => {
    const health = namedQueryHealth(SOURCE(ARTIFACT({ status: "failed" })));
    expect(health.status).toBe("failed");
    expect(health.reasonKey).toBe("namedQueries.healthFailedGeneric");
  });

  it("in-build (invalidating) is surfaced as stale, never as fresh", () => {
    const health = namedQueryHealth(SOURCE(ARTIFACT({ status: "invalidating" })));
    expect(health.status).toBe("stale");
    expect(health.reasonKey).toBe("namedQueries.healthRefreshing");
  });

  it("stale with a failure reason shows the reason verbatim", () => {
    const health = namedQueryHealth(
      SOURCE(ARTIFACT({ status: "stale", failure_reason: "target moved" })),
    );
    expect(health.status).toBe("stale");
    expect(health.reasonKey).toBeNull();
    expect(health.detail).toBe("target moved");
  });

  it("stale without a reason falls back to the generic key", () => {
    const health = namedQueryHealth(SOURCE(ARTIFACT({ status: "stale" })));
    expect(health.status).toBe("stale");
    expect(health.reasonKey).toBe("namedQueries.healthStaleGeneric");
  });

  it("unknown status fails closed to stale", () => {
    const health = namedQueryHealth(SOURCE(ARTIFACT({ status: "retired" })));
    expect(health.status).toBe("stale");
  });

  it("chip colours follow the triad", () => {
    expect(namedQueryHealthColor("fresh")).toBe("success");
    expect(namedQueryHealthColor("stale")).toBe("warning");
    expect(namedQueryHealthColor("failed")).toBe("error");
  });
});
