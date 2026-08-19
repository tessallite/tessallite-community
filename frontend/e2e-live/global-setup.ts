/**
 * Playwright global setup: log in ONCE as the admin user, save storageState
 * (cookies + localStorage) so all specs start already-authenticated.
 *
 * This eliminates the per-spec login that was hammering the model-service
 * rate limiter (60 req/min).
 */
import { chromium, type FullConfig } from "@playwright/test";
import { fileURLToPath } from "url";
import * as path from "path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const STORAGE_STATE_PATH = path.join(__dirname, "test-results", ".auth-state.json");

export { STORAGE_STATE_PATH };

async function globalSetup(config: FullConfig) {
  const baseURL =
    process.env.LIVE_FRONTEND_URL || "http://localhost:3000";
  const modelServiceURL =
    process.env.LIVE_MODEL_SERVICE_URL || "http://localhost:8001";
  const tenantSlug = process.env.LIVE_TENANT_SLUG || "acme-demo";
  const tenantEmail = process.env.LIVE_TENANT_EMAIL || "admin@acme-demo.com";
  const tenantPassword = process.env.LIVE_TENANT_PASSWORD || "acme-demo";

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    ignoreHTTPSErrors: true,
  });
  const page = await context.newPage();

  // Navigate to the login page
  await page.goto(`${baseURL}/login`, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle");

  // If already authenticated (unlikely on first run), skip login
  if (!page.url().includes("/login")) {
    await context.storageState({ path: STORAGE_STATE_PATH });
    await browser.close();
    return;
  }

  // Strategy 1: Use the "Sign in to demo" button if available
  const demoBtn = page.getByRole("button", { name: /Sign in to demo/i });
  if (
    tenantSlug === "acme-demo" &&
    (await demoBtn.count()) > 0 &&
    (await demoBtn.first().isVisible())
  ) {
    await demoBtn.first().click();
  } else {
    // Strategy 2: Fill the login form
    const tenantField = page.getByLabel(/Workspace \(tenant slug\)/i);
    await tenantField.waitFor({ state: "visible", timeout: 15_000 });
    await tenantField.fill(tenantSlug);
    await page.getByLabel(/Email/i).fill(tenantEmail);
    await page.getByLabel(/Password/i).fill(tenantPassword);
    await page.getByRole("button", { name: /^Sign In$/i }).click();
  }

  // Wait for navigation away from /login
  await page.waitForURL((url) => !url.toString().includes("/login"), {
    timeout: 15_000,
  });
  await page.waitForLoadState("networkidle");

  // Complete onboarding via the browser's session cookie
  try {
    await page.request.post(
      `${baseURL}/api/v1/auth/users/me/complete-onboarding`,
    );
  } catch {
    // Non-fatal -- may already be complete
  }

  // If we're on /welcome, click through
  if (page.url().includes("/welcome")) {
    const finishBtn = page.getByRole("button", {
      name: /get started|finish|skip|done|continue|close/i,
    });
    if (await finishBtn.count() > 0) {
      await finishBtn.first().click();
      await page.waitForLoadState("networkidle");
    }
    if (page.url().includes("/welcome")) {
      await page.goto(`${baseURL}/`, { waitUntil: "domcontentloaded" });
      await page.waitForLoadState("networkidle");
    }
  }

  // Save the authenticated state (cookies + localStorage)
  await context.storageState({ path: STORAGE_STATE_PATH });

  // Also obtain a JWT token via API and save it for specs that need
  // Node-side API calls (measure CRUD, deploy). This single login
  // avoids each spec needing its own apiLogin call.
  try {
    const loginResp = await page.request.post(
      `${baseURL}/api/v1/auth/login`,
      {
        data: {
          tenant_id: tenantSlug,
          email: tenantEmail,
          password: tenantPassword,
        },
      },
    );
    if (loginResp.ok()) {
      const loginData = await loginResp.json();
      const tokenPath = path.join(__dirname, "test-results", ".api-token.json");
      const fs = await import("fs");
      fs.writeFileSync(
        tokenPath,
        JSON.stringify({
          access_token: loginData.access_token,
          tenant_slug: tenantSlug,
          obtained_at: new Date().toISOString(),
        }),
        "utf-8",
      );
    }
  } catch {
    // Non-fatal -- specs can fall back to apiLogin
  }

  await browser.close();
}

export default globalSetup;
