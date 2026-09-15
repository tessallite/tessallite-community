/**
 * Load the BUILT `functions.iife.js` into this Node realm.
 *
 * Loading the build output (not the TypeScript source) is deliberate: the
 * bundle is the artefact Excel actually executes, so a build-only regression —
 * a dropped module, a broken minifier assumption, a `typeof CustomFunctions`
 * guard that no longer fires — is caught here and cannot be caught by a Vitest
 * unit test that imports the source.
 *
 * `runInThisContext` shares this realm's globals, so the bundle sees the
 * installed `CustomFunctions` / `OfficeRuntime` stubs and the real `fetch`.
 */
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

export function loadBundle(bundlePath) {
  const code = readFileSync(bundlePath, 'utf8');
  // The IIFE assigns `var TessalliteFunctions = ...`; the trailing expression
  // hands the module namespace (its exports) back to the caller.
  return vm.runInThisContext(`${code}\n;TessalliteFunctions`, { filename: bundlePath });
}
