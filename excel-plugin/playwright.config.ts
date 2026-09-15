/**
 * Playwright pane harness (harness (b), browser tier).
 *
 * Drives the REAL add-in — the built test-profile bundle, the real React tree,
 * the real MUI controls — in headless Chromium, with the recording Office.js
 * shim injected before the app loads, against a REAL Tessallite server. The
 * assertions are about the WORKBOOK the clicks produced, and about the JS TYPE
 * of every value written into it (the Bug-9876 class).
 *
 * Serial and single-worker on purpose: every spec drives one signed-in pane
 * against one shared model on a live stack, so parallel workers would race each
 * other's server state for no gain on a suite this size.
 *
 * `npx vitest run` never sees these; they live outside `src/`.
 */
import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  testDir: resolve(__dirname, 'tests-harness/pane-e2e'),
  testMatch: /.*\.spec\.ts/,
  globalSetup: resolve(__dirname, 'tests-harness/pane-e2e/globalSetup.mjs'),
  fullyParallel: false,
  workers: 1,
  // A live stack is not a place to paper over a flake with a retry: a check
  // that only passes sometimes is telling the truth about the add-in.
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 30_000 },
  reporter: [['list']],
  use: {
    ...devices['Desktop Chrome'],
    // The task pane is a narrow, tall surface; render it at the size the
    // add-in actually gets so layout-dependent controls behave as they do in
    // Excel.
    viewport: { width: 420, height: 900 },
    actionTimeout: 30_000,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'pane', use: { ...devices['Desktop Chrome'], viewport: { width: 420, height: 900 } } }],
});
