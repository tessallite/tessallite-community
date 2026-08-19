/**
 * LIVE-FRONTEND-CREATE-SAVE-001
 *
 * Perform a create/save action (create a measure via API), then verify it
 * in the Measures panel of Model Builder, close/reopen the panel to confirm
 * persistence, and double-check via an independent API GET.
 *
 * Mutation discipline: uses a run-scoped name, deletes the created measure
 * in teardown, and verifies cleanup via API.
 *
 * Auth is pre-established by global-setup; storageState provides browser
 * cookies. Node-side API calls use the shared token from global-setup
 * to avoid additional login requests against the rate limiter.
 */
import { test, expect } from "@playwright/test";
import {
  loadProfile,
  isModelyProfile,
  getToken,
  apiGet,
  apiPost,
  apiDelete,
  resolveProjectId,
  resolveModelId,
  navigateAuthenticated,
  runScopedName,
  waitForNetworkIdle,
} from "./helpers";

const profile = loadProfile();

test.describe("LIVE-FRONTEND-CREATE-SAVE-001", () => {
  let token: string;
  let projectId: string;
  let modelId: string;
  let createdMeasureId: string | null = null;
  const measureDisplayName = runScopedName(profile, "e2e-test-measure");

  test.beforeAll(async () => {
    // Use the shared token (no extra login request)
    token = await getToken(profile);
    projectId = await resolveProjectId(profile, token);
    modelId = await resolveModelId(profile, token, projectId);
  });

  test.afterAll(async () => {
    if (createdMeasureId) {
      const deleteUrl = `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/measures/${createdMeasureId}`;
      const { status } = await apiDelete(deleteUrl, token);
      if (status !== 200 && status !== 204 && status !== 404) {
        console.error(`Cleanup failed: DELETE measure returned ${status}`);
      }
      const { status: verifyStatus } = await apiGet(deleteUrl, token);
      if (verifyStatus !== 404) {
        console.error(
          `Cleanup verification: GET measure returned ${verifyStatus} (expected 404)`,
        );
      }
    }
  });

  test("create measure via API, verify in Measures panel after reload", async ({ page }) => {
    test.skip(!isModelyProfile(profile), "requires modely profile");

    // Step 1: Get an existing measure to derive table_id and column_name
    const measuresResp = await apiGet(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/measures`,
      token,
    );
    expect(measuresResp.status).toBe(200);
    const existingMeasures = measuresResp.body as any[];
    expect(existingMeasures.length).toBeGreaterThan(0);

    const refMeasure = existingMeasures.find(
      (m: any) => m.default_agg === "count" || m.default_agg === "sum",
    );
    expect(refMeasure).toBeTruthy();

    // Step 2: Create a run-scoped measure via the API
    const createResp = await apiPost(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/measures`,
      token,
      {
        name: measureDisplayName.replace(/-/g, "_"),
        display_name: measureDisplayName,
        table_id: refMeasure.table_id,
        column_name: refMeasure.column_name,
        default_agg: refMeasure.default_agg,
        description: "E2e live test measure - will be cleaned up",
      },
    );
    expect(createResp.status).toBe(201);
    createdMeasureId = createResp.body.id;
    expect(createdMeasureId).toBeTruthy();

    // Step 3: Navigate to the model builder.
    // First go to Explorer root to establish the session (lighter page load
    // that gives the rate limiter time to recover from prior tests' internal
    // SPA API calls).
    await navigateAuthenticated(page, profile, `${profile.frontendUrl}/`);

    // Now navigate to the specific model builder
    const modelUrl = `${profile.frontendUrl}/tenants/${profile.tenantSlug}/projects/${projectId}/models/${modelId}`;
    await page.goto(modelUrl, { waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle");

    // If still on login (SPA rate-limited), re-auth and retry
    if (page.url().includes("/login")) {
      await navigateAuthenticated(page, profile, modelUrl);
    }

    // Wait for model builder to load
    await expect(
      page.getByRole("tab", { name: "Canvas" }),
    ).toBeVisible({ timeout: 30_000 });

    // Step 4: Open the Measures panel
    const measuresToolBtn = page.locator('[data-testid="tool-measures"]');
    if (await measuresToolBtn.count() > 0) {
      await measuresToolBtn.first().click();
    } else {
      const measBtn = page.getByRole("button", { name: /Measures/i }).first();
      await measBtn.click();
    }
    await waitForNetworkIdle(page);

    // Step 5: Verify the created measure display name is visible
    await expect(
      page.getByText(measureDisplayName),
    ).toBeVisible({ timeout: 15_000 });

    // Step 6: Close and re-open the Measures panel (persistence check)
    await page.keyboard.press("Escape");
    await waitForNetworkIdle(page);

    const measuresToolBtnReload = page.locator('[data-testid="tool-measures"]');
    if (await measuresToolBtnReload.count() > 0) {
      await measuresToolBtnReload.first().click();
    } else {
      const measBtnReload = page.getByRole("button", { name: /Measures/i }).first();
      await measBtnReload.click();
    }
    await waitForNetworkIdle(page);

    await expect(
      page.getByText(measureDisplayName),
    ).toBeVisible({ timeout: 15_000 });

    // Step 7: Verify via API independently (double-check persistence)
    const listResp = await apiGet(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/measures`,
      token,
    );
    expect(listResp.status).toBe(200);
    const allMeasures = listResp.body as any[];
    const found = allMeasures.find((m: any) => m.id === createdMeasureId);
    expect(found).toBeTruthy();
    expect(found.display_name).toBe(measureDisplayName);
  });
});
