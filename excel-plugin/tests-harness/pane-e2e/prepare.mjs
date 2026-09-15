#!/usr/bin/env node
/**
 * Build what the Playwright pane harness loads.
 *
 * Two artefacts, both written to git-ignored directories:
 *
 *  1. `dist-test-profile/` — the REAL add-in, built with
 *     `VITE_TESSALLITE_TEST_PROFILE=1`, so the pane signs itself in and the
 *     harness never has to drive a login form. Built here rather than in the
 *     Playwright config because the server origin is BAKED INTO the bundle: the
 *     build and the shim the tests start have to agree on a URL, and a fixed
 *     port is the simplest way for two processes to do that.
 *  2. `tests-harness/.playwright/browserShim.js` — the recording Office.js shim
 *     as a classic script, injected before the app loads.
 *
 * Run through `npm run harness:pane:playwright`, which runs this first.
 */
import { build } from 'vite';
import { spawnSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { serverUrl, TEST_PROFILE_OUT_DIR, stackConfigured } from './harnessEnv.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const PLUGIN_DIR = resolve(HERE, '../..');

if (!stackConfigured()) {
  console.error(
    'The Playwright pane harness needs a Tessallite server.\n'
    + 'Set TESS_HARNESS_TENANT, TESS_HARNESS_EMAIL, TESS_HARNESS_PASSWORD and either\n'
    + 'TESS_HARNESS_SERVER_URL or TESS_HARNESS_QUERY_ROUTER_URL. See tests-harness/README.md.',
  );
  process.exit(1);
}

// 1. The shim, as a classic script for page.addInitScript.
await build({
  root: PLUGIN_DIR,
  configFile: false,
  logLevel: 'warn',
  build: {
    outDir: resolve(PLUGIN_DIR, 'tests-harness/.playwright'),
    emptyOutDir: true,
    lib: {
      entry: resolve(PLUGIN_DIR, 'tests-harness/lib/browserShim.ts'),
      name: 'TessalliteBrowserShim',
      formats: ['iife'],
      fileName: () => 'browserShim.js',
    },
  },
});
console.log('built tests-harness/.playwright/browserShim.js');

// 2. The pane itself, as a TEST PROFILE build. Spawned rather than called
//    in-process because `vite.config.ts` reads the guard and the profile from
//    `process.env` at module-evaluation time.
const result = spawnSync(
  process.execPath,
  [resolve(PLUGIN_DIR, 'node_modules/vite/bin/vite.js'), 'build'],
  {
    cwd: PLUGIN_DIR,
    stdio: 'inherit',
    env: {
      ...process.env,
      VITE_TESSALLITE_TEST_PROFILE: '1',
      VITE_OUT_DIR: TEST_PROFILE_OUT_DIR,
      // Served from the harness's own static server at the root, not from a
      // deployment's /excel-plugin/ path.
      VITE_BASE_PATH: '/',
      TESS_HARNESS_SERVER_URL: serverUrl(),
      // The guard must see a local base URL; this build is never a release.
      PLUGIN_BASE_URL: `http://127.0.0.1`,
    },
  },
);
if (result.status !== 0) {
  console.error('test-profile build failed');
  process.exit(result.status ?? 1);
}
console.log(`built ${TEST_PROFILE_OUT_DIR}/ (test profile, origin ${serverUrl()})`);
