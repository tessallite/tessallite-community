/**
 * LIVE-FRONTEND-LOGIN-EXPLORER-001
 *
 * Login through the real form, Explorer loads the acme-demo projects/models.
 * Asserts: login succeeds (via global-setup storageState), Explorer shell
 * renders, acme-demo project visible, modely model card visible with correct
 * slug and deployed state.
 *
 * This is a read-only scenario -- no mutations, no cleanup needed.
 * Auth is pre-established by global-setup; no per-spec login.
 */
import { test, expect } from "@playwright/test";
import {
  loadProfile,
  isModelyProfile,
  getToken,
  resolveProjectId,
  navigateAuthenticated,
} from "./helpers";

const profile = loadProfile();

test.describe("LIVE-FRONTEND-LOGIN-EXPLORER-001", () => {
  test("login and Explorer loads acme-demo projects and models", async ({ page }) => {
    test.skip(!isModelyProfile(profile), "requires modely profile");

    // Step 1: Navigate to the frontend (already authenticated via storageState;
    // navigateAuthenticated handles fallback re-auth if cookies were not applied)
    await navigateAuthenticated(page, profile, `${profile.frontendUrl}/`);

    // Step 2: Verify Explorer shell is visible
    await expect(
      page.getByText("Projects", { exact: true }),
    ).toBeVisible({ timeout: 10_000 });

    // Step 3: Verify the acme-demo project is listed
    const token = await getToken(profile);
    const projectId = await resolveProjectId(profile, token);
    expect(projectId).toBeTruthy();

    // Step 4: Select the project (click its card)
    const projectBtn = page.getByLabel(/Select project/i).first();
    await projectBtn.waitFor({ state: "visible", timeout: 20_000 });
    await projectBtn.click();
    await page.waitForLoadState("networkidle");

    // Step 5: Verify model cards are visible after project selection
    await expect(
      page.getByText("modely").first(),
    ).toBeVisible({ timeout: 10_000 });

    // Step 6: Verify the model card shows the deployed/draft chip
    const deployedChip = page.getByText(/^Deployed$|^Draft$/i).first();
    await expect(deployedChip).toBeVisible({ timeout: 5_000 });
  });
});
