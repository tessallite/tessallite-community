/**
 * Every queryRouterApiClient.execute() consumer must classify a row-security
 * denial.
 *
 * R3 review finding B-2. The Python guard
 * (tessallite/tests/unit/test_execute_consumer_enumeration.py) discovers clients
 * by the literal string "/api/v1/execute" and is therefore structurally blind to
 * the frontend, where every call goes through a typed API client. Its
 * ACKNOWLEDGED_GAPS entry for client.ts asserted "classification in
 * rowSecurity.ts, consumed by QueryPanel.tsx" — a statement that was factually
 * wrong (there were FIVE consumers, only one wired) and that was the thing
 * granting the pass. A coverage mechanism whose own acknowledgement is false is
 * worse than no mechanism, so the Python guard now defers to this one and this
 * one enumerates mechanically.
 *
 * Its own limits, stated honestly:
 * - SCOPE. It enumerates `.execute()` callers only. Member-discovery and
 *   drill-through also return router-produced, RLS-filtered rows, and were
 *   invisible to BOTH this guard and its Python sibling until R4 finding 3 --
 *   a route-shaped guard cannot see a behaviour-shaped property. Those callers
 *   are wired, but adding a new row-returning client does not automatically
 *   bring it into scope; extend CALL when one appears.
 * - it proves a consumer REFERENCES the contract, not that it branches
 *   correctly; the per-surface behaviour tests own that;
 * - discovery is regex-based on `queryRouterApiClient….execute(`, so a consumer
 *   that aliases the client would be missed. The first test below pins the known
 *   consumer list so the verdict can never become vacuous.
 */
import fs from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const SRC = path.resolve(__dirname, "..");
// R5 finding F2: `.execute()` was never the whole set. /discover/members and
// /drill-through also return router-produced, RLS-filtered rows; the drill
// denial field shipped in R4 with NO consumer on any client precisely because
// this regex could not see those callers.
const CALL =
  /queryRouterApiClient[\s\S]{0,120}?\.(execute|drillThrough|discoverMembers)\s*\(/;
const CONTRACT = /rowSecurityDeniedAll|rowSecurityNarrowed|securityRulesApplied/;

// path (relative to src/) -> why classification does not happen HERE.
// Every entry must say where it happens instead. Empty today: api/client.ts is
// the transport and DEFINES `execute` rather than calling it, so it is not
// discovered as a caller at all (the separate anchor test below keeps that
// assumption honest).
const ACKNOWLEDGED: Record<string, string> = {};

function walk(dir: string, out: string[] = []): string[] {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (e.name !== "node_modules") walk(p, out);
    } else if (/\.tsx?$/.test(e.name) && !/\.test\.tsx?$/.test(e.name)) {
      out.push(p);
    }
  }
  return out;
}

const callers = walk(SRC)
  .map(
    (p) =>
      [
        path.relative(SRC, p).split(path.sep).join("/"),
        fs.readFileSync(p, "utf8"),
      ] as const,
  )
  .filter(([, text]) => CALL.test(text));

describe("execute consumer enumeration (frontend)", () => {
  it("discovers the known consumers, so the verdict is never vacuous", () => {
    const rels = callers.map(([r]) => r);
    for (const expected of [
      "components/Panels/QueryPanel.tsx",
      "components/Panels/MeasureQueryPanel/index.tsx",
      "components/Panels/MeasureQueryPanel/controls/SlicerBar.tsx",
      "components/KpiBusinessBuilder/KpiFilterBar.tsx",
      "components/Panels/MeasureQueryPanel/drawer/DrillMiniPanel.tsx",
      "components/Panels/NamedSetsPanel.tsx",
    ]) {
      expect(rels).toContain(expected);
    }
  });

  it("every consumer classifies a row-security denial", () => {
    const unclassified = callers
      .filter(([rel, text]) => !CONTRACT.test(text) && !(rel in ACKNOWLEDGED))
      .map(([rel]) => rel);
    expect(unclassified).toEqual([]);
  });

  it("the transport still exposes the method this scanner keys on", () => {
    // If queryRouterApiClient.execute is ever renamed, the CALL regex above
    // would silently match nothing and every other assertion here would pass
    // vacuously. Pin the anchor itself.
    const client = fs.readFileSync(path.join(SRC, "api", "client.ts"), "utf8");
    expect(client).toMatch(/execute\s*:\s*\(/);
    expect(client).toContain("/api/v1/execute");
  });

  it("acknowledged entries do not go stale", () => {
    const live = new Set(callers.map(([r]) => r));
    expect(
      Object.keys(ACKNOWLEDGED).filter((k) => !live.has(k)),
    ).toEqual([]);
  });
});
