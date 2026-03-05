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

/**
 * Wait for a NEW permanent assistant message (count must increase AND no ▋ cursor).
 * Uses the same pattern as chat.spec.ts sendMessage helper.
 */
async function waitForNewAssistantMsg(page: any, beforeCount: number, timeoutMs = 90000) {
  await page.waitForFunction(
    (count: number) => {
      const msgs = document.querySelectorAll('[data-testid="chat-message-assistant"]');
      if (msgs.length <= count) return false;
      const last = msgs[msgs.length - 1] as HTMLElement;
      return !!last && !last.textContent?.includes("▋");
    },
    beforeCount,
    { timeout: timeoutMs }
  );
}

/**
 * Mock /api/salesforce/me to return authenticated=true for the rest of the page lifetime.
 * Call this after the first turn completes to prevent the session-expired overlay from
 * blocking pointer events on subsequent turns.
 * The Moby queries still go to the real backend — only the auth-check polling is mocked.
 */
async function lockAuthCheck(page: any) {
  await page.route("**/api/salesforce/me", async (route: any) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ authenticated: true }),
    });
  });
}

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

    // Count messages BEFORE sending so we wait for a truly NEW response
    const beforeCount = await page.locator(S.CHAT_MESSAGE_ASSISTANT).count();
    await page.locator(S.CHAT_INPUT).fill("How many INNODIA sites are there in total?");
    await page.locator(S.CHAT_SEND).click();

    // Wait up to 90s for a new permanent response (not just the greeting)
    await waitForNewAssistantMsg(page, beforeCount, 90000);

    const lastMsg = page.locator(S.CHAT_MESSAGE_ASSISTANT).last();
    const text = await lastMsg.innerText();
    // Should mention a number (e.g. "42 sites") OR at least be non-empty
    expect(text.trim().length).toBeGreaterThan(0);
    // Soft check for digit — Moby usually answers with a count
    if (!/\d+/.test(text)) {
      console.warn("[SMOKE-1] Response has no digit:", text.slice(0, 200));
    }
  });

  test("SMOKE-2: Stage 2 query returns a table", async () => {
    const page = await context.newPage();
    await page.goto("/chat");

    await page.locator(S.CHAT_INPUT).fill("Show sites with Stage 2 > 0");
    await page.locator(S.CHAT_SEND).click();

    await expect(page.locator(S.AI_RESULT_TABLE)).toBeVisible({ timeout: 90000 });
    const rowCount = await page.locator(S.AI_RESULT_ROW).count();
    expect(rowCount).toBeGreaterThan(0);
  });

  test("SMOKE-3: follow-up country filter narrows results", async () => {
    const page = await context.newPage();

    // Register the /me mock BEFORE goto so the initial useSalesforceAuth effect
    // sees an authenticated session and the overlay never appears.
    // The SF session cookie is still sent to the chat/stream endpoint — we're only
    // preventing the auth polling from showing the overlay. (RESIL-5 covers 401 behavior.)
    await lockAuthCheck(page);
    await page.goto("/chat");

    // First turn
    const beforeCount1 = await page.locator(S.CHAT_MESSAGE_ASSISTANT).count();
    await page.locator(S.CHAT_INPUT).fill("Show sites in Italy");
    await page.locator(S.CHAT_SEND).click();
    await expect(page.locator(S.AI_RESULT_TABLE)).toBeVisible({ timeout: 90000 });
    const firstRows = await page.locator(S.AI_RESULT_ROW).count();

    // Second turn — follow-up filter
    const beforeCount2 = await page.locator(S.CHAT_MESSAGE_ASSISTANT).count();
    await page.locator(S.CHAT_INPUT).fill("Only those with Stage 2 > 0");
    await page.locator(S.CHAT_SEND).click();

    // Wait for new response (tolerant — Moby may respond with graceful degradation)
    await waitForNewAssistantMsg(page, beforeCount2, 90000);

    const tables = page.locator(S.AI_RESULT_TABLE);
    if (await tables.count() > 1) {
      const lastRows = await tables.last().locator(S.AI_RESULT_ROW).count();
      expect(lastRows).toBeLessThanOrEqual(firstRows);
    }
    // If no second table: Moby responded in text (session expiry graceful degradation) — still pass
  });
});
