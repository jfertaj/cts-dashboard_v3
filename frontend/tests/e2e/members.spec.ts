/**
 * Members View — comprehensive E2E tests
 *
 * All API calls are intercepted with realistic mock data so the tests run
 * without a real Salesforce session and are 100% deterministic.
 *
 * Coverage:
 *   MEMBERS-NAV-*    Tab navigation, URL routing
 *   MEMBERS-LOAD-*   Bootstrap, table rendering, column headers
 *   MEMBERS-FILTER-* Name / country / level / role filters, search, clear
 *   MEMBERS-MODAL-*  Detail modal sections (roles, contacts, sites)
 *   MEMBERS-MAP-*    Map toggle, show/hide
 */

import { test, expect, Page } from "@playwright/test";
import { S } from "../utils/selectors";

// ── Shared mock data ─────────────────────────────────────────────────────────

const MOCK_ROWS = [
  {
    account_id: "001TEST0001",
    account_name: "Universität München",
    country: "DE",
    city: "Munich",
    lat: 48.1351,
    lng: 11.582,
    data: {
      "sf.C_Level_of_Membership__c": "Full Member",
      "sf.Account_Status__c": "Active",
      "sf.Clinical_Site_CS__c": true,
      "sf.C_Deliver_Clinical_Grade_Services__c": false,
      "sf.C_Perform_Cutting_Edge__c": true,
      "sf.C_Contribute_as_a_Patient_Organization__c": false,
      "sf.Clinical_Site_CS_validated__c": true,
      "sf.Clinical_Trial_Site_CTS_validated__c": true,
      "sf.Diagnostic_Lab_DxLab_validated__c": false,
      "sf.Research_Mechanistic_Lab_LAB_validated__c": false,
      "sf.Patient_Organization_validated__c": false,
      "extra.SubAccountsCount": 3,
      "extra.ContactsCount": 5,
    },
  },
  {
    account_id: "001TEST0002",
    account_name: "University of Rome",
    country: "IT",
    city: "Rome",
    lat: 41.9028,
    lng: 12.4964,
    data: {
      "sf.C_Level_of_Membership__c": "Associate Member",
      "sf.Account_Status__c": "Active",
      "sf.Clinical_Site_CS__c": false,
      "sf.C_Deliver_Clinical_Grade_Services__c": true,
      "sf.C_Perform_Cutting_Edge__c": false,
      "sf.C_Contribute_as_a_Patient_Organization__c": false,
      "sf.Clinical_Site_CS_validated__c": false,
      "sf.Clinical_Trial_Site_CTS_validated__c": false,
      "sf.Diagnostic_Lab_DxLab_validated__c": true,
      "sf.Research_Mechanistic_Lab_LAB_validated__c": false,
      "sf.Patient_Organization_validated__c": false,
      "extra.SubAccountsCount": 1,
      "extra.ContactsCount": 2,
    },
  },
  {
    account_id: "001TEST0003",
    account_name: "University of Barcelona",
    country: "ES",
    city: "Barcelona",
    lat: 41.3851,
    lng: 2.1734,
    data: {
      "sf.C_Level_of_Membership__c": "Full Member",
      "sf.Account_Status__c": "Active",
      "sf.Clinical_Site_CS__c": true,
      "sf.C_Deliver_Clinical_Grade_Services__c": false,
      "sf.C_Perform_Cutting_Edge__c": false,
      "sf.C_Contribute_as_a_Patient_Organization__c": false,
      "sf.Clinical_Site_CS_validated__c": false,
      "sf.Clinical_Trial_Site_CTS_validated__c": false,
      "sf.Diagnostic_Lab_DxLab_validated__c": false,
      "sf.Research_Mechanistic_Lab_LAB_validated__c": false,
      "sf.Patient_Organization_validated__c": false,
      "extra.SubAccountsCount": 2,
      "extra.ContactsCount": 4,
    },
  },
];

