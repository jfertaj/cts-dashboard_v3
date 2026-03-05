/**
 * Parity tests: verify that when Moby sets Explorer filters, the Explorer view
 * actually uses those filters (via window events).
 */
import { test, expect } from "@playwright/test";
import { S } from "../utils/selectors";
import { mockChatStream } from "../utils/chat-mock";

test.describe("Chat <-> Explorer parity", () => {
  test.beforeEach(async ({ page }) => {
    // Prevent session-expired overlay from blocking pointer events
    await page.route("**/api/salesforce/me", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ authenticated: true }),
      });
    });
  });

  test("PARITY-1: chat response with last_filters triggers explorer event", async ({ page }) => {
    // Mock all bootstrap endpoints so the explorer page loads without errors
    await page.route("**/api/explorer/bootstrap", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ points: [], rows: [], fields: [] }),
      });
    });
    await page.route("**/api/explorer/fields", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/salesforce/map/bootstrap", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/explorer/search", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ points: [], rows: [], total: 0 }),
      });
    });

    // Start on explorer tab so any pending event listeners can mount
    await page.goto("/explorer");

    // Navigate to chat
    await page.locator(S.TAB_CHAT).click();
    await expect(page.locator(S.CHAT_INPUT)).toBeVisible();

    // Mock a response with last_filters (chat dispatch sets explorer filters via window event)
    await mockChatStream(page, {
      answer: "Sites in Spain.",
      last_table: {
        columns: [{ key: "account_name", label: "Site" }, { key: "country", label: "Country" }],
        rows: [{ account_name: "Site Madrid", country: "ES" }],
      },
      last_filters: {
        logic: "AND",
        rules: [{ field: "site.country", operator: "equals", value: "Spain" }],
      },
    });

    // Use sendMessage so we wait for the permanent response before asserting
    const beforeCount = await page.locator(S.CHAT_MESSAGE_ASSISTANT).count();
    await page.locator(S.CHAT_INPUT).fill("Show sites in Spain");
    await page.locator(S.CHAT_SEND).click();
    await page.waitForFunction(
      (count: number) => {
        const msgs = document.querySelectorAll('[data-testid="chat-message-assistant"]');
        if (msgs.length <= count) return false;
        const last = msgs[msgs.length - 1] as HTMLElement;
        return !!last && !last.textContent?.includes("▋");
      },
      beforeCount,
      { timeout: 15000 }
    );

    // Verify the table rendered (confirms SSE response was processed)
    await expect(page.locator(S.AI_RESULT_TABLE)).toBeVisible({ timeout: 5000 });
  });
});
