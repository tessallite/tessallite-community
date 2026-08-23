/**
 * Bug-7736: verify that every singleton dependency shared between
 * excel-plugin and @tessallite/shared-ui is listed in the Vite build
 * config's resolve.dedupe array, preventing duplicate bundles that break
 * shared state and the react-query cache.
 *
 * The vitest config already had the complete list; this test ensures the
 * production vite.config.ts stays in sync.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const pluginRoot = resolve(__dirname, "../..");
const viteConfigPath = resolve(pluginRoot, "vite.config.ts");
const vitestConfigPath = resolve(pluginRoot, "vitest.config.ts");

/** Extract the dedupe array entries from a Vite/Vitest config source. */
function extractDedupeEntries(source: string): string[] {
  // Match the dedupe array block (possibly multi-line).
  const match = source.match(/dedupe:\s*\[([^\]]*)\]/s);
  if (!match) return [];
  // Strip full-line and inline comments before splitting on commas.
  const cleaned = match[1].replace(/\/\/[^\n]*/g, "");
  return cleaned
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
    .map((s) => s.replace(/^['"]|['"]$/g, ""));
}

describe("Bug-7736 — Vite build dedupe covers all shared singletons", () => {
  const viteSrc = readFileSync(viteConfigPath, "utf-8");
  const vitestSrc = readFileSync(vitestConfigPath, "utf-8");
  const viteEntries = extractDedupeEntries(viteSrc);
  const vitestEntries = extractDedupeEntries(vitestSrc);

  it("vite.config.ts has a dedupe array", () => {
    expect(viteEntries.length).toBeGreaterThan(0);
  });

  it("every vitest.config.ts dedupe entry is also in vite.config.ts", () => {
    const missing = vitestEntries.filter((e) => !viteEntries.includes(e));
    expect(missing).toEqual([]);
  });

  it("every vite.config.ts dedupe entry is also in vitest.config.ts", () => {
    const missing = viteEntries.filter((e) => !vitestEntries.includes(e));
    expect(missing).toEqual([]);
  });

  const REQUIRED_SINGLETONS = [
    "react",
    "react-dom",
    "@tanstack/react-query",
    "zustand",
    "echarts",
    "@mui/material",
    "@mui/icons-material",
    "dompurify",
    "react-markdown",
    "remark-gfm",
  ];

  it.each(REQUIRED_SINGLETONS)(
    "vite.config.ts dedupe includes %s",
    (pkg) => {
      expect(viteEntries).toContain(pkg);
    },
  );
});
