import { test, expect } from "@playwright/test";

test("shows innodia.org sign-in gate when unauthenticated", async ({ page }) => {
  await page.route("**/api/auth/me", (r) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: false }) })
  );
  await page.goto("/");
  await expect(page.getByTestId("signin-innodia")).toBeVisible();
});
