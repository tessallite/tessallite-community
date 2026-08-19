/**
 * LIVE-FRONTEND-DOWNSTREAM-ASSETS-001
 *
 * Create a downstream asset from the Impact panel in the Model Builder,
 * verify it appears in the list, delete it, and verify removal.
 *
 * The create/delete uses the API (the same boundary the Impact panel calls),
 * then the browser verifies rendering in the Impact panel.  This avoids
 * brittle dialog interactions while still proving the UI reflects the
 * persisted state through the real deployed frontend.
 *
 * Auth is pre-established by global-setup.
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

test.describe("LIVE-FRONTEND-DOWNSTREAM-ASSETS-001", () => {
  let token: string;
  let projectId: string;
  let modelId: string;
  let createdAssetId: string | null = null;
  const assetName = runScopedName(profile, "e2e-downstream-asset");

  test.beforeAll(async () => {
    token = await getToken(profile);
    projectId = await resolveProjectId(profile, token);
    modelId = await resolveModelId(profile, token, projectId);
  });

  test.afterAll(async () => {
    // Cleanup: delete the asset if it still exists -- throw on failure
    if (createdAssetId) {
      const delUrl = `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/downstream-assets/${createdAssetId}`;
      const { status } = await apiDelete(delUrl, token);
      if (status !== 200 && status !== 204 && status !== 404) {
        throw new Error(
          `CLEANUP_FAIL: DELETE downstream asset returned ${status}`,
        );
      }
    }
  });

  test("create, list, delete downstream asset via API + verify in Impact panel", async ({
    page,
  }) => {
    test.skip(!isModelyProfile(profile), "requires modely profile");

    // Step 1: Create a downstream asset via API
    const createResp = await apiPost(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/downstream-assets`,
      token,
      {
        asset_type: "dashboard",
        asset_name: assetName,
        asset_url: "https://e2e-test.example.com/dashboard",
        owner: "E2E Test",
        notes: "Created by live e2e test - will be cleaned up",
      },
    );
    expect(createResp.status).toBe(201);
    createdAssetId = createResp.body.id;
    expect(createdAssetId).toBeTruthy();
    expect(createResp.body.asset_name).toBe(assetName);

    // Step 2: Verify asset in list via API
    const listResp = await apiGet(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/downstream-assets`,
      token,
    );
    expect(listResp.status).toBe(200);
    const assets = listResp.body as any[];
    const found = assets.find((a: any) => a.id === createdAssetId);
    expect(found).toBeTruthy();
    expect(found.asset_name).toBe(assetName);

    // Step 3: Navigate to the Model Builder
    const modelUrl = `${profile.frontendUrl}/tenants/${profile.tenantSlug}/projects/${projectId}/models/${modelId}`;
    await navigateAuthenticated(page, profile, modelUrl);

    await expect(
      page.getByRole("tab", { name: "Canvas" }),
    ).toBeVisible({ timeout: 30_000 });

    // Step 4: Open the Impact panel (downstream assets live here)
    const impactBtn = page.locator(
      'button:has-text("Impact"), [aria-label*="Impact"], [data-testid="tool-impact"]',
    );
    if ((await impactBtn.count()) > 0) {
      await impactBtn.first().click();
    } else {
      // Fallback: look by role
      const btn = page.getByRole("button", { name: /Impact/i }).first();
      await btn.click();
    }
    await waitForNetworkIdle(page);

    // Step 5: Verify the created asset name is visible in the Impact panel
    await expect(page.getByText(assetName)).toBeVisible({ timeout: 15_000 });

    // Step 6: Delete the asset via API
    const delResp = await apiDelete(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/downstream-assets/${createdAssetId}`,
      token,
    );
    expect([200, 204]).toContain(delResp.status);
    createdAssetId = null; // prevent double-delete in afterAll

    // Step 7: Verify deletion via API
    const listAfterResp = await apiGet(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/downstream-assets`,
      token,
    );
    expect(listAfterResp.status).toBe(200);
    const afterAssets = listAfterResp.body as any[];
    const stillThere = afterAssets.find((a: any) => a.asset_name === assetName);
    expect(stillThere).toBeFalsy();

    // Step 8: Reload the Impact panel and verify the asset is gone from the UI
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle");

    if (page.url().includes("/login")) {
      await navigateAuthenticated(page, profile, modelUrl);
    }

    await expect(
      page.getByRole("tab", { name: "Canvas" }),
    ).toBeVisible({ timeout: 30_000 });

    const impactBtnReload = page.locator(
      'button:has-text("Impact"), [aria-label*="Impact"], [data-testid="tool-impact"]',
    );
    if ((await impactBtnReload.count()) > 0) {
      await impactBtnReload.first().click();
    } else {
      await page.getByRole("button", { name: /Impact/i }).first().click();
    }
    await waitForNetworkIdle(page);

    // The asset should no longer be visible
    await expect(page.getByText(assetName)).toHaveCount(0, { timeout: 10_000 });
  });
});