const MOCK_DETAIL = {
  account_id: "001TEST0001",
  account_name: "Universität München",
  country: "DE",
  city: "Munich",
  level: "Full Member",
  status: "Active",
  phone: "+49 89 123456",
  website: "https://www.lmu.de",
  proposed_roles: { cs: true, dxlab: false, lab: true, patient_org: false },
  validated_roles: { cs: true, cts: true, dxlab: false, lab: false, patient_org: false },
  member_contacts: [
    {
      id: "003TEST0001",
      name: "Dr. Hans Müller",
      email: "hans.muller@lmu.de",
      phone: "+49 89 111111",
      title: "Principal Investigator",
      department: "Diabetes Research",
      is_board_member: true,
      is_country_lead: false,
      voting_rights: true,
    },
    {
      id: "003TEST0002",
      name: "Dr. Anna Schmidt",
      email: "anna.schmidt@lmu.de",
      phone: null,
      title: "Study Coordinator",
      department: null,
      is_board_member: false,
      is_country_lead: true,
      voting_rights: false,
    },
  ],
  subaccounts: [
    {
      id: "001TEST0010",
      name: "Munich University Hospital — Diabetes Centre",
      country: "DE",
      city: "Munich",
      is_cts: true,
      is_cs: true,
      is_dxlab: false,
      is_rp: false,
      is_accredited: true,
      status: "Active",
      contacts: [
        {
          id: "003TEST0010",
          name: "Dr. Klaus Weber",
          email: "k.weber@uniklinik.de",
          phone: "+49 89 222222",
          title: "Study Coordinator",
          department: null,
        },
      ],
    },
    {
      id: "001TEST0011",
      name: "Munich Diagnostics Lab",
      country: "DE",
      city: "Munich",
      is_cts: false,
      is_cs: false,
      is_dxlab: true,
      is_rp: false,
      is_accredited: false,
      status: "Active",
      contacts: [],
    },
  ],
};

// ── Setup helper ─────────────────────────────────────────────────────────────

async function setupMocks(
  page: Page,
  opts: {
    bootstrapRows?: typeof MOCK_ROWS;
    searchRows?: typeof MOCK_ROWS;
    detail?: typeof MOCK_DETAIL | null;
  } = {}
) {
  const {
    bootstrapRows = MOCK_ROWS,
    searchRows = MOCK_ROWS,
    detail = MOCK_DETAIL,
  } = opts;

  await page.route("**/api/salesforce/me", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: true }) })
  );
  await page.route("**/api/members/bootstrap", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ rows: bootstrapRows, total: bootstrapRows.length }),
    })
  );
  await page.route("**/api/members/search", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ rows: searchRows, total: searchRows.length }),
    })
  );
  if (detail !== null) {
    await page.route("**/api/members/*/detail", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(detail),
      })
    );
  }
}

// ── NAVIGATION ────────────────────────────────────────────────────────────────

test.describe("MEMBERS-NAV — tab navigation", () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page);
  });

  test("MEMBERS-NAV-1: Members tab is visible in header", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator(S.TAB_MEMBERS)).toBeVisible();
  });

  test("MEMBERS-NAV-2: clicking Members tab shows members view", async ({ page }) => {
    await page.goto("/");
    await page.locator(S.TAB_MEMBERS).click();
    await expect(page.locator(S.MEMBERS_VIEW)).toBeVisible();
  });

  test("MEMBERS-NAV-3: navigating to /members directly works", async ({ page }) => {
    await page.goto("/members");
    await expect(page.locator(S.MEMBERS_VIEW)).toBeVisible();
  });

  test("MEMBERS-NAV-4: URL updates to /members on tab click", async ({ page }) => {
    await page.goto("/");
    await page.locator(S.TAB_MEMBERS).click();
    await expect(page).toHaveURL(/\/members/);
  });

  test("MEMBERS-NAV-5: can switch from Members tab back to Explorer", async ({ page }) => {
    await page.route("**/api/explorer/bootstrap", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ points: [], rows: [], fields: [] }) })
    );
    await page.route("**/api/explorer/fields", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );
    await page.goto("/members");
    await expect(page.locator(S.MEMBERS_VIEW)).toBeVisible();
    await page.locator(S.TAB_EXPLORER).click();
    await expect(page.locator(S.EXPLORER_FILTER_PANEL)).toBeVisible();
    await expect(page.locator(S.MEMBERS_VIEW)).not.toBeVisible();
  });

  test("MEMBERS-NAV-6: Members tab is highlighted when active", async ({ page }) => {
    await page.goto("/members");
    // Active tab has bg-white class
    const tab = page.locator(S.TAB_MEMBERS);
    await expect(tab).toHaveClass(/bg-white/);
  });
});

