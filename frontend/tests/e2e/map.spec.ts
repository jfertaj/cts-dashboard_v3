import { test, expect } from "@playwright/test";
import { S } from "../utils/selectors";

test.describe("Map — Explorer map view", () => {
  test.beforeEach(async ({ page }) => {
    // Prevent session-expired overlay from blocking pointer events
    await page.route("**/api/salesforce/me", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ authenticated: true }),
      });
    });
    // explorerBootstrap() calls /api/salesforce/map/bootstrap
    await page.route("**/api/salesforce/map/bootstrap", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });
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
        body: JSON.stringify([]),
      });
    });
    // explorerSearch() is called by the SWR bootstrap handler
    await page.route("**/api/explorer/search", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ points: [], rows: [], total: 0 }),
      });
    });
    await page.goto("/explorer");
  });

  test("MAP-1: map container is rendered on /explorer", async ({ page }) => {
    // Either the map loads or we see the VITE_GOOGLE_MAPS_API_KEY missing message
    // In test env, the API key is not set, so we expect the fallback message
    const mapOrFallback = page.locator(`${S.MAP_CONTAINER}, [class*="amber"]`);
    await expect(mapOrFallback.first()).toBeVisible({ timeout: 10000 });
  });

  test("MAP-2: map container exists in DOM", async ({ page }) => {
    // data-testid="map-container" is always rendered (even in fallback modes)
    // It may contain the GoogleMap or a fallback message
    const mapDiv = page.locator(S.MAP_CONTAINER);
    // Give the bootstrap time to settle
    await page.waitForTimeout(1000);
    // Container exists (even if empty/fallback)
    await expect(mapDiv).toHaveCount(1, { timeout: 5000 });
  });
});
