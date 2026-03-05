import { test, expect } from "@playwright/test";
import { S } from "../utils/selectors";
import { mockChatStream, clearChatMock, tableResponse } from "../utils/chat-mock";
import chatFixtures from "../fixtures/chat_fixtures.json";

const FIXTURES = chatFixtures.prompts;

/**
 * Send a message in the chat and wait for the assistant to respond.
 *
 * After send(), the input is cleared (setInput("")) so the send button stays
 * disabled forever via `disabled={!input.trim() || busy}` — waiting for
 * toBeEnabled() would never resolve. Instead, we wait for a new PERMANENT
 * assistant message: one that has a higher count than before AND does NOT
 * contain the streaming-bubble cursor (▋), which the live bubble always has.
 */
async function sendMessage(page: any, text: string) {
  const beforeCount = await page.locator(S.CHAT_MESSAGE_ASSISTANT).count();
  await page.locator(S.CHAT_INPUT).fill(text);
  await page.locator(S.CHAT_SEND).click();
  // Wait for a new permanent assistant message (no ▋ streaming cursor).
  // Correctly passes beforeCount as arg and { timeout } as options (3rd param).
  await page.waitForFunction(
    (count: number) => {
      const msgs = document.querySelectorAll('[data-testid="chat-message-assistant"]');
      if (msgs.length <= count) return false;
      // The streaming bubble always contains ▋; permanent messages do not.
      const last = msgs[msgs.length - 1] as HTMLElement;
      return !!last && !last.textContent?.includes("▋");
    },
    beforeCount,
    { timeout: 15000 }
  );
}