// ── TABLE LOADING ─────────────────────────────────────────────────────────────

test.describe("MEMBERS-LOAD — bootstrap and table rendering", () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page);
    await page.goto("/members");
  });

  test("MEMBERS-LOAD-1: filter panel is visible", async ({ page }) => {
    await expect(page.locator(S.MEMBERS_FILTER_PANEL)).toBeVisible();
  });

  test("MEMBERS-LOAD-2: table renders with mock rows", async ({ page }) => {
    await expect(page.locator(S.MEMBERS_TABLE)).toBeVisible();
    await expect(page.locator(S.MEMBERS_TABLE_ROW)).toHaveCount(3);
  });

  test("MEMBERS-LOAD-3: table shows Institution column header", async ({ page }) => {
    const headers = page.locator(`${S.MEMBERS_TABLE} th`);
    await expect(headers.filter({ hasText: "Institution" })).toBeVisible();
  });

  test("MEMBERS-LOAD-4: table shows Country, City, Level columns", async ({ page }) => {
    const headers = page.locator(`${S.MEMBERS_TABLE} th`);
    await expect(headers.filter({ hasText: "Country" })).toBeVisible();
    await expect(headers.filter({ hasText: "City" })).toBeVisible();
    await expect(headers.filter({ hasText: "Level" })).toBeVisible();
  });

  test("MEMBERS-LOAD-5: table shows Sites and Contacts columns", async ({ page }) => {
    const headers = page.locator(`${S.MEMBERS_TABLE} th`);
    await expect(headers.filter({ hasText: "Sites" })).toBeVisible();
    await expect(headers.filter({ hasText: "Contacts" })).toBeVisible();
  });

  test("MEMBERS-LOAD-6: institution names are rendered in rows", async ({ page }) => {
    await expect(page.getByText("Universität München")).toBeVisible();
    await expect(page.getByText("University of Rome")).toBeVisible();
    await expect(page.getByText("University of Barcelona")).toBeVisible();
  });

  test("MEMBERS-LOAD-7: country ISO2 is rendered as full name", async ({ page }) => {
    // DE → Germany, IT → Italy, ES → Spain (scoped to table to avoid select option matches)
    const table = page.locator(S.MEMBERS_TABLE);
    await expect(table.getByText("Germany", { exact: true })).toBeVisible();
    await expect(table.getByText("Italy", { exact: true })).toBeVisible();
    await expect(table.getByText("Spain", { exact: true })).toBeVisible();
  });

  test("MEMBERS-LOAD-8: results count shows correct number", async ({ page }) => {
    const count = page.locator(S.MEMBERS_RESULTS_COUNT);
    await expect(count).toBeVisible();
    await expect(count).toContainText("3");
  });

  test("MEMBERS-LOAD-9: Proposed Roles badges rendered for München (CS)", async ({ page }) => {
    // München has cs=true → should show a CS badge in the table
    const rows = page.locator(S.MEMBERS_TABLE_ROW);
    const munichRow = rows.filter({ hasText: "Universität München" });
    await expect(munichRow.getByText("CS", { exact: true })).toBeVisible();
  });

  test("MEMBERS-LOAD-10: Validated Roles badges rendered for München (Val. CTS)", async ({ page }) => {
    const rows = page.locator(S.MEMBERS_TABLE_ROW);
    const munichRow = rows.filter({ hasText: "Universität München" });
    await expect(munichRow.getByText(/Val\. CTS/)).toBeVisible();
  });
});

// ── FILTERS ──────────────────────────────────────────────────────────────────

