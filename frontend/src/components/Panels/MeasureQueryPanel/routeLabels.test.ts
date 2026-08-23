import { describe, it, expect } from "vitest";
import { routeBadgeLabel } from "./routeLabels";

// Bug-6282: the drill panel and the pivot route badge must translate the raw
// router ``route_type`` the same way. This pins the shared helper both use.
const t = (key: string): string => {
  const map: Record<string, string> = {
    "pivot.liveSource": "live (source)",
    "pivot.routeAggregate": "Aggregate",
    "pivot.routePocket": "Pocket",
  };
  return map[key] ?? key;
};

describe("routeBadgeLabel", () => {
  it("translates the three known route types", () => {
    expect(routeBadgeLabel("source", t)).toBe("live (source)");
    expect(routeBadgeLabel("aggregate", t)).toBe("Aggregate");
    expect(routeBadgeLabel("pocket", t)).toBe("Pocket");
  });

  it("passes an unknown route type through unchanged", () => {
    expect(routeBadgeLabel("mystery", t)).toBe("mystery");
  });
});
