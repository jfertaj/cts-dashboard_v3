import { test, expect } from "@playwright/test";

test.describe("Referral DB tab", () => {
  test.beforeEach(async ({ page }) => {
    // Auth gate: GET /api/auth/me — return the full real shape so the
    // app treats the session as authenticated and renders the header/tabs.
    await page.route("**/api/auth/me", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          authenticated: true,
          instance_url: "https://example.my.salesforce.com",
          issued_at: 1,
          has_refresh: true,
        }),
      })
    );
  });

  test("runs report and renders table", async ({ page }) => {
    await page.route("**/api/assignments/report/options", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          studies: ["Baricade Delay (JAJJ)", "Safeguard", "Beta Preserve"],
          stages: ["Activated"],
          roles: ["Investigator", "Study Coordinator"],
        }),
      })
    );
    let reportBody: any = null;
    await page.route("**/api/assignments/report", (route, request) => {
      reportBody = request.postDataJSON();
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          columns: [
            { key: "first_name", label: "First Name" },
            { key: "email", label: "Email" },
            { key: "role", label: "Role" },
          ],
          rows: [{ first_name: "Bart", email: "bart@uzbrussel.be", role: "Investigator" }],
        }),
      });
    });

    await page.goto("/");
    await page.getByTestId("tab-assignments").click();
    await expect(page.getByTestId("assignments-view")).toBeVisible();

    // Role options from /report/options render and can be selected; the picked
    // role must be sent in the report request body.
    const roleFilter = page.getByText("Investigator", { exact: true });
    await expect(roleFilter).toBeVisible();
    await roleFilter.click();

    await page.getByTestId("assignments-run").click();
    await expect(page.getByTestId("assignments-table")).toContainText("Investigator");
    await expect(page.getByTestId("assignments-table")).toContainText("bart@uzbrussel.be");
    expect(reportBody?.roles).toEqual(["Investigator"]);
  });
});
