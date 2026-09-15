import { describe, it, expect } from "vitest";
import { existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { PANEL_HELP_LINKS } from "./Drawer";

// Bug-9091 code review: PANEL_HELP_LINKS carried
// "named-sets": "/help/modelling/named-sets.html", a file that never
// existed — the drawer's help icon 404'd for every named-sets user. Nothing
// caught it because no test resolved these hrefs against the filesystem.
const here = dirname(fileURLToPath(import.meta.url));
const helpRoot = join(here, "..", "..", "..", "..", "help");

describe("Drawer PANEL_HELP_LINKS (Bug-9091)", () => {
  it("resolves every panel help href to a file that actually exists", () => {
    const missing: string[] = [];
    for (const [panel, href] of Object.entries(PANEL_HELP_LINKS)) {
      const relative = href.replace(/^\/help\//, "");
      const full = join(helpRoot, relative);
      if (!existsSync(full)) missing.push(`${panel}: ${href}`);
    }
    expect(missing, `Panel help links pointing at nonexistent files:\n${missing.join("\n")}`).toEqual([]);
  });
});