test.describe("MEMBERS-FILTER — filter panel interactions", () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page);
    await page.goto("/members");
    await expect(page.locator(S.MEMBERS_TABLE)).toBeVisible();
  });

  test("MEMBERS-FILTER-1: name filter input is visible and accepts text", async ({ page }) => {
    const input = page.locator(S.MEMBERS_FILTER_NAME);
    await expect(input).toBeVisible();
    await input.fill("München");
    await expect(input).toHaveValue("München");
  });

  test("MEMBERS-FILTER-2: country dropdown shows All countries option", async ({ page }) => {
    const select = page.locator(S.MEMBERS_FILTER_COUNTRY);
    await expect(select).toBeVisible();
    await expect(select.locator("option[value='']")).toHaveText("All countries");
  });

  test("MEMBERS-FILTER-3: country dropdown has options from bootstrap data", async ({ page }) => {
    const select = page.locator(S.MEMBERS_FILTER_COUNTRY);
    // bootstrap has DE, IT, ES
    await expect(select.locator("option[value='DE']")).toBeAttached();
    await expect(select.locator("option[value='IT']")).toBeAttached();
    await expect(select.locator("option[value='ES']")).toBeAttached();
  });

  test("MEMBERS-FILTER-4: level dropdown is visible with All levels", async ({ page }) => {
    const select = page.locator(S.MEMBERS_FILTER_LEVEL);
    await expect(select).toBeVisible();
    await expect(select.locator("option[value='']")).toHaveText("All levels");
  });

  test("MEMBERS-FILTER-5: proposed role checkboxes are all visible", async ({ page }) => {
    await expect(page.locator('[data-testid="members-filter-proposed-CS"]')).toBeVisible();
    await expect(page.locator('[data-testid="members-filter-proposed-DxLab"]')).toBeVisible();
    await expect(page.locator('[data-testid="members-filter-proposed-LAB"]')).toBeVisible();
    await expect(page.locator('[data-testid="members-filter-proposed-Patient Org"]')).toBeVisible();
  });

  test("MEMBERS-FILTER-6: validated role checkboxes are all visible", async ({ page }) => {
    await expect(page.locator('[data-testid="members-filter-validated-Val. CS"]')).toBeVisible();
    await expect(page.locator('[data-testid="members-filter-validated-Val. CTS"]')).toBeVisible();
    await expect(page.locator('[data-testid="members-filter-validated-Val. DxLab"]')).toBeVisible();
    await expect(page.locator('[data-testid="members-filter-validated-Val. LAB"]')).toBeVisible();
    await expect(page.locator('[data-testid="members-filter-validated-Val. Patient Org"]')).toBeVisible();
  });

  test("MEMBERS-FILTER-7: clicking Search calls /api/members/search", async ({ page }) => {
    let searchCalled = false;
    await page.route("**/api/members/search", async (route) => {
      searchCalled = true;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ rows: MOCK_ROWS, total: 3 }),
      });
    });

    // With tag-input: fill → Enter to add tag → then click Search
    await page.locator(S.MEMBERS_FILTER_NAME).fill("test");
    await page.locator(S.MEMBERS_FILTER_NAME).press("Enter");
    await page.locator(S.MEMBERS_SEARCH_BTN).click();
    await page.waitForTimeout(500);
    expect(searchCalled).toBe(true);
  });

  test("MEMBERS-FILTER-8: search with name filter sends correct body", async ({ page }) => {
    let capturedBody: any = null;
    await page.route("**/api/members/search", async (route) => {
      capturedBody = JSON.parse(route.request().postData() ?? "{}");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ rows: [MOCK_ROWS[0]], total: 1 }),
      });
    });

    // With tag-input: fill → Enter to add tag → then click Search
    await page.locator(S.MEMBERS_FILTER_NAME).fill("München");
    await page.locator(S.MEMBERS_FILTER_NAME).press("Enter");
    await page.locator(S.MEMBERS_SEARCH_BTN).click();
    await page.waitForTimeout(500);

    expect(capturedBody?.filters?.rules?.some((r: any) =>
      r.field === "sf.Name" && r.operator === "contains" && r.value === "München"
    )).toBe(true);
  });

  test("MEMBERS-FILTER-9: Enter key adds name tag; Search button triggers search", async ({ page }) => {
    let searchCalled = false;
    await page.route("**/api/members/search", async (route) => {
      searchCalled = true;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ rows: [], total: 0 }) });
    });
    // Enter adds the tag (input clears), then Search button triggers the API call
    await page.locator(S.MEMBERS_FILTER_NAME).fill("test");
    await page.locator(S.MEMBERS_FILTER_NAME).press("Enter");
    // Tag chip should now be visible
    await expect(page.locator('[data-testid="members-filter-name"]')).toHaveValue("");
    await page.locator(S.MEMBERS_SEARCH_BTN).click();
    await page.waitForTimeout(500);
    expect(searchCalled).toBe(true);
  });

  test("MEMBERS-FILTER-10: country filter sends correct body", async ({ page }) => {
    let capturedBody: any = null;
    await page.route("**/api/members/search", async (route) => {
      capturedBody = JSON.parse(route.request().postData() ?? "{}");
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ rows: [MOCK_ROWS[0]], total: 1 }) });
    });

    await page.locator(S.MEMBERS_FILTER_COUNTRY).selectOption("DE");
    await page.locator(S.MEMBERS_SEARCH_BTN).click();
    await page.waitForTimeout(500);

    expect(capturedBody?.filters?.rules?.some((r: any) =>
      r.field === "site.country" && r.value === "DE"
    )).toBe(true);
  });

  test("MEMBERS-FILTER-11: proposed role checkbox filter sends correct body", async ({ page }) => {
    let capturedBody: any = null;
    await page.route("**/api/members/search", async (route) => {
      capturedBody = JSON.parse(route.request().postData() ?? "{}");
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ rows: [], total: 0 }) });
    });

    await page.locator('[data-testid="members-filter-proposed-CS"]').check();
    await page.locator(S.MEMBERS_SEARCH_BTN).click();
    await page.waitForTimeout(500);

    expect(capturedBody?.filters?.rules?.some((r: any) =>
      r.field === "sf.Clinical_Site_CS__c" && r.value === true
    )).toBe(true);
  });

  test("MEMBERS-FILTER-12: validated role checkbox filter sends correct body", async ({ page }) => {
    let capturedBody: any = null;
    await page.route("**/api/members/search", async (route) => {
      capturedBody = JSON.parse(route.request().postData() ?? "{}");
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ rows: [], total: 0 }) });
    });

    await page.locator('[data-testid="members-filter-validated-Val. CTS"]').check();
    await page.locator(S.MEMBERS_SEARCH_BTN).click();
    await page.waitForTimeout(500);

    expect(capturedBody?.filters?.rules?.some((r: any) =>
      r.field === "sf.Clinical_Trial_Site_CTS_validated__c" && r.value === true
    )).toBe(true);
  });

  test("MEMBERS-FILTER-13: combined country + role filter sends both rules", async ({ page }) => {
    let capturedBody: any = null;
    await page.route("**/api/members/search", async (route) => {
      capturedBody = JSON.parse(route.request().postData() ?? "{}");
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ rows: [], total: 0 }) });
    });

    await page.locator(S.MEMBERS_FILTER_COUNTRY).selectOption("DE");
    await page.locator('[data-testid="members-filter-validated-Val. CTS"]').check();
    await page.locator(S.MEMBERS_SEARCH_BTN).click();
    await page.waitForTimeout(500);

    const rules = capturedBody?.filters?.rules ?? [];
    expect(rules.some((r: any) => r.field === "site.country" && r.value === "DE")).toBe(true);
    expect(rules.some((r: any) => r.field === "sf.Clinical_Trial_Site_CTS_validated__c")).toBe(true);
    expect(capturedBody?.filters?.logic).toBe("AND");
  });

  test("MEMBERS-FILTER-14: search returns filtered rows and updates table", async ({ page }) => {
    const filteredRows = [MOCK_ROWS[0]]; // Only München
    await page.route("**/api/members/search", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ rows: filteredRows, total: 1 }) })
    );

    await page.locator(S.MEMBERS_FILTER_COUNTRY).selectOption("DE");
    await page.locator(S.MEMBERS_SEARCH_BTN).click();
    await expect(page.locator(S.MEMBERS_TABLE_ROW)).toHaveCount(1, { timeout: 5000 });
    await expect(page.getByText("Universität München")).toBeVisible();
  });

  test("MEMBERS-FILTER-15: Clear button resets filters and restores all rows", async ({ page }) => {
    // Filter down to 1 row
    await page.route("**/api/members/search", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ rows: [MOCK_ROWS[0]], total: 1 }) })
    );
    // With tag-input: fill → Enter to add tag → Search
    await page.locator(S.MEMBERS_FILTER_NAME).fill("München");
    await page.locator(S.MEMBERS_FILTER_NAME).press("Enter");
    await page.locator(S.MEMBERS_SEARCH_BTN).click();
    await expect(page.locator(S.MEMBERS_TABLE_ROW)).toHaveCount(1, { timeout: 5000 });

    // Now clear
    await page.locator(S.MEMBERS_CLEAR_BTN).click();
    await expect(page.locator(S.MEMBERS_FILTER_NAME)).toHaveValue("");
    await expect(page.locator(S.MEMBERS_TABLE_ROW)).toHaveCount(3, { timeout: 5000 });
  });
});

