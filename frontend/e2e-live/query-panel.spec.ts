/**
 * LIVE-FRONTEND-QUERY-PANEL-001
 *
 * Run a standard query in the Query Panel, assert a KNOWN-ANSWER result value.
 * Reuses the known answer from JDBC live tests: modely US transaction_count = 11133.
 *
 * The model must be deployed for queries to execute. This test verifies that
 * the deploy happened (preflight) and does not deploy/undeploy itself.
 *
 * Read-only scenario -- no mutations, no cleanup needed.
 * Auth is pre-established by global-setup; no per-spec login.
 */
import { test, expect } from "@playwright/test";
import {
  loadProfile,
  isModelyProfile,
  getToken,
  resolveProjectId,
  resolveModelId,
  navigateAuthenticated,
  waitForNetworkIdle,
} from "./helpers";

const profile = loadProfile();

// Known answer: SUM(transaction_count) WHERE country_code='US' for modely = 11133
// Source: tessallite/tests/live/test_query_gateway_live.py _KNOWN_US_TC_BY_MODEL
const KNOWN_US_TRANSACTION_COUNT = "11133";

test.describe("LIVE-FRONTEND-QUERY-PANEL-001", () => {
  test("Query Panel executes and shows known-answer result", async ({ page }) => {
    test.skip(!isModelyProfile(profile), "requires modely profile");

    // Resolve model URL via API (single login call shared across the spec)
    const token = await getToken(profile);
    const projectId = await resolveProjectId(profile, token);
    const modelId = await resolveModelId(profile, token, projectId);

    // Navigate directly to the model builder (authenticated via storageState;
    // navigateAuthenticated handles fallback re-auth)
    const modelUrl = `${profile.frontendUrl}/tenants/${profile.tenantSlug}/projects/${projectId}/models/${modelId}`;
    await navigateAuthenticated(page, profile, modelUrl);

    // Wait for the model builder to load (Canvas tab visible)
    await expect(
      page.getByRole("tab", { name: "Canvas" }),
    ).toBeVisible({ timeout: 20_000 });

    // Step 2: Open the Query tab
    await page.getByRole("tab", { name: "Query" }).click();
    await waitForNetworkIdle(page);

    // Step 2b: Click "Free-form Query" sub-tab to get the SQL textarea
    const freeFormTab = page.getByText("Free-form Query", { exact: true });
    await freeFormTab.waitFor({ state: "visible", timeout: 10_000 });
    await freeFormTab.click();
    await waitForNetworkIdle(page);

    // Step 3: Find the SQL textarea and enter the query
    const textarea = page.locator("textarea").first();
    await textarea.waitFor({ state: "visible", timeout: 10_000 });

    const query = `SELECT SUM(transaction_count) AS tc FROM modely WHERE country_code = 'US'`;
    await textarea.fill(query);

    // Step 4: Click the Execute button
    const executeBtn = page.getByRole("button", { name: /^Execute$/i });
    await expect(executeBtn).toBeEnabled({ timeout: 5_000 });
    await executeBtn.click();

    // Step 5: Wait for results to appear -- assert the KNOWN ANSWER
    const resultCell = page.getByText(KNOWN_US_TRANSACTION_COUNT, { exact: true });
    await expect(resultCell).toBeVisible({ timeout: 30_000 });

    // Step 6: Verify the route chip is visible (source or aggregate)
    const routeChip = page.getByText(/Route:/i);
    await expect(routeChip).toBeVisible({ timeout: 5_000 });

    // Step 7: Verify the rows count chip shows "1 row"
    const rowsChip = page.getByText(/1 row/i);
    await expect(rowsChip).toBeVisible({ timeout: 5_000 });
  });
});
