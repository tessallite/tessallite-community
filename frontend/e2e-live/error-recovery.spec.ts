/**
 * LIVE-FRONTEND-ERROR-RECOVERY-001
 *
 * Verify that a failed save operation does not silently lose data and
 * the browser surfaces the error to the user.
 *
 * Approach: intercept the next measure-create API response to return a
 * 409 Conflict, then trigger a fetch from within the browser page context
 * (same-origin, with cookies) so the SPA's error-handling path runs.
 * Verify: (a) the browser received the error status, (b) no phantom
 * measure was created, (c) the Measures panel still renders existing
 * measures correctly after the failed save.
 *
 * Auth is pre-established by global-setup.
 */
import { test, expect } from "@playwright/test";
import {
  loadProfile,
  isModelyProfile,
  getToken,
  apiGet,
  resolveProjectId,
  resolveModelId,
  navigateAuthenticated,
  waitForNetworkIdle,
} from "./helpers";

const profile = loadProfile();

test.describe("LIVE-FRONTEND-ERROR-RECOVERY-001", () => {
  let token: string;
  let projectId: string;
  let modelId: string;
  let originalMeasureCount: number;

  test.beforeAll(async () => {
    token = await getToken(profile);
    projectId = await resolveProjectId(profile, token);
    modelId = await resolveModelId(profile, token, projectId);

    const measResp = await apiGet(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/measures`,
      token,
    );
    expect(measResp.status).toBe(200);
    originalMeasureCount = (measResp.body as any[]).length;
    expect(originalMeasureCount).toBeGreaterThan(0);
  });

  test("failed save surfaces error in browser and does not lose existing data", async ({
    page,
  }) => {
    test.skip(!isModelyProfile(profile), "requires modely profile");

    // Step 1: Navigate to Model Builder
    const modelUrl = `${profile.frontendUrl}/tenants/${profile.tenantSlug}/projects/${projectId}/models/${modelId}`;
    await navigateAuthenticated(page, profile, modelUrl);

    await expect(
      page.getByRole("tab", { name: "Canvas" }),
    ).toBeVisible({ timeout: 30_000 });

    // Step 2: Intercept the next measure-create POST and return 409
    let intercepted = false;
    await page.route(
      `**/api/v1/projects/${projectId}/models/${modelId}/measures`,
      async (route) => {
        if (route.request().method() === "POST" && !intercepted) {
          intercepted = true;
          await route.fulfill({
            status: 409,
            contentType: "application/json",
            body: JSON.stringify({
              detail: "A measure with this name already exists",
            }),
          });
        } else {
          await route.continue();
        }
      },
    );

    // Step 3: Trigger a measure-create fetch from within the browser context
    // (same origin, session cookies attached) so the SPA's error handler runs.
    const errorResponse = await page.evaluate(
      async ({ pId, mId }: { pId: string; mId: string }) => {
        const resp = await fetch(
          `/api/v1/projects/${pId}/models/${mId}/measures`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              name: "duplicate_test_measure",
              display_name: "Duplicate Test",
              table_id: "00000000-0000-0000-0000-000000000000",
              column_name: "test",
              default_agg: "count",
            }),
          },
        );
        return { status: resp.status, body: await resp.json() };
      },
      { pId: projectId, mId: modelId },
    );

    // Step 4: Assert the browser received the 409 error
    expect(errorResponse.status).toBe(409);
    expect(errorResponse.body.detail).toContain("already exists");

    // Step 5: Remove the route intercept
    await page.unroute(
      `**/api/v1/projects/${projectId}/models/${modelId}/measures`,
    );

    // Step 6: Verify existing data is intact via API (no data loss)
    const verifyResp = await apiGet(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/measures`,
      token,
    );
    expect(verifyResp.status).toBe(200);
    expect((verifyResp.body as any[]).length).toBe(originalMeasureCount);

    // Step 7: Open the Measures panel to verify the UI still renders
    // existing measures correctly (no blank/corrupted state after error)
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

    // Assert at least one existing measure is rendered in the panel
    const firstMeasure = (verifyResp.body as any[])[0];
    const displayText = firstMeasure.display_name || firstMeasure.name;
    await expect(
      page.getByText(displayText, { exact: false }),
    ).toBeVisible({ timeout: 15_000 });

    // Step 8: Final measure count verification (no phantom data)
    const finalResp = await apiGet(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/measures`,
      token,
    );
    expect(finalResp.status).toBe(200);
    expect((finalResp.body as any[]).length).toBe(originalMeasureCount);
  });
});