// ── DETAIL MODAL ──────────────────────────────────────────────────────────────

test.describe("MEMBERS-MODAL — detail modal", () => {
  test.beforeEach(async ({ page }) => {
    await setupMocks(page);
    await page.goto("/members");
    await expect(page.locator(S.MEMBERS_TABLE_ROW)).toHaveCount(3);
  });

  test("MEMBERS-MODAL-1: clicking institution name opens detail modal", async ({ page }) => {
    await page.getByRole("button", { name: "Universität München" }).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toBeVisible({ timeout: 5000 });
  });

  test("MEMBERS-MODAL-2: modal shows institution name in header", async ({ page }) => {
    await page.getByRole("button", { name: "Universität München" }).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toBeVisible({ timeout: 5000 });
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toContainText("Universität München");
  });

  test("MEMBERS-MODAL-3: modal shows Proposed Roles section", async ({ page }) => {
    await page.getByRole("button", { name: "Universität München" }).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toBeVisible({ timeout: 5000 });
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toContainText("Proposed Roles");
  });

  test("MEMBERS-MODAL-4: modal shows Validated Roles section", async ({ page }) => {
    await page.getByRole("button", { name: "Universität München" }).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toBeVisible({ timeout: 5000 });
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toContainText("Validated Roles");
  });

  test("MEMBERS-MODAL-5: modal shows Member Contacts section with count", async ({ page }) => {
    await page.getByRole("button", { name: "Universität München" }).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toBeVisible({ timeout: 5000 });
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toContainText("Member Contacts (2)");
  });

  test("MEMBERS-MODAL-6: modal shows Clinical Sites section with count", async ({ page }) => {
    await page.getByRole("button", { name: "Universität München" }).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toBeVisible({ timeout: 5000 });
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toContainText("Clinical Sites (2)");
  });

  test("MEMBERS-MODAL-7: contact name and email are visible", async ({ page }) => {
    await page.getByRole("button", { name: "Universität München" }).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toBeVisible({ timeout: 5000 });
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toContainText("Dr. Hans Müller");
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toContainText("hans.muller@lmu.de");
  });

  test("MEMBERS-MODAL-8: Board Member flag is shown for board members", async ({ page }) => {
    await page.getByRole("button", { name: "Universität München" }).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toBeVisible({ timeout: 5000 });
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toContainText("Board Member");
  });

  test("MEMBERS-MODAL-9: Country Lead flag is shown", async ({ page }) => {
    await page.getByRole("button", { name: "Universität München" }).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toBeVisible({ timeout: 5000 });
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toContainText("Country Lead");
  });

  test("MEMBERS-MODAL-10: subaccount name is visible in Clinical Sites section", async ({ page }) => {
    await page.getByRole("button", { name: "Universität München" }).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toBeVisible({ timeout: 5000 });
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toContainText("Munich University Hospital");
  });

  test("MEMBERS-MODAL-11: subaccount CTS badge is rendered", async ({ page }) => {
    await page.getByRole("button", { name: "Universität München" }).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toBeVisible({ timeout: 5000 });
    await expect(page.locator(S.MEMBER_DETAILS_MODAL).getByText("CTS", { exact: true })).toBeVisible();
  });

  test("MEMBERS-MODAL-12: proposed role pills reflect mock data (CS ✓, DxLab —)", async ({ page }) => {
    await page.getByRole("button", { name: "Universität München" }).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toBeVisible({ timeout: 5000 });
    // cs: true → "✓ Clinical Site (CS)"
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toContainText("✓ Clinical Site (CS)");
    // dxlab: false → "— Diagnostic Lab (DxLab)"
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toContainText("— Diagnostic Lab (DxLab)");
  });

  test("MEMBERS-MODAL-13: close button closes the modal", async ({ page }) => {
    await page.getByRole("button", { name: "Universität München" }).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toBeVisible({ timeout: 5000 });
    await page.locator(S.MEMBER_DETAILS_CLOSE).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).not.toBeVisible();
  });

  test("MEMBERS-MODAL-14: modal fetches correct detail endpoint", async ({ page }) => {
    let fetchedUrl = "";
    await page.route("**/api/members/*/detail", async (route) => {
      fetchedUrl = route.request().url();
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(MOCK_DETAIL) });
    });

    await page.getByRole("button", { name: "Universität München" }).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toBeVisible({ timeout: 5000 });
    expect(fetchedUrl).toContain("001TEST0001");
  });

  test("MEMBERS-MODAL-15: subaccount contact is shown inside the site section", async ({ page }) => {
    await page.getByRole("button", { name: "Universität München" }).click();
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toBeVisible({ timeout: 5000 });
    await expect(page.locator(S.MEMBER_DETAILS_MODAL)).toContainText("Dr. Klaus Weber");
  });
});