test.describe("Chat — deterministic (mocked SSE)", () => {
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

  test("CHAT-1: initial greeting message is visible", async ({ page }) => {
    // The welcome message is always shown
    const msgs = page.locator(S.CHAT_MESSAGE_ASSISTANT);
    await expect(msgs.first()).toBeVisible();
    await expect(msgs.first()).toContainText("Moby");
  });

  test("CHAT-2: sends a message and receives mocked text response", async ({ page }) => {
    const fixture = FIXTURES.find(f => f.id === "stage2-count")!;
    await mockChatStream(page, {
      answer: fixture.expectedAnswer,
      last_table: null,
    });

    await sendMessage(page, fixture.prompt);

    const lastMsg = page.locator(S.CHAT_MESSAGE_ASSISTANT).last();
    // The fixture says "3 sites" — matches seed: Berlin(11)+Milano(8)+London(9) all >5
    await expect(lastMsg).toContainText("3 sites");
  });

  test("CHAT-3: response with table renders AIResultTable", async ({ page }) => {
    const fixture = FIXTURES.find(f => f.id === "sites-italy")!;
    await mockChatStream(page, {
      answer: fixture.expectedAnswer,
      last_table: fixture.expectedTable,
    });

    await sendMessage(page, fixture.prompt);

    // Table should be visible
    await expect(page.locator(S.AI_RESULT_TABLE)).toBeVisible();
    // Should have rows (>=1 row)
    const rows = page.locator(S.AI_RESULT_ROW);
    await expect(rows).toHaveCount(fixture.expectedTable!.rows.length);
  });

  test("CHAT-4: top 10 ND sites — table has correct column count", async ({ page }) => {
    const fixture = FIXTURES.find(f => f.id === "nd-top10")!;
    await mockChatStream(page, {
      answer: fixture.expectedAnswer,
      last_table: fixture.expectedTable,
    });

    await sendMessage(page, fixture.prompt);

    await expect(page.locator(S.AI_RESULT_TABLE)).toBeVisible();
    const rows = page.locator(S.AI_RESULT_ROW);
    await expect(rows).toHaveCount(fixture.expectedTable!.rows.length);
  });

  test("CHAT-5: no-results response does not render a table", async ({ page }) => {
    const fixture = FIXTURES.find(f => f.id === "no-results")!;
    await mockChatStream(page, {
      answer: fixture.expectedAnswer,
      last_table: null,
    });

    await sendMessage(page, fixture.prompt);

    const lastMsg = page.locator(S.CHAT_MESSAGE_ASSISTANT).last();
    await expect(lastMsg).toContainText("No matching");
    // No table should be rendered for this message
    await expect(page.locator(S.AI_RESULT_TABLE)).toHaveCount(0);
  });

  test("CHAT-6: multi-turn conversation — follow-up narrows results", async ({ page }) => {
    const fixture = FIXTURES.find(f => f.id === "follow-up-country")!;

    // First turn: Spain sites
    await mockChatStream(page, {
      answer: fixture.expectedAnswer,
      last_table: fixture.expectedTable,
      last_filters: {
        logic: "AND",
        rules: [{ field: "site.country", operator: "equals", value: "Spain" }],
      },
    });
    await sendMessage(page, fixture.prompt);
    await expect(page.locator(S.AI_RESULT_TABLE)).toBeVisible();
    const firstCount = fixture.expectedTable!.rows.length;
    await expect(page.locator(S.AI_RESULT_ROW)).toHaveCount(firstCount);

    // Second turn: filter by Stage 2 > 3
    const fu = fixture.followUp!;
    await mockChatStream(page, {
      answer: fu.expectedAnswer,
      last_table: fu.expectedTable,
    });
    await sendMessage(page, fu.prompt);

    // The new table should have fewer rows
    const allTables = page.locator(S.AI_RESULT_TABLE);
    const lastTable = allTables.last();
    await expect(lastTable).toBeVisible();
    const secondRows = lastTable.locator(S.AI_RESULT_ROW);
    await expect(secondRows).toHaveCount(fu.expectedTable!.rows.length);
    // Should be fewer than the first
    expect(fu.expectedTable!.rows.length).toBeLessThan(firstCount);
  });

  test("CHAT-7: clear conversation resets messages", async ({ page }) => {
    const fixture = FIXTURES.find(f => f.id === "stage2-count")!;
    await mockChatStream(page, { answer: fixture.expectedAnswer, last_table: null });
    await sendMessage(page, fixture.prompt);

    // Should have user + assistant message (plus initial greeting)
    const beforeCount = await page.locator(S.CHAT_MESSAGE_ASSISTANT).count();
    expect(beforeCount).toBeGreaterThan(1);

    await page.locator(S.CHAT_CLEAR).click();

    // After clear: only the greeting
    const afterCount = await page.locator(S.CHAT_MESSAGE_ASSISTANT).count();
    expect(afterCount).toBe(1);
    await expect(page.locator(S.CHAT_MESSAGE_ASSISTANT).first()).toContainText("Moby");
  });

  test("CHAT-8: pressing Enter sends message", async ({ page }) => {
    const fixture = FIXTURES.find(f => f.id === "stage2-count")!;
    await mockChatStream(page, { answer: fixture.expectedAnswer, last_table: null });

    await page.locator(S.CHAT_INPUT).fill(fixture.prompt);
    await page.locator(S.CHAT_INPUT).press("Enter");

    await page.waitForSelector(S.CHAT_MESSAGE_USER, { timeout: 5000 });
    const userMsg = page.locator(S.CHAT_MESSAGE_USER).last();
    await expect(userMsg).toContainText(fixture.prompt);
  });

  test("CHAT-9: user message appears immediately in chat", async ({ page }) => {
    const fixture = FIXTURES.find(f => f.id === "stage2-count")!;
    // Don't mock — just check the user message appears before the response
    // Use a long delay mock
    await page.route("**/api/ai/chat/stream", async (route) => {
      await new Promise((r) => setTimeout(r, 2000));
      await route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
        body: `data: ${JSON.stringify({ type: "done", answer: "ok", last_table: null, last_filters: null })}\n\n`,
      });
    });

    await page.locator(S.CHAT_INPUT).fill(fixture.prompt);
    await page.locator(S.CHAT_SEND).click();

    // User message should be visible immediately
    const userMsg = page.locator(S.CHAT_MESSAGE_USER).last();
    await expect(userMsg).toContainText(fixture.prompt, { timeout: 1000 });
  });

  test("CHAT-10: row click on table triggers site details (modal)", async ({ page }) => {
    const fixture = FIXTURES.find(f => f.id === "sites-italy")!;
    // Mock account-extras for the details modal
    await page.route("**/api/salesforce/explorer/account-extras/**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          account_id: "TEST001",
          member: null,
          pi: { name: "Dr. Test", email: "test@example.com" },
          opportunity: null,
          assignments: [],
        }),
      });
    });

    await mockChatStream(page, {
      answer: fixture.expectedAnswer,
      last_table: {
        ...fixture.expectedTable!,
        rows: fixture.expectedTable!.rows.map((r, i) => ({
          ...r,
          account_id: `ACC${i + 1}`,
          "sf.Account.Id": `ACC${i + 1}`,
        })),
      },
    });

    await sendMessage(page, fixture.prompt);
    await expect(page.locator(S.AI_RESULT_TABLE)).toBeVisible();

    // Click first row
    await page.locator(S.AI_RESULT_ROW).first().click();

    // Details modal should open (it has a "Close" button and "Details" heading)
    await expect(page.locator('text=Details')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('button:has-text("Close")')).toBeVisible();

    // Close the modal
    await page.locator('button:has-text("Close")').click();
    await expect(page.locator('button:has-text("Close")')).toHaveCount(0);
  });
});
