/**
 * tests/utils/data.ts
 * Helpers for reading and parsing table data from the Playwright page.
 */
import { Page, Locator } from "@playwright/test";
import { S } from "./selectors";

/** Read all visible rows from AIResultTable as objects keyed by column index. */
export async function readAITableRows(
  page: Page,
  tableLocator?: Locator
): Promise<string[][]> {
  const table = tableLocator ?? page.locator(S.AI_RESULT_TABLE).first();
  const rows = table.locator(S.AI_RESULT_ROW);
  const count = await rows.count();
  const result: string[][] = [];
  for (let i = 0; i < count; i++) {
    const cells = rows.nth(i).locator(S.AI_RESULT_CELL);
    const cellCount = await cells.count();
    const row: string[] = [];
    for (let j = 0; j < cellCount; j++) {
      row.push((await cells.nth(j).innerText()).trim());
    }
    result.push(row);
  }
  return result;
}

/** Read column headers from AIResultTable. */
export async function readAITableHeaders(
  page: Page,
  tableLocator?: Locator
): Promise<string[]> {
  const table = tableLocator ?? page.locator(S.AI_RESULT_TABLE).first();
  const headers = table.locator("thead th");
  const count = await headers.count();
  const result: string[] = [];
  for (let i = 0; i < count; i++) {
    result.push((await headers.nth(i).innerText()).trim());
  }
  return result;
}

/** Read all rows from Explorer TanStack table as raw text arrays. */
export async function readExplorerTableRows(page: Page): Promise<string[][]> {
  const table = page.locator(S.EXPLORER_RESULTS_TABLE).first();
  const rows = table.locator(S.EXPLORER_TABLE_ROW);
  const count = await rows.count();
  const result: string[][] = [];
  for (let i = 0; i < count; i++) {
    const cells = rows.nth(i).locator(S.EXPLORER_TABLE_CELL);
    const cellCount = await cells.count();
    const row: string[] = [];
    for (let j = 0; j < cellCount; j++) {
      row.push((await cells.nth(j).innerText()).trim());
    }
    result.push(row);
  }
  return result;
}

/** Extract plain text from last assistant message (handles HTML strings). */
export async function lastAssistantText(page: Page): Promise<string> {
  const msgs = page.locator(S.CHAT_MESSAGE_ASSISTANT);
  const count = await msgs.count();
  if (count === 0) return "";
  return (await msgs.nth(count - 1).innerText()).trim();
}

/** Wait for a new permanent assistant message (no streaming ▋ cursor).
 *  Must be called BEFORE clicking send; pass the count before send. */
export async function waitForAssistantReply(
  page: Page,
  beforeCount: number,
  timeoutMs = 15000
): Promise<void> {
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

/** Read the results count text from Explorer. */
export async function explorerResultsCount(page: Page): Promise<number> {
  const el = page.locator(S.EXPLORER_RESULTS_COUNT);
  const text = await el.innerText();
  const match = text.match(/^[\d,]+/);
  return match ? parseInt(match[0].replace(/,/g, ""), 10) : 0;
}

/** Standard mock for all Explorer bootstrap endpoints. */
export async function mockExplorerBootstrap(
  page: Page,
  rows: Record<string, any>[] = [],
  points: any[] = []
) {
  await page.route("**/api/salesforce/me", (r) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: true }) })
  );
  await page.route("**/api/explorer/bootstrap", (r) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ points, rows, fields: [] }) })
  );
  await page.route("**/api/explorer/fields", (r) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(MOCK_FIELDS) })
  );
  await page.route("**/api/salesforce/map/bootstrap", (r) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
  );
  await page.route("**/api/explorer/search", (r) =>
    r.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ points, rows, total: rows.length }),
    })
  );
}

