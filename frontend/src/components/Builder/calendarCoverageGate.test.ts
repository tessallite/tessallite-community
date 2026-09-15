/**
 * Bug-9900 — the calendar coverage probe is offered only to modeller+.
 *
 * `calendarApi.coverage` hits model-service `check_calendar_coverage`, which
 * runs raw `SELECT MIN(col), MAX(col)` statements on the PHYSICAL calendar and
 * fact tables through the query-router `/introspect/batch` route with NO
 * persona, NO CLS and NO RLS. Both hops are now gated at modeller-or-above, so
 * a viewer who is shown the affordance gets a dead button and a 403.
 *
 * The privilege decision itself is covered by
 * `src/auth/explorerPrivileges.test.ts` (the persona truth table). This guard
 * covers the other half: that every file which CALLS the coverage endpoint
 * actually consults that decision.
 */
import fs from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const SRC = path.resolve(__dirname, "..", "..");

const COVERAGE_CALL = /calendarApi\.coverage\s*\(/g;
const GATE = /canPerform\(\s*["']calendar\.checkCoverage["']\s*\)/;

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "node_modules") continue;
      walk(full, out);
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      out.push(full);
    }
  }
  return out;
}

/** Files that call the coverage endpoint, excluding the typed client itself. */
function callerFiles(): { rel: string; text: string; sites: number }[] {
  const hits: { rel: string; text: string; sites: number }[] = [];
  for (const file of walk(SRC)) {
    const rel = path.relative(SRC, file).replace(/\\/g, "/");
    if (rel === "api/client.ts") continue;
    const text = fs.readFileSync(file, "utf8");
    const sites = (text.match(COVERAGE_CALL) ?? []).length;
    if (sites > 0) hits.push({ rel, text, sites });
  }
  return hits;
}

describe("Bug-9900 calendar coverage affordance gate", () => {
  it("discovery is not vacuous", () => {
    const files = callerFiles();
    expect(files.map((f) => f.rel).sort()).toEqual([
      "components/Builder/DimensionCalendarAssociation.tsx",
    ]);
    expect(files.reduce((n, f) => n + f.sites, 0)).toBeGreaterThanOrEqual(1);
  });

  it("every component that runs the probe consults canPerform('calendar.checkCoverage')", () => {
    for (const { rel, text } of callerFiles()) {
      expect(
        GATE.test(text),
        `${rel} must gate the coverage probe on canPerform("calendar.checkCoverage")`,
      ).toBe(true);
    }
  });
});
