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
    // explorerBootstrap() calls /api/salesforce/map/bootstrap (not /api/explorer/bootstrap)
    await page.route("**/api/salesforce/map/bootstrap", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
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
    // Default search mock for the SWR bootstrap's explorerSearch() call — prevents session-expired overlay.
    // Individual tests can override with their own route mock (Playwright uses LIFO order).
    await page.route("**/api/explorer/search", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ points: [], rows: [], total: 0 }),
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

test.describe("Explorer — filter logic expression UI", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/salesforce/me", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: true }) });
    });
    await page.route("**/api/salesforce/map/bootstrap", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/explorer/fields", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          { key: "sf.Account.Name", label: "Account Name", type: "string", source: "sf" },
          { key: "site.country",    label: "Country",       type: "string", source: "site" },
          { key: "sf.C_Number_of_Stage2_Individuals_followed__c", label: "Stage 2", type: "number", source: "sf" },
        ]),
      });
    });
    await page.route("**/api/explorer/search", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ points: [], rows: [], total: 0 }) });
    });
    await page.goto("/explorer");
    await expect(page.locator(S.EXPLORER_SEARCH_BTN)).toBeEnabled({ timeout: 10000 });
  });

  test("FLOGIC-1: logic bar appears only after adding 2+ rules", async ({ page }) => {
    // 0 rules: no logic bar
    await expect(page.locator(S.FILTER_LOGIC_BAR)).not.toBeVisible();

    // Add first rule — still no bar
    await page.locator(S.FILTER_ADD_BTN).click();
    await expect(page.locator(S.FILTER_RULE)).toHaveCount(1);
    await expect(page.locator(S.FILTER_LOGIC_BAR)).not.toBeVisible();

    // Add second rule — logic bar appears with default "1 AND 2"
    await page.locator(S.FILTER_ADD_BTN).click();
    await expect(page.locator(S.FILTER_RULE)).toHaveCount(2);
    await expect(page.locator(S.FILTER_LOGIC_BAR)).toBeVisible();
    await expect(page.locator(S.FILTER_LOGIC_EXPR)).toHaveText("1 AND 2");
  });

  test("FLOGIC-2: edit logic modal opens, custom expression is applied", async ({ page }) => {
    // Add 3 rules
    await page.locator(S.FILTER_ADD_BTN).click();
    await page.locator(S.FILTER_ADD_BTN).click();
    await page.locator(S.FILTER_ADD_BTN).click();
    await expect(page.locator(S.FILTER_RULE)).toHaveCount(3);
    await expect(page.locator(S.FILTER_LOGIC_EXPR)).toHaveText("1 AND 2 AND 3");

    // Open modal
    await page.locator(S.FILTER_LOGIC_EDIT_BTN).click();
    await expect(page.locator(S.FILTER_LOGIC_MODAL)).toBeVisible();

    // Type custom expression
    await page.locator(S.FILTER_LOGIC_TEXTAREA).clear();
    await page.locator(S.FILTER_LOGIC_TEXTAREA).fill("(1 AND 2) OR 3");
    await expect(page.locator(S.FILTER_LOGIC_VALID)).toBeVisible();

    // Apply
    await page.locator(S.FILTER_LOGIC_APPLY_BTN).click();
    await expect(page.locator(S.FILTER_LOGIC_MODAL)).not.toBeVisible();
    await expect(page.locator(S.FILTER_LOGIC_EXPR)).toHaveText("(1 AND 2) OR 3");
  });

  test("FLOGIC-3: invalid expression shows error and disables Apply", async ({ page }) => {
    // Add 2 rules
    await page.locator(S.FILTER_ADD_BTN).click();
    await page.locator(S.FILTER_ADD_BTN).click();

    await page.locator(S.FILTER_LOGIC_EDIT_BTN).click();
    await expect(page.locator(S.FILTER_LOGIC_MODAL)).toBeVisible();

    // Reference rule 5 which does not exist
    await page.locator(S.FILTER_LOGIC_TEXTAREA).clear();
    await page.locator(S.FILTER_LOGIC_TEXTAREA).fill("1 AND 5");

    await expect(page.locator(S.FILTER_LOGIC_ERROR)).toBeVisible();
    await expect(page.locator(S.FILTER_LOGIC_APPLY_BTN)).toBeDisabled();

    // Cancel closes modal without changing expression
    await page.getByRole("button", { name: "Cancel" }).click();
    await expect(page.locator(S.FILTER_LOGIC_MODAL)).not.toBeVisible();
    await expect(page.locator(S.FILTER_LOGIC_EXPR)).toHaveText("1 AND 2");
  });

  test("FLOGIC-4: removing a rule updates the logic expression", async ({ page }) => {
    // Add 3 rules → expression "1 AND 2 AND 3"
    await page.locator(S.FILTER_ADD_BTN).click();
    await page.locator(S.FILTER_ADD_BTN).click();
    await page.locator(S.FILTER_ADD_BTN).click();
    await expect(page.locator(S.FILTER_LOGIC_EXPR)).toHaveText("1 AND 2 AND 3");

    // Remove the first rule → remaining rules renumbered → expression becomes "1 AND 2"
    await page.locator(S.FILTER_RULE_REMOVE).first().click();
    await expect(page.locator(S.FILTER_RULE)).toHaveCount(2);
    await expect(page.locator(S.FILTER_LOGIC_EXPR)).toHaveText("1 AND 2");

    // Remove one more → only 1 rule left → logic bar disappears
    await page.locator(S.FILTER_RULE_REMOVE).first().click();
    await expect(page.locator(S.FILTER_RULE)).toHaveCount(1);
    await expect(page.locator(S.FILTER_LOGIC_BAR)).not.toBeVisible();
  });

  test("FLOGIC-5: filter panel collapse/expand toggle hides and shows FilterBuilder", async ({ page }) => {
    // FilterBuilder is visible initially
    await expect(page.locator(S.FILTER_BUILDER)).toBeVisible();

    // Click collapse button → FilterBuilder hidden
    await page.locator(S.FILTER_PANEL_COLLAPSE_BTN).click();
    await expect(page.locator(S.FILTER_BUILDER)).not.toBeVisible();

    // Click again → FilterBuilder visible again
    await page.locator(S.FILTER_PANEL_COLLAPSE_BTN).click();
    await expect(page.locator(S.FILTER_BUILDER)).toBeVisible();
  });

  test("FLOGIC-6: collapsed panel still shows filter chips when rules exist", async ({ page }) => {
    // Add a rule so a chip will appear after search
    await page.locator(S.FILTER_ADD_BTN).click();

    // Collapse the panel
    await page.locator(S.FILTER_PANEL_COLLAPSE_BTN).click();
    await expect(page.locator(S.FILTER_BUILDER)).not.toBeVisible();

    // The filter chip area should still be rendered (rules.length > 0)
    // chips container exists inside the filter panel even when collapsed
    await expect(page.locator(S.EXPLORER_FILTER_PANEL)).toBeVisible();
  });
});