// Minimal field catalog used in tests
export const MOCK_FIELDS = [
  { key: "sf.Account.Name", label: "Account Name", type: "string", source: "sf" },
  { key: "site.country", label: "Country", type: "string", source: "site" },
  { key: "sf.C_Number_of_Stage1_Individuals_followed__c", label: "Stage 1", type: "number", source: "sf" },
  { key: "sf.C_Number_of_Stage2_Individuals_followed__c", label: "Stage 2", type: "number", source: "sf" },
  { key: "sf.C_Number_of_new_T1D_diagnosed_O_18__c", label: "ND ≥18", type: "number", source: "sf" },
  { key: "sf.C_Number_of_new_T1D_diagnosed_U_18__c", label: "ND <18", type: "number", source: "sf" },
  { key: "qual.onsite_pharmacy", label: "On-site pharmacy", type: "boolean", source: "qual" },
  { key: "qual.overnight_stay", label: "Overnight stay", type: "boolean", source: "qual" },
];

/** Seed rows matching seed_expectations.json */
export const SEED_ROWS: Record<string, any>[] = [
  { account_id: "SITE001", account_name: "Milano Diabetes Center", country: "IT", city: "Milan",
    data: { "sf.C_Number_of_Stage1_Individuals_followed__c": 12, "sf.C_Number_of_Stage2_Individuals_followed__c": 8, "sf.C_Number_of_new_T1D_diagnosed_O_18__c": 95, "sf.C_Number_of_new_T1D_diagnosed_U_18__c": 40 } },
  { account_id: "SITE002", account_name: "Roma Clinical Research", country: "IT", city: "Rome",
    data: { "sf.C_Number_of_Stage1_Individuals_followed__c": 5, "sf.C_Number_of_Stage2_Individuals_followed__c": 3, "sf.C_Number_of_new_T1D_diagnosed_O_18__c": 60, "sf.C_Number_of_new_T1D_diagnosed_U_18__c": 22 } },
  { account_id: "SITE003", account_name: "Madrid Hospital Universitario", country: "ES", city: "Madrid",
    data: { "sf.C_Number_of_Stage1_Individuals_followed__c": 9, "sf.C_Number_of_Stage2_Individuals_followed__c": 6, "sf.C_Number_of_new_T1D_diagnosed_O_18__c": 70, "sf.C_Number_of_new_T1D_diagnosed_U_18__c": 30 } },
  { account_id: "SITE004", account_name: "Barcelona Endocrinology Unit", country: "ES", city: "Barcelona",
    data: { "sf.C_Number_of_Stage1_Individuals_followed__c": 3, "sf.C_Number_of_Stage2_Individuals_followed__c": 2, "sf.C_Number_of_new_T1D_diagnosed_O_18__c": 35, "sf.C_Number_of_new_T1D_diagnosed_U_18__c": 15 } },
  { account_id: "SITE005", account_name: "Berlin Charité T1D Unit", country: "DE", city: "Berlin",
    data: { "sf.C_Number_of_Stage1_Individuals_followed__c": 18, "sf.C_Number_of_Stage2_Individuals_followed__c": 11, "sf.C_Number_of_new_T1D_diagnosed_O_18__c": 120, "sf.C_Number_of_new_T1D_diagnosed_U_18__c": 55 } },
  { account_id: "SITE006", account_name: "Amsterdam LUMC Diabetes", country: "NL", city: "Amsterdam",
    data: { "sf.C_Number_of_Stage1_Individuals_followed__c": 7, "sf.C_Number_of_Stage2_Individuals_followed__c": 4, "sf.C_Number_of_new_T1D_diagnosed_O_18__c": 50, "sf.C_Number_of_new_T1D_diagnosed_U_18__c": 18 } },
  { account_id: "SITE007", account_name: "Paris Hôpital Necker", country: "FR", city: "Paris",
    data: { "sf.C_Number_of_Stage1_Individuals_followed__c": 0, "sf.C_Number_of_Stage2_Individuals_followed__c": 0, "sf.C_Number_of_new_T1D_diagnosed_O_18__c": 25, "sf.C_Number_of_new_T1D_diagnosed_U_18__c": 10 } },
  { account_id: "SITE008", account_name: "London King's College Diabetes", country: "GB", city: "London",
    data: { "sf.C_Number_of_Stage1_Individuals_followed__c": 14, "sf.C_Number_of_Stage2_Individuals_followed__c": 9, "sf.C_Number_of_new_T1D_diagnosed_O_18__c": 110, "sf.C_Number_of_new_T1D_diagnosed_U_18__c": 48 } },
];
