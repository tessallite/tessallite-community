/**
 * Playwright configuration for the Tessallite deployed-session browser tests.
 *
 * These tests run against a live deployed frontend at http://localhost:3000.
 * They exercise real browser interactions, real auth, and real persistence.
 * No in-process TestClient, no mocked sessions.
 *
 * Auth strategy: a global-setup logs in ONCE and saves storageState. All
 * specs that use the admin session load that state, so they start already
 * authenticated (no per-spec login, no rate-limiter pressure). Specs that
 * need a different user (permission-gating) do their own login.
 */
import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "url";
import * as path from "path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BASE_URL = process.env.LIVE_FRONTEND_URL || "http://localhost:3000";
const STORAGE_STATE_PATH = path.join(__dirname, "test-results", ".auth-state.json");

export { STORAGE_STATE_PATH };

export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.ts",
  fullyParallel: false,
  retries: 2,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  globalSetup: path.resolve(__dirname, "global-setup.ts"),
  use: {
    baseURL: BASE_URL,
    headless: true,
    ignoreHTTPSErrors: true,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    viewport: { width: 1280, height: 720 },
    // All specs get the admin's authenticated state by default.
    // Specs that need a different user override this in their own config.
    storageState: STORAGE_STATE_PATH,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  reporter: [
    ["list"],
    ["json", { outputFile: "test-results/results.json" }],
  ],
  outputDir: "test-results",
});
