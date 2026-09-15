/**
 * Bug-9896 — the raw source-table preview is offered only to modeller+.
 *
 * `modelTablesApi.preview` hits model-service `preview_table`, which runs a raw
 * `SELECT * FROM <physical table>` through the query-router `/introspect` route
 * with NO persona, NO CLS and NO RLS. Both are now gated at modeller-or-above,
 * so a viewer who is shown the affordance gets a dead button and a 403.
 *
 * The privilege decision itself is covered by
 * `src/auth/explorerPrivileges.test.ts` (the persona truth table). This guard
 * covers the other half: that every file which CALLS the preview endpoint
 * actually consults that decision. A file-scoped check is enough here because
 * the affordance appears once per file; `test_discovery_is_not_vacuous` pins
 * the call-site count so a rename cannot silently empty the scan.
 */
import fs from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const SRC = path.resolve(__dirname, "..", "..");

const PREVIEW_CALL = /modelTablesApi\.preview\s*\(/g;
const GATE = /canPerform\(\s*["']table\.previewData["']\s*\)/;

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

/** Files that call the preview endpoint, excluding the typed client itself. */
function callerFiles(): { rel: string; text: string; sites: number }[] {
  const hits: { rel: string; text: string; sites: number }[] = [];
  for (const file of walk(SRC)) {
    const rel = path.relative(SRC, file).replace(/\\/g, "/");
    if (rel === "api/client.ts") continue;
    const text = fs.readFileSync(file, "utf8");
    const sites = (text.match(PREVIEW_CALL) ?? []).length;
    if (sites > 0) hits.push({ rel, text, sites });
  }
  return hits;
}

describe("Bug-9896 table preview affordance gate", () => {
  it("discovery is not vacuous", () => {
    const files = callerFiles();
    // ERDTableNode (rows + row count) and DataPreviewPanel (dialog body).
    expect(files.map((f) => f.rel).sort()).toEqual([
      "components/Builder/DataPreviewPanel.tsx",
      "components/Builder/ERDTableNode.tsx",
    ]);
    expect(files.reduce((n, f) => n + f.sites, 0)).toBeGreaterThanOrEqual(3);
  });

  it("every component that opens the preview consults canPerform('table.previewData')", () => {
    // DataPreviewPanel is a controlled dialog: it renders only when its owner
    // (SourcesPanel) opens it, so the gate lives at the owner's affordance.
    const OWNED_BY = {
      "components/Builder/DataPreviewPanel.tsx": "components/Panels/SourcesPanel.tsx",
    } as const;

    for (const { rel, text } of callerFiles()) {
      const owner = (OWNED_BY as Record<string, string | undefined>)[rel];
      const gateText = owner
        ? fs.readFileSync(path.join(SRC, owner), "utf8")
        : text;
      expect(
        GATE.test(gateText),
        `${owner ?? rel} must gate the table preview on canPerform("table.previewData")`,
      ).toBe(true);
    }
  });
});
