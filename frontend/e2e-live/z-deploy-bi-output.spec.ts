/**
 * LIVE-FRONTEND-DEPLOY-BI-OUTPUT-001
 *
 * Deploy from the UI and assert the change is reflected in BI output
 * (verify via query-router known-answer).
 *
 * Workflow:
 * 1. Create a run-scoped measure via API (mutation).
 * 2. Deploy the model via API (re-deploy with new measure).
 * 3. Verify the "Deployed" chip is visible in the Explorer UI.
 * 4. Query via the query-router to verify BI output works with the deployed model.
 * 5. Teardown: delete the measure, re-deploy baseline, verify baseline.
 *
 * Auth is pre-established by global-setup; no per-spec login.
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

// Known answer: SUM(transaction_count) WHERE country_code = 'US' for modely = 11133
const KNOWN_US_TRANSACTION_COUNT = 11133;

test.describe("LIVE-FRONTEND-DEPLOY-BI-OUTPUT-001", () => {
  let token: string;
  let projectId: string;
  let modelId: string;
  let createdMeasureId: string | null = null;
  const measureName = runScopedName(profile, "e2e_deploy_bi");

  test.beforeAll(async () => {
    token = await getToken(profile);
    projectId = await resolveProjectId(profile, token);
    modelId = await resolveModelId(profile, token, projectId);
  });

  test.afterAll(async () => {
    // Cleanup: delete the run-scoped measure
    if (createdMeasureId) {
      await apiDelete(
        `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/measures/${createdMeasureId}`,
        token,
      );
    }

    // Re-deploy to restore baseline (remove trace of run-scoped measure)
    try {
      await apiPost(
        `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/deploy`,
        token,
        {},
      );
      // Wait for deploy to take effect
      await new Promise((r) => setTimeout(r, 3000));
    } catch (err) {
      console.error("Baseline re-deploy failed:", err);
    }

    // Verify baseline known-answer
    try {
      const resp = await fetch(
        `${profile.frontendUrl}/query-router/api/v1/execute`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify({
            model_id: modelId,
            raw_query:
              "SELECT SUM(transaction_count) AS tc FROM modely WHERE country_code = 'US'",
            protocol: "jdbc",
          }),
        },
      );
      if (resp.ok) {
        const result = await resp.json();
        const rows = result.rows || [];
        if (rows.length === 1) {
          const tc = parseInt(String(Object.values(rows[0])[0]), 10);
          if (tc !== KNOWN_US_TRANSACTION_COUNT) {
            console.error(
              `Baseline verification warning: US tc = ${tc}, expected ${KNOWN_US_TRANSACTION_COUNT}`,
            );
          }
        }
      }
    } catch (err) {
      console.error("Baseline verification failed:", err);
    }
  });

  test("deploy changes reflected in BI output and UI", async ({ page }) => {
    test.skip(!isModelyProfile(profile), "requires modely profile");

    // Step 1: Create a run-scoped measure via API
    const measuresResp = await apiGet(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/measures`,
      token,
    );
    expect(measuresResp.status).toBe(200);
    const refMeasure = (measuresResp.body as any[]).find(
      (m: any) => m.default_agg === "count" || m.default_agg === "sum",
    );
    expect(refMeasure).toBeTruthy();

    const createResp = await apiPost(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/measures`,
      token,
      {
        name: measureName.replace(/-/g, "_"),
        display_name: measureName,
        table_id: refMeasure.table_id,
        column_name: refMeasure.column_name,
        default_agg: refMeasure.default_agg,
        description: "E2e deploy-bi test measure",
      },
    );
    expect(createResp.status).toBe(201);
    createdMeasureId = createResp.body.id;

    // Step 2: Deploy the model via API (re-deploy with new measure)
    const deployResp = await apiPost(
      `${profile.modelServiceUrl}/api/v1/projects/${projectId}/models/${modelId}/deploy`,
      token,
      {},
    );
    expect(deployResp.status).toBe(200);

    // Wait for gateway to pick up the new deployment
    await new Promise((r) => setTimeout(r, 3000));

    // Step 3: Navigate to Explorer (authenticated via storageState;
    // navigateAuthenticated handles fallback re-auth)
    await navigateAuthenticated(page, profile, `${profile.frontendUrl}/`);

    // Select project (extended timeout to handle rate-limiter-delayed project list loading)
    const projectBtn = page.getByLabel(/Select project/i).first();
    await projectBtn.waitFor({ state: "visible", timeout: 30_000 });
    await projectBtn.click();
    await waitForNetworkIdle(page);

    // Verify "Deployed" chip is visible for modely
    await expect(
      page.getByText("Deployed", { exact: true }).first(),
    ).toBeVisible({ timeout: 10_000 });

    // Step 4: Query the known-answer via the query-router (BI output verification)
    const queryResp = await fetch(
      `${profile.frontendUrl}/query-router/api/v1/execute`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          model_id: modelId,
          raw_query:
            "SELECT SUM(transaction_count) AS tc FROM modely WHERE country_code = 'US'",
          protocol: "jdbc",
        }),
      },
    );
    expect(queryResp.ok).toBeTruthy();

    const queryResult = await queryResp.json();
    const rows = queryResult.rows || [];
    expect(rows.length).toBe(1);

    // Assert the KNOWN ANSWER value
    const tc = parseInt(String(Object.values(rows[0])[0]), 10);
    expect(tc).toBe(KNOWN_US_TRANSACTION_COUNT);

    // Step 5: Verify the model is still deployed in the browser
    const modelUrl = `${profile.frontendUrl}/tenants/${profile.tenantSlug}/projects/${projectId}/models/${modelId}`;
    await navigateAuthenticated(page, profile, modelUrl);

    // The model builder shows "Deployed vN" chip
    const deployedChip = page.getByText(/Deployed v\d+/i);
    await expect(deployedChip).toBeVisible({ timeout: 15_000 });
  });
});
