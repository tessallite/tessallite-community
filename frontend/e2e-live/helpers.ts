/**
 * Shared helpers for Tessallite e2e-live Playwright tests.
 *
 * Provides API helpers, run-scoped naming, and cleanup utilities.
 * All interactions go through real user boundaries -- browser + public API.
 *
 * Auth strategy: global-setup logs in ONCE and saves storageState. Most
 * specs load that state automatically (no per-spec login). Only specs
 * needing a different user (permission-gating) use browserLoginAs.
 */
import { type Page, expect } from "@playwright/test";
import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// ---------------------------------------------------------------------------
// Profile configuration
// ---------------------------------------------------------------------------

export interface LiveProfile {
  tenantSlug: string;
  projectSlug: string;
  modelSlug: string;
  frontendUrl: string;
  modelServiceUrl: string;
  tenantEmail: string;
  tenantPassword: string;
  runId: string;
}

export function loadProfile(): LiveProfile {
  return {
    tenantSlug: process.env.LIVE_TENANT_SLUG || "acme-demo",
    projectSlug: process.env.LIVE_PROJECT_SLUG || "project1",
    modelSlug: process.env.LIVE_MODEL_SLUG || "modely",
    frontendUrl: (process.env.LIVE_FRONTEND_URL || "http://localhost:3000").replace(/\/$/, ""),
    modelServiceUrl: (process.env.LIVE_MODEL_SERVICE_URL || "http://localhost:8001").replace(/\/$/, ""),
    tenantEmail: process.env.LIVE_TENANT_EMAIL || "admin@acme-demo.com",
    tenantPassword: process.env.LIVE_TENANT_PASSWORD || "acme-demo",
    runId: `liverun-${crypto.randomBytes(4).toString("hex")}`,
  };
}

/**
 * Returns true if the active profile targets modely.
 * Use with test.skip() at the test level to cleanly skip on non-modely profiles.
 */
export function isModelyProfile(profile: LiveProfile): boolean {
  return profile.modelSlug === "modely";
}

// ---------------------------------------------------------------------------
// API helpers (HTTP fetch against model-service)
// ---------------------------------------------------------------------------

/**
 * Load the JWT token saved by global-setup. Falls back to apiLogin if
 * the file is missing or stale.
 */
export function loadSharedToken(): string | null {
  try {
    const tokenPath = path.join(__dirname, "test-results", ".api-token.json");
    const data = JSON.parse(fs.readFileSync(tokenPath, "utf-8"));
    return data.access_token || null;
  } catch {
    return null;
  }
}

/**
 * Get a JWT token, preferring the shared token from global-setup.
 * Falls back to a fresh apiLogin if the shared token is unavailable.
 */
export async function getToken(profile: LiveProfile): Promise<string> {
  const shared = loadSharedToken();
  if (shared) return shared;
  return apiLogin(profile);
}

export async function apiLogin(profile: LiveProfile): Promise<string> {
  const resp = await fetch(`${profile.modelServiceUrl}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      tenant_id: profile.tenantSlug,
      email: profile.tenantEmail,
      password: profile.tenantPassword,
    }),
  });
  if (!resp.ok) {
    throw new Error(`API login failed: ${resp.status} ${await resp.text()}`);
  }
  const data = await resp.json();
  return data.access_token;
}

export async function apiGet(
  url: string,
  token: string,
): Promise<{ status: number; body: any }> {
  const resp = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const body = await resp.json().catch(() => null);
  return { status: resp.status, body };
}

export async function apiPost(
  url: string,
  token: string,
  data: any,
): Promise<{ status: number; body: any }> {
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(data),
  });
  const body = await resp.json().catch(() => null);
  return { status: resp.status, body };
}

export async function apiPatch(
  url: string,
  token: string,
  data: any,
): Promise<{ status: number; body: any }> {
  const resp = await fetch(url, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(data),
  });
  const body = await resp.json().catch(() => null);
  return { status: resp.status, body };
}

export async function apiDelete(
  url: string,
  token: string,
): Promise<{ status: number; body: any }> {
  const resp = await fetch(url, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${token}` },
  });
  const body = await resp.json().catch(() => null);
  return { status: resp.status, body };
}

// ---------------------------------------------------------------------------
// Dynamic UUID resolution
// ---------------------------------------------------------------------------

export async function resolveProjectId(
  profile: LiveProfile,
  token: string,
): Promise<string> {
  const { body } = await apiGet(
    `${profile.modelServiceUrl}/api/v1/projects`,
    token,
  );
  const match = (body as any[]).find(
    (p: any) => p.slug === profile.projectSlug,
  );
  if (!match) {
    throw new Error(
      `Project '${profile.projectSlug}' not found in tenant '${profile.tenantSlug}'`,
    );
  }
  return match.id;
}

export async function resolveModelId(
  profile: LiveProfile,
  token: string,
  projectId: string,
): Promise<string> {
  const { body } = await apiGet(
    `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models`,
    token,
  );
  const match = (body as any[]).find(
    (m: any) => m.slug === profile.modelSlug,
  );
  if (!match) {
    throw new Error(
      `Model '${profile.modelSlug}' not found in project '${profile.projectSlug}'`,
    );
  }
  return match.id;
}

