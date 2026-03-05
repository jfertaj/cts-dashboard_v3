import { test, expect } from "@playwright/test";
import { S } from "../utils/selectors";

test.describe("Explorer — filter panel", () => {
  test.beforeEach(async ({ page }) => {
    // Prevent session-expired overlay from blocking pointer events
    await page.route("**/api/salesforce/me", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ authenticated: true }),
      });
    });
    // Mock the bootstrap endpoint so the page loads without real SF auth
    await page.route("**/api/explorer/bootstrap", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ points: [], rows: [], fields: [] }),
      });
    });
    await page.route("**/api/explorer/fields", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          { key: "sf.Account.Name", label: "Account Name", type: "string", source: "sf" },
          { key: "site.country", label: "Country", type: "string", source: "site" },
          { key: "sf.C_Number_of_Stage2_Individuals_followed__c", label: "Stage 2", type: "number", source: "sf" },
        ]),
      });
    });
    await page.goto("/explorer");
  });

  test("FILTER-1: filter panel is visible on /explorer", async ({ page }) => {
    await expect(page.locator(S.EXPLORER_FILTER_PANEL)).toBeVisible();
  });

  test("FILTER-2: filter builder is rendered inside filter panel", async ({ page }) => {
    await expect(page.locator(S.FILTER_BUILDER)).toBeVisible();
  });

  test("FILTER-3: search button is visible and present", async ({ page }) => {
    await expect(page.locator(S.EXPLORER_SEARCH_BTN)).toBeVisible();
  });

  test("FILTER-4: search fires POST /api/explorer/search on click", async ({ page }) => {
    let searchCalled = false;
    await page.route("**/api/explorer/search", async (route) => {
      searchCalled = true;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ points: [], rows: [], total: 0 }),
      });
    });

    await page.locator(S.EXPLORER_SEARCH_BTN).click();
    await page.waitForTimeout(500);
    expect(searchCalled).toBe(true);
  });

  test("FILTER-5: results count updates after search", async ({ page }) => {
    const mockRows = [
      { account_id: "1", account_name: "Site A", country: "IT", city: "Milan", data: {} },
      { account_id: "2", account_name: "Site B", country: "IT", city: "Rome",  data: {} },
    ];
    await page.route("**/api/explorer/search", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ points: [], rows: mockRows, total: 2 }),
      });
    });

    await page.locator(S.EXPLORER_SEARCH_BTN).click();
    const countEl = page.locator(S.EXPLORER_RESULTS_COUNT);
    await expect(countEl).toBeVisible({ timeout: 5000 });
  });
});
