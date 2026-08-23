/**
 * Every INDIVIDUAL call site of a row-returning query-router client must
 * classify a row-security denial.
 *
 * R6 finding 1. The sibling guard (`rowSecurity.consumers.test.ts`) is
 * FILE-scoped: it asks "does this file mention the contract anywhere?". That
 * is structurally incapable of seeing a file with TWO call sites where only
 * one is wired — which is exactly what shipped. `MeasureQueryPanel/index.tsx`
 * classified its `/execute` call and its subtotals grain, so the file passed,
 * while `loadDrillPage` — the PRIMARY drill surface, reached by every
 * pivot-cell click — went unwired and rendered an RLS deny-all as "0 rows".
 *
 * This guard counts call sites, not files: every `.execute(` /
 * `.drillThrough(` / `.discoverMembers(` invocation must have a
 * `rowSecurityDeniedAll` check within a short window after it.
 *
 * Its own limits, stated honestly (CLAUDE.md coverage-tool rule):
 * - It is proximity-based. A call site that classifies far away (in a
 *   dedicated handler, say) would be a false positive; add it to
 *   ACKNOWLEDGED_SITES with the reason, rather than widening the window until
 *   the guard stops discriminating.
 * - It proves a check is PRESENT, not that the branch is correct. The
 *   per-surface behaviour tests own that.
 * - Discovery is regex-based on the typed client. `test_discovery_is_not_
 *   vacuous` pins the known count so a rename cannot silently empty the scan.
 */
import fs from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const SRC = path.resolve(__dirname, "..");

/** A call to a row-returning query-router client method. */
const CALL_SITE =
  /queryRouterApiClient[\s\S]{0,120}?\.(execute|drillThrough|discoverMembers)\s*\(/g;

/** How far after a call site we look for the classification. */
const WINDOW = 1400;

const CLASSIFIER = /rowSecurityDeniedAll|rowSecurityNarrowed|securityRulesApplied/;

/** "file:method" -> why this specific site does not classify inline. */
const ACKNOWLEDGED_SITES: Record<string, string> = {
  "components/Panels/MeasureQueryPanel/index.tsx:execute":
    "classified by DERIVATION, not inline: `rowSecurityDenied` is computed " +
    "from the stored `executeResult` at the top of the component (R6 finding " +
    "3 -- a separate useState desynced from the persisted result on remount " +
    "and resurfaced the fabricated zero). Deriving it is the stronger design, " +
    "so the check legitimately does not sit next to the call.",
};

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

interface Site {
  file: string;
  method: string;
  key: string;
  classified: boolean;
}

function callSites(): Site[] {
  const sites: Site[] = [];
  for (const abs of walk(SRC)) {
    const rel = path.relative(SRC, abs).split(path.sep).join("/");
    // The transport module DEFINES these methods; it does not call them.
    if (rel === "api/client.ts") continue;
    const text = fs.readFileSync(abs, "utf8");
    CALL_SITE.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = CALL_SITE.exec(text)) !== null) {
      const after = text.slice(m.index, m.index + WINDOW);
      sites.push({
        file: rel,
        method: m[1],
        key: `${rel}:${m[1]}`,
        classified: CLASSIFIER.test(after),
      });
    }
  }
  return sites;
}

describe("row-security classification, per CALL SITE", () => {
  it("discovers the known call sites, so the verdict is never vacuous", () => {
    const sites = callSites();
    expect(sites.length).toBeGreaterThanOrEqual(6);
    const files = new Set(sites.map((s) => s.file));
    for (const expected of [
      "components/Panels/QueryPanel.tsx",
      "components/Panels/MeasureQueryPanel/index.tsx",
      "components/Panels/MeasureQueryPanel/controls/SlicerBar.tsx",
      "components/Panels/MeasureQueryPanel/drawer/DrillMiniPanel.tsx",
      "components/Panels/NamedSetsPanel.tsx",
      "components/KpiBusinessBuilder/KpiFilterBar.tsx",
    ]) {
      expect(files).toContain(expected);
    }
  });

  it("finds the drill call site inside MeasureQueryPanel specifically", () => {
    // The exact site R6 finding 1 was about: a file-scoped guard could not see
    // it because its neighbours in the same file were already wired.
    const drill = callSites().filter(
      (s) =>
        s.file === "components/Panels/MeasureQueryPanel/index.tsx" &&
        s.method === "drillThrough",
    );
    expect(drill.length).toBeGreaterThan(0);
  });

  it("every call site classifies a row-security denial", () => {
    const unclassified = callSites()
      .filter((s) => !s.classified && !(s.key in ACKNOWLEDGED_SITES))
      .map((s) => s.key);
    expect(unclassified).toEqual([]);
  });

  it("an acknowledged key silences exactly one unclassified site", () => {
    // ACKNOWLEDGED_SITES is keyed file:method, but a file may hold several
    // sites of the same method: index.tsx has two `.execute(` calls -- the
    // acknowledged main run, and the subtotals grain whose inline check stops
    // a fabricated zero being published as a grand total. Removing the grain
    // check left this guard green, because one acknowledgement covered both.
    const sites = callSites();
    for (const key of Object.keys(ACKNOWLEDGED_SITES)) {
      const unclassified = sites.filter((s) => s.key === key && !s.classified);
      expect(
        unclassified.length,
        `${key}: acknowledgement is silencing ${unclassified.length} sites`,
      ).toBe(1);
    }
  });

  it("acknowledged sites do not go stale", () => {
    const live = new Set(callSites().map((s) => s.key));
    expect(
      Object.keys(ACKNOWLEDGED_SITES).filter((k) => !live.has(k)),
    ).toEqual([]);
  });
});
