import { BrowserContext, Page } from "@playwright/test";

/**
 * Sets the sf_session cookie on the given context so the app
 * believes the user is authenticated with Salesforce.
 * Requires SF_SESSION_COOKIE env var.
 */
export async function setAuthCookie(context: BrowserContext, baseURL: string) {
  const sessionId = process.env.SF_SESSION_COOKIE;
  if (!sessionId) throw new Error("SF_SESSION_COOKIE env var required for authenticated tests");
  const url = new URL(baseURL);
  await context.addCookies([
    {
      name: "sf_session",
      value: sessionId,
      domain: url.hostname,
      path: "/",
      httpOnly: false, // Playwright can only set non-httpOnly in this API
      secure: false,
    },
  ]);
}

/** Navigate to /chat tab */
export async function goToChat(page: Page) {
  await page.goto("/chat");
}

/** Navigate to /explorer tab */
export async function goToExplorer(page: Page) {
  await page.goto("/explorer");
}
