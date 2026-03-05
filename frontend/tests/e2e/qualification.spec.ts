/**
 * tests/e2e/qualification.spec.ts
 * Verifies qualification-related behavior:
 * - Only 1 qual visit record exists (by design, schema supports more)
 * - Chat correctly reflects "1 site has qual data"
 * - Explorer qual.* filters work
 * - Partial sync edge case (qual without matching SF account)
 */
import { test, expect } from "@playwright/test";
import { S } from "../utils/selectors";
import { mockChatStream } from "../utils/chat-mock";
import { mockExplorerBootstrap, waitForAssistantReply } from "../utils/data";
import chatFixtures from "../fixtures/chat_fixtures.json";
import seedData from "../fixtures/seed_expectations.json";

const FIXTURES = chatFixtures.prompts;

// Standard auth mock
const mockAuth = (page: Parameters<typeof mockExplorerBootstrap>[0]) =>
  page.route("**/api/salesforce/me", (r) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: true }) })
  );

// Single qual visit row for mock API responses
const QUAL_VISIT = seedData.qualification.visits[0];

test.describe("Qualification — visit data", () => {
  test.beforeEach(async ({ page }) => {
    await mockAuth(page);
  });

  // ── QUAL-1: Chat reflects exactly 1 qual visit ──────────────────────────
  test("QUAL-1: chat reports exactly 1 qualification visit site", async ({ page }) => {
    const fixture = FIXTURES.find((f) => f.id === "qual-sites")!;
    await mockChatStream(page, {
      answer: fixture.expectedAnswer,
      last_table: fixture.expectedTable,
    });

    await page.goto("/chat");
    const beforeCount = await page.locator(S.CHAT_MESSAGE_ASSISTANT).count();
    await page.locator(S.CHAT_INPUT).fill(fixture.prompt);
    await page.locator(S.CHAT_SEND).click();
    await waitForAssistantReply(page, beforeCount);

    // Table must show exactly 1 row
    await expect(page.locator(S.AI_RESULT_TABLE)).toBeVisible({ timeout: 10000 });
    await expect(page.locator(S.AI_RESULT_ROW)).toHaveCount(1);

    // That row must be Milano (the only site with qual data in seed)
    const firstCell = page.locator(S.AI_RESULT_CELL).first();
    await expect(firstCell).toContainText(QUAL_VISIT.site_name);
  });

  // ── QUAL-2: Explorer search with qual.onsite_pharmacy filter returns 1 site ─
  test("QUAL-2: explorer qual.onsite_pharmacy filter returns only sites with pharmacy", async ({ page }) => {
    // One site has pharmacy (SITE001), others don't
    const pharmacyRow = {
      account_id: "SITE001",
      account_name: "Milano Diabetes Center",
      country: "IT",
      city: "Milan",
      data: { "qual.onsite_pharmacy": true },
    };

    await page.route("**/api/explorer/bootstrap", (r) =>
      r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ points: [], rows: [], fields: [] }) })
    );
    await page.route("**/api/explorer/fields", (r) =>
      r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([
        { key: "qual.onsite_pharmacy", label: "On-site pharmacy", type: "boolean", source: "qual" },
        { key: "site.country", label: "Country", type: "string", source: "site" },
      ]) })
    );
    await page.route("**/api/salesforce/map/bootstrap", (r) =>
      r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );
    await page.route("**/api/explorer/search", (r) =>
      r.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ points: [], rows: [pharmacyRow], total: 1 }),
      })
    );

    await page.goto("/explorer");
    await page.locator(S.EXPLORER_SEARCH_BTN).click();
    const count = page.locator(S.EXPLORER_RESULTS_COUNT);
    await expect(count).toBeVisible({ timeout: 5000 });
    await expect(count).toContainText("1");
  });

  // ── QUAL-3: Chat pharmacy query returns only qual-linked sites ──────────
  test("QUAL-3: chat pharmacy query returns qual site", async ({ page }) => {
    await mockChatStream(page, {
      answer: "<p>Sites with on-site pharmacy.</p>",
      last_table: {
        columns: [
          { key: "account_name", label: "Site Name" },
          { key: "country", label: "Country" },
        ],
        rows: [{ account_name: QUAL_VISIT.site_name, country: "IT" }],
      },
    });

    await page.goto("/chat");
    const beforeCount = await page.locator(S.CHAT_MESSAGE_ASSISTANT).count();
    await page.locator(S.CHAT_INPUT).fill("Which sites have an on-site pharmacy?");
    await page.locator(S.CHAT_SEND).click();
    await waitForAssistantReply(page, beforeCount);

    await expect(page.locator(S.AI_RESULT_TABLE)).toBeVisible({ timeout: 10000 });
    await expect(page.locator(S.AI_RESULT_ROW)).toHaveCount(1);
  });

  // ── QUAL-4: Partial sync — qual record for unknown SF account_id ────────
  test("QUAL-4: partial sync — qual without matching SF account handled gracefully", async ({ page }) => {
    // Simulate a qual record that has no matching SF account (orphan)
    const orphanQualRow = {
      account_id: "ORPHAN_SF_ID",
      account_name: null, // no SF match
      country: null,
      city: null,
      data: { "qual.overnight_stay": true },
    };

    await page.route("**/api/explorer/bootstrap", (r) =>
      r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ points: [], rows: [], fields: [] }) })
    );
    await page.route("**/api/explorer/fields", (r) =>
      r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([
        { key: "qual.overnight_stay", label: "Overnight stay", type: "boolean", source: "qual" },
      ]) })
    );
    await page.route("**/api/salesforce/map/bootstrap", (r) =>
      r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );
    // Backend returns the orphan row when filtering by qual.overnight_stay
    await page.route("**/api/explorer/search", (r) =>
      r.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ points: [], rows: [orphanQualRow], total: 1 }),
      })
    );

    await page.goto("/explorer");
    await page.locator(S.EXPLORER_SEARCH_BTN).click();

    // Page must not crash — filter panel remains visible
    await expect(page.locator(S.EXPLORER_FILTER_PANEL)).toBeVisible({ timeout: 5000 });
    // Results count renders (even if 1 weird row)
    await expect(page.locator(S.EXPLORER_RESULTS_COUNT)).toBeVisible({ timeout: 5000 });
  });

  // ── QUAL-5: Chat overnight stay query — qual data ───────────────────────
  test("QUAL-5: chat overnight stay query reflects qual data", async ({ page }) => {
    await mockChatStream(page, {
      answer: "<p>Sites with overnight stay facilities.</p>",
      last_table: {
        columns: [
          { key: "account_name", label: "Site Name" },
          { key: "overnight_stay", label: "Overnight Stay" },
        ],
        rows: [{ account_name: QUAL_VISIT.site_name, overnight_stay: "Yes" }],
      },
    });

    await page.goto("/chat");
    const beforeCount = await page.locator(S.CHAT_MESSAGE_ASSISTANT).count();
    await page.locator(S.CHAT_INPUT).fill("Which sites have overnight stay facilities?");
    await page.locator(S.CHAT_SEND).click();
    await waitForAssistantReply(page, beforeCount);

    await expect(page.locator(S.AI_RESULT_TABLE)).toBeVisible({ timeout: 10000 });
    const rows = page.locator(S.AI_RESULT_ROW);
    // Only the qual-linked site (Milano) should appear
    await expect(rows).toHaveCount(1);
  });

  // ── QUAL-6: qual.* filter count badge ──────────────────────────────────
  test("QUAL-6: qualification count matches seed — exactly 1 visit", async ({ page }) => {
    expect(seedData.qualification.visit_count).toBe(1);
    expect(seedData.qualification.visits).toHaveLength(1);
    expect(seedData.qualification.visits[0].account_id).toBe("SITE001");
  });
});
