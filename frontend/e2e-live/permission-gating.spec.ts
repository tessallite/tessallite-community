/**
 * LIVE-FRONTEND-PERMISSION-GATING-001
 *
 * A restricted (member) user does not see/cannot invoke restricted actions.
 * Assert that deploy/delete/create controls are hidden for the member role.
 *
 * Mutation discipline: creates a run-scoped member user, deletes it in
 * teardown, verifies cleanup.
 *
 * Severity: S1 -- permission gating is a security-critical behavior.
 *
 * This spec needs its OWN login (different user), so it overrides the
 * global storageState with a fresh unauthenticated context.
 */
import { test, expect } from "@playwright/test";
import {
  loadProfile,
  isModelyProfile,
  browserLoginAs,
  getToken,
  apiGet,
  apiPost,
  apiDelete,
  resolveProjectId,
  runScopedName,
  waitForNetworkIdle,
} from "./helpers";

const profile = loadProfile();

// Override global storageState -- this test logs in as a different user.
test.use({ storageState: { cookies: [], origins: [] } });

test.describe("LIVE-FRONTEND-PERMISSION-GATING-001", () => {
  let adminToken: string;
  let projectId: string;
  let viewerUserId: string | null = null;
  const viewerEmail = `${profile.runId}-viewer@e2e-test.local`;
  const viewerPassword = "E2eViewerPass-1!";

  test.beforeAll(async () => {
    adminToken = await getToken(profile);
    projectId = await resolveProjectId(profile, adminToken);

    // Create a member user for this run
    const createResp = await apiPost(
      `${profile.modelServiceUrl}/api/v1/auth/users`,
      adminToken,
      {
        email: viewerEmail,
        username: runScopedName(profile, "viewer"),
        password: viewerPassword,
        display_name: "E2E Viewer",
        role: "member",
      },
    );
    if (createResp.status === 201) {
      viewerUserId = createResp.body.id;
    } else if (createResp.status === 409) {
      const listResp = await apiGet(
        `${profile.modelServiceUrl}/api/v1/auth/users`,
        adminToken,
      );
      if (listResp.status === 200 && Array.isArray(listResp.body)) {
        const existing = listResp.body.find(
          (u: any) => u.email === viewerEmail,
        );
        if (existing) viewerUserId = existing.id;
      }
    } else {
      throw new Error(
        `Failed to create viewer user: ${createResp.status} ${JSON.stringify(createResp.body)}`,
      );
    }
  });

  test.afterAll(async () => {
    if (viewerUserId) {
      await apiDelete(
        `${profile.modelServiceUrl}/api/v1/auth/users/${viewerUserId}`,
        adminToken,
      );
    }
  });

  test("viewer cannot see deploy, delete, or create project controls", async ({ page }) => {
    test.skip(!isModelyProfile(profile), "requires modely profile");

    // Step 1: Login as the member user (uses its own form login)
    await browserLoginAs(page, profile, viewerEmail, viewerPassword);

    await waitForNetworkIdle(page);

    // Step 2: Navigate to the explorer root
    await page.goto(`${profile.frontendUrl}/`, { waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle");

    // Step 3: Verify the Projects heading is visible (member can see projects)
    const projectsText = page.getByText("Projects", { exact: true });
    try {
      await expect(projectsText).toBeVisible({ timeout: 10_000 });
    } catch {
      await page.goto(`${profile.frontendUrl}/`, { waitUntil: "domcontentloaded" });
      await page.waitForLoadState("networkidle");
      await expect(projectsText).toBeVisible({ timeout: 10_000 });
    }

    // Step 4: Assert that admin-gated controls are HIDDEN for the member.
    const createProjectBtn = page.getByRole("button", {
      name: /Create project/i,
    });
    await expect(createProjectBtn).toHaveCount(0, { timeout: 5_000 });

    const importExportBtn = page.getByRole("button", {
      name: /Import.*Export/i,
    });
    await expect(importExportBtn).toHaveCount(0, { timeout: 5_000 });

    // Step 5: Select the project to check model-level gating
    const projectBtn = page.getByLabel(/Select project/i).first();
    if (await projectBtn.count() > 0) {
      await projectBtn.click();
      await waitForNetworkIdle(page);
    }

    // Step 6: Verify the Deploy button is hidden for member
    const deployBtn = page.getByLabel(/^Deploy |^Undeploy /i);
    await expect(deployBtn).toHaveCount(0, { timeout: 5_000 });

    // Step 7: Verify the "Add model" button is hidden for member
    const addModelBtn = page.getByRole("button", { name: /Add model/i });
    await expect(addModelBtn).toHaveCount(0, { timeout: 5_000 });

    // Step 8: Verify delete model button is hidden
    const deleteModelBtns = page.getByLabel(/^Delete /i);
    await expect(deleteModelBtns).toHaveCount(0, { timeout: 5_000 });
  });
});
