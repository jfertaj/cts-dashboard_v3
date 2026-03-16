import { test, expect } from "@playwright/test";
import { S } from "../utils/selectors";

test.describe("Resilience — network errors and edge cases", () => {
  test.beforeEach(async ({ page }) => {
    // Default: prevent session-expired overlay from blocking pointer events
    // Individual tests can override this (e.g. RESIL-5 tests the 401 case explicitly)
    await page.route("**/api/salesforce/me", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ authenticated: true }),
      });
    });
  });

  test("RESIL-1: chat gracefully handles 500 error from stream endpoint", async ({ page }) => {
    await page.route("**/api/ai/chat/stream", async (route) => {
      await route.fulfill({ status: 500, body: "Internal Server Error" });
    });
    await page.goto("/chat");

    await page.locator(S.CHAT_INPUT).fill("Hello");
    await page.locator(S.CHAT_SEND).click();

    // The app should not crash — input should become re-enabled eventually
    await page.waitForFunction(
      () => !(document.querySelector('[data-testid="chat-input"]') as HTMLInputElement)?.disabled,
      { timeout: 10000 }
    );

    // Chat messages area still exists
    await expect(page.locator(S.CHAT_MESSAGES)).toBeVisible();
  });

  test("RESIL-2: chat handles slow stream (timeout simulation)", async ({ page }) => {
    // Load chat page FIRST (before registering the slow route so navigation isn't delayed)
    await page.goto("/chat");
    await expect(page.locator(S.CHAT_INPUT)).toBeVisible({ timeout: 10000 });

    // NOW register the slow stream route
    let routeHandled = false;
    await page.route("**/api/ai/chat/stream", async (route) => {
      routeHandled = true;
      await new Promise((r) => setTimeout(r, 4000));
      await route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
        body: `data: ${JSON.stringify({ type: "done", answer: "Finally!", last_table: null, last_filters: null })}\n\n`,
      });
    });

    await page.locator(S.CHAT_INPUT).fill("Test slow response");
    await page.locator(S.CHAT_SEND).click();

    // User message should appear immediately
    await expect(page.locator(S.CHAT_MESSAGE_USER).last()).toBeVisible({ timeout: 2000 });

    // Wait for eventual response
    const lastAssistant = page.locator(S.CHAT_MESSAGE_ASSISTANT).last();
    await expect(lastAssistant).toContainText("Finally", { timeout: 10000 });
    expect(routeHandled).toBe(true);
  });

  test("RESIL-3: explorer search handles 401 gracefully", async ({ page }) => {
    // explorerBootstrap() calls /api/salesforce/map/bootstrap (not /api/explorer/bootstrap)
    await page.route("**/api/salesforce/map/bootstrap", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/explorer/fields", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/explorer/search", async (route) => {
      await route.fulfill({ status: 401, body: "Unauthorized" });
    });

    await page.goto("/explorer");

    // 401 from explorerSearch -> ExplorerView renders its own "session expired" banner.
    // Filter panel should be visible (app didn't crash) and loading is done (no overlay).
    await expect(page.locator(S.EXPLORER_FILTER_PANEL)).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/salesforce session expired/i)).toBeVisible({ timeout: 5000 });
  });

  test("RESIL-4: chat handles malformed SSE gracefully", async ({ page }) => {
    await page.route("**/api/ai/chat/stream", async (route) => {
      await route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
        body: "data: INVALID_JSON\n\ndata: {}\n\n",
      });
    });

    await page.goto("/chat");
    await page.locator(S.CHAT_INPUT).fill("Test malformed");
    await page.locator(S.CHAT_SEND).click();

    // App should not crash
    await page.waitForTimeout(3000);
    await expect(page.locator(S.CHAT_CONTAINER)).toBeVisible();
  });

  test("RESIL-5: app loads even when SF /me returns 401", async ({ page }) => {
    await page.route("**/api/salesforce/me", async (route) => {
      await route.fulfill({ status: 401, body: "Unauthorized" });
    });
    await page.goto("/");

    // Header should still render
    await expect(page.locator(S.TAB_UPLOAD)).toBeVisible();
    await expect(page.locator(S.TAB_EXPLORER)).toBeVisible();
    await expect(page.locator(S.TAB_CHAT)).toBeVisible();
  });
});
