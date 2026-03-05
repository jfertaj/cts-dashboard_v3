import { test, expect } from "@playwright/test";
import { S } from "../utils/selectors";
import { mockChatStream } from "../utils/chat-mock";
import chatFixtures from "../fixtures/chat_fixtures.json";

const FIXTURES = chatFixtures.prompts;

test.describe("AIResultTable — chat table interactions", () => {
  test.beforeEach(async ({ page }) => {
    // Prevent session-expired overlay from blocking pointer events
    await page.route("**/api/salesforce/me", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ authenticated: true }),
      });
    });
    await page.goto("/chat");
  });

  test("TABLE-1: table renders correct number of rows for Italy fixture", async ({ page }) => {
    const fixture = FIXTURES.find(f => f.id === "sites-italy")!;
    await mockChatStream(page, {
      answer: fixture.expectedAnswer,
      last_table: fixture.expectedTable,
    });

    await page.locator(S.CHAT_INPUT).fill(fixture.prompt);
    await page.locator(S.CHAT_SEND).click();

    await expect(page.locator(S.AI_RESULT_TABLE)).toBeVisible({ timeout: 15000 });
    await expect(page.locator(S.AI_RESULT_ROW)).toHaveCount(fixture.expectedTable!.rows.length);
  });

  test("TABLE-2: table renders column headers", async ({ page }) => {
    const fixture = FIXTURES.find(f => f.id === "sites-italy")!;
    await mockChatStream(page, {
      answer: fixture.expectedAnswer,
      last_table: fixture.expectedTable,
    });

    await page.locator(S.CHAT_INPUT).fill(fixture.prompt);
    await page.locator(S.CHAT_SEND).click();

    await expect(page.locator(S.AI_RESULT_TABLE)).toBeVisible({ timeout: 15000 });
    // Check a column header is present
    await expect(page.locator(`${S.AI_RESULT_TABLE} th`).first()).toBeVisible();
  });

  test("TABLE-3: no table rendered when answer has no table data", async ({ page }) => {
    const fixture = FIXTURES.find(f => f.id === "no-results")!;
    await mockChatStream(page, { answer: fixture.expectedAnswer, last_table: null });

    await page.locator(S.CHAT_INPUT).fill(fixture.prompt);
    await page.locator(S.CHAT_SEND).click();

    // Wait for assistant to respond
    await page.waitForTimeout(2000);
    await expect(page.locator(S.AI_RESULT_TABLE)).toHaveCount(0);
  });

  test("TABLE-4: cell values from mock data are visible", async ({ page }) => {
    const fixture = FIXTURES.find(f => f.id === "sites-italy")!;
    await mockChatStream(page, {
      answer: fixture.expectedAnswer,
      last_table: fixture.expectedTable,
    });

    await page.locator(S.CHAT_INPUT).fill(fixture.prompt);
    await page.locator(S.CHAT_SEND).click();

    await expect(page.locator(S.AI_RESULT_TABLE)).toBeVisible({ timeout: 15000 });
    // First row, first cell should have "Site Milano" or similar
    const firstCell = page.locator(S.AI_RESULT_CELL).first();
    await expect(firstCell).not.toBeEmpty();
  });
});
