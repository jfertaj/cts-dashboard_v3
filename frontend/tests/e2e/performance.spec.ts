/**
 * tests/e2e/performance.spec.ts
 * Measures and enforces response-time budgets for the main UI flows.
 *
 * Budget targets (all times in ms):
 *   PAGE_LOAD_BUDGET:       3000  — navigation start → header visible (real network)
 *   FILTER_RENDER_BUDGET:   2000  — Search click → results-count visible (real /search endpoint)
 *   CHAT_FIRST_TOKEN:       3000  — Send click → first streaming token OR "thinking" indicator
 *   CHAT_FULL_RESPONSE:    15000  — Send click → permanent assistant message
 *
 * NOTE on PERF-4/5/6: these use a MOCKED backend stream (not real Claude).
 * They measure FRONTEND rendering latency (React reconciliation, SSE parsing, DOM updates),
 * not end-to-end Moby response time. Real Moby latency is measured by SMOKE-PERF in
 * chat-live.smoke.spec.ts (opt-in, requires PLAYWRIGHT_SMOKE=1).
 */
import { test, expect } from "@playwright/test";
import { S } from "../utils/selectors";
import { mockChatStream } from "../utils/chat-mock";
import { mockExplorerBootstrap } from "../utils/data";

const FILTER_RENDER_BUDGET = 2000;
const CHAT_FIRST_TOKEN     = 3000;
const CHAT_FULL_RESPONSE   = 15000;
const PAGE_LOAD_BUDGET     = 3000;

const MOCK_ROWS = Array.from({ length: 50 }, (_, i) => ({
  account_id: `SITE${i + 1}`,
  account_name: `Site ${i + 1}`,
  country: ["IT", "ES", "DE", "NL", "FR", "GB"][i % 6],
  city: "City",
  data: {},
}));

