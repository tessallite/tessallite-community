import { describe, it, expect } from "vitest";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

// Bug-9573/Bug-9574 (R3-02): Drawer.helpLinksExist.test.ts only enumerates
// Drawer.tsx's own PANEL_HELP_LINKS map, so it was structurally blind to
// EndpointsPanel.tsx's separate hardcoded "/help/excel-plugin.html" href (a
// 404 — the file never existed). This guard scans every "/help/...html"
// string literal anywhere in src/ and resolves it against the filesystem, so
// no future stale href in ANY component can hide behind a narrower guard.
const here = dirname(fileURLToPath(import.meta.url));
const srcRoot = join(here, "..");
const helpRoot = join(srcRoot, "..", "..", "help");

const HREF_RE = /"(\/help\/[a-zA-Z0-9/_-]+\.html)"/g;

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const st = statSync(full);
    if (st.isDirectory()) {
      if (name === "node_modules" || name === "dist") continue;
      out.push(...walk(full));
    } else if (/\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name)) {
      out.push(full);
    }
  }
  return out;
}

function collectHelpHrefs(): Map<string, string[]> {
  const hrefToFiles = new Map<string, string[]>();
  for (const file of walk(srcRoot)) {
    const text = readFileSync(file, "utf8");
    let m: RegExpExecArray | null;
    while ((m = HREF_RE.exec(text)) !== null) {
      const href = m[1];
      const arr = hrefToFiles.get(href) ?? [];
      arr.push(file.replace(srcRoot, "src"));
      hrefToFiles.set(href, arr);
    }
  }
  return hrefToFiles;
}

describe("help link filesystem guard (Bug-9573/Bug-9574)", () => {
  it("resolves every /help/*.html string literal in src/ to a file that actually exists", () => {
    const hrefs = collectHelpHrefs();
    expect(hrefs.size).toBeGreaterThan(0);

    const missing: string[] = [];
    for (const [href, files] of hrefs) {
      const relative = href.replace(/^\/help\//, "");
      const full = join(helpRoot, relative);
      if (!existsSync(full)) {
        missing.push(`${href}  (used in ${files.join(", ")})`);
      }
    }
    expect(
      missing,
      `Help links pointing at nonexistent files:\n${missing.join("\n")}`,
    ).toEqual([]);
  });
});
