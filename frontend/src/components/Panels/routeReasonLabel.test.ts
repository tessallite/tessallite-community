import { describe, it, expect } from "vitest";

/**
 * Bug-6971: the routeReasonLabel function (inline in QueryPanel.tsx and
 * MeasureQueryPanel/index.tsx) was discarding the router's detailed reason
 * string for known route types. After the fix, it appends the detailed
 * reason on a new line.
 *
 * This test duplicates the pure logic to verify the contract, since the
 * functions are not exported from the component modules.
 */

function routeReasonLabel(
  routeType: string,
  reason: string | undefined,
  t: (key: string) => string,
): string {
  const LOCALIZED: Record<string, string> = {
    source: "query.routeReasonSource",
    aggregate: "query.routeReasonAggregate",
    pocket: "query.routeReasonPocket",
  };
  const i18nKey = LOCALIZED[routeType];
  if (i18nKey) {
    const summary = t(i18nKey);
    return reason ? `${summary}\n${reason}` : summary;
  }
  return reason || `${t("query.routeType")}: ${routeType}`;
}

const t = (key: string): string => {
  const map: Record<string, string> = {
    "query.routeReasonSource": "Executed live against the source database.",
    "query.routeReasonAggregate": "Answered from a pre-built aggregate table for faster results.",
    "query.routeReasonPocket": "Answered from a cached pocket table for faster results.",
    "query.routeType": "Route type",
  };
  return map[key] ?? key;
};

describe("routeReasonLabel (Bug-6971)", () => {
  it("returns localized summary for source when no reason", () => {
    expect(routeReasonLabel("source", undefined, t)).toBe(
      "Executed live against the source database.",
    );
  });

  it("appends detailed reason for source route", () => {
    const result = routeReasonLabel("source", "No matching aggregate for grain [month]", t);
    expect(result).toBe(
      "Executed live against the source database.\nNo matching aggregate for grain [month]",
    );
  });

  it("appends detailed reason for aggregate route", () => {
    const result = routeReasonLabel("aggregate", "Matched agg_sales_monthly (grain: [month, product])", t);
    expect(result).toBe(
      "Answered from a pre-built aggregate table for faster results.\nMatched agg_sales_monthly (grain: [month, product])",
    );
  });

  it("appends detailed reason for pocket route", () => {
    const result = routeReasonLabel("pocket", "Pocket table pk_top_products is fresh", t);
    expect(result).toBe(
      "Answered from a cached pocket table for faster results.\nPocket table pk_top_products is fresh",
    );
  });

  it("returns only the localized summary for aggregate when no reason", () => {
    expect(routeReasonLabel("aggregate", undefined, t)).toBe(
      "Answered from a pre-built aggregate table for faster results.",
    );
  });

  it("falls back to raw reason for unknown route type", () => {
    expect(routeReasonLabel("custom", "some reason", t)).toBe("some reason");
  });

  it("falls back to route-type label for unknown route type with no reason", () => {
    expect(routeReasonLabel("custom", undefined, t)).toBe("Route type: custom");
  });
});
