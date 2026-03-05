/**
 * LIVE SMOKE TESTS — require real SF session + Claude API.
 * Run with: PLAYWRIGHT_SMOKE=1 SF_SESSION_COOKIE=<value> npx playwright test --grep @smoke
 */
import { test, expect, BrowserContext } from "@playwright/test";
import { S } from "../utils/selectors";
import { setAuthCookie } from "../utils/auth";

const SMOKE = !!process.env.PLAYWRIGHT_SMOKE;

// Skip entire suite if not opted in
test.skip(!SMOKE, "Smoke tests disabled — set PLAYWRIGHT_SMOKE=1 to enable");

test.describe("Chat — live smoke @smoke", () => {
  let context: BrowserContext;

  test.beforeEach(async ({ browser, baseURL }) => {
    // Pass ignoreHTTPSErrors explicitly — browser.newContext() does not inherit
    // the value from playwright.config.ts `use:` block (only page-level contexts do)
    context = await browser.newContext({ ignoreHTTPSErrors: true });
    if (process.env.SF_SESSION_COOKIE) {
      await setAuthCookie(context, baseURL!);
    }
  });

  test.afterEach(async () => {
    await context.close();
  });

  test("SMOKE-1: sends a basic sites query and gets a non-empty response", async () => {
    const page = await context.newPage();
    await page.goto("/chat");

    await page.locator(S.CHAT_INPUT).fill("How many INNODIA sites are there in total?");
    await page.locator(S.CHAT_SEND).click();

    // Wait up to 60s for Claude + tools
    const lastMsg = page.locator(S.CHAT_MESSAGE_ASSISTANT).last();
    await expect(lastMsg).not.toBeEmpty({ timeout: 60000 });

    // Should mention a number
    const text = await lastMsg.innerText();
    expect(/\d+/.test(text)).toBe(true);
  });

  test("SMOKE-2: Stage 2 query returns a table", async () => {
    const page = await context.newPage();
    await page.goto("/chat");

    await page.locator(S.CHAT_INPUT).fill("Show sites with Stage 2 > 0");
    await page.locator(S.CHAT_SEND).click();

    await expect(page.locator(S.AI_RESULT_TABLE)).toBeVisible({ timeout: 60000 });
    const rowCount = await page.locator(S.AI_RESULT_ROW).count();
    expect(rowCount).toBeGreaterThan(0);
  });

  test("SMOKE-3: follow-up country filter narrows results", async () => {
    const page = await context.newPage();
    await page.goto("/chat");

    await page.locator(S.CHAT_INPUT).fill("Show sites in Italy");
    await page.locator(S.CHAT_SEND).click();
    await expect(page.locator(S.AI_RESULT_TABLE)).toBeVisible({ timeout: 60000 });
    const firstRows = await page.locator(S.AI_RESULT_ROW).count();

    // Follow up with a narrowing filter
    await page.locator(S.CHAT_INPUT).fill("Only those with Stage 2 > 0");
    await page.locator(S.CHAT_SEND).click();
    // Wait for a new table or updated response
    await page.waitForTimeout(2000);
    const tables = page.locator(S.AI_RESULT_TABLE);
    if (await tables.count() > 1) {
      const lastRows = await tables.last().locator(S.AI_RESULT_ROW).count();
      expect(lastRows).toBeLessThanOrEqual(firstRows);
    }
  });
});
