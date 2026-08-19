import { describe, expect, it } from "vitest";
import { hierarchyDisplayStatus } from "./hierarchyHealth";
import type { HierarchyHealthStatus } from "../../api/types";

function entry(
  status: HierarchyHealthStatus["status"],
  members_probed: boolean,
): HierarchyHealthStatus {
  return {
    hierarchy_id: "h1",
    hierarchy_name: "Geography",
    status,
    members_probed,
    issues: [],
  };
}

describe("hierarchyDisplayStatus", () => {
  it("maps a fully-probed clean hierarchy to ok", () => {
    expect(hierarchyDisplayStatus(entry("ok", true), "probed")).toBe("ok");
  });

  // F-016-08: the backend now reports unverified_members (not ok) when the
  // member probe did not run — the indicator must read partial, never healthy.
  it("maps unverified_members to partial", () => {
    expect(hierarchyDisplayStatus(entry("unverified_members", false), "not_requested")).toBe("partial");
  });

  it("never reads healthy when the probe was not requested, even if status is ok", () => {
    expect(hierarchyDisplayStatus(entry("ok", false), "not_requested")).toBe("partial");
  });

  it("reports error/warning regardless of probe state", () => {
    expect(hierarchyDisplayStatus(entry("error", false), "not_requested")).toBe("error");
    expect(hierarchyDisplayStatus(entry("warning", false), "probed")).toBe("warning");
  });

  it("a denied probe cannot read healthy", () => {
    expect(hierarchyDisplayStatus(entry("ok", true), "denied")).toBe("partial");
  });

  it("undefined health is pending", () => {
    expect(hierarchyDisplayStatus(undefined, "not_requested")).toBe("pending");
  });
});
