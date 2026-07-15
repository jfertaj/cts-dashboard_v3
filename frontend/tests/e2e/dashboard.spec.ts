import { test, expect } from "@playwright/test";
import { S } from "../utils/selectors";

test.describe("Dashboard navigation", () => {
  test.beforeEach(async ({ page }) => {
    // Prevent session-expired overlay from blocking pointer events
    await page.route("**/api/auth/me", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ authenticated: true }),
      });
    });
  });

  test("loads homepage and shows header tabs", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator(S.TAB_UPLOAD)).toBeVisible();
    await expect(page.locator(S.TAB_EXPLORER)).toBeVisible();
    await expect(page.locator(S.TAB_CHAT)).toBeVisible();
  });

  test("navigates to /explorer via URL", async ({ page }) => {
    await page.goto("/explorer");
    await expect(page.locator(S.EXPLORER_FILTER_PANEL)).toBeVisible();
  });

  test("navigates to /chat via URL", async ({ page }) => {
    await page.goto("/chat");
    await expect(page.locator(S.CHAT_CONTAINER)).toBeVisible();
  });

  test("clicking tab-explorer shows explorer view", async ({ page }) => {
    await page.goto("/");
    await page.locator(S.TAB_EXPLORER).click();
    await expect(page.locator(S.EXPLORER_FILTER_PANEL)).toBeVisible();
  });

  test("clicking tab-chat shows chat view", async ({ page }) => {
    await page.goto("/");
    await page.locator(S.TAB_CHAT).click();
    await expect(page.locator(S.CHAT_CONTAINER)).toBeVisible();
    await expect(page.locator(S.CHAT_INPUT)).toBeVisible();
    await expect(page.locator(S.CHAT_SEND)).toBeVisible();
  });

  test("chat input is disabled while busy (send btn)", async ({ page }) => {
    await page.goto("/chat");
    // The send button should be disabled when input is empty
    await expect(page.locator(S.CHAT_SEND)).toBeDisabled();
  });

  test("chat send button enables when input has text", async ({ page }) => {
    await page.goto("/chat");
    await page.locator(S.CHAT_INPUT).fill("Hello");
    await expect(page.locator(S.CHAT_SEND)).toBeEnabled();
  });

  test("chat clear button is present", async ({ page }) => {
    await page.goto("/chat");
    await expect(page.locator(S.CHAT_CLEAR)).toBeVisible();
  });

  test("page title contains CTS", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveTitle(/CTS/i);
  });

  test("URL updates when switching tabs", async ({ page }) => {
    // Mock explorer APIs so the Explorer view doesn't block with a loading state
    // explorerBootstrap() calls /api/salesforce/map/bootstrap (not /api/explorer/bootstrap)
    await page.route("**/api/salesforce/map/bootstrap", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );
    await page.route("**/api/explorer/fields", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );
    await page.route("**/api/explorer/search", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ points: [], rows: [], total: 0 }) })
    );
    await page.goto("/");
    await page.locator(S.TAB_EXPLORER).click();
    await expect(page).toHaveURL(/\/explorer/);
    // Wait for the Explorer bootstrap to complete (removes the full-screen loading overlay)
    // so that TAB_CHAT is not blocked by the z-[9999] LoadingOverlay
    await expect(page.locator(S.EXPLORER_SEARCH_BTN)).toBeEnabled({ timeout: 15000 });
    await page.locator(S.TAB_CHAT).click();
    await expect(page).toHaveURL(/\/chat/);
  });
});
