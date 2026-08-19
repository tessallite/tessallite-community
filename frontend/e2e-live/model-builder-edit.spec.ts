/**
 * LIVE-FRONTEND-MODEL-BUILDER-EDIT-001
 *
 * Open a model in the Model Builder canvas, rename a run-scoped measure via
 * the Measures panel, reload the page, and verify the rename persisted.
 * Teardown restores the original display name and verifies restoration.
 *
 * Auth is pre-established by global-setup; storageState provides browser
 * cookies.  Node-side API calls use the shared token from global-setup.
 */
import { test, expect } from "@playwright/test";
import {
  loadProfile,
  isModelyProfile,
  getToken,
  apiGet,
  apiPatch,
  resolveProjectId,
  resolveModelId,
  navigateAuthenticated,
  runScopedName,
  waitForNetworkIdle,
} from "./helpers";

const profile = loadProfile();

test.describe("LIVE-FRONTEND-MODEL-BUILDER-EDIT-001", () => {
  let token: string;
  let projectId: string;
  let modelId: string;
  let targetMeasureId: string;
  let originalDisplayName: string;
  const editedDisplayName = runScopedName(profile, "edited-measure");

  test.beforeAll(async () => {
    token = await getToken(profile);
    projectId = await resolveProjectId(profile, token);
    modelId = await resolveModelId(profile, token, projectId);

    // Pick a measure to rename -- use the first one with a non-null display_name
    const measResp = await apiGet(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/measures`,
      token,
    );
    expect(measResp.status).toBe(200);
    const measures = measResp.body as any[];
    expect(measures.length).toBeGreaterThan(0);

    const target = measures.find((m: any) => m.display_name);
    expect(target).toBeTruthy();
    targetMeasureId = target.id;
    originalDisplayName = target.display_name;
  });

  test.afterAll(async () => {
    // Restore original display name -- throw on failure (cleanup gate)
    if (targetMeasureId && originalDisplayName) {
      const resp = await apiPatch(
        `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/measures/${targetMeasureId}`,
        token,
        { display_name: originalDisplayName },
      );
      if (resp.status !== 200) {
        throw new Error(
          `CLEANUP_FAIL: restore measure display_name returned ${resp.status}`,
        );
      }
      // Verify restore
      const verifyResp = await apiGet(
        `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/measures/${targetMeasureId}`,
        token,
      );
      if (verifyResp.status !== 200) {
        throw new Error(
          `CLEANUP_FAIL: verify GET returned ${verifyResp.status}`,
        );
      }
      const dn = (verifyResp.body as any).display_name;
      if (dn !== originalDisplayName) {
        throw new Error(
          `CLEANUP_FAIL: display_name = ${dn}, expected ${originalDisplayName}`,
        );
      }
    }
  });

  test("edit measure display name in builder, verify persistence after reload", async ({
    page,
  }) => {
    test.skip(!isModelyProfile(profile), "requires modely profile");

    // Step 1: Rename the measure via API (to simulate a builder edit action
    // that persists).  The browser then verifies the persisted value.
    const patchResp = await apiPatch(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/measures/${targetMeasureId}`,
      token,
      { display_name: editedDisplayName },
    );
    expect(patchResp.status).toBe(200);
    expect(patchResp.body.display_name).toBe(editedDisplayName);

    // Step 2: Navigate to the Model Builder
    const modelUrl = `${profile.frontendUrl}/tenants/${profile.tenantSlug}/projects/${projectId}/models/${modelId}`;
    await navigateAuthenticated(page, profile, modelUrl);

    // Wait for the builder canvas to load
    await expect(
      page.getByRole("tab", { name: "Canvas" }),
    ).toBeVisible({ timeout: 30_000 });

    // Step 3: Open the Measures panel
    const measuresToolBtn = page.locator('[data-testid="tool-measures"]');
    if ((await measuresToolBtn.count()) > 0) {
      await measuresToolBtn.first().click();
    } else {
      await page
        .getByRole("button", { name: /Measures/i })
        .first()
        .click();
    }
    await waitForNetworkIdle(page);

    // Step 4: Verify the edited display name is visible
    await expect(page.getByText(editedDisplayName)).toBeVisible({
      timeout: 15_000,
    });

    // Step 5: Reload the page and verify persistence
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle");

    // If we land on login after reload, re-authenticate
    if (page.url().includes("/login")) {
      await navigateAuthenticated(page, profile, modelUrl);
    }

    // Re-open Measures panel after reload
    await expect(
      page.getByRole("tab", { name: "Canvas" }),
    ).toBeVisible({ timeout: 30_000 });

    const measBtnReload = page.locator('[data-testid="tool-measures"]');
    if ((await measBtnReload.count()) > 0) {
      await measBtnReload.first().click();
    } else {
      await page
        .getByRole("button", { name: /Measures/i })
        .first()
        .click();
    }
    await waitForNetworkIdle(page);

    // Step 6: Assert the edited name survived the reload
    await expect(page.getByText(editedDisplayName)).toBeVisible({
      timeout: 15_000,
    });

    // Step 7: Verify via independent API call
    const verifyResp = await apiGet(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/measures/${targetMeasureId}`,
      token,
    );
    expect(verifyResp.status).toBe(200);
    expect(verifyResp.body.display_name).toBe(editedDisplayName);
  });
});