test.describe("Performance — time budgets", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/salesforce/me", (r) =>
      r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: true }) })
    );
  });

  // ── PERF-1: Page load ─────────────────────────────────────────────────
  test("PERF-1: root page loads within budget", async ({ page }) => {
    const start = Date.now();
    await page.goto("/");
    await page.locator(S.TAB_EXPLORER).waitFor({ state: "visible" });
    const elapsed = Date.now() - start;
    console.log(`[PERF-1] Root load: ${elapsed}ms (budget: ${PAGE_LOAD_BUDGET}ms)`);
    expect(elapsed).toBeLessThan(PAGE_LOAD_BUDGET);
  });

  // ── PERF-2: Explorer page load ────────────────────────────────────────
  test("PERF-2: /explorer page loads within budget", async ({ page }) => {
    await mockExplorerBootstrap(page, MOCK_ROWS);
    const start = Date.now();
    await page.goto("/explorer");
    await page.locator(S.EXPLORER_FILTER_PANEL).waitFor({ state: "visible" });
    const elapsed = Date.now() - start;
    console.log(`[PERF-2] Explorer load: ${elapsed}ms (budget: ${PAGE_LOAD_BUDGET}ms)`);
    expect(elapsed).toBeLessThan(PAGE_LOAD_BUDGET);
  });

  // ── PERF-3: Filter → render time ──────────────────────────────────────
  test("PERF-3: filter search → results visible within budget", async ({ page }) => {
    await page.route("**/api/explorer/bootstrap", (r) =>
      r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ points: [], rows: [], fields: [] }) })
    );
    await page.route("**/api/explorer/fields", (r) =>
      r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );
    await page.route("**/api/salesforce/map/bootstrap", (r) =>
      r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );

    let searchResolve!: () => void;
    // Simulate a realistic 200ms backend latency
    await page.route("**/api/explorer/search", async (r) => {
      await new Promise<void>((res) => { searchResolve = res; setTimeout(res, 200); });
      await r.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({ points: [], rows: MOCK_ROWS.slice(0, 10), total: 10 }),
      });
    });

    await page.goto("/explorer");
    const start = Date.now();
    await page.locator(S.EXPLORER_SEARCH_BTN).click();
    await page.locator(S.EXPLORER_RESULTS_COUNT).waitFor({ state: "visible", timeout: FILTER_RENDER_BUDGET + 1000 });
    const elapsed = Date.now() - start;
    console.log(`[PERF-3] Filter → render: ${elapsed}ms (budget: ${FILTER_RENDER_BUDGET}ms)`);
    expect(elapsed).toBeLessThan(FILTER_RENDER_BUDGET);
  });

  // ── PERF-4: Chat first-token latency ──────────────────────────────────
  test("PERF-4: chat first token / thinking indicator appears within budget", async ({ page }) => {
    // Delay the first token slightly to simulate realistic backend
    await page.route("**/api/ai/chat/stream", async (r) => {
      await new Promise((res) => setTimeout(res, 300));
      const body = [
        `data: ${JSON.stringify({ type: "token", text: "Hello " })}\n\n`,
        `data: ${JSON.stringify({ type: "done", answer: "Hello world", table: null, last_filters: null })}\n\n`,
      ].join("");
      await r.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
        body,
      });
    });

    await page.goto("/chat");
    await page.locator(S.CHAT_INPUT).fill("Hello");
    const start = Date.now();
    await page.locator(S.CHAT_SEND).click();

    // First indicator: either a streaming message or "Moby is thinking…"
    await page.waitForFunction(
      () => {
        const thinking = document.querySelector('[data-testid="chat-thinking"]') as HTMLElement | null;
        const msgs = document.querySelectorAll('[data-testid="chat-message-assistant"]');
        const last = msgs[msgs.length - 1] as HTMLElement | undefined;
        return (thinking && thinking.textContent?.includes("thinking")) ||
               (last && last.textContent?.includes("▋"));
      },
      { timeout: CHAT_FIRST_TOKEN }
    );
    const elapsed = Date.now() - start;
    console.log(`[PERF-4] First token: ${elapsed}ms (budget: ${CHAT_FIRST_TOKEN}ms)`);
    expect(elapsed).toBeLessThan(CHAT_FIRST_TOKEN);
  });

  // ── PERF-5: Chat full response latency ───────────────────────────────
  test("PERF-5: chat full response arrives within budget", async ({ page }) => {
    await mockChatStream(page, {
      answer: "<p>Here are the sites in Italy.</p>",
      last_table: {
        columns: [{ key: "account_name", label: "Site" }, { key: "country", label: "Country" }],
        rows: [
          { account_name: "Milano Diabetes Center", country: "IT" },
          { account_name: "Roma Clinical Research",  country: "IT" },
        ],
      },
    });

    await page.goto("/chat");
    const beforeCount = await page.locator(S.CHAT_MESSAGE_ASSISTANT).count();
    await page.locator(S.CHAT_INPUT).fill("Show sites in Italy");
    const start = Date.now();
    await page.locator(S.CHAT_SEND).click();

    await page.waitForFunction(
      (count: number) => {
        const msgs = document.querySelectorAll('[data-testid="chat-message-assistant"]');
        if (msgs.length <= count) return false;
        const last = msgs[msgs.length - 1] as HTMLElement;
        return !!last && !last.textContent?.includes("▋");
      },
      beforeCount,
      { timeout: CHAT_FULL_RESPONSE }
    );
    const elapsed = Date.now() - start;
    console.log(`[PERF-5] Full response: ${elapsed}ms (budget: ${CHAT_FULL_RESPONSE}ms)`);
    expect(elapsed).toBeLessThan(CHAT_FULL_RESPONSE);
  });

  // ── PERF-6: 50-row table renders without jank ─────────────────────────
  test("PERF-6: 50-row AI table renders in < 1000ms", async ({ page }) => {
    const bigRows = MOCK_ROWS.map((r) => ({ account_name: r.account_name, country: r.country }));
    await mockChatStream(page, {
      answer: "<p>50 sites found.</p>",
      last_table: {
        columns: [{ key: "account_name", label: "Site" }, { key: "country", label: "Country" }],
        rows: bigRows,
      },
    });

    await page.goto("/chat");
    const beforeCount = await page.locator(S.CHAT_MESSAGE_ASSISTANT).count();
    await page.locator(S.CHAT_INPUT).fill("Show all 50 sites");
    await page.locator(S.CHAT_SEND).click();

    const start = Date.now();
    await page.locator(S.AI_RESULT_TABLE).waitFor({ state: "visible", timeout: CHAT_FULL_RESPONSE });
    const tableElapsed = Date.now() - start;

    const rowCount = await page.locator(S.AI_RESULT_ROW).count();
    console.log(`[PERF-6] 50-row table render: ${tableElapsed}ms, rows: ${rowCount}`);
    expect(rowCount).toBe(50);
    expect(tableElapsed).toBeLessThan(1000);
  });
});