// ---------------------------------------------------------------------------
// Browser login helper (for specs that need a DIFFERENT user)
// ---------------------------------------------------------------------------

/**
 * Login with specific credentials via the browser form.
 * Used only by specs that need a non-admin user (e.g. permission-gating).
 * Most specs skip this entirely and use storageState from global-setup.
 */
export async function browserLoginAs(
  page: Page,
  profile: LiveProfile,
  email: string,
  password: string,
): Promise<void> {
  await page.goto(`${profile.frontendUrl}/login`, {
    waitUntil: "domcontentloaded",
  });
  await page.waitForLoadState("networkidle");

  // If not on login (already authenticated), clear state first
  if (!page.url().includes("/login")) {
    // Clear localStorage and cookies to force re-login
    await page.evaluate(() => localStorage.clear());
    await page.context().clearCookies();
    await page.goto(`${profile.frontendUrl}/login`, {
      waitUntil: "domcontentloaded",
    });
    await page.waitForLoadState("networkidle");
  }

  const tenantField = page.getByLabel(/Workspace \(tenant slug\)/i);
  await tenantField.waitFor({ state: "visible", timeout: 15_000 });
  await tenantField.fill(profile.tenantSlug);
  await page.getByLabel(/Email/i).fill(email);
  await page.getByLabel(/Password/i).fill(password);
  await page.getByRole("button", { name: /^Sign In$/i }).click();

  await page.waitForURL((url) => !url.toString().includes("/login"), {
    timeout: 15_000,
  });

  // Complete onboarding via the browser's cookie context
  try {
    await page.request.post(
      `${profile.frontendUrl}/api/v1/auth/users/me/complete-onboarding`,
    );
  } catch {
    // Non-fatal
  }

  await page.waitForLoadState("networkidle");

  // Handle /welcome redirect
  if (page.url().includes("/welcome")) {
    const finishBtn = page.getByRole("button", {
      name: /get started|finish|skip|done|continue|close/i,
    });
    if (await finishBtn.count() > 0) {
      await finishBtn.first().click();
      await page.waitForLoadState("networkidle");
    }
    if (page.url().includes("/welcome")) {
      await page.goto(`${profile.frontendUrl}/`, {
        waitUntil: "domcontentloaded",
      });
      await page.waitForLoadState("networkidle");
    }
  }
}

// ---------------------------------------------------------------------------
// Auth-aware navigation (for specs using storageState)
// ---------------------------------------------------------------------------

/**
 * Navigate to a URL, and if the SPA redirects to /login (cookie expired or
 * not applied), re-authenticate via the demo button. Handles the rare case
 * where storageState cookies are not applied to the new context.
 */
export async function navigateAuthenticated(
  page: Page,
  profile: LiveProfile,
  url: string,
): Promise<void> {
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle");

  // If we landed on the login page, re-authenticate
  if (page.url().includes("/login")) {
    const demoBtn = page.getByRole("button", { name: /Sign in to demo/i });
    if (
      profile.tenantSlug === "acme-demo" &&
      (await demoBtn.count()) > 0 &&
      (await demoBtn.first().isVisible())
    ) {
      await demoBtn.first().click();
    } else {
      const tenantField = page.getByLabel(/Workspace \(tenant slug\)/i);
      await tenantField.waitFor({ state: "visible", timeout: 10_000 });
      await tenantField.fill(profile.tenantSlug);
      await page.getByLabel(/Email/i).fill(profile.tenantEmail);
      await page.getByLabel(/Password/i).fill(profile.tenantPassword);
      await page.getByRole("button", { name: /^Sign In$/i }).click();
    }
    await page.waitForURL(
      (u) => !u.toString().includes("/login"),
      { timeout: 15_000 },
    );
    await page.waitForLoadState("networkidle");

    // Complete onboarding
    try {
      await page.request.post(
        `${profile.frontendUrl}/api/v1/auth/users/me/complete-onboarding`,
      );
    } catch {
      // Non-fatal
    }

    // If the target was not the root, navigate there now
    if (url !== `${profile.frontendUrl}/` && url !== profile.frontendUrl) {
      await page.goto(url, { waitUntil: "domcontentloaded" });
      await page.waitForLoadState("networkidle");
    }
  }
}

// ---------------------------------------------------------------------------
// Run-scoped naming
// ---------------------------------------------------------------------------

export function runScopedName(profile: LiveProfile, baseName: string): string {
  return `${profile.runId}__${baseName}`;
}

// ---------------------------------------------------------------------------
// Wait helper
// ---------------------------------------------------------------------------

export async function waitForNetworkIdle(page: Page, timeout = 5000): Promise<void> {
  try {
    await page.waitForLoadState("networkidle", { timeout });
  } catch {
    // networkidle may not settle on long-polling; non-fatal
  }
}
