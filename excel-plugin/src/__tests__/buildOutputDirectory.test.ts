/**
 * Both Vite configs must send their output to the SAME directory.
 *
 * `npm run build` produces two bundles from two configs: the pane
 * (`vite.config.ts`) and the custom-functions IIFE (`vite.config.functions.ts`).
 * A test-profile build points `VITE_OUT_DIR` at its own directory so a bundle
 * carrying baked credentials can never land in the `dist/` a deploy picks up.
 *
 * Until this was fixed the functions config hard-coded `outDir: 'dist'` and
 * ignored `VITE_OUT_DIR`, so a test-profile build wrote the pane to
 * `dist-test-profile/` and left `functions.iife.js` behind in `dist/`. The
 * consequence was silent and total: the served test origin had no functions
 * bundle, the manifest's `<Script>` URL 404'd, and every TESSALLITE function on
 * the host showed `#NAME?` -- the exact failure the native-Excel harness exists
 * to detect, produced by the harness's own build step.
 *
 * Checked against the config SOURCE rather than by importing it: a Vite config
 * pulls in esbuild, which cannot run inside this suite's jsdom environment.
 * The behaviour itself was verified by building with `VITE_OUT_DIR` set and
 * confirming `functions.iife.js` lands in that directory.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, it, expect } from 'vitest';

const PLUGIN_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '../..');

const CONFIGS = ['vite.config.ts', 'vite.config.functions.ts'];

/**
 * The one expression both configs must derive the directory from,
 * whitespace-insensitively. Either spelling counts — inline in the `build`
 * block, or a `const outDir` the block then uses (Bug-9879 introduced the
 * second: the namespace-stamping plugin needs the same value).
 */
const REQUIRED = /outDir[:=]process\.env\.VITE_OUT_DIR\|\|'dist'/;

describe('build output directory', () => {
  it.each(CONFIGS)('%s honours VITE_OUT_DIR and defaults to dist', (name) => {
    const source = readFileSync(resolve(PLUGIN_DIR, name), 'utf-8');
    const squeezed = source.replace(/\s+/g, '');

    expect(
      REQUIRED.test(squeezed),
      `${name} must set build.outDir from VITE_OUT_DIR (default 'dist'), or a `
      + 'test-profile build splits its two bundles across two directories',
    ).toBe(true);

    // ...and the `build` block must actually USE it, rather than deriving the
    // value and then writing somewhere else.
    expect(
      /build:\{[^}]*outDir[,:]/.test(squeezed),
      `${name} must pass that directory to build.outDir`,
    ).toBe(true);
  });

  it('no config hard-codes an output directory', () => {
    for (const name of CONFIGS) {
      const squeezed = readFileSync(resolve(PLUGIN_DIR, name), 'utf-8').replace(/\s+/g, '');
      expect(squeezed.includes("outDir:'dist'"), name).toBe(false);
      expect(squeezed.includes('outDir:"dist"'), name).toBe(false);
    }
  });
});