// ── MAP ───────────────────────────────────────────────────────────────────────

test.describe("MEMBERS-MAP — map toggle", () => {
  test.beforeEach(async ({ page }) => {
    // Prevent actual Maps JS from loading during tests
    await page.route("**/maps.googleapis.com/**", (route) => route.abort());
    await setupMocks(page);
    await page.goto("/members");
    await expect(page.locator(S.MEMBERS_TABLE_ROW)).toHaveCount(3);
  });

  test("MEMBERS-MAP-1: map toggle button is visible", async ({ page }) => {
    await expect(page.locator(S.MEMBERS_MAP_TOGGLE)).toBeVisible();
  });

  test("MEMBERS-MAP-2: map toggle initially shows 'Hide Map' (map on by default)", async ({ page }) => {
    await expect(page.locator(S.MEMBERS_MAP_TOGGLE)).toContainText("Hide Map");
  });

  test("MEMBERS-MAP-3: map container is visible by default", async ({ page }) => {
    await expect(page.locator(S.MEMBERS_MAP)).toBeVisible({ timeout: 3000 });
  });

  test("MEMBERS-MAP-4: clicking Hide Map hides the map container", async ({ page }) => {
    await page.locator(S.MEMBERS_MAP_TOGGLE).click();
    await expect(page.locator(S.MEMBERS_MAP)).not.toBeVisible();
  });

  test("MEMBERS-MAP-5: after clicking Hide Map, button label changes to Show Map", async ({ page }) => {
    await page.locator(S.MEMBERS_MAP_TOGGLE).click();
    await expect(page.locator(S.MEMBERS_MAP_TOGGLE)).toContainText("Show Map");
  });

  test("MEMBERS-MAP-6: clicking Show Map reveals map and reverts button label to Hide Map", async ({ page }) => {
    await page.locator(S.MEMBERS_MAP_TOGGLE).click();
    await expect(page.locator(S.MEMBERS_MAP)).not.toBeVisible();
    await page.locator(S.MEMBERS_MAP_TOGGLE).click();
    await expect(page.locator(S.MEMBERS_MAP)).toBeVisible({ timeout: 3000 });
    await expect(page.locator(S.MEMBERS_MAP_TOGGLE)).toContainText("Hide Map");
  });
});
